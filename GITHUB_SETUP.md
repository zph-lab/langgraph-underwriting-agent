# GitHub 分发版配置

此仓库包含 LangGraph 智能体、只读检索工具、Web 流式交互及规则编辑器源码。真实凭据、业务 BOM、数据库、会话检查点、依赖目录和历史备份不随源码分发；克隆后需配置并准备业务数据才能运行。

## 初始化

1. 安装 Python 3.12 和 Node.js，在根目录创建 `.venv` 并执行 `pip install -r requirements.txt`。
2. 在 `ar_agent开发/agent_1/airong_agent_test1` 中执行 `npm ci`。
3. 将 `config-examples/models.example.json` 复制到助手目录的 `.pi/models.json`；设置实际服务地址与模型 ID，并使其与 `.pi/settings.json` 一致。通过 `AGENT_API_KEY` 环境变量设置密钥。
4. 创建助手目录的 `config` 文件夹，将 `mysql.example.json` 和 `rule-editor.example.json` 分别复制为 `config/mysql.json` 和 `config/rule-editor.json`，填写自己的连接配置。合并入口会绑定编辑器 URL 和登录配置路径。
5. 将 `editor.config.example.yaml` 复制为 `rule_editor_new/rule_editor/config.yaml`，配置本地登录账号、密码、BOM 和数据库路径。示例登录值为空，必须填写后使用。
6. 准备有权使用的 BOM 和数据库。按 `docs/01_文档编辑器开发文档.md`、`docs/02_Agent业务助手开发文档.md` 与 `rule_editor_new/数据更换指南.md` 配置或生成配套数据。示例中的路径需根据实际数据调整。
7. 回到根目录执行 `powershell -ExecutionPolicy Bypass -File .\start.ps1`，打开 http://127.0.0.1:8009/。

请勿提交填写后的配置、业务数据或真实测试报文。本地配置已加入 `.gitignore`。历史文档中“复制整个目录”及已完成验收的说明属于原开发环境，不能视为全新克隆无需配置即可运行。

当前发布没有新增开源许可证；第三方资源继续遵循各自许可。
