# 十一、Git 代码位置、部署环境配置及服务启动和开发指南

本节依据当前合并项目源码、Git 交付记录和交接文档整理，核对日期为 2026-11-04。项目将 adp 规则编辑器与规则 AI 助手整合为一个 Python 服务、一个 HTTP 端口。

## 11.1 Git 代码位置与目录说明

- Git 仓库：<https://git.airongkeji.com/ai-lab/genesis-lora-v0.git>
- 分支：`master`。
- 合并项目目录：仓库内的 `rule-editor-agent-merge/`。仓库中另有历史业务目录，启动本项目须进入该合并目录。
- 本地交付仓库记录的提交：`5d8058325cfe626a3068b1289bd35bba1e194e4f`，提交说明为 `Add merged rule editor and agent project with handover docs`。此为本地记录，未在线核验远端最新提交。

```powershell
git clone --branch master https://git.airongkeji.com/ai-lab/genesis-lora-v0.git
cd genesis-lora-v0/rule-editor-agent-merge
git rev-parse HEAD
```

主要目录如下，后续命令均以合并项目目录为起点：

| 路径 | 用途 |
| --- | --- |
| `start.py`、`start.ps1` | 统一启动入口 |
| `merged_app.py` | 挂载两个 FastAPI 应用，管理启动和退出生命周期 |
| `requirements.txt` | 汇总两个业务模块的 Python 依赖 |
| `rule_editor_new/` | 编辑器源码、配置、BOM、方法注册表及索引 |
| `ar_agent开发/agent_1/airong_agent_test1/` | 当前 AI 助手业务代码，下文简称“助手目录” |
| `ar_agent开发/agent_1/pi-main/` | 保留的 Pi 底座，当前 LangGraph 主链路无需构建或启动 |
| `docs/`、`tests/` | 交接文档、合并集成与导航检查 |

Git 交付已排除虚拟环境、`node_modules`、日志、缓存、历史备份、运行会话及私有评测记录。助手的 `config/` 和 `.pi/models.json` 需要由部署人员本机创建或受控迁移；编辑器配置中的登录和密钥字段已置空，启动前填写。业务 BOM、索引及配套数据随项目保留。具体见项目 `UPLOAD_NOTES.md`，不能将本机已有环境等同于新克隆代码可以直接运行。

## 11.2 部署环境与依赖

| 项目 | 要求或用途 |
| --- | --- |
| Python | 推荐 3.12；使用项目独立虚拟环境 |
| Node.js | 至少 22.5，需支持内置 `node:sqlite`；用于 BOM 和已有规则检索 |
| Python 服务 | FastAPI + Uvicorn；助手使用 LangGraph |
| 前端 | 静态 HTML、JavaScript、CSS；助手使用本地 Vue 3，无需前端构建 |
| SQLite | 编辑器索引、助手 BOM 与会话持久化使用本地文件，无需独立数据库服务 |
| 模型服务 | 支持当前 OpenAI 兼容 Chat Completions、流式输出及函数工具调用 |
| MySQL | 用于已有规则只读检索，需提供业务库访问权限及正确查询范围 |
| ADP | 真实上传和远端测试时需要；离线规则生成不要求连接 ADP |

当前默认仅监听 `127.0.0.1:8009`。页面、模型及数据库健康需分别验收，首页可打开不代表外部服务可用。新电脑应重建环境，不直接复用复制来的虚拟环境。

## 11.3 配置文件与数据准备

| 配置或数据 | 位置与配置重点 |
| --- | --- |
| 编辑器配置 | `rule_editor_new/rule_editor/config.yaml`：`data/etl` 数据路径、`login` 本地登录、`upload` ADP 地址/token/关联 ID |
| 助手模型选择 | 助手目录 `.pi/settings.json`：默认 provider/model |
| 助手模型连接 | 助手目录 `.pi/models.json`：provider、服务地址、密钥、协议和模型元数据 |
| 助手提示和技能 | 助手目录 `.pi/APPEND_SYSTEM.md`、`.pi/skills/` |
| MySQL 与检索范围 | 助手目录 `config/mysql.json`：数据库连接及 `ruleSearch` 业务范围 |
| 编辑器复核 | 助手目录 `config/rule-editor.json`：离线复核 profile |
| 编辑器业务数据 | `rule_editor_new/新车险915/`、`data/bom_catalog.sqlite`、`desc_to_rulecontent/bom_methods_新车险915.json` |
| 助手 BOM | 助手目录 `bom/bom.sqlite` 及对应源 JSON |
| 助手运行历史 | 助手目录 `config/langgraph/`，运行账号需有写权限 |

BOM、索引、方法注册表与业务范围须成套匹配。已有完整数据时无需重新 ETL；切换业务模型时，编辑器和助手的数据及 MySQL 查询范围要分别核对。账号、密钥、token 通过受控渠道交接，不填入共享说明或提交到 Git。

常用环境覆盖变量：

- 模型：`AGENT_MODELS_FILE`、`AGENT_PROVIDER`、`AGENT_MODEL`、`AGENT_BASE_URL`、`AGENT_API_KEY`。
- 工具：`AGENT_NODE`、`BOM_SQLITE_PATH`。
- MySQL：`RULE_MYSQL_HOST`、`RULE_MYSQL_PORT`、`RULE_MYSQL_USER`、`RULE_MYSQL_PASSWORD`、`RULE_MYSQL_DATABASE`、`RULE_BOM_BASE_INFO_ID`。连接配置文件仍需存在。
- 运行预算：`AGENT_TASK_TIMEOUT_SECONDS`、`AGENT_MODEL_REQUEST_LIMIT`。

合并入口会将已配置的编辑器复核 profile 地址绑定到当前端口，并将登录配置路径绑定到本项目。更换端口使用统一启动参数，避免直接运行 Uvicorn 后 `PORT` 与实际端口不一致。

## 11.4 Windows 安装与服务启动

首次安装，在合并项目根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
Push-Location 'ar_agent开发/agent_1/airong_agent_test1'
npm ci --ignore-scripts
Pop-Location
```

补齐上一节配置后启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

访问地址：

- 编辑器：`http://127.0.0.1:8009/`。
- AI 助手：`http://127.0.0.1:8009/assistant/`，保留末尾斜杠。
- 健康接口：`/api/health`、`/assistant/api/health`。

终端保持打开，`Ctrl+C` 停止。启动脚本优先使用根目录 `.venv`，不存在时才尝试助手原 `.venv-langgraph`。Git 交付不带虚拟环境，新电脑按上述步骤创建即可。两页顶部导航整页切换，无需额外启动 8712。

更换端口或监听局域网：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Port 8010
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Port 8009 -BindHost 0.0.0.0
```

以上为两种启动方式，按需选一种运行。局域网客户端访问服务器实际 IP。对其他用户开放时，需配置组织认可的入口认证、HTTPS 和网络权限；编辑器登录不覆盖助手 API，当前助手尚无完整多用户会话归属控制。

## 11.5 Linux 部署与长期运行

安装 Python 3.12、合适版本的 Node.js，并准备配置和数据后，从合并项目根目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix ar_agent开发/agent_1/airong_agent_test1 ci --ignore-scripts
.venv/bin/python start.py --host 127.0.0.1 --port 8009
```

Linux 迁移需替换本机配置中的 Windows 绝对路径。长期运行使用 systemd 或 Windows 服务管理工具固定项目路径、Python 解释器、Node 路径、启动参数与日志策略；具体示例见 `docs/03_项目启动部署文档.md`，Linux 示例尚未在当前 Windows 环境验收。

当前保持单 worker、单实例。编辑器登录会话、助手任务及 SSE 订阅包含进程内状态，不能直接增加 worker 扩容。Nginx 等反向代理须保留 `/assistant/` 路径、关闭 SSE 缓冲，并设置与模型任务期限匹配的超时。当前根目录没有完整合并版 Docker 配置；`rule_editor_new/` 内旧 Docker/Compose 仅部署编辑器。

## 11.6 启动验收与常见问题

另开 PowerShell 检查页面和健康接口：

```powershell
$baseUrl = 'http://127.0.0.1:8009'
(Invoke-WebRequest "$baseUrl/" -UseBasicParsing).StatusCode
(Invoke-WebRequest "$baseUrl/assistant/" -UseBasicParsing).StatusCode
Invoke-RestMethod "$baseUrl/api/health"
Invoke-RestMethod "$baseUrl/assistant/api/health"
```

两页应返回 HTTP 200；助手健康检查确认 `runtime=langgraph`、`persistenceReady=true`，并检查 Node 信息。随后在页面完成编辑器登录，检查候选、标注、离线生成；助手用简短问题验证模型流式答复、BOM 查询，再验证已有规则查询与编辑器离线复核。健康接口不会替代真实模型和 MySQL 验收。ADP 保存属于独立业务操作，须在确认的目标环境执行。

| 现象 | 排查重点 |
| --- | --- |
| 缺包或模块导入失败 | 从合并根目录启动，确认实际 Python 环境及依赖安装位置 |
| Node 或 `node:sqlite` 不可用 | Node 版本、服务进程 PATH、`AGENT_NODE` |
| 端口占用 | 核对对应实例后停止旧服务，或指定新端口 |
| 登录失败或 401 | 本机配置是否补齐、重启后 Cookie 是否失效、请求是否携带 Cookie |
| 页面正常但问答/检索失败 | 分别检查模型地址和协议、MySQL 连接、BOM 文件和业务范围 |
| SSE 无输出或持续忙碌 | `/assistant/` 前缀、代理缓冲、连接状态、任务结束事件 |
| 助手离线复核不可用 | profile、PORT、编辑器登录配置与本地回环连接 |

## 11.7 开发入口与回归验证

建议先读根目录 `README.md` 和 `merged_app.py`，再按修改范围阅读下列文件：

| 开发内容 | 主要入口 |
| --- | --- |
| 编辑器 API 与业务编排 | `rule_editor_new/rule_editor/api/routes.py`、`service.py` |
| 编辑器候选、标注和语法 | `rule_editor_new/rule_editor/core/textsearch.py` |
| 中文规则转换 | `rule_editor_new/desc_to_rulecontent/rule_desc_to_content.py`、`parameterized_paths.py`、`selector_values.py` |
| ADP 载荷与上传 | `rule_editor_new/desc_to_rulecontent/write_rule_to_adp.py` |
| 编辑器前端 | `rule_editor_new/rule_editor/web/` |
| 助手 HTTP、SSE 与附件 | 助手目录 `backend/main.py`、`backend/file_extract.py` |
| 助手模型、图与会话 | 助手目录 `langgraph_app/config.py`、`model.py`、`graph.py`、`manager.py` |
| 助手检索工具 | 助手目录 `tools/bridge.py`、`bom-search.mjs`、`rule-search.mjs` |
| 助手前端 | 助手目录 `web/index.html`、`web/app.js`、`web/styles.css` |

静态前端修改后刷新页面，缓存未更新时强制刷新；修改 Python、主要配置或 BOM 后重启统一服务。编辑器当前主要是确定性规则转换；助手当前主运行层是 LangGraph，`.pi` 目录保留配置、提示与技能，不代表需要启动 Pi Agent。

常用离线回归命令，假设已建立根 `.venv`：

```powershell
.\.venv\Scripts\python.exe -m pip install -r 'ar_agent开发/agent_1/airong_agent_test1/requirements-dev.txt'
$projectRoot = (Get-Location).Path
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe'
& $pythonPath -m unittest discover -s tests -v
node tests/navigation.cjs

Push-Location 'rule_editor_new'
& $pythonPath -B -m unittest rule_test.test_parameterized_paths rule_test.test_selector_values rule_test.test_rule_evaluator -v
Pop-Location

Push-Location 'ar_agent开发/agent_1/airong_agent_test1'
& $pythonPath -m pytest -q
node --check web/app.js
node tests/web_status.cjs
node tests/web_editor.cjs
Pop-Location
```

测试需具备配套 BOM、Node 和可写临时目录。离线通过不等于真实模型语义、数据库或 ADP 发布验收通过。当前布尔方法语法误报仍有已知问题，不能沿用已回退改动的修复结论。本次仅整理说明，未重新运行上述业务回归，也未部署服务。

完整开发资料：`docs/01_文档编辑器开发文档.md`、`docs/02_Agent业务助手开发文档.md`、`docs/03_项目启动部署文档.md`。升级前记录 Git 提交、依赖与配置版本，并备份成套 BOM、索引、方法注册表和所需会话数据；SQLite 宜停服后备份，回退时恢复配套代码与数据。
