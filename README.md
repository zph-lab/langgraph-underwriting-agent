# LangGraph Underwriting Agent

基于 LangGraph 的核保规则开发智能体，支持工具调用、流式响应、会话持久化，并集成规则编辑器。

**从 GitHub 克隆后，请先阅读 [分发版配置说明](GITHUB_SETUP.md)。仓库不包含真实凭据、业务数据库、BOM 或本地依赖。**

# 核保工作台（单端口合并版）

## 开发与交接文档（2026-11-03 核对）

- [文档编辑器开发文档](docs/01_文档编辑器开发文档.md)：架构、接口、BOM/注册表、语法维护、验证与排障。
- [Agent 业务助手开发文档](docs/02_Agent业务助手开发文档.md)：LangGraph、模型/Skill、只读工具、SSE、会话与扩展入口。
- [项目启动部署文档](docs/03_项目启动部署文档.md)：本机和新电脑安装、配置、验收、服务托管、代理、备份回退。

接手时先读启动部署文档，再按业务模块阅读开发文档。旧模块说明中的双端口、iframe、默认模型及测试数量可能属于历史记录；本套文档按当前代码说明现状，并区分已实现功能与部署示例。

编辑器与规则 AI 助手已整合为一个项目入口、一个 Python 服务、一个 HTTP 端口。原有两个目录现在作为业务模块保留，数据库、模型、提示词、上传流程及已有登录配置均沿用；无需分别启动原服务。

## 在当前电脑启动

在本目录打开 PowerShell，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

浏览器打开 **http://127.0.0.1:8009/**。右上角导航按钮可切换 `规则AI助手` 和 `adp规则编辑器`，分别整页跳转到 `/assistant/` 和 `/`。助手完整显示自身顶栏、会话侧栏和聊天区域；编辑器占满顶栏下方空间。切换会重新加载页面，草稿和历史会话沿用各功能原有浏览器存储恢复策略，未发送附件不跨页面保留。助手的“复制并到编辑器验证”会跳转编辑器，仍需手动粘贴。

终端保持打开，按 `Ctrl+C` 停止。无需再启动 8712。若 8009 已被旧服务占用，请停止对应旧服务，或指定其他端口：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Port 8010
```

此时访问 http://127.0.0.1:8010/。助手离线校验也会使用该端口。

脚本优先使用根目录 `.venv`；不存在时复用已附带的助手 `.venv-langgraph` 环境。本次已为该环境补装编辑器需要的 jieba。

## 换电脑或重新安装环境

推荐 Python 3.12，另需安装 Node.js（助手的 BOM/规则检索仍依赖 Node）。复制整个项目目录，包括原有数据、模型和本机配置；虚拟环境不可跨电脑直接复用。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

助手现有 Node 依赖位于 `ar_agent开发/agent_1/airong_agent_test1/node_modules`；如未复制依赖，在该业务目录执行 `npm ci`。已有 SQLite/BOM 数据时不需要重新运行 ETL。

## 结构和保留行为

- `start.py`、`start.ps1`：统一启动；默认仅监听本机 8009，可通过 `-BindHost 0.0.0.0` 指定局域网监听。
- `merged_app.py`：组合两套 ASGI 应用，显式管理助手启动、检查点和退出清理。
- `/`、`/api/*`、`/static/*`：原编辑器页面和接口。
- `/assistant/`、`/assistant/api/*`：助手页面、聊天、SSE、附件和会话接口；与编辑器同源。
- 编辑器继续使用 `rule_editor_new/rule_editor/config.yaml` 中的登录和 ADP 配置。账号、密码与 token 未修改。
- 助手继续使用原 `.pi`、`config/mysql.json`、BOM 和检查点。统一运行时仅将编辑器校验 URL 和配置路径绑定到当前项目，不修改原配置文件。模型和外部数据库服务仍需可达。

使用统一入口运行，保持单 worker；进程内登录会话不适合直接扩成多个 worker。编辑器原有重启时草稿恢复策略保持不变；整页跳转后不能承诺全部临时状态可恢复。

## 验证

合并集成检查（不调用模型、不向 ADP 保存规则）：

```powershell
& '.\ar_agent开发\agent_1\airong_agent_test1\.venv-langgraph\Scripts\python.exe' -m unittest discover -s tests -v
```

检查页面和资源、子应用生命周期、原登录门禁、带 Cookie 的候选搜索、标注、离线生成、助手附件接口和配置路径。`node tests/navigation.cjs` 检查顶部原生导航链接、整页跳转、旧 hash 地址兼容和接口前缀。

本次验证：助手 Python 53 项、编辑器 Python 32 项、合并集成 4 项、原前端状态/复制及新增导航检查均通过，依赖 `pip check` 通过；实际临时启动单一 18009 端口，两个页面和助手 SSE 连接验证通过，测试服务已关闭。未执行真实浏览器视觉验收。真实模型生成质量、外部数据库可达性及 ADP 写入须在对应环境另行验收；本次不执行真实规则保存。

两个业务目录中的历史说明包含原独立端口，合并部署以本说明为准。
