### BUILD_LOG

时间: 2025-06-10
操作类型: 修复
涉及文件:
- atmcp/mcp_transport.py（新增）
- atmcp/mcp_server.py
- run_local.sh
- .env.example
- tests/test_mcp_transport.py（新增）

变更摘要:
- 新增 `build_mcp_transport_security()`，从 `ATMCP_PUBLIC_URL` 推导 MCP 允许的 Host/Origin
- FastMCP 启动时注入 `transport_security`，LAN IP（如 192.168.2.7:18000）不再触发 HTTP 421
- 支持 `ATMCP_MCP_DNS_REBINDING=0` 关闭防护，以及 `ATMCP_MCP_ALLOWED_HOSTS/ORIGINS` 额外白名单
- `run_local.sh` 默认 `ATMCP_PUBLIC_URL` 与端口 18000 对齐

设计决策:
- FastMCP 默认 host=127.0.0.1 时仅允许 localhost Host，内网 IP 会被 DNS rebinding 中间件拒绝
- 复用已有 `ATMCP_PUBLIC_URL` 作为 agent 可达地址的单点配置，避免重复维护
- 始终保留 localhost 通配符，本地开发不受影响

兼容性影响:
- 不破坏已有接口；需将 `ATMCP_PUBLIC_URL` 设为 agent 实际访问的 URL 后重启服务
- 无需数据库迁移
