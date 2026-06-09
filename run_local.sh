#!/usr/bin/env bash
# Local dev: start a Redis container, then run the app from the venv.
set -euo pipefail
cd "$(dirname "$0")"

REDIS_NAME=atmcp-redis
if ! docker ps --format '{{.Names}}' | grep -q "^${REDIS_NAME}\$"; then
  echo "starting redis container ($REDIS_NAME) ..."
  docker run -d --rm --name "$REDIS_NAME" -p 6379:6379 \
    redis:7-alpine redis-server --save "" --appendonly no >/dev/null
fi

export ATMCP_REDIS_URL="${ATMCP_REDIS_URL:-redis://localhost:6379/0}"
export ATMCP_SQLITE_PATH="${ATMCP_SQLITE_PATH:-./data/atmcp.db}"
export ATMCP_ADMIN_TOKEN="${ATMCP_ADMIN_TOKEN:-change-me-admin-token}"
export ATMCP_PUBLIC_URL="${ATMCP_PUBLIC_URL:-http://localhost:8000}"

source .venv/bin/activate
echo "ATMcp on http://localhost:8000  (MCP: /mcp · dashboard: /dashboard?team=<team>)"
exec uvicorn atmcp.app:app --host 0.0.0.0 --port 8000 "$@"
