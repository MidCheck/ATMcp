#!/usr/bin/env bash
# Bulletproof ATMcp worker loop (recommended over the in-app `/loop`).
#
# Why: Claude Code's dynamic `/loop` (no interval) needs the model to re-arm a wakeup
# every turn, so it can silently stop (worse on Windows PowerShell). An OS-level loop that
# re-invokes Claude Code headless each tick is independent of that — if a turn ends or even
# crashes, the next tick just runs again. No 7-day expiry, fully cross-platform.
#
# Two-model handoff: the POLLER runs on a cheap/fast model ($ATMCP_MODEL, default haiku);
# the atmcp-worker skill delegates the actual instruction to the `atmcp-executor` subagent
# (model: opus — see agents/atmcp-executor.md), so reasoning stays strong while polling is cheap.
#
# Prereqs: this agent's MCP client is configured for the team with headers
#   Authorization: Bearer <join_token>   and   X-ATMcp-Agent: <this worker's name>
# and the atmcp-worker / atmcp-executor skills+agents are installed (see prompts/console-worker.md).
set -u

: "${ATMCP_MODEL:=haiku}"        # fast poller model (alias or full id)
: "${INTERVAL:=5}"               # seconds to sleep between iterations
# Tools to pre-approve so it runs unattended. mcp__atmcp = all ATMcp tools; Task = delegate
# to the executor subagent; Read/Edit/Bash/Write = let the executor actually do work.
# TIGHTEN THIS to what your workers should be allowed to do.
: "${ATMCP_ALLOWED:=mcp__atmcp,Task,Read,Edit,Bash,Write}"

echo "[atmcp] worker loop: model=$ATMCP_MODEL interval=${INTERVAL}s (Ctrl-C to stop)"
while true; do
  claude -p --model "$ATMCP_MODEL" --allowedTools "$ATMCP_ALLOWED" "/atmcp-worker" || true
  sleep "$INTERVAL"
done
