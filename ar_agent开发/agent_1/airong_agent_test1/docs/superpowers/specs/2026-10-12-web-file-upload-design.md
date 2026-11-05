# Web 前端文件上传（方案 B：纯内联提取）设计

日期：2026-10-12 ｜ 状态：已确认 ｜ 范围：`web/`（FastAPI + 零构建 Vue3）

## 背景与目标

核保规则助手网页版（`web/`）目前只能发纯文本。参考豆包/GPT 网页版，增加"上传文件当上下文"能力：用户选文件 → 后端提取纯文本 → 发送时全文拼进消息 → pi agent 直接阅读。

已确认的取舍：
- 文件类型：文本/代码类 + `.pdf` + `.docx`；图片、老版 `.doc`、扫描件不做（友好报错）。
- 注入方式：**纯内联**（方案 B）。不做落盘/路径注入，原始文件在内存中提取后即丢，无需清理逻辑；提取文本随历史消息常驻 agent 上下文，后续轮次仍可引用。
- agent 侧零改动：不走 pi RPC 的 `images` 字段，不改 `.pi` 扩展与系统提示。

## 架构与数据流

```
[index.html 📎] → onFilesPicked → POST /api/upload (multipart, 单文件)
                                  └ file_extract.extract_text(name, bytes) → {ok,name,text,chars,truncated}
[输入框上方附件条 chip: ⏳/📄字数/❌原因/×移除]
[发送] message = 用户文本 + 每个就绪附件的【上传文件：…】块 → POST /api/prompt（原链路不变）
[用户气泡] 只显示正文 + 文件 chip（📄 名 · N字），不展开全文
```

## 组件

### 1. `web/server/file_extract.py`（新增）
- 常量：`MAX_BYTES = 20MB`；`MAX_CHARS = 30000`（每文件独立截断）。
- 白名单：txt/md/markdown/json/csv/tsv/log/xml/yaml/yml/ini/conf/properties/sql/py/js/ts/java/c/h/cpp/hpp/go/rs/sh/bat/ps1/html/css + pdf/docx。
- 解码：文本类按 `utf-8-sig → gbk → utf-8(replace)` 试探；换行统一为 `\n`。
- PDF：`pypdf.PdfReader`；加密且空密码解不开 → 报错；无文字层（扫描件）→ 报"暂不支持 OCR"。
- docx：`python-docx` 段落 + 表格（行内单元格以 ` | ` 连接）；解析失败提示".doc 请另存为 .docx"。
- 对外 `extract_text(filename, data) -> (text, truncated)`；一切用户可见错误抛 `ExtractError(友好文案)`。
- 依赖缺失（ImportError）→ `ExtractError("服务端未安装 pypdf/python-docx…")`。

### 2. `web/server/main.py`（修改）
- 新增 `POST /api/upload`：`UploadFile` 读取（≤ MAX_BYTES+1 探测超限）；提取放 `asyncio.to_thread` 防阻塞事件循环；**恒返回 200**，用 `{ok:false, error}` 表达业务失败，前端免分支处理 HTTP 状态。
- 其余端点不动。

### 3. `web/client/`（修改三件套）
- `index.html`：input-row 前置 📎 按钮 + hidden `input[type=file][multiple][accept]`；status 下方加 `attach-bar`（未发送附件 chips）；用户气泡内加 `m.files` chips；`pre.content` 加 `v-if="m.text"` 支持"仅附件"消息；发送按钮 disabled 放宽为 `!draft.trim() && !attachments.length`。
- `app.js`：`attachments` 状态机（uploading/ready/error，uid 递增）；选中即逐个 `fetch /api/upload`（FormData）；`removeAttachment`；`send()` 组装 `【上传文件：name｜共N字｜内容过长已截断】\n全文\n【文件结束】` 块（多个块之间空行），提取中禁止发送并提示；发送后清空附件；user 消息记录 `files` 元数据用于气泡渲染。
- `styles.css`：`.attach-bar/.file-chip(+uploading/error)/.file-x/.btn.attach/.msg-files/.msg-file-chip`，沿用现有 CSS 变量。

### 4. `web/requirements.txt`
追加 `pypdf`、`python-docx`（按实际安装版本钉住）。

## 错误处理

| 场景 | 表现 |
|---|---|
| 超 20MB / 扩展名不在白名单 / 空文件 | 后端或前端预检 → chip 红色显示原因 |
| 扫描件/图片型 PDF、加密 PDF、空 docx | `ExtractError` 友好文案 |
| 超 30000 字 | 截断，块头标注"内容过长已截断"，chip 显示"(截断)" |
| `/api/upload` 网络/5xx | chip 显示 `HTTP xxx`，可 × 移除后重传 |

## 测试与验收（手动）

1. 脚本生成含中文段落+表格的 docx、utf-8/gbk 双编码 txt → `/api/upload` 返回文本正确、docx 表格行含 ` | `。
2. 伪造 `.exe`、空文件、纯图片 PDF、>20MB → 各自友好 error。
3. 3 万+字 docx → `truncated=True`，截断标记出现。
4. 起 uvicorn 实测：上传 docx 后提问，模型能引用文档内容；仅附件+空文本可发送；气泡显示文件 chip 不显示全文。

## 明确不做（YAGNI）

- 不落盘、无会话附件目录、无下载/预览原件。
- 不做图片 OCR、扫描件、`.doc`；不做分块/检索（RAG）。
- 不改 `/api/prompt`、pi RPC 协议与扩展。
