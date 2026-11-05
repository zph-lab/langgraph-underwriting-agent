> 历史文档：以下为旧目录和旧入口记录，不是当前启动指南。当前使用方法见 [项目 README](../README.md)。

> 2026-10-23 迁移更新：业务默认 Web 入口 `server.main:app` 已切换为 LangGraph；新终端入口为 `run-agent.bat`。原 Pi 说明保留作历史和回退参考。当前运行说明、配置、持久化差异及验证命令以 `agent_1/airong_agent_test1/web/MIGRATION.md` 为准。Pi 源码未修改或删除。

# 核保规则助手 Web（FastAPI + Vue3，零构建）

把 pi CLI（`--mode rpc` 无头 JSONL 协议）桥成网页聊天：打字机流式输出、`bom_search / rule_search` 工具执行时显示“正在检索业务数据…”脉冲。

支持 📎 上传资料文件（txt/md/json/csv/代码、pdf、docx）：`/api/upload` 在内存提取纯文本（不落盘，单文件 ≤20MB，超 30000 字截断），发送时以 `【上传文件：…】` 块拼进消息供模型阅读。图片/扫描件/老版 .doc 不支持，前端会给出友好报错。

## 结构

```
web/
  server/pi_rpc.py   pi 子进程桥（每个浏览器会话一个常驻 node --mode rpc 进程）
  server/main.py     FastAPI：/api/prompt /api/abort /api/new_session /api/close /api/events(SSE) /api/health
  client/            Vue3 单页（vendor/vue.global.prod.js 本地化，不依赖 CDN）
  run-web.ps1        一键启动（含端口占用清理）
  requirements.txt
```

## 启动

```powershell
cd airong_agent_test1\web
powershell -NoProfile -ExecutionPolicy Bypass -File .\run-web.ps1
```

浏览器打开 <http://127.0.0.1:8712/> 。页面建立 SSE 时会拉起 pi 子进程，之后常驻，多轮上下文在该子进程内。启动脚本优先使用 `web/.venv/Scripts/python.exe`；上述执行策略只影响本次 PowerShell 进程。

源码首次启动若报缺少 `packages/ai/src/providers/data/*.json`，在 `pi-main` 运行 `npm run hydrate:model-data` 补齐模型目录数据，无需全量构建。

## 模型/参数

通过 `run-web.ps1` 启动时，从业务 `.pi/settings.json` 读取默认 provider/model。未设置 `PI_CODING_AGENT_DIR` 时，脚本把业务 `.pi/models.json` 复制到被 Git 忽略的 `config/pi-agent/models.json`，使用这个独立运行目录，并仅为当前业务项目记录信任，保证业务扩展与护栏加载。密钥不应提交到版本库。

可用环境变量覆盖模型选择；显式设置 `PI_CODING_AGENT_DIR` 时，应自行准备对应目录中的模型和项目信任配置。直接启动 Uvicorn 不执行脚本初始化，桥接器无环境变量时仍使用旧的 qwen-maas 默认值。

```powershell
$env:PI_EXTRA_ARGS = "--provider qwen-maas --model qwen3.8-flash"
```

## 口径备忘

- 子进程启动逐字复刻 `pi-test.ps1:62-64`，cwd 固定 `airong_agent_test1`（否则 `.pi/extensions` 业务工具与人设不加载）。
- 事件流实测：`message_update.assistantMessageEvent.text_delta` 增量；`message_end.message`（含 `stopReason/error`）权威定稿；`tool_execution_start/end{toolName,isError}`；`agent_settled` 收尾。
- 扩展对话框请求（select/confirm/input）后端自动回 `cancelled`，防止 agent 挂起。
## 历史会话（多会话）

- 左侧列表管理会话：每个会话独立 `sid`（crypto.randomUUID）+ 独立 pi 子进程；“＋ 新会话”开新 sid，旧会话切回可继续多轮对话。
- 消息/草稿/标题存 localStorage（`piweb.sessions_v1`，上限 50 会话 × 300 条），刷新、关页、切换都不丢；删除会话走 `/api/close` 释放子进程。
- 子进程以 `--session-id <sid>` 启动：空闲 30 分钟回收或进程意外退出后，同 sid 重开会话从磁盘 jsonl 恢复模型上下文。默认脚本使用 `config/pi-agent/sessions/`；显式配置运行目录时使用该目录下的 sessions，另设会话目录时以该配置为准。旧全局会话不会自动迁移。
- 生成中切走会自动 `/api/abort` 该会话本轮生成；切换后旧会话迟到的 SSE 事件按流实例丢弃。
- 常驻子进程上限 `MAX_BRIDGES = 10`，超出按最近未活动时间 LRU 回收（有在线订阅者的不回收）。


//    "bomBaseInfoId": "f8e7be25e1d6475a83c232d544cc36bb",
// "bomBaseInfoName": "车险",
"bomBaseInfoId": "dfd0188c74e34e75be847d43a65f7ce1",
//  "bomBaseInfoName": "非车",
