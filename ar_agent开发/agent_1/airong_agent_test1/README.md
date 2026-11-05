# 核保规则助手

日志与转换过程查看：[运行日志及长度截断排查](docs/LOGGING.md)。转换中实际返回的思考和未校验草稿进入默认折叠的处理过程，失败后保留；终端新增不含业务正文的请求诊断，更新后需重启加载。

2026-10-27转换优化及实测：[上下文、耗时对比报告](docs/OPTIMIZATION_2026-10-27.md)。模型输入精简重复工具证据，原始结果保留；转换重复检索进入完整草稿收尾。默认整项任务600秒、最多16次模型generate调用（含摘要和修正，HTTP重试另计），可用正整数环境变量 `AGENT_TASK_TIMEOUT_SECONDS`、`AGENT_MODEL_REQUEST_LIMIT` 调整。18份历史证据经服务tokenizer实测减少33.23%输入token，此比例不是整项转换耗时提升。

项目使用 Python LangGraph 运行智能体，FastAPI 提供接口，Vue 页面负责交互。阅读代码建议从下面的目录开始。

```text
airong_agent_test1/
├─ web/                       前端：index.html、app.js、styles.css、vendor/
├─ backend/                   后端：HTTP API、SSE、附件提取和静态文件托管
├─ langgraph_app/             智能体：图、模型适配、会话管理、提示词/技能加载、CLI
├─ mcp_service/               MCP 服务：对外暴露只读业务工具
├─ tools/                     共用工具：Python 桥接、Node 检索核心和数据管理脚本
├─ tests/                     自动化回归及按需真实服务验证
├─ docs/                      迁移说明、设计文档和历史技术文档
├─ config/                    本地配置与会话数据（不入库）
│  ├─ mysql.json              原有 MySQL 只读检索配置
│  ├─ langgraph/              应用会话和 LangGraph 检查点
│  └─ pi-agent/               原 Pi 配置与历史
├─ .pi/                       继续复用的模型配置、业务提示词和 Skills；Pi 回退资源
├─ bom/                       BOM JSON、SQLite 和运算符参考
├─ data/                      业务数据存档
├─ legacy/pi/                 Pi 后端回退代码与旧 Python 环境
├─ .venv-langgraph/           当前 Python 3.12 专属环境（不入库）
├─ project_paths.py           统一的项目路径定义
├─ requirements*.txt          Python 依赖与锁定版本
├─ run-web.ps1                网页启动入口
├─ run-agent.bat              LangGraph 终端入口
├─ run-mcp.bat                MCP stdio 服务入口
├─ run-web-pi.ps1             Pi 网页回退入口
└─ run-pi.bat                 Pi 终端回退入口
```

使用 `langgraph_app` 和 `mcp_service` 而非 `langgraph`、`mcp` 作为 Python 包名，避免与第三方依赖同名。Python 工具桥接放在 `tools/bridge.py`，供 LangGraph 与 MCP 共用，两个模块之间不相互调用。

## 直接启动

这台机器的环境已整理到项目根目录 `.venv-langgraph`，无需重新安装，也无需激活 Conda。在 PowerShell 中执行：

```powershell
cd "C:\Users\admin\Desktop\ar_agent开发\agent_1\airong_agent_test1"
powershell -ExecutionPolicy Bypass -File .\run-web.ps1
```

打开 [http://127.0.0.1:8712](http://127.0.0.1:8712)。保持窗口打开，按 `Ctrl+C` 停止。端口占用时脚本报错，不结束其他进程；可先设置 `$env:PORT = "8713"` 再运行。

终端模式：运行 `run-agent.bat [sid]`，`/new` 新建上下文，`/exit` 退出。MCP 入口是 `run-mcp.bat`，通常由 MCP 客户端启动；正常使用网页不必另起 MCP 服务。

如需手动激活，CMD 使用 `.venv-langgraph\Scripts\activate.bat`，PowerShell 使用 `.\.venv-langgraph\Scripts\Activate.ps1`。环境不由 Conda 管理，不会出现在 `conda env list` 中。

## 新机器安装

准备 Python 3.12 和 Node >=22.19，在项目根目录运行：

```powershell
py -3.12 -m venv .venv-langgraph
.\.venv-langgraph\Scripts\python.exe -m pip install -r requirements.lock.txt
npm ci --ignore-scripts
```

准备本地 `.pi/models.json`、`.pi/settings.json`、`config/mysql.json` 和已有 BOM SQLite。凭据不得提交。模型仍使用原默认配置，可通过 `AGENT_*` 环境变量覆盖，详见 [迁移说明](docs/MIGRATION.md)。不需要启动或安装 Pi 底座依赖即可运行新入口。

## 模块调用关系

```text
浏览器 → backend.main → langgraph_app.manager → LangGraph → 模型
                                            → tools.bridge → 原 Node 检索核心
MCP 客户端 → mcp_service.server → tools.bridge → 原 Node 检索核心
```

前端只消费 HTTP/SSE，不包含模型密钥或数据库配置。`backend` 保持现有 API 和事件格式；`langgraph_app` 不负责托管页面；MCP 与 LangGraph 共用检索核心，避免复制 SQL。

## 验证

```powershell
.\.venv-langgraph\Scripts\python.exe -m pytest -q
.\.venv-langgraph\Scripts\python.exe -m pip check
```

回归测试使用模拟模型和本地 BOM，不连接真实模型或 MySQL。按需真实只读验证：`python -m tests.live_smoke`，该命令会访问当前配置的私有模型和业务数据库。

目录整理后，10 项回归测试已通过。历史配置、BOM、业务数据和会话数据库位置保持不变；尚未做本轮浏览器人工验收或重新调用真实模型。原 Pi 源码保留在相邻的 `../pi-main`，回退说明见 [legacy/pi/README.md](legacy/pi/README.md)。

## 业务评测

已从现有范围MySQL和本地BOM构建32条待业务审核的首版案例，包含只读数据快照和审核表；建集时未运行模型评测；本轮针对性评测见文末。文件、覆盖限制、各功能核心指标及性能测量方法见 [evals/README.md](evals/README.md)。真实样本保存在被Git忽略的 `evals/private/`，不默认上传。

## 2026-10-26 交互与文档规则修正

发送后持续显示任务状态，覆盖上下文准备、模型等待、检索、生成以及异常收尾；页面标识为 LangGraph。文档“提取/生成规则”请求进入原 `rule-authoring` Skill 的转写流程，要求 BOM/相关规则参考、独立代码块的 `如果:`/`那么:` 和来源；缺少映射依据列待确认。文档草稿经交付检查后显示，可能增加首正文等待时间，状态提示不计入正文性能指标。检查只覆盖格式及成功工具调用存在性，不是字段级语义校验或规则编译器。

验证：`python -m pytest -q`、`node tests/web_status.cjs`。真实针对性评测：`python -m evals.run_authoring`，6 个现有案例各重复 3 次，限每次 300 秒，隔离存储；输出在 `evals/private/authoring-*`，不含密钥、不上传。运行器测会话事件耗时，不包含 HTTP/SSE 传输；语义指标须根据原始输出与标注单独核对。

本轮实际结果：14 项回归通过；18 次真实评测 7 次完成、11 次超时。完整质量、性能、证据缺失及测量限制见[验证报告](docs/VALIDATION_2026-10-26.md)，不能据此宣布业务验收通过。

## 2026-10-27 展示与检索治理

一次提问中的工具前过渡文字及模型思考合并在同一个助手框的“处理过程”，默认折叠、可展开滚动查看；最终答复保留在正文。支持无增量、只有定稿的事件。已保存的历史气泡不自动改写。

LangGraph 每次任务独立缓存参数校验后完全相同的成功检索，失败不缓存，新一轮不复用；现有12轮模型限制之外，每任务最多处理48次工具请求（包括缓存命中），达到上限明确报错并结束。此预算是工程默认值，不是业务验收门槛。

新增 `task_metric` 事件区分 `model`、`tool`、`summary`、`delivery_repair` 耗时；工具完成事件带 `cached` 和 `durationMs`。指标不携带查询参数或凭据，通过现有事件订阅消费，未新增永久指标存储，也不代表已定位模型服务内部瓶颈。

验证：16项本地回归、前端事件生命周期检查和JS语法检查通过。未重做真实业务质量/性能评测、实机浏览器全流程，未重启现有服务。字段级可信证据登记、结构化规则校验、长文档覆盖台账及语义复核仍待实现，不能宣称本次已解决全部文档正确性或超时问题。

### 规则全文交付修正

已有规则查询在 `rule_search` 返回后，由宿主直接展示工具已还原的完整四段式文本及所有动作，逐条标注规则库来源、名称和原有编号，不再经模型改写为参数表。重复编号去重；仅报告实际展示条数，有截断时明确范围有限，不把候选数当全库总数。检索失败保留工具友好错误。此路径不会自动扩大检索范围或取回全部候选。

规则转换同时覆盖文档和直接粘贴的规则。交付检查不再接受仅有“待确认”的清单：先交付完整草稿，再在末尾列待确认项。未核实部分保留用户中文业务原文，不假造BOM字段/动作，也不宣称草稿已可执行。检查仍是格式及工具调用层面的有限检查，不是逐条件语义保真或证据编译器。

验证更新：19项回归通过，新增已有规则全文及多动作/否则保留、截断说明、去重、绕过模型概括、直接粘贴转换及拒绝仅待确认清单检查。未调用真实模型/MySQL，未重启现有服务。后端与提示词更新需要重启项目后加载。
