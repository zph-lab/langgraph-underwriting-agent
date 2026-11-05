> 当前入口与模块结构见 [业务项目 README](airong_agent_test1/README.md)。本文件下文保留旧 Pi 部署记录；原 `web/server`、`web/.venv-langgraph` 及 `server.main:app` 已调整。

> 2026-10-23 迁移更新：业务默认 Web 入口 `server.main:app` 已切换为 LangGraph；新终端入口为 `run-agent.bat`。原 Pi 说明保留作历史和回退参考。当前运行说明、配置、持久化差异及验证命令以 `airong_agent_test1/docs/MIGRATION.md` 为准。Pi 源码未修改或删除。

# 核保规则业务助手 · 部署说明

本仓库是一个 **pi 编码智能体（`pi-main/`）+ 业务助手项目（`airong_agent_test1/`）** 的组合：
助手项目只提供 `.pi/extensions` 扩展、`tools/` 检索核心和数据文件，运行时由 pi 以「项目目录为 cwd」的方式加载。

```
agent1008/
├─ pi-main/                 pi 智能体本体（npm workspaces 单仓，源码运行）
├─ airong_agent_test1/      核保规则业务助手（扩展 + 检索工具 + BOM 数据）  ← 实际使用的入口
└─ airong_agent_origin/     早期快照，无 package.json，不需要安装依赖
```

启动链路：`airong_agent_test1/run-pi.bat` → `pi-main/pi-test.bat` → `pi-test.ps1` →
`node --import packages/coding-agent/src/experimental/source-resolver.ts packages/coding-agent/src/cli.ts`

---

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Node.js | **≥ 22.19.0**（实测 v24.12.0） | `pi-main/package.json` engines；`tools/bom-to-sqlite.mjs` 用 `node:sqlite`，需 ≥ 22.5 |
| npm | 随 Node（实测 11.6.2） | 必须支持 workspaces |
| 操作系统 | Windows / Linux / macOS | Windows 无需管理员权限（npm 建的是 junction） |
| MySQL | 网络可达的规则库（`ya_car` 库，只读账号） | 只有 `rule_search` 需要；不配也能用 `bom_search` |
| 模型服务 | 一个 OpenAI 兼容的推理服务 + API Key | 写在 `.pi/models.json`（不入库，需自行创建） |

## 2. 安装依赖（只有两处）

```powershell
# ① pi 本体：一次装齐 root + packages/ai + packages/coding-agent，并在
#    node_modules/@earendil-works/* 建 15 个指向 packages/* 的 junction
cd pi-main
npm ci

# ② 助手项目：只有 mysql2（约 4 MB）
cd ../airong_agent_test1
npm ci
```

要点：

- 用 `npm ci` 而非 `npm install`：两处都有 `package-lock.json`，可精确复现版本。
- **不要拆开单独安装 `pi-main/packages/*`**，workspaces 必须从 `pi-main` 根一次装完。
- `airong_agent_origin/` 没有 `package.json`，跳过。
- 内网或无外网：追加镜像参数
  `npm ci --registry=https://registry.npmmirror.com`
- 体积参考：`pi-main/node_modules` ≈ 330 MB（2.6 万文件），`airong_agent_test1/node_modules` ≈ 4 MB。

### 不需要做的事

| 事项 | 结论 |
|---|---|
| `npm run build` | **不需要**。pi 走 TypeScript 源码运行时：`pi-main/tsconfig.json` 把 `@earendil-works/*` 全部映射到 `packages/*/src/*.ts`，由 `source-resolver.ts` 在 ESM 层解析。当前所有 `packages/*/dist` 都不存在也照常运行。只有要出发布产物/bundle 时才 build。 |
| 给 `airong_agent_test1` 装 `typebox` | **不需要**。`.pi/extensions/*.ts` 里的 `import { Type } from "typebox"` 和 `@earendil-works/pi-coding-agent` 由 pi 的 jiti 扩展加载器注入别名（`pi-main/packages/coding-agent/src/core/extensions/loader.ts` 的 `getAliases()`），从 `pi-main/node_modules` 解析。 |
| 给 `airong_agent_origin` 装依赖 | 不需要（无 `package.json`，且扩展别名同上）。 |
| 全局安装 pi | 不需要，始终用 `run-pi.bat` / `pi-test.ps1` 从源码启动。 |

## 3. 创建两个被 `.gitignore` 挡住的配置文件

clone 出来的仓库里**没有**下面两个文件，必须手工创建，否则启动即报错。

### 3.1 `airong_agent_test1/.pi/models.json`（模型与密钥）

```json
{
  "providers": {
    "qwen-maas": {
      "name": "Qwen MaaS",
      "baseUrl": "https://<你的兼容模式网关>/compatible-mode/v1",
      "api": "openai-completions",
      "apiKey": "<你的 API Key>",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": true,
        "thinkingFormat": "qwen"
      },
      "models": [
        {
          "id": "<模型 id>",
          "name": "<模型显示名>",
          "reasoning": true,
          "input": ["text"],
          "contextWindow": 1000000,
          "maxTokens": 131072,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
```

### 3.2 `airong_agent_test1/config/mysql.json`（规则库连接 + 检索规模）

```json
{
  "mysql": {
    "host": "<MySQL 地址>",
    "port": 3306,
    "user": "<只读账号>",
    "password": "<密码>",
    "database": "ya_car",
    "connectTimeoutMs": 10000,
    "statementTimeoutMs": 15000
  },
  "ruleSearch": {
    "bomBaseInfoId": "<BOM_BASE_INFO_ID，用于圈定规则范围>",
    "bomBaseInfoName": "<该 ID 对应的业务名称，仅用于展示>",
    "defaultLimit": 6,
    "maxLimit": 30,
    "fetchFactor": 5
  }
}
```

每个字段都可以用环境变量覆盖（容器/多环境部署时更实用）：

`RULE_MYSQL_HOST` / `RULE_MYSQL_PORT` / `RULE_MYSQL_USER` / `RULE_MYSQL_PASSWORD` / `RULE_MYSQL_DATABASE` / `RULE_BOM_BASE_INFO_ID`

> 账号需授权当前来源 IP；`bomBaseInfoId` 决定 `rule_search` 能查到哪些规则，换业务线时必须改。

## 4. 启动前自检（不进 pi 也能验）

在 `airong_agent_test1/` 目录下执行：

```powershell
# BOM 检索：只读本地 bom/bom.sqlite，不依赖 MySQL
node tools/bom-search.mjs 机构
node tools/bom-search.mjs 违章 --methods --limit 3

# 规则库连通性 + 规模统计
node tools/rule-search.mjs --ping
node tools/rule-search.mjs --stats

# 真实关键词检索
node tools/rule-search.mjs 手续费率 --limit 3
node tools/rule-search.mjs 车龄 违章 --json --content
```

任何一步报错，先修配置再启动 pi —— `--ping` 失败时 `rule_search` 工具一定不可用。

## 5. 启动助手

```powershell
# Windows：双击或在任意目录执行，脚本内部会 cd 到 airong_agent_test1
airong_agent_test1\run-pi.bat

# 传参同样透传
airong_agent_test1\run-pi.bat --help
```

Linux / macOS 下等价命令（**cwd 必须是 `airong_agent_test1`**，pi 按 `<cwd>/.pi/extensions` 加载项目扩展）：

```bash
cd airong_agent_test1
RESOLVER_URL=$(node -p 'require("node:url").pathToFileURL(process.argv[1]).href' ../pi-main/packages/coding-agent/src/experimental/source-resolver.ts)
node --import "$RESOLVER_URL" ../pi-main/packages/coding-agent/src/cli.ts
```

启动后助手应注册两个工具：`bom_search`（BOM 字段检索）、`rule_search`（已有规则检索），
并按 `.pi/skills/rule-authoring/SKILL.md` 提供规则撰写技能。

## 6. 只读护栏（部署时不要误改）

面向业务人员，助手默认**禁止任何写文件与执行命令**，改动会绕过安全边界：

| 位置 | 作用 |
|---|---|
| `airong_agent_test1/.pi/settings.json` | 声明层：`defaultTools` 只留 `read/grep/find/ls`，模型看不到 edit/write/bash |
| `airong_agent_test1/.pi/extensions/read-only-guard.ts` | 兜底层：拦 `tool_call`、`user_bash`（`!`/`!!` 直接执行的命令），并在系统提示里声明能力边界 |

**新增查询类扩展工具后，必须把工具名加进 `read-only-guard.ts` 的 `ALLOWED_TOOLS`**，否则会被护栏一起拦掉。

## 7. 数据文件与重建 BOM

| 路径 | 体积 | 说明 |
|---|---|---|
| `airong_agent_test1/bom/bom.json` | ~11 MB | BOM 原始导出（重建输入） |
| `airong_agent_test1/bom/bom.sqlite` | ~35 MB | `bom_search` 的实际数据源，已随仓库提供 |
| `airong_agent_test1/bom/bom.sqlite-shm` / `-wal` | 小 | SQLite 运行时文件，可删 |
| `airong_agent_test1/bom/_tmp_rebuild.sqlite` | ~35 MB | 重建过程的临时库，可删 |
| `airong_agent_test1/data/predefine_nonnull.jsonl` | ~12 MB | 预定义非空数据存档（当前 Node 侧工具未引用） |

换 BOM（业务线更新）后重建：

```powershell
node tools/bom-to-sqlite.mjs import --in bom/bom.json --out bom/bom.sqlite --drop
node tools/bom-to-sqlite.mjs stats
```

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 启动报 `配置文件不存在：...config/mysql.json` | 见 §3.2 创建该文件（该目录被 `.gitignore` 排除，不会随 clone 带来）。 |
| `rule_search` 查询失败提示检查 `192.168.x.x:3306` | MySQL 不可达或账号未授权来源 IP；先跑 `node tools/rule-search.mjs --ping` 定位。 |
| 报 `Cannot find package 'typebox'` | 说明没先装 `pi-main` 依赖（别名来自 pi 的 jiti 加载器）；`cd pi-main && npm ci`。 |
| 扩展候选/工具不加载 | cwd 不是 `airong_agent_test1`；必须用 `run-pi.bat` 启动（它会先 cd）。 |
| Node 报类型语法错误 | Node 版本低于 22.19，升级 Node。 |
| 装完依赖后发现 git 钩子异常 | `pi-main` 根 `package.json` 有 `"prepare": "husky"`，而本仓库 git 根是 `agent1008`。检查 `git config core.hooksPath`，被改了执行 `git config --unset core.hooksPath`。 |
| 需要发布产物（非源码运行） | 才需要 `cd pi-main && npm run build`（Windows 原生模块另见 `npm run build:native:win32`）。 |

## 9. 入库范围提醒

根 `.gitignore` 已排除：`airong_agent_test1/config/`、`airong_agent_test1/.pi/models.json`、`node_modules/`、`dist/`、日志与编辑器文件。
因此**部署必需的只有源码 + 数据文件**；依赖用 §2 两条 `npm ci` 还原，配置用 §3 两个模板生成。





setting.json示例
{
  "defaultProvider": "Qwen3.8-27B-NVFP4",
  "defaultModel": "Qwen3.8-27B-NVFP4",
  "defaultTools": ["read", "grep", "find", "ls"],
  "extensions": ["-builtin:codemode", "-builtin:tool-search", "-builtin:mcp"]
}
