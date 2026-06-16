# ATMcp —— Agent 团队协作 MCP 服务

[English](README.md) · **中文文档**

一个部署在中心、可被网络访问的 **MCP 服务**:让分布在**不同设备 / 网络 / 地域**的 LLM 编码
Agent(Claude Code 及任意 MCP 客户端)组成一个 **团队** 协同工作 —— 共享**知识、记忆、任务目标、
进度与完成情况** —— 并配有一个**实时网页看板**展示每个 Agent 的状态。

技术栈:**Python + FastAPI + Redis + SQLite**。设计借鉴了分布式系统的成熟模式(云端的在线/心跳、
追加式内容寻址日志、CRDT 风格的合并语义、基于租约的任务调度),但只保留在 MVP 规模下**真正划算**的
部分,不上共识/哈希链。

```
 远程 Agent(不同设备/网络)                        浏览器
   Claude Code A   Claude Code B   …                看板 Dashboard
        │   streamable-HTTP MCP  │                      │ HTTP + WebSocket
        ▼                        ▼                      ▼
   ┌──────────────────── 单一 FastAPI 进程(uvicorn) ───────────────────┐
   │  /mcp  FastMCP(streamable-http)   /dashboard  /ws/{team}  /api/*      │
   │  SQLite(WAL)= 唯一真相源 · events 日志 = 审计/回放/看板源            │
   │  进程内 hub → WebSocket 扇出 · reaper → 回收过期租约的任务            │
   └───────────────────────────────────┬───────────────────────────────────┘
                                        ▼  软状态(可重建)
        Redis:心跳(在线 TTL) · 任务租约 · 事件流(补帧/扇出)
```

## 核心特性

- **SQLite 是唯一真相源**(WAL,单写者,由一把锁串行化)。**Redis 只是软状态** —— 丢了只会丢
  "活性"(在线状态、实时扇出、租约重派),绝不丢持久数据;一切都能从 SQLite 重建。
- **提交后发布(commit-then-publish)**:每次变更 `BEGIN IMMEDIATE → 写表 → 追加一条 events →
  COMMIT → 扇出`;工具返回值基于已提交事务(read-your-writes)。
- **在线状态是"推导"的**,从不存储:`在线 = 心跳 key 存在`(30s TTL,~10s 续期)。崩溃/断网的
  Agent 在过期后自动清理。
- **知识内容寻址**(`sha256`):相同发现自动去重并积累溯源(`contributor_count`);建模为 OR-Set,
  配快读投影表 + FTS5 全文检索。
- **记忆是 LWW 寄存器**,按 per-team Lamport 逻辑时钟排序;可选 `expected_version` 提供乐观 CAS,
  冲突当数据返回。
- **任务基于租约认领**:DB 仲裁的原子认领 + Redis 租约 + `fencing_token`,确保跨设备 Agent 绝不
  重复干活;5s 的 **reaper** 自动把崩溃 Agent 丢下的任务重新入队;僵尸 Agent 的过期 token 被拒。
- **多租户隔离是结构性的**:`team_id` 领衔每个索引、前缀每个 Redis key;scoped 工具的 team 从加入
  会话推导,绝不信任客户端输入。

## 快速开始(Docker)

```bash
cp .env.example .env            # 设置 ATMCP_ADMIN_TOKEN
docker compose up --build       # 启动 redis + atmcp,监听 :8000

# 创建一个团队(返回 join token 与各 URL):
curl -s -X POST http://localhost:8000/api/teams \
  -H "X-Admin-Token: $ATMCP_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-team"}' | jq
```

## 快速开始(本地开发)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run_local.sh                  # 自动起一个 redis 容器 + uvicorn,监听 :8000

# 不经 HTTP API 直接建团队(直接写 SQLite):
python -m atmcp.admin create-team my-team
```

## 接入一个 Agent

把任意 streamable-HTTP MCP 客户端指向 `http://<host>:8000/mcp`,在请求头里带上团队 join token
(以及可选的稳定 Agent 名)。配好这两个头之后,Agent **第一次调用任意工具就会自动加入团队** ——
无需显式调用 `join_team`。

```bash
# Claude Code
claude mcp add --transport http atmcp http://<host>:8000/mcp \
  --header "Authorization: Bearer <join_token>" \
  --header "X-ATMcp-Agent: alice"
```

> **添加服务器只是让工具"可用"。** Agent(Claude/Cursor/Qwen)在被告知之前不会主动上报 ——
> 因为 MCP 是 *拉,不是推*,而且 LLM 没有定时器。所以:
> 1. 给每个 Agent 一段工作流提示词 —— Claude Code / Cursor / Qwen 的现成规则在 **[`prompts/`](prompts/)**。
> 2. 想要可靠的在线状态(连"只在思考"时也保持在线),挂上心跳 sidecar —— 它走 REST 心跳,与 LLM
>    是否调工具**解耦**:
>    ```bash
>    python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
>      --team <team> --token <join_token> --name alice --interval 10
>    ```

如果你的客户端无法设置请求头,Agent 可以把 token 直接传给 `join_team`:
`join_team(team_name="my-team", display_name="alice", join_token="<join_token>")`。

## 让 Agent 真正用起来(重点)

MCP 是**拉,不是推**:工具是"可用"的,但模型自己决定何时调用,而且没有定时器。三层配套:

| 机制 | 作用 | 你要做的 |
|---|---|---|
| **自动 join** | 请求头带 `Authorization: Bearer <join_token>`(+ 可选 `X-ATMcp-Agent: <名字>`)→ 首次调用任意工具自动入队 | 在客户端配置里加两个 header |
| **工作流提示词** | 告诉 Agent 主动 `post_knowledge` / `claim_next_task` / `complete_task` 等;不给提示它"知道有工具但不会用" | 把 `prompts/agent-workflow.md` 贴进 system prompt / 规则文件 |
| **心跳 sidecar** | LLM 没定时器,靠它自觉打心跳不稳;sidecar 按 timer 走 REST,让在线状态与模型是否调工具解耦 | 每个 Agent 旁边跑 `scripts/atmcp_heartbeat.py` |

详见 **[`prompts/README.md`](prompts/README.md)**:各客户端连接方式、自动 join 的请求头、以及保持在线
状态的三种方式(模型自驱 / sidecar / 客户端 hook)。REST 在线端点
`POST /api/teams/{team}/heartbeat`(用 join token 鉴权)即 sidecar 的后端。

## 团队控制台 —— 在一个窗口里管理整个团队

一个交互式 **console** 窗口 + N 个后台 **worker** 循环。在 console 里你可以:列出所有 agent 的状态
与 TODO、给**某个指定 agent**发指令、等它的结果、实时查看另一个 agent 的输出 —— 解决"一个人同一
时刻只能待在一个 agent shell"。

```
  你 ── /team ──►  Console agent ──MCP──►  ATMcp  ◄──MCP── Worker agents(/loop /atmcp-worker)
       命令         send_directive ───────► directives ──► inbox → claim → 执行
       结果   ◄──  wait_directive  ◄─────── (状态)    ◄── report_directive
       输出   ◄──  get_agent_output ◄────── agent_output ◄ append_output / hook
```

```
/team status                 # 名册(在线·当前任务·进度)+ TODO 看板
/team send bob "重构 X"      # 给指定 agent 发指令 → 返回 directive_id
/team watch <directive_id>   # 长轮询直到 bob 上报 done/失败,打印结果
/team logs bob --follow      # 实时 tail bob 的输出
/team dispatch "修 flaky 测试"  # 不指定 agent → 谁空谁认领的任务
```

服务端是**指令总线**(`send_directive`/`inbox`/`claim_directive`/`report_directive`/
`wait_directive`)+ **Agent 输出流**(`append_output`/`get_agent_output`,以及给 hook 用的
`POST /api/teams/{team}/agents/{agent}/output`)。"watch/通知"靠长轮询实现 —— worker 一上报,
结果立刻出现在对话里。

**省 token 地跑 worker**:在模型里一直 loop `atmcp-worker` 很费 token —— 每次轮询都是一整个模型回合
(system prompt + 全部工具 schema),仅为发现"收件箱是空的",一天能白烧几百万 token。**首选零 token
轮询器** `scripts/atmcp_worker_poller.py`:纯 HTTP 长轮询收件箱(空闲时**零模型 token**),只有真有
指令时才 `claude -p` 起一次模型执行:
```bash
python scripts/atmcp_worker_poller.py --url http://<host>:8000 \
  --team <team> --token <join_token> --name bob --model opus   # 加 --dry-run 可空跑测试
```
默认**每个 worker 保持一个可恢复 session**(`--session-mode resume`:抓取 Claude `session_id` 并在每条
指令时 `--resume`),所以 worker **记得之前的任务**(Claude 自动压缩上下文),而空闲轮询仍是零 token;
收件箱长轮询在指令发出瞬间返回(别用 `/loop` 的 1 分钟 cron)。它走 worker REST API
(`GET …/agents/{agent}/inbox` 长轮询、`POST …/directives/{id}/claim`、`…/report`)。
若更想把 `atmcp-worker` **技能**当 agent 循环跑,用运行脚本 `scripts/atmcp_worker_runner.{sh,ps1}`
(轻量模型轮询 + Opus `atmcp-executor` 子 agent 执行);别用裸 `/loop /atmcp-worker`(动态模式会悄悄
停,Windows PowerShell 尤其明显;改用 `/loop 30s`)。详见
**[`prompts/console-worker.md`](prompts/console-worker.md)** 与 **[`skills/`](skills/)**。

## 网页看板

打开 `http://<host>:8000/dashboard?team=<队名>` —— 整页满高的**三栏**布局(页面本身不滚动,每栏各自
内部滚动):
- **左栏**:目标进度 + 统计 + 实时 agent 名册(绿/黄/灰 在线徽标)。
- **中栏(标签页)**:任务看板 · 活动流 · 知识 · **Tokens**(各 agent 的 token / 成本计量,带 5 小时 /
  7 天滚动窗口)—— **点击某个 agent** 会就地打开一个 **Agent** 标签页,里面是它的指令、任务与实时
  输出(不用再往下滚)。
- **右栏(常驻)**:**团队控制台** —— 输入 `/team` 命令(伪模型对话),输入框固定在底部,命令结果与
  实时指令事件流入对话区。

页面先拉一次 JSON 快照,再通过 WebSocket 实时增量更新(断线自动重连并补帧)。鉴权**默认关闭**;设
`ATMCP_DASHBOARD_AUTH=1` 可要求每团队一个只读令牌(控制台发指令始终需要 join token)。

## MCP 工具

| 分组 | 工具 |
|---|---|
| 身份 | `join_team`、`leave_team` |
| 在线 | `heartbeat` |
| 知识 | `post_knowledge`、`search_knowledge`、`retract_knowledge` |
| 记忆 | `set_memory`、`get_memory` |
| 目标/任务 | `create_goal`、`create_task`、`claim_task`、`claim_next_task`、`update_task_progress`、`complete_task`、`fail_task`、`release_task`、`list_tasks` |
| 指令(点对点) | `send_directive`、`inbox`、`claim_directive`、`report_directive`、`wait_directive`、`cancel_directive`、`list_directives` |
| 输出流 | `append_output`、`get_agent_output` |
| 状态/同步 | `list_agents`、`get_team_status`、`sync` |

所有写工具都接受可选的 `idem_key`(幂等)。预期内的情况以**数据**返回(`{conflict}`、`{taken_by}`、
`{stale_token}`、`{not_joined}`),而非报错。`sync(since_event_id, wait_ms)` 会长轮询等待下一个事件,
让 Agent 能尽快响应。

## Token / 成本监测(让限额不再悄悄耗尽)

`claude -p --output-format json` 的返回里本就带 `usage` + `total_cost_usd`,所以省 token 的 poller
顺手就能捕获并上报(`POST …/agents/{agent}/usage`)。服务端用一张 append-only 的 `usage_events`
计量表记账,前端 **Tokens** 标签页展示各 agent 的输入/输出/缓存 token、成本,以及 **5 小时 / 7 天滚动
窗口**(对齐 Claude 的限流窗口),实时看出每个 agent 距离限额还有多少。

poller 还提供**硬性预算刹车**:`--cost-budget <USD>` / `--token-budget <N>`(0 = 不限;累计值按 worker
持久化在 `--state-dir`,重启不丢)。一旦达到预算,worker 就**停止 claim 指令**,前端显示"paused:
budget reached"——后台跑再久也不会无声烧穿限额。调高预算或加 `--reset-usage` 即可恢复。

## 配置

见 `.env.example`。要点:`ATMCP_ADMIN_TOKEN`、`ATMCP_SQLITE_PATH`、`ATMCP_REDIS_URL`、
`ATMCP_PUBLIC_URL`、心跳 TTL/间隔(`30`/`10s`)、租约 TTL(`90s`)、reaper 间隔(`5s`)、
`ATMCP_TASK_MAX_ATTEMPTS`、`ATMCP_DASHBOARD_AUTH`、用量保留(`ATMCP_USAGE_RETENTION_S`,30 天)。

## 失效模型(摘要)

| 失效 | 行为 |
|---|---|
| Agent 崩溃 / 网络分区 | 心跳 key 过期 → 显示离线;持有的租约过期 → reaper 重派;最后一次持久进度仍在。 |
| Agent 重连 | 重新 `join_team`(经 `(team, display_name)` 复用同一 `agent_id`)→ `sync(since_event_id)` 补帧。 |
| 重复 / 重试调用 | `idem_key` 返回已存结果;相同知识自动去重;过期 fencing token 被拒。 |
| Redis 宕机 | 变更仍提交到 SQLite;在线状态降级到 `last_seen`;防重复认领仍由 DB 保证。 |
| 服务重启 | SQLite 完好(WAL);Agent 重连并重新加入;reaper 协调租约。 |

## 测试

```bash
source .venv/bin/activate
pip install -r requirements.txt
pytest -q     # 服务级测试:认领竞争、fencing/僵尸、reaper、去重、LWW/CAS、
              # 隔离、幂等、hub 唤醒、自动 join、REST 快照与心跳(共 42 项)
```

## 目录结构

```
atmcp/
  app.py            FastAPI 组装 + lifespan(挂载 /mcp,接线发布器)
  mcp_server.py     FastMCP 工具面(~33 个工具)
  web.py            看板、/api/*、/ws/{team}、REST 心跳/输出、健康检查、admin 建团
  db.py             单写者 SQLite,transaction() = 提交后发布
  redis_bus.py      软状态:心跳、租约、事件流、会话(尽力而为)
  hub.py            进程内 WebSocket 扇出 + 长轮询唤醒(代际计数器)
  reaper.py         回收过期租约的任务;清理幂等表与 agent 输出
  events.py         追加到单调 events 日志
  idempotency.py    事务内的持久幂等(写工具可安全重试)
  session.py        MCP 会话 →(team, agent)身份绑定 + 请求头自动 join
  canonical.py      内容寻址(规范化 JSON + sha256)
  schema.sql        完整 DDL(含 FTS5)
  services/         identity · presence · knowledge · memory · tasks · status · clock
                    · directives(console→agent 指令)· output(agent 输出流)
  static/           dashboard.html + dashboard.js
prompts/            现成的 Agent 规则 + console/worker 配置说明
scripts/            atmcp_worker_poller.py(零 token worker)· atmcp_heartbeat.py
                    · atmcp_output_hook.py · atmcp_worker_runner.{sh,ps1}
skills/             team(控制台)+ atmcp-worker(worker 循环)Claude Code 技能
agents/             atmcp-executor(worker 委派执行用的 Opus 子 agent)
```

## 许可

Apache License 2.0(见 [LICENSE](LICENSE))。
