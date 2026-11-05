# LangGraph 智能体

建议按此顺序阅读：

1. `config.py`：加载项目模型配置、系统提示词和 Skills。
2. `graph.py`：模型与工具循环、上下文预算、摘要及消息校验。
3. `model.py`：私有模型的 OpenAI 兼容流式适配。
4. `manager.py`：会话、SQLite 检查点、中止、空闲回收和旧历史导入。
5. `cli.py`：终端交互入口，与网页共用运行层。

`context.py` 构造模型侧证据视图，保留原检查点消息；统一检查包含提示、工具Schema和输出预留的请求容量。`graph.py` 另计含摘要/修正的generate总预算并控制转换重复检索收尾；`manager.py` 执行可配置任务总期限。实测和限制见 [优化记录](../docs/OPTIMIZATION_2026-10-27.md)。

工具执行统一使用 `tools/bridge.py`。当前包不依赖 FastAPI，也不托管前端或启动 MCP 服务。模型仅可调用 `bom_search`、`rule_search`。

运行终端：在项目根目录执行 `python -m langgraph_app.cli [sid]`，或使用 `run-agent.bat`。
