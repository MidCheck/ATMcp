### BUILD_LOG

时间: 2025-06-10
操作类型: 修复
涉及文件:
- atmcp/mcp_compat.py（新增）
- atmcp/app.py
- tests/test_mcp_compat.py
- .cursor/mcp.json
- scripts/cursor_with_atmcp.sh（新增）

变更摘要:
- 新增 McpProxyCompatMiddleware：代理剥离 Content-Type/Accept 或改写 Host 时自动补全
- 根因：系统 HTTP 代理（Clash :7890）经 httpx trust_env 转发 MCP POST，body 被清空 → HTTP 400
- mcp.json 改为 127.0.0.1:18000；提供 cursor_with_atmcp.sh 设置 NO_PROXY 启动 Cursor

设计决策:
- body 被代理吞掉无法服务端修复，必须 NO_PROXY 绕过本地/LAN 地址
- Host/Content-Type/Accept 补全仍保留，减轻部分代理对头部的破坏

兼容性影响:
- 需重启 ATMcp 服务；Cursor 需 NO_PROXY 后 Reload 或重新打开
