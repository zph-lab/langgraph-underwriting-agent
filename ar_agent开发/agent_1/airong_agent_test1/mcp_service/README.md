# MCP 服务

`server.py` 使用 MCP Python SDK 提供 stdio 服务，只注册 `bom_search` 和 `rule_search`。它调用 `tools/bridge.py`，不复制查询逻辑，也不依赖 LangGraph 运行层。

在项目根目录执行：

```powershell
.\.venv-langgraph\Scripts\python.exe -m mcp_service.server
```

也可使用 `run-mcp.bat`。MCP 客户端配置时，`command` 指向根目录 `.venv-langgraph/Scripts/python.exe` 的绝对路径，`args` 为 `["-m", "mcp_service.server"]`，`cwd` 为项目根目录的绝对路径。不要再使用旧 `web` 工作目录或 `server.agent_runtime.mcp_server` 模块名。

stdio 服务通常由客户端管理生命周期，不提供浏览器页面。Web 默认直接通过共用工具桥接检索，无需额外启动此服务。
