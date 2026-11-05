> 历史文档：以下为旧目录和旧入口记录，不是当前启动指南。当前使用方法见 [项目 README](../README.md)。

> 2026-10-23 迁移更新：业务默认 Web 入口 `server.main:app` 已切换为 LangGraph；新终端入口为 `run-agent.bat`。原 Pi 说明保留作历史和回退参考。当前运行说明、配置、持久化差异及验证命令以 `agent_1/airong_agent_test1/web/MIGRATION.md` 为准。Pi 源码未修改或删除。

# 核保规则助手 Web 版 · 开发技术文档

> 范围：`airong_agent_test1/web/`（FastAPI 后端 + 零构建 Vue3 前端 + pi RPC 子进程桥 + 文件上传）
> 更新日期：2026-10-12

---

## 1. 项目定位

把 pi CLI（核保业务 agent，挂 `.pi/extensions` 的 `bom_search` / `rule_search` 业务工具与 `APPEND_SYSTEM.md` 人设）桥接成网页聊天应用，供一线核保业务人员使用：

- 打字机流式输出、思考过程折叠展示；
- 业务工具执行时显示"正在检索业务数据…"脉冲；
- 支持 📎 上传资料文件（pdf/docx/文本/代码）作为对话上下文；
- 会话管理：新会话、停止生成、断线自动重连。

## 2. 技术选型与演进

| 阶段 | 形态 | 说明 |
|---|---|---|
| v1 | Node 原生 http + 原生 ES 模块（`server/index.mjs`、`pi-rpc.mjs`，端口 8710） | 零依赖验证 RPC 子进程方案可行性 |
| v2（现行） | Python FastAPI + uvicorn（端口 8712）+ Vue3 零构建单页 | 迁到 Python 栈与规则编辑器（Flask/FastAPI 系）统一运维；客户端升级为 Vue3 响应式渲染 |

**"零构建"原则**：前端不用 npm/打包器，`vendor/vue.global.prod.js` 本地化引入（不依赖 CDN），三个文件（`index.html` / `app.js` / `styles.css`）直接由 FastAPI `StaticFiles` 托管。

**方案核心（RPC 子进程）**：不改 pi 源码，每个浏览器会话常驻一个 `node --mode rpc` 子进程，Web 层只做 JSONL 协议的双向翻译。

## 3. 总体架构与数据流

```
浏览器 (Vue3 SPA)
  │  POST /api/prompt | /api/abort | /api/new_session | /api/upload
  │  GET  /api/events (SSE, EventSource 自动重连)
  ▼
FastAPI (server/main.py, 127.0.0.1:8712)
  │  PiSessionManager: sid -> PiBridge
  ▼
PiBridge (server/pi_rpc.py)
  │  stdin:  {"id","type",...} JSONL 下行命令
  │  stdout: {"type":"response",id,...} 按 id 回填 Future
  │          其余事件行 -> asyncio.Queue 广播给 SSE 订阅者
  ▼
node --import <source-resolver file://URL> pi-main/.../cli.ts --mode rpc
      --provider qwen-maas --model qwen3.8-flash
      cwd = airong_agent_test1（决定 .pi/extensions 业务工具与人设是否加载）
```

一次提问的完整链路：`send()` 组装消息 → `POST /api/prompt` → 桥下发 `{"type":"prompt"}` → 子进程流式吐事件 → SSE 转发 → `handleEvent()` reducer 更新气泡 → `agent_settled` 收尾解除 busy。

## 4. 后端模块

### 4.1 `server/pi_rpc.py` — pi 子进程桥

- **启动口径**：逐字复刻 `pi-test.ps1:62-64`（裸 run-pi.bat）。`--import` 必须传 `file://` URL（`RESOLVER_TS.as_uri()`），Windows 裸路径不是合法 specifier。
- **路径常量**：`WEB_DIR → PROJECT_DIR(airong_agent_test1) → PI_DIR(../pi-main)`，`CLI_TS`、`RESOLVER_TS` 启动前校验存在，缺失抛友好 `PiBridgeError`。
- **命令-应答**：`send()` 生成 uuid 命令 id，挂 `asyncio.Future` 到 `_pending`，读循环见到 `type=="response"` 按 id 回填；60s 超时；进程退出时 `_fail_all_pending` 统一失败。
- **事件路由 `_route()`**：`response` 回填；`extension_ui_request` 中 `select/confirm/input` 属交互对话框——Web 无 TUI 可弹，自动回 `cancelled` 防 agent 挂起；`notify/setStatus` 等被动消息继续转发前端；其余事件广播。
- **SSE 订阅**：每订阅者一个 `asyncio.Queue(maxsize=2000)`，溢出丢最旧保推流不断。
- **stderr**：只留尾部 4000 字符作诊断，`bridge_dead` 事件附带最后 500 字符。
- **会话管理 `PiSessionManager`**：`sid -> PiBridge`，`ensure()` 惰性拉起/复活；回收任务每 60s 扫描，**空闲 >30 分钟且无 SSE 连接**才终止子进程；lifespan 关停时 `stop_all()`。

### 4.2 `server/main.py` — HTTP API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | node 路径、CLI/resolver 存在性、cwd、各 sid 存活状态 |
| `/api/prompt` | POST | `{sid, message}`；桥错误 502、应答超时 504 |
| `/api/abort` | POST | `{sid}`，中止当前生成（会话不存活直接报错） |
| `/api/new_session` | POST | `{sid}`，复用子进程、清上下文 |
| `/api/upload` | POST | multipart 单文件；见 4.3 |
| `/api/events?sid=` | GET | SSE：首个 `event: hello`，20s 无事件发 `: ping` 心跳；头带 `X-Accel-Buffering: no` 防代理缓冲 |
| `/`（兜底） | StaticFiles | `client/` 目录，html=True，挂载放最后 |

约定：业务失败走 `HTTPException`（detail），`/api/upload` 例外——**恒返回 200**，用 `{ok:false, error}` 表达失败，前端免 HTTP 状态分支。

### 4.3 `server/file_extract.py` — 附件文本提取（方案 B：纯内联）

设计文档：`docs/superpowers/specs/2026-10-12-web-file-upload-design.md`。

- **不落盘**：原始字节在内存提取后即丢，无清理逻辑；提取文本随历史消息常驻 agent 上下文。
- 上限：单文件 `MAX_BYTES = 20MB`；提取文本 `MAX_CHARS = 30000`（每文件独立截断，块头标注"内容过长已截断"）。
- 白名单：29 种文本/代码扩展名 + `pdf` + `docx`；`.doc` 明确报"请另存为 .docx"。
- 解码试探链：`utf-8-sig → gbk → utf-8(replace)`；换行统一 `\n`。
- PDF：`pypdf`；加密且空密码解不开 → 报错；提取不到文字（扫描件）→ "暂不支持 OCR"。
- docx：`python-docx` 段落 + 表格（行内单元格 ` | ` 连接）。
- 依赖缺失（ImportError）→ `ExtractError("服务端未安装 pypdf…")`；一切用户可见错误统一 `ExtractError` 友好文案。
- `/api/upload` 中 `asyncio.to_thread(extract_text, …)` 防解析阻塞事件循环。

注入格式（`app.js buildAttachmentBlocks`）：

```
【上传文件：文件名｜共N字，内容过长已截断】
<全文>
【文件结束】
```

多个块之间空行分隔，与用户正文以 `\n\n\n` 拼接后原路走 `/api/prompt`，agent 侧零改动。

## 5. 前端（`client/`）

### 5.1 结构（`index.html`）

- 顶栏：连接状态（SSE open/error 驱动）、"新会话"按钮；
- 空态引导 + 示例 chips；
- 消息流：user / assistant / system / error 四类气泡；助手气泡含折叠"思考过程"（`thinking_delta` 累加）、流式光标 caret、附件 chip（📄 名 · N字（截断），不展开全文）；
- 活动条：`v-show="activity.active"` + 脉冲点；
- 输入区：状态行、附件条（uploading ⏳ / ready 📄 / error ❌ + × 移除）、📎 按钮 + hidden `input[type=file][multiple][accept]`、自适应高度 textarea（38–160px）、Enter 发送 / Shift+Enter 换行、busy 时切换"停止"按钮；仅附件无正文也可发送。

### 5.2 事件 reducer（`app.js handleEvent`）

pi `--mode rpc` 事件口径（实测）：

| 事件 | 处理 |
|---|---|
| `message_update.assistantMessageEvent.text_delta` | 累加进当前流式助手气泡 |
| `message_update…thinking_delta / reasoning_delta` | 累加进折叠"思考过程" |
| `message_end.message{role,content[],stopReason,error}` | **权威定稿**：`stopReason==="error" ‖ error` → 删空气泡 + 推错误气泡（附 PI_EXTRA_ARGS 提示）；无正文且含 `toolCall` → 中间轮空气泡静默丢弃（不留"无输出"占位）；否则用 `content` 里 text 段拼接结果覆盖增量累加 |
| `tool_execution_start/end{toolCallId,toolName,isError}` | `toolName ∈ {bom_search, rule_search}` → "正在检索业务数据…"；其余工具显示通用文案；`read` 等默认工具不触发检索脉冲语义 |
| `agent_settled` | busy=false、收尾未完结气泡 |
| `auto_retry_start` / `compaction_start` | 状态行提示 |
| `extension_ui_request`（notify/setStatus） | 系统消息 / 状态文本 |
| `bridge_dead` | 错误气泡（含退出原因+stderr 尾巴），提示下条消息自动重启会话 |

### 5.3 会话与状态

- `sid` 存 `localStorage("piweb.sid")`（crypto.randomUUID），服务端重启后同 sid 自动开新会话（内存映射已丢）。
- `EventSource` 断线自动重连；`onMounted` 建连。
- 发送后清空附件（含 error chip，失败重选即可）；用户气泡记 `files` 元数据。

### 5.4 样式要点（`styles.css`）

- CSS 变量主题（`--brand` / `--muted` 等），附件 chip、错误态复用；
- **防坑条目**：`.pulse[hidden]{display:none!important}`——`.pulse` 的 `display:inline-block` 会覆盖 UA `[hidden]{display:none}`，不加此行脉冲常亮。

## 6. 启动与部署

```powershell
cd airong_agent_test1\web
pip install -r requirements.txt      # fastapi/uvicorn/pypdf/python-docx/python-multipart（钉版本）
powershell -File .\run-web.ps1       # 默认 127.0.0.1:8712
```

- `run-web.ps1`：先按端口 `Get-NetTCPConnection` 找 PID `taskkill` 清旧进程，再 `python -m uvicorn server.main:app`；`$env:PORT` 可覆盖端口。**脚本必须保持纯 ASCII**——Windows PowerShell 5.1 会把无 BOM UTF-8 的 .ps1 按 GBK 读，中文串字面量会碎。
- 模型参数：默认 `--provider qwen-maas --model qwen3.8-flash`；`$env:PI_EXTRA_ARGS` 可整体覆盖（空格分词）。
- 首次发消息拉起 pi 子进程（TS 加载约 10–15s），之后常驻；前置：本机 PATH 有 node，且 `../pi-main` 源码在位。

## 7. 关键决策与踩坑记录

1. **cwd 必须为 `airong_agent_test1`**：`.pi/extensions`（bom_search/rule_search）与 `APPEND_SYSTEM.md` 人设按 cwd 加载，起错目录 = 通用编码助手。
2. **裸起 401**：不设 provider 会落全局 `openai/gpt-5.5`，key 无效 → `message_end stopReason=error`（"模型被调但瞬间失败"，不是没调用）。前端已按此显示错误气泡 + 修复提示。
3. **`--import` 需 `file://` URL**：Windows 反斜杠路径直接被 node 拒绝。
4. **`extension_ui_request` 必须自动回 cancelled**：否则 agent 等 TUI 交互永久挂起。
5. **空 `message_end` 不能显示"（本轮无文本输出）"**：工具中间轮天然无文本，删气泡；真错误走 error 分支——旧版把两者混为一谈误导用户。
6. **SSE 溢出丢最旧而非丢最新**：保证推流连续性优先于事件完整性。
7. **stdout 非 JSON 行直接忽略**：pi 可能输出杂项日志，读循环不可崩。
8. **上传恒 200 + ok 标志**：业务失败（类型不支持/扫描件/超 20MB）与传输失败统一渲染进红色 chip，前端只分支 `j.ok`。
9. **提取放 `asyncio.to_thread`**：30MB 级 PDF 解析不放线程会卡死整个事件循环。
10. **验证探针法**（Node 时代沉淀）：临时脚本 spawn 服务 + `http.get /api/events` 收 SSE + POST `/api/prompt`，dump `message_end` 形态与服务 stderr；探针 teardown 末尾 `read ECONNRESET` 是 kill 服务断 SSE socket 的无害噪声。

## 8. 已知局限与后续方向

- 附件注入为全文内联，长文档挤占上下文；未做分块/RAG 检索（有意 YAGNI）。
- 不支持图片/扫描件 OCR、老版 `.doc`。
- 会话隔离靠 sid 约定，无鉴权——当前定位本机单人使用（绑定 127.0.0.1）。
- pi 子进程冷启动 10–15s，可考虑预热；`/api/health` 已具备存活探测能力。
- 多标签页共享同 sid 会共用一个子进程与事件流（当前可接受）。

## 9. 相关文件索引

| 文件 | 角色 |
|---|---|
| `web/server/pi_rpc.py` | pi JSONL 子进程桥 + 会话管理器 |
| `web/server/main.py` | FastAPI 路由 + SSE + 静态托管 |
| `web/server/file_extract.py` | 附件纯文本提取 |
| `web/client/{index.html,app.js,styles.css}` | Vue3 零构建单页 |
| `web/client/vendor/vue.global.prod.js` | 本地化 Vue3 运行时 |
| `web/run-web.ps1` | 一键启动（端口清理 + uvicorn） |
| `web/requirements.txt` | Python 依赖钉版 |
| `web/README.md` | 快速上手 |
| `docs/superpowers/specs/2026-10-12-web-file-upload-design.md` | 文件上传设计稿 |
| `.pi/APPEND_SYSTEM.md` | agent 人设与规则四段式交付格式 |
| `../pi-test.ps1:62-64` | 子进程启动口径的事实来源 |
