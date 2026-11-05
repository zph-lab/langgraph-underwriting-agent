# HTTP 后端

- `main.py`：FastAPI 入口，提供 `/api/prompt`、`/api/events`、停止、新会话、关闭、健康检查和上传 API，并托管 `web/` 前端。
- `file_extract.py`：附件文本提取及大小限制。

在项目根目录运行 `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8712`。推荐使用根目录 `run-web.ps1`，它会选择项目虚拟环境并检查端口。

智能体执行交给 `langgraph_app.manager.SessionManager`；后端不直接拼装 SQL 或调用模型。SSE 事件格式保持兼容现有前端。
