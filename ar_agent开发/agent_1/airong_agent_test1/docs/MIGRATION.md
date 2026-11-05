> 目录整理更新（2026-10-23）：启动位置已统一为业务项目根目录。前端在 `web/`，API 在 `backend/`，LangGraph 在 `langgraph_app/`，MCP 在 `mcp_service/`；虚拟环境为根目录 `.venv-langgraph/`。完整结构见 [项目 README](../README.md)。

本轮目录整理验证：10 项回归通过（含页面及 JS/CSS/Vue 静态资源、API、SSE、会话恢复、取消与 MCP），环境依赖一致性与 `pip`/`pytest` 命令入口检查通过，从其他工作目录调用根目录 `run-agent.bat` 可正常启动退出。PowerShell 启动入口语法检查通过。未重新调用真实模型或 MySQL，未重启已有服务。

# LangGraph 迁移说明

业务默认入口已改为 Python LangGraph，Pi 源码及回退入口保留。迁移范围是核保应用可用的模型与工具循环、技能、终端及 Web 会话能力；不将 Pi 框架中未接入业务的远程服务、编辑器、任意 shell 等功能开放给模型。

## 启动与配置

建议 Python 3.12。在项目根目录创建 `.venv-langgraph` 虚拟环境并安装 `requirements.lock.txt`（包含已验证的间接依赖和测试依赖），项目根目录执行 `npm ci --ignore-scripts`。Node 必须支持 `node:sqlite`（建议保持原项目 Node >=22.19）。

```powershell
.\.venv-langgraph\Scripts\python.exe -m pip install -r requirements.lock.txt
powershell -File run-web.ps1
```

默认仍监听 `127.0.0.1:8712`，`PORT` 可调整；端口被占用会报错，不再自动杀进程。也可在项目根目录直接运行 `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8712`。终端入口为业务目录 `run-agent.bat [sid]`，传入相同 sid 可恢复；`/new` 清空当前模型上下文，`/exit` 退出。

沿用 `.pi/settings.json` 默认 provider/model 和 `.pi/models.json` 的对应 provider；不会自动切换模型。可用 `AGENT_MODELS_FILE`、`AGENT_PROVIDER`、`AGENT_MODEL`、`AGENT_BASE_URL`、`AGENT_API_KEY` 覆盖。当前适配 OpenAI 兼容 Chat Completions（含 vLLM），保留流式文字、`reasoning_content`、工具调用分片。密钥可引用同名环境变量，不执行命令形式的密钥。模型凭据仍在 Git 忽略的本地文件中，禁止提交或打印。

## 运行职责

`langgraph_app/` 包含模型适配、图、会话管理、CLI 和配置；`tools/bridge.py` 定义共用工具 Schema/校验，`mcp_service/server.py` 提供可选 MCP 服务，`backend/` 提供 HTTP/SSE 接口。图为 `model → tools → model` 循环，最终经过消息完整性校验；每轮最多 12 次模型调用。没有完整规则 IR，故这里的校验不代表已经实现字段、运算符和动作参数的编译级校验。

`tools/runtime-tools.mjs` 结构化调用原 `bom-search.mjs` / `rule-search.mjs`，查询、排序、去重、动作填参及 SQL 重试逻辑只有一份。工具入参使用严格 Schema，模型只可调用 `bom_search` / `rule_search`；规则范围由原管理员配置指定，不接受模型跨范围覆盖。取消会关闭本轮模型 HTTP 流并终止会话工具子进程。数据库服务器可能需要时间感知连接断开。

宿主显式读取项目 `.pi/APPEND_SYSTEM.md` 和 `.pi/skills/**/SKILL.md`，普通问答和 CLI 都获得技能全文，支持 `/skill:rule-authoring ...`。模型不获得读取文件、shell 或任意 SQL 能力。新增技能须由项目维护者审查可信内容；技能的相对资源不会自动变成模型文件访问权限。

HTTP API 路径、SSE 文本/思考增量、消息定稿、工具开始/结束、错误及 `agent_settled` 与现有 Vue 客户端保持兼容。附件提取逻辑不变；浏览器多会话、草稿、切换/停止/删除继续使用原流程。重复发送正在运行的 sid 会拒绝。每进程最多 10 个会话，回收无订阅且非运行的会话，空闲 30 分钟回收。

## 持久化与旧历史

`config/langgraph/checkpoints.sqlite` 为 LangGraph 检查点，`config/langgraph/sessions.sqlite` 保存 sid→thread_id。与 BOM SQLite、业务 MySQL 分开。关闭/空闲回收不删除检查点；新会话更换 thread_id。取消、错误或崩溃后下一轮从最近完整对话继续，不携带半截工具调用。检查点不代表自动继续中断时的生成。

首次使用旧 sid 时，尝试从 `config/pi-agent/sessions` 只读导入 Pi 当前分支的用户/助手消息、工具调用及工具文本结果。原 JSONL、工具结果和分支信息原样保留用于回退；不能声称两种持久化格式完全等价。超出预算时，对早期消息分块摘要，向模型传入摘要和最近可容纳的完整对话轮次，磁盘历史仍保留。摘要保留需求、阈值、例外和待澄清项，精确原文须重新检索；摘要质量仍取决于模型。单轮含附件仍过长会明确拒绝，不静默截断后宣称处理完整。

## 可选 MCP

在项目根目录运行：

```powershell
.\.venv-langgraph\Scripts\python.exe -m mcp_service.server
```

这是 stdio MCP 服务，供其他可信客户端管理和复用两个只读工具；由客户端启动进程，不是 HTTP 公网服务。客户端配置使用该 Python 的绝对路径、上述 `-m` 参数和 项目根目录绝对 cwd。默认 LangGraph 通过结构化 Node 接口调用工具，MCP 服务与其共享同一实现，未叠加双重工具调度。没有自动发现并信任任意外部 MCP 服务。MCP SDK 使用 1.x API，版本锁定在 requirements。

## 验证与回退

```powershell
.\.venv-langgraph\Scripts\python.exe -m pytest tests -q
```

测试依赖见 `requirements-dev.txt`。测试使用模拟模型，覆盖多轮、工具事件、持久化重启、取消后继续、新会话、容量、白名单、上传及真实 BOM 结果对照。私有模型和 MySQL 的真实端到端验收应单独记录，不用健康接口代替。现有附件 30000 字符提取上限、无 OCR、单机单 worker、无认证及 SSE 无可靠补发的边界仍存在。

2026-10-23 验证记录：Python 3.12 独立环境依赖检查通过，10 项回归测试通过；使用项目原默认私有模型完成两条真实只读问答，分别成功调用 `bom_search`、`rule_search`，各返回 1 条结果，工具均无错误，产生文本增量、最终答复并以 `agent_settled` 收尾。MCP stdio 工具发现及真实 BOM 调用通过。真实模型验收通过 `python -m tests.live_smoke` 执行，结果仅输出状态，不打印凭据和规则原文。此结果不等同于全部业务样例的规则质量评测或浏览器人工验收；未重启原本运行的服务。

Pi 终端回退：原 `run-pi.bat`。Web 回退：先停止 LangGraph 服务，再运行 `run-web-pi.ps1`（此入口保留原配置，端口占用时报错），使用 `legacy.pi.main:app`。两种运行层使用不同会话存储，切换前应备份本地 config，后续 LangGraph 对话不会自动写回 Pi JSONL。

实现 API 依据：[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[MCP Python SDK 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
