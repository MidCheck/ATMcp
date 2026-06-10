### MEMORY_SNAPSHOT

当前项目结构:
- Python 包 `atmcp/`：FastAPI 入口 app.py、FastMCP mcp_server.py、新增 mcp_transport.py、web 仪表盘、services 业务层、SQLite+Redis

当前技术栈:
- FastAPI + uvicorn、FastMCP streamable-HTTP（/mcp）、Redis、aiosqlite、mcp>=1.9

已实现模块:
- 团队/agent 协作 MCP 工具（join、heartbeat、task、knowledge、memory、sync 等）
- MCP transport 安全：从 ATMCP_PUBLIC_URL 配置 allowed Host/Origin

当前待完成任务:
- 用户侧：重启服务并设置 ATMCP_PUBLIC_URL=http://192.168.2.7:18000，agent 重连 /mcp

关键架构决策:
- MCP DNS rebinding 防护通过 mcp_transport.build_mcp_transport_security() 集中配置
- 内网可设 ATMCP_MCP_DNS_REBINDING=0 或 ATMCP_MCP_ALLOWED_HOSTS 扩展白名单
