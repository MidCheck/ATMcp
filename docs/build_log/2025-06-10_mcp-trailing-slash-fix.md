### BUILD_LOG

时间: 2025-06-10
操作类型: 修复
涉及文件:
- atmcp/app.py
- tests/test_mcp_mount.py（新增）

变更摘要:
- 新增 `normalize_mcp_scope` 中间件，将 `/mcp` 内部重写为 `/mcp/`，避免 Starlette Mount 返回 307
- MCP HTTP 客户端通常 POST 到配置的 URL（无尾斜杠）且不跟随重定向，此前会表现为 502

设计决策:
- Host 校验问题已在 mcp_transport 修复；本次 502 根因是 POST /mcp 的 307 重定向
- 用 scope 路径重写而非改客户端 URL，与文档/claude mcp add 默认无尾斜杠保持一致

兼容性影响:
- 不破坏已有接口；重启服务后 agent 可继续用 `http://<host>:18000/mcp` 连接
