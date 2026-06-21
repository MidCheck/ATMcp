# Workbench quickstart — drive a team from one web page (with a local model)

Stand up the **workbench**: a chat-style page where you create sessions and command agents
from any device. This walks through the full loop, including running a **local Ollama model**
(`qwen3.6:35b`) as an agent that can actually run shell / edit files — guarded by Command Guard.

Prerequisites: Python venv installed (`pip install -r requirements.txt`), and for the local-model
part, [Ollama](https://ollama.com) running with a tool-capable model:
```bash
ollama pull qwen3.6:35b      # any tool-calling model works
```

## 1. Start the server
```bash
# local dev (starts redis + uvicorn on :18000 per run_local.sh), or just:
export ATMCP_ADMIN_TOKEN=change-me ATMCP_SQLITE_PATH=./data/atmcp.db
uvicorn atmcp.app:app --host 0.0.0.0 --port 8000
```

## 2. Create a team (returns the join token)
```bash
curl -s -X POST http://localhost:8000/api/teams \
  -H "X-Admin-Token: change-me" -H 'Content-Type: application/json' \
  -d '{"name":"my-team"}' | jq
# → note the "join_token"
```

## 3. Start a worker host (one per agent)

The host registers the agent, long-polls for work (token-free while idle), and runs each
session's turns — concurrently, streaming output, each session in its own git worktree.

### Option A — Claude (default)
```bash
python scripts/atmcp_workbench_host.py --url http://localhost:8000 \
  --team my-team --token <join_token> --name alice \
  --base-repo ~/code/myrepo            # omit --base-repo to run in the cwd
```

### Option B — a local model via Ollama (acts with tools)
```bash
python scripts/atmcp_workbench_host.py --url http://localhost:8000 \
  --team my-team --token <join_token> --name qwen \
  --executor openai --api-base http://localhost:11434/v1 --model qwen3.6:35b \
  --base-repo ~/code/myrepo
```
The local model runs a **tool-calling agent loop** — `run_bash` / `read_file` / `write_file`,
confined to the session's worktree. Every shell command passes through **Command Guard** (see §6).
Its memory is stored **server-side per session**, so it survives a host restart / moving machines.

### Option C — Codex / Cursor
```bash
python scripts/atmcp_workbench_host.py --url http://localhost:8000 \
  --team my-team --token <join_token> --name cdx \
  --executor codex --base-repo ~/code/myrepo
#   or:  --executor cursor
#   or a custom command:  --executor-cmd "codex exec resume --last {prompt}"
```

Run several hosts (different `--name`) to have multiple agents. `--dry-run` exercises the loop
without calling a model. Keep a host alive with tmux / nohup / systemd.

## 4. Open the workbench
```
http://localhost:8000/workbench?team=my-team
```
- **Left:** a tree of **agents → sessions**. Click an agent's **＋** to start a new session.
- **Right:** the chat. Type a message and click **Send** (Enter inserts a newline). The agent's
  output streams in live; a status pill shows **working… / ✓ done / ✗ failed**.
- Paste the **join token** in the box at the bottom-right once (stored locally) — it's required to
  create sessions and send messages.
- Each session is an independent thread with its own memory. Rename / archive from the header.
- Works from a phone or any device; it's all server-hosted and resumes on reconnect.

> One window of continuous chat = one session = one memory. Same agent, multiple sessions = parallel
> threads (run concurrently; serial within a single thread).

## 5. Watch cost & status (dashboard)
`http://localhost:8000/dashboard?team=my-team` → the **Tokens** tab shows per-agent token/cost with
rolling 5h/7d windows. (Local Ollama models report token counts; cost shows $0.)

## 6. Command Guard — keep tool-running agents safe
A safety gate on every shell command (critical for local models). Pipeline:
**team deny rules → built-in dangerous-command deny-list → team `ask` rules → allow**.

- Built-in deny-list blocks the catastrophic stuff (`rm -rf /`, fork bombs, `curl|sh`, writing to
  block devices, reading SSH/cloud keys, `sudo`, force-push, …). If the server guard is unreachable
  the host falls back to a local deny-list (fail-closed).
- Manage it in the dashboard **Security** tab: add `deny` / `ask` / `allow` rules (substring or
  regex), see recent checks, and **approve/deny** any pending `ask`.
- An **`ask`** rule escalates a gray-zone command for **human approval**: the worker pauses and polls
  until you approve/deny in the Security tab (fail-closed if it times out).

```bash
# example: require human approval for any "deploy", block "terraform destroy"
curl -s -X POST http://localhost:8000/api/teams/my-team/guard/rules \
  -H "Authorization: Bearer <join_token>" -H 'Content-Type: application/json' \
  -d '{"kind":"ask","pattern":"deploy"}'
curl -s -X POST http://localhost:8000/api/teams/my-team/guard/rules \
  -H "Authorization: Bearer <join_token>" -H 'Content-Type: application/json' \
  -d '{"kind":"deny","pattern":"terraform destroy"}'
```

## Useful host flags
| Flag | Meaning |
|---|---|
| `--executor {claude,codex,cursor,openai}` | which executor this agent uses |
| `--model` | model id (e.g. `opus`, `qwen3.6:35b`) |
| `--api-base` / `--api-key` | OpenAI-compatible endpoint (Ollama: `http://localhost:11434/v1`) |
| `--base-repo` | git repo to base per-session worktrees on (omit → process cwd) |
| `--max-concurrent` | how many sessions run at once (default 4) |
| `--max-steps` | max tool rounds per turn, openai executor (default 12) |
| `--tool-timeout` / `--openai-timeout` | per-bash / per-model-call timeouts (s) |
| `--allowed-tools` / `--permission-mode` | passed to `claude -p` (claude executor) |
| `--dry-run` | run the loop without invoking a model |

`python scripts/atmcp_workbench_host.py --help` lists everything.

## Backward compatibility
The workbench is additive: the `/team` console, the dashboard, the directive bus, and the
token-free `atmcp_worker_poller.py` all keep working. A directive with no session (the classic
`/team send`) routes to the agent's default thread.
