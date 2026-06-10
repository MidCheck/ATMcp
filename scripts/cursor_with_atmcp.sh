#!/usr/bin/env bash
# Launch Cursor with NO_PROXY so MCP requests to local ATMcp bypass Clash/V2Ray.
# Usage: ./scripts/cursor_with_atmcp.sh
set -euo pipefail
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,192.168.0.0/16,10.0.0.0/8"
export no_proxy="$NO_PROXY"
exec open -a Cursor "$@"
