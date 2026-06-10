# Bulletproof ATMcp worker loop for Windows PowerShell (recommended over the in-app /loop).
#
# Claude Code's dynamic /loop (no interval) needs the model to re-arm a wakeup each turn and
# can silently stop — especially noticeable in PowerShell. This OS-level loop re-invokes
# Claude Code headless every tick, independent of that, and survives a turn ending or crashing.
#
# Two-model handoff: the POLLER runs on a fast model ($env:ATMCP_MODEL, default haiku); the
# atmcp-worker skill delegates the actual instruction to the atmcp-executor subagent (Opus),
# so reasoning is strong while polling stays cheap.
#
# Prereqs: this agent's MCP client is configured for the team with headers
#   Authorization: Bearer <join_token>  and  X-ATMcp-Agent: <this worker's name>
# and the atmcp-worker / atmcp-executor skills+agents are installed.

if (-not $env:ATMCP_MODEL)   { $env:ATMCP_MODEL = "haiku" }   # fast poller model
if (-not $env:INTERVAL)      { $env:INTERVAL = "5" }          # seconds between iterations
# Pre-approved tools for unattended runs. TIGHTEN to what your workers may do.
if (-not $env:ATMCP_ALLOWED) { $env:ATMCP_ALLOWED = "mcp__atmcp,Task,Read,Edit,Bash,Write" }

Write-Host "[atmcp] worker loop: model=$($env:ATMCP_MODEL) interval=$($env:INTERVAL)s (Ctrl-C to stop)"
while ($true) {
  try {
    claude -p --model $env:ATMCP_MODEL --allowedTools $env:ATMCP_ALLOWED "/atmcp-worker"
  } catch {
    Write-Host "[atmcp] iteration error: $_"
  }
  Start-Sleep -Seconds ([int]$env:INTERVAL)
}
