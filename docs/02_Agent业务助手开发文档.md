# Agent 业务助手开发与交接文档

核对日期：2026-11-03。本文对应页面上的 **规则AI助手**，源码根目录为 `ar_agent开发/agent_1/airong_agent_test1/`，下文简称 `A`。这是路径缩写，不是环境变量。

建议先读[项目启动部署文档](03_项目启动部署文档.md)并启动合并入口，再读本文。助手调用的离线编译能力由[文档编辑器](01_文档编辑器开发文档.md)提供。

## 1. 产品能力与当前实现

助手面向核保业务人员，主要完成三类任务：查询 BOM 字段或方法、检索已有业务规则、将用户需求或附件中的规则转写为中文规则文本。前端用 Vue 3 全局构建版，后端使用 FastAPI、Python LangGraph 和自定义流式模型适配；业务检索由 Node.js 工具完成。

当前默认运行层是 **LangGraph**。`.pi` 仍保存模型配置、系统提示和业务 Skill，但主链路不启动 Pi Agent，也不通过 MCP 二次调度工具。Pi 底座和旧后端保留作回退。

已有规则查询与新规则编写是两条不同路径：前者由工具查询、补齐动作参数，再由宿主保真展示；后者仍主要由模型理解原文、选择检索和组织草稿，并在可用时调用编辑器做离线检查。不能把离线语法通过描述为完整语义、业务执行或发布验证通过。

## 2. 项目地图与阅读顺序

下表路径均相对 `A`。

| 位置 | 实际职责 |
| --- | --- |
| `project_paths.py` | 稳定定义 `PROJECT_DIR`、`WEB_DIR`、`STATE_DIR` |
| `backend/main.py` | HTTP、SSE、上传、静态文件、运行层生命周期 |
| `backend/file_extract.py` | 文件类型检查、解码、PDF/DOCX 纯文本提取及截断 |
| `langgraph_app/config.py` | 模型配置、系统提示、Skill 全文加载、运行预算 |
| `langgraph_app/manager.py` | sid、任务、检查点、取消、空闲回收、旧历史导入 |
| `langgraph_app/graph.py` | 模型与工具循环、任务分类、检索收尾、摘要、编辑器复核 |
| `langgraph_app/model.py` | OpenAI 兼容 Chat Completions 流式 HTTP 适配 |
| `langgraph_app/context.py` | 精简模型侧工具证据视图、去重、请求容量估算 |
| `langgraph_app/authoring.py` | 任务识别、格式化、语法规范化、绑定缺口提示、已有规则展示 |
| `langgraph_app/editor.py` | 宿主专用离线编辑器客户端，不是模型工具 |
| `langgraph_app/cli.py` | 终端入口，复用会话和图 |
| `tools/bridge.py` | 两个工具的严格 Schema、白名单及 Node 子进程管理 |
| `tools/runtime-tools.mjs` | JSONL 调用适配，复用原查询核心 |
| `tools/bom-search.mjs` | BOM SQLite 只读查询、父链、参数及对象访问模板 |
| `tools/rule-search.mjs` | MySQL 规则召回、排序、预定义/动作补全和友好错误 |
| `tools/bom-to-sqlite.mjs` | 运维导入与查询脚本，不是模型可执行工具 |
| `mcp_service/server.py` | 可选 MCP stdio 服务，独立暴露同两个只读工具 |
| `.pi/APPEND_SYSTEM.md` | 受众、回答格式、业务及交付约束 |
| `.pi/skills/rule-authoring/SKILL.md` | 规则编写、证据核实和草稿交付规范 |
| `.pi/settings.json`、`.pi/models.json` | 默认 provider/model 与服务元数据，含敏感配置 |
| `config/mysql.json`、`config/rule-editor.json` | MySQL 与范围、编辑器复核配置 |
| `config/langgraph/` | 当前运行层持久化数据，含业务对话 |
| `web/` | 无需构建的 Vue 前端、样式、公司 Logo、本地 vendor |
| `tests/` | mock/本地机制回归，以及显式命名的真实服务验收入口 |
| `evals/` | 业务质量与性能评测；输出不是应用启动必需品 |
| `docs/` | 迁移、优化、日志与历史评测资料 |
| `legacy/pi/`、`.pi/extensions/` | Pi 回退后端与扩展 |

`ar_agent开发/agent_1/pi-main/` 是保留的 Pi monorepo，包含 coding-agent、agent、ai、tui、MCP、远程协议等基础包；不是当前合并部署必须逐个启动的服务。`ar_agent开发/backups/stable-20261029/` 是历史稳定备份，不当成当前生产配置覆盖回来。

建议阅读顺序：根目录合并入口 → `backend/main.py` → `manager.py` → `config.py` → `graph.py` → `model.py` → `tools/bridge.py` 和两个查询核心 → `authoring.py`/`editor.py` → 前端及测试。

## 3. 一次请求如何执行

```text
浏览器 /assistant/
  ├─ POST /assistant/api/upload → 提取附件文本 → 前端拼入用户消息
  ├─ GET /assistant/api/events?sid=... → 建立 SSE 订阅
  └─ POST /assistant/api/prompt {sid,message}
       → SessionManager.ensure/prompt → 创建异步 Session.run
       → 恢复最近完整历史、加载配置和系统提示
       → LangGraph: model → tools → model ... → validate → END
                        │         │
                        │         └─ Node JSONL → BOM SQLite / MySQL
                        └─ 私有模型 /chat/completions
       → 新规则按需格式化、参数绑定提示、编辑器离线复核
       → message_end 定稿 → agent_settled 收尾
```

`POST prompt` 返回成功只代表任务已接受。业务完成和失败应从 SSE 的 `message_end`、`agent_settled` 判断，不能把 HTTP 200 作为生成成功率。

### 3.1 图与收敛控制

`State` 保存 `messages`、`rounds`、`validation`、`summary`、`summarized_until`。每次 `Session.run` 创建新的图实例和任务内工具缓存，避免不同用户、模型范围或任务复用错误结果。工具节点按顺序执行，不支持把现有串行桥简单改成无约束并发。

当前代码中的主要限制：

| 限制 | 当前值或来源 | 解释 |
| --- | --- | --- |
| 普通模型循环 | 路由上限 12 轮 | 摘要等额外请求也受总请求预算约束，不能称最多 12 次 HTTP |
| 模型总生成请求 | 默认 16，`AGENT_MODEL_REQUEST_LIMIT` | 包括摘要与收尾调用；HTTP 重试另计 |
| 工具请求 | 每任务最多 48 次 | 成功且参数相同的请求可复用任务内缓存 |
| 整任务期限 | 默认 600 秒，`AGENT_TASK_TIMEOUT_SECONDS` | 包含图执行；超时取消并保留已接收过程 |
| 单次工具等待 | 60 秒 | JSONL 响应超时，不等于整任务期限 |
| 模型 HTTP | connect 15 秒、其余默认 180 秒 | 不替代任务总期限 |
| 编辑器复核 | 总 25 秒、请求 10 秒 | 失败时保留草稿并说明未完成 |

短规则与长文档分别使用 4/8 批检索的收尾策略；连续两批无新增证据也可进入无工具收尾。这是程序限制，不保证模型已经找到全部正确映射。

### 3.2 上下文与证据

`context.model_messages()` 对模型输入做投影，不改检查点原消息：去掉历史 `reasoning_content`；BOM 主要保留结构化 `details`；规则保留已填参文本；重复结果用引用去重。原始证据仍可留在检查点和工具事件中。

历史选择只保留完整轮次，早期内容按预算分块摘要；超长单轮或摘要失败会明确报错。最后还会检查提示、证据、工具 Schema、输出预留和模板余量组成的完整请求容量。`request_token_bound` 是保守 UTF-8 字节估算，不是真实 token 使用量；摘要也可能丢失语义，精确条件应重新核实。

## 4. 模型配置与协议

`config.model_config()` 读取 `.pi/settings.json` 的默认 provider/model，再读取 `.pi/models.json`，可被环境变量覆盖。核对日磁盘默认 provider/model 为 **Qwen3.8-27B-NVFP4**，配置上下文 262,144、最大输出 32,768；这是本地文件快照，不证明远端实际容量或正在运行的进程已加载该值。

| 环境变量 | 用途 |
| --- | --- |
| `AGENT_MODELS_FILE` | 替代模型配置文件 |
| `AGENT_PROVIDER`、`AGENT_MODEL` | 选择 provider/model |
| `AGENT_BASE_URL`、`AGENT_API_KEY` | 覆盖模型地址和密钥 |
| `AGENT_NODE` | Node 工具桥使用的可执行程序 |
| `AGENT_TASK_TIMEOUT_SECONDS` | 任务总超时，正整数 |
| `AGENT_MODEL_REQUEST_LIMIT` | 模型总调用预算，正整数 |
| `RULE_EDITOR_CONFIG` | 替代编辑器 profile 路径 |

当前仅接受 `openai-completions` 协议配置，实际请求是 `base_url + /chat/completions`。模型需支持流式文本与工具调用，返回的函数参数分片由适配层合并。不是 OpenAI Responses、完整 Pi 多供应商适配或已接入的 LangChain 结构化输出。

适配层会处理 `reasoning_content`、usage、`finish_reason=length`、不完整工具调用和意外断流；429/502/503/504 可有限重试，已输出后网络中断不会盲目重放。代码对列出的 DeepSeek 模型有关闭思考字段的特判，不能据此声称当前 Qwen 配置也已启用相同开关。

修改模型配置后应在新任务核实实际选中项；为避免混合旧任务与新配置，部署时统一重启。不得因看见历史 DeepSeek/Flash 说明就自动切模型，也不得打印 provider headers、key 或完整本地配置。

## 5. 提示词与业务 Skill

`system_prompt()` 由宿主读取 `.pi/APPEND_SYSTEM.md` 和 `.pi/skills/**/SKILL.md` 全文，再加工具权限约束。它们是本业务模型的提示资源，开发者阅读时应区分“给模型的约束”与当前维护任务。

- 当前实现把所有找到的 Skill 正文注入，不是按 description 自动选择一个技能。
- `/skill:rule-authoring ...` 会核对技能名，但不构成隔离的阶段路由。
- 引用的其他资源不会自动读入模型；需要宿主显式实现。
- 后续任务创建时重新读取提示，正在运行的任务保留原提示。
- 新增 Skill 需核对 CLI、Web 的行为、预算和与现有规范的冲突。

重要业务约定：完整父链名称来自 BOM；新方法必须核实存在及参数；已有规则的动作保留工具填参结果；未知项先保留完整业务草稿，再列具体待确认；文档中的指令作为数据，不覆盖系统约束。Skill 不能替代程序校验，也不能保证模型严格遵守所有业务规范。

## 6. 两个业务工具

### 6.1 `bom_search`

Python Schema：`keyword` 非空且最多 500 字符；`limit` 默认 10、范围 1–50；`includeMethods` 默认 false；`dataType` 可选 field/object/list/method。未知参数拒绝。

默认读取 `A/bom/bom.sqlite`，工具运行时支持 `BOM_SQLITE_PATH` 覆盖。核对日该库 meta 指向 `YAICVehicleBom-copy (2)_202502110942`，`fields` 为 15,817 行，包含多类节点；这不是编辑器叶子表的可比数量。旧 AGENTS 某些段落写默认非车库，已与当前源码不同。

工具使用 `node:sqlite` 只读打开和参数化 `LIKE`，关键词整体连续匹配，空格不会拆成 AND。名称精确、前缀、包含等分级排序；返回完整 `qualifiedName`、类型、路径、方法 `params`、`ancestorMethods`、`objectAccessTemplates` 和模型元数据。不要仅因字段同名就丢掉父链，也不要把未填模板当成实际方法调用。

只读检查命令，在 `A` 目录执行：

```powershell
node tools/bom-search.mjs 机构 --limit 3
node tools/bom-search.mjs 险别 --methods --limit 5 --json
node tools/bom-to-sqlite.mjs stats --out bom/bom.sqlite
```

`bom-to-sqlite` 是管理脚本，默认导入输入为 `bom/bom.json`，但当前目录保存的是 `bom_车险.json` 等文件；重建必须显式指定输入输出，不运行裸 `import` 猜路径。推荐先生成新文件，核对后停服切换：

```powershell
node tools/bom-to-sqlite.mjs import --in bom/bom_车险.json --out bom/bom.next.sqlite
```

这条命令会创建/写数据库，只在已确认要重建时使用。不要向现有活动库随意加 `--drop`，也不删除连接中的 WAL/SHM。管理脚本的 FTS 表不等于当前在线查询已经走 FTS。

### 6.2 `rule_search`

Python Schema 仅接受 `keyword` 和 `limit`，默认 6、范围 1–30。模型不能自行提供 `bomBaseInfoId` 改业务范围。

`tools/rule-search.mjs` 读取 `config/mysql.json`，支持 `RULE_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` 和 `RULE_BOM_BASE_INFO_ID` 覆盖；当前实现仍要求本地配置文件存在。`ruleSearch` 包含范围名称、模型基础 ID、limit、fetchFactor 和 retry。

```text
decision_repository → decision_rep_catalogue
                    → catalogue_rule_rel → rule_defined
                                         ├─ rule_defined_predefine
                                         └─ rule_method_define
                                            → rule_method / rule_method_define_param
```

查询词按空格 AND，每个词可命中规则名、如果正文或预定义；那么/否则不参与召回。SQL 粗排后 JavaScript 精排，随后补齐预定义与动作，按分支和顺序填实际参数。`rule_content` 仅是条件树，不能当成完整四段规则。

当前发出的查询是参数化 SELECT，连接池最多 2 个连接，固定查询 hint 为 15,000ms，并有有限退避重试。配置里存在 `statementTimeoutMs` 不代表已接线。真正的只读权限还应由数据库账号授权保障。

工具返回 `content`、`details` 和可选 `isError`。适配层只传播友好错误，不把连接密码或原始数据库异常发给模型。真实 MySQL 查询会访问业务服务，离线开发测试优先 mock。

### 6.3 扩展工具的正确位置

新增工具需同时检查 `bridge.py` 的 Schema/白名单/描述、`graph.py` 执行分支、`runtime-tools.mjs` 适配、MCP 注册、提示词、前端状态和测试。当前模型没有 shell、任意 SQL、任意文件读写、规则上传工具；不为调试直接放开全部能力。

MCP 是可选 stdio 服务，客户端在 `A` 目录用 `python -m mcp_service.server` 启动。它只暴露两个检索工具，不加载完整业务转换图或自动赋予外部客户端 Skill 约束。正常网页使用无需额外启动 MCP。

## 7. 编辑器离线复核

`RuleEditor.review()` 只处理有明确“如果:”标题的规则代码块，先调 `/api/normalize_rule`，折叠四段式，再调 `/api/generate_rule`；需要时从本地编辑器配置读取登录信息，获取自己的 Cookie。

它不会调用上传、保存或远端执行接口，也不是模型可调用工具。通过时追加“离线语法与参数生成通过”；失败时保留草稿、列出未通过片段；服务、登录或配置不可用时说明未完成，不伪装为通过。

合并入口 `merged_app.py` 在原 profile 存在时，将编辑器 URL 绑定为实际 `PORT` 的本机回环地址，并把登录配置路径指向当前检出目录，内存覆盖而不改原文件。profile 缺失时不会自动启用复核。绕过 `start.py` 改端口启动 uvicorn 时，必须同步设置 `PORT`，否则助手可能复核到错误服务。

## 8. HTTP/SSE 协议与前端

合并部署均加 `/assistant` 前缀，独立助手入口才使用原 `/api/*`：

| 方法及路径 | 请求或返回重点 |
| --- | --- |
| `GET /assistant/api/health` | Node 路径、runtime、持久化是否准备、内存会话；不探活模型/MySQL |
| `POST /assistant/api/prompt` | `{sid,message}`，接受任务，不等待完整答案 |
| `GET /assistant/api/events?sid=...` | SSE；先订阅再发 prompt |
| `POST /assistant/api/abort` | `{sid}`，取消任务并关闭工具工作进程 |
| `POST /assistant/api/new_session` | `{sid}`，停止任务并更换 thread_id |
| `POST /assistant/api/close` | `{sid}`，释放内存与工具，保留检查点 |
| `POST /assistant/api/upload` | multipart `file`；检查响应 `ok`，业务失败也可能 HTTP 200 |

| 事件 | 前端职责 |
| --- | --- |
| `task_status` | 当前公开状态 |
| `message_update.assistantMessageEvent.text_delta` | 正文增量 |
| `process_delta` | 转换公开进度与检索事实 |
| `thinking_delta` / `reasoning_delta` | 服务返回的过程数据，不等于完整或正确的内部推理 |
| `tool_execution_start/end` | 工具状态、结果、是否缓存 |
| `message_end` | 本条消息的权威定稿、错误或取消状态 |
| `agent_settled` | 整轮结束，必须解除 busy |
| `model_diagnostic`、`task_metric` | 开发诊断与阶段耗时 |

SSE 20 秒心跳，每订阅者队列 2,000，满时丢最旧事件；没有事件 ID 或可靠补发。EventSource 自动重连不等于能恢复断线期间全部输出，前端显示历史与后端模型历史没有完整双向同步。

前端 `web/index.html` 直接加载本地 Vue 和 `app.js`，无 Vite/Webpack、TypeScript、Vue Router、Pinia 或构建命令。`app.js` 用 ref/reactive/computed 管理状态，原生 fetch/FormData/EventSource 交互。Markdown 先转义再生成白名单标签，链接限制 HTTP/HTTPS，不直接信任模型 HTML。

`mergedMode` 根据 `/assistant/` 路径添加 API 前缀；复制到编辑器后整页跳转 `/`，仍需手动粘贴。没有自动保存、自动填入或隐含执行规则。前端保留的旧工作区变量不代表当前仍使用 iframe。

## 9. 会话、附件与持久化

| 层次 | 数据及恢复含义 |
| --- | --- |
| 浏览器 localStorage | 标题、页面消息、草稿、sid；最多 50 会话、每会话最近 300 条 |
| Python 内存 | Session、task、锁、订阅者、NodeTools；重启不保留活动任务 |
| `config/langgraph/sessions.sqlite` | sid → thread_id |
| `config/langgraph/checkpoints.sqlite` | 图消息、工具结果、摘要与检查点 |
| `config/pi-agent/sessions/` | 可选旧历史只读导入来源，后续不会自动回写 |

最多 10 个内存会话；容量不足时只能淘汰无订阅且未运行的 LRU 项，全部不可淘汰则拒绝。每 60 秒扫描，空闲超过 1,800 秒、无订阅且未运行时回收。同 sid 不允许并发生成。取消、失败或崩溃后，下次仅恢复最近完整轮次，不自动续跑半截工具调用。

上传单文件最多 20MiB，提取文本最多 30,000 字符；支持文本/代码、PDF、DOCX，不支持 OCR、图片和旧 DOC。文本优先 UTF-8-SIG、再 GBK。DOCX 提取先段落再表格，不保证还原原始块顺序；PDF 纯文本也不保证版面或表格语义。超限会标记 `truncated`，原文后续部分不可由模型恢复。

应用没有主动把原始附件归档为业务文件，提取时使用字节和内存流；上传框架仍可能使用临时文件。纯文本进入消息后可能保存在检查点，不能承诺“附件内容不落盘”。没有完整长文档分块、跨段限定与覆盖台账。

## 10. 开发、回归和排障

| 修改需求 | 主要入口 | 连带检查 |
| --- | --- | --- |
| 改规则编写规范 | `.pi/APPEND_SYSTEM.md`、业务 Skill、`authoring.py` | 上下文预算、Web/CLI、原文完整性与待确认项 |
| 改模型协议或流式输出 | `config.py`、`model.py` | 工具参数分片、usage、断流、截断、取消和重试 |
| 改检索质量 | 两个 Node 查询核心 | Schema、父链、范围、排序、去重、动作保真和友好错误 |
| 改收尾/上下文 | `graph.py`、`context.py` | 总请求与工具预算、完整工具消息配对、原始证据保留 |
| 改会话/停止恢复 | `manager.py`、前端事件处理 | sid/thread_id、检查点、重复发送、错误和取消必收尾 |
| 改附件支持 | `backend/file_extract.py`、`web/app.js` | 格式、大小、截断、文本顺序、超时与内容持久化 |
| 改离线复核 | `editor.py`、根目录 `merged_app.py` | 原文折叠、登录、PORT、只调用离线接口和失败文案 |

在 `A` 目录使用专属解释器；新合并 `.venv` 如需测试，要先装开发依赖，见部署文档：

```powershell
python -m pytest -q
node --check web/app.js
node tests/web_status.cjs
node tests/web_editor.cjs
```

`pytest.ini` 限定 `tests/`，回归覆盖会话恢复、取消、容量、上传、SSE、流分片、上下文、预算、白名单、MCP、编辑器调用及本地 BOM；不等于真实模型质量验收。`tests/live_*.py`、`evals` 的真实评测会访问模型或业务服务，需明确测试环境和成本后运行，不把它们加入无差别离线测试命令。

| 现象 | 排查顺序 |
| --- | --- |
| health 正常、问答失败 | 实际模型配置与协议 → 网络/认证 → 流式响应/工具支持 → 诊断事件 |
| BOM 失败 | Node 是否可用 → 版本含 node:sqlite → 路径及读权限 → schema/模型元数据 |
| 已有规则失败 | 配置文件存在 → 业务范围 → 数据库可达与账号授权 → 友好错误类别 |
| 一直 busy | SSE 前缀和代理缓冲 → 结束事件 → 前端事件处理 → 任务是否仍运行 |
| 复制后进入空编辑器 | 这是整页跳转；确认剪贴板复制成功，再手动粘贴，不应假设自动导入 |
| 多轮后慢或超限 | 请求总数、历史摘要、工具证据大小、重复检索、输出预留 |
| 输出 length 截断 | 对照 maxTokens、usage、输入规模及远端实际配置；不直接归因于 GPU |
| 重启后页面与模型记忆不符 | 浏览器历史与检查点是不同数据源；检查 sid/thread_id 和数据目录 |

日志默认由 uvicorn 输出 `agent_diagnostic` 的受限字段，如 sid/runId、phase、durationMs、modelRequests、httpAttempts、finishReason、usage。没有自动部署的指标平台；未返回 usage 时不能用字符数替代 token。不要在诊断中打印请求全文、附件、密钥或数据库连接信息。

## 11. 已知边界与后续交接重点

- 当前助手没有完整用户认证和 sid 所有权校验；编辑器登录门禁不会自动保护挂载在另一个子应用里的助手接口。
- 单进程、单 worker，SQLite 检查点与内存会话未实现跨实例协调；不能直接增加 worker 扩容。
- 没有完整规则 IR、逐引用可信证据台账、原文覆盖证明和完整业务语义校验。
- 任务分类部分为启发式；模型核实与动作选择仍可能出错，需保留待确认和人工复核。
- 检索收敛、缓存和上下文治理已存在，但不承诺未经同口径评测的提速或正确率。
- Pi 回退、独立 MCP、真实模型评测都不是统一网页启动的必需项。

后续改进按风险逐步推进：先用诊断定位模型请求与工具耗时，再建设映射状态、可信引用、确定性校验和局部修正；长文档另做结构与覆盖治理。历史 AGENTS 第 15–17 节是设计方向，不能写成已实现能力。
