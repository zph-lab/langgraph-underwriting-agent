# 共用业务工具

- `bridge.py`：Python 工具 Schema、白名单、参数校验和 Node 子进程生命周期，供 LangGraph 与 MCP 共用。
- `runtime-tools.mjs`：结构化 JSONL 适配，不包含独立 SQL 实现。
- `bom-search.mjs`：本地 BOM SQLite 只读检索及格式化。
- `rule-search.mjs`：已有 MySQL 规则检索、排序、动作填参及错误分类。
- 其他 `.mjs`：现有导入、管理、检索辅助脚本；不因目录整理自动执行。

模型不获得任意 SQL、shell 或文件访问。BOM 和 MySQL 配置、数据位置不变；新增工具时同步检查 Schema、白名单、提示词、MCP 注册和前端状态展示。
