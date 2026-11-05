# adp 智能规则编辑器（状态机 + 引导式编辑，BOM 接地）

基于 ilog 规则引擎 + 车险规则库，实现**引导式规则编辑器**：
用户逐步输入（`如果 → 保单 → 字段 → 运算符 → 值 → 并且/或者/结束`），
最终产出 **BOM 接地的纯自然语言 desc**（不带 `<span>` 标记）。

> desc→ruleContent 的转换由已有链路完成，本项目不产出规则树。

## 核心原则

**状态机是骨架，LLM 只做翻译器（可关闭），一切候选必须 BOM/语料接地。**

- **候选来源（自由编辑）**：候选池 = 全量 BOM 字段 + 运算符表 + 值词典。
  用户可以**自由组合**出任何规则（不限于 12,050 条历史规则），
  历史规则只提供「排序权重」（高频项靠前），不限制可选范围。
- **12,050 条规则的作用**：提供语料素材（字段/运算符/值）+ 频次统计（排序用）。
  **不是**让编辑结果去拟合它们。
- **BOM 接地**：字段/值一律用 bom.json 的中文 label + `field_path`。
  样本与 bom.json 两套数据中 `field_path` 完全一致，故统一以 `field_path` 为主键。

## 目录结构

```
rule_editor/
  config.yaml           # 数据路径、top-k、LLM 开关
  service.py            # 服务装配（BOM/值词典/频次/语料）
  etl/                  # 数据管道
    bom_catalog.py      # BOM 目录（对象/字段/运算符兼容表）
    value_dict.py       # 值词典 + 字段运算符频次
    freq_stats.py       # 状态机条件频次（P(FIELD|OBJECT) 等）
    corpus_index.py     # 规则语料索引（供评估/few-shot 复用，编辑器当前不加载）
    phrase_grammar.py   # 运算符短语 ↔ desc 模板
    rule_tree.py        # ruleContent 树解析
    run_etl.py          # ETL 主入口
  core/
    statemachine.py     # 状态机引擎
    suggest.py          # 候选生成（模式A 状态提示 / 模式B 输入匹配）
    desc_codec.py       # 状态机序列 ⇄ desc 纯文本
    validator.py        # 校验器
  rag/
    search.py           # BM25（二级兜底）
    embed.py            # embedding 接口（v1 跳过）
  llm/
    base.py             # LLM 统一接口（v1 仅 mock）
  api/
    main.py             # FastAPI 入口
    routes.py           # 路由 + SSE
  web/
    index.html, app.js  # 轻量单页前端
data/                   # ETL 产物（SQLite）
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r rule_editor/requirements.txt

# 2. 运行 ETL（一次性，约 25s，生成 data/*.sqlite）
#    需在项目根（Intelligent_Rule_Editor/）下运行
python -m rule_editor.etl.run_etl

# 3. 启动后端
python -m rule_editor.api.main          # 或 uvicorn rule_editor.api.main:app --port 8010
```

浏览器访问 `http://127.0.0.1:8010/`（端口见 config.yaml，默认 8010）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/session` | 新建会话 |
| GET  | `/api/suggest?sid=&query=` | 候选（模式A/B） |
| GET  | `/api/suggest/stream` | SSE 流式候选 |
| POST | `/api/select` | 选择候选并推进状态机 |
| POST | `/api/back?sid=` | 回退一步 |
| GET  | `/api/desc/{sid}` | 当前 desc |

## 测试报文（上传成功后自动生成）

`POST /api/upload_rule` 上传成功后，后端把生成的 ruleContent + 预定义 + then/else 结构交给
`rule_test/input_generator.py::build_input`，按 `/test/getInputByRuleId` 接口口径生成
**input 骨架**（叶子为 null，命中值测试时手工填写），随响应以 `testInput` 字段返回；
前端右侧「测试报文」卡片展示（生成失败只附加 `testInputError`，不影响上传结果）。

2026-09-04 起「测试报文」卡片为**可编辑表单**：null 叶子渲染为输入框（留空保持 null；
填 `[`/`{` 开头的 JSON 文本可构造列表/对象），并提供「测试规则命中」按钮
→ `POST /api/test_rule_hit`：提交填值后的报文到 ADP `/test/ruleSingleTest`
（自动用 url/token/登录，同上传口径），同时若随请求带上上传响应中的 ruleContent，
后端会用 `rule_test/rule_evaluator.py` 离线求值做对照（`localCheck` 字段，含逐条件
真值树与诊断；接口侧登录/网络失败时本地对照仍会返回）。

- then/else 引用来源：`generate_and_upload` 上传结果附带的转换后 `thenList/elseList`
  （与上传完全同口径，不做二次转换）；`dynamicsAttributeList.Helper.*` 不进骨架。
- 纯离线复现同一骨架：`python rule_test/input_generator.py --rule-id <ID>`。

## 回写方法注册表 `bom_methods_*.json`（换 BOM 后必做）

「那么/否则」动作上传到 ADP 需要**真实的 `expressionId`**（如 `ResultXom_addUnderwriteRuleInfo_7`，
尾部 `_7` 是 ADP 分配的序号，无法从 BOM 推导，缺失会导致 `/rule/method/define` 返回 1034）。
注册表 `desc_to_rulecontent/bom_methods_<BOM目录名>.json` 保存这些 ID。

在项目根（`intelligent_Rule_Editor/`）下执行（**expressionId 一律以 ADP 导出的 `rule_model.txt` 为准**）：

```bash
python desc_to_rulecontent\write_rule_to_adp.py --gen-methods-model "车险\bom.json" --model-methods "车险\rule_model.txt" --gen-methods-out desc_to_rulecontent\bom_methods_车险.json
```

`rule_model.txt` 每行以 Tab 分隔：`bomModelId \t 模板label \t methodId \t 类#方法#参数`；
取第 3 列 `methodId` 作为 `expressionId`，第 2 列 label 归一化（去尾部 `()`、去全部空白）后与 BOM
方法节点（`fieldPath` 以 `()` 结尾）的 label 匹配。

- **不从规则样本取样**（2026-09-14）：`--gen-methods "*_samples.jsonl"` 入口仅作临时排查，不进正式流程——
  样本里的 expressionId/参数结构是历史快照，可能与当前 BOM 模型错位。注册表无 `expressionArray`，
  占位模板/参数切分回退 **BOM label 数字槽位切分**（`effective_parse_basis`/`split_template`），
  参数类型取 BOM 节点 `detailsList`。
- 加载：`default_methods_registry()` 按 `config.yaml` 的 `data.samples_dir` 找
  `bom_methods_<samples_dir>.json`，不存在时回退 `bom_methods_非车.json`，故**输出文件名建议与 samples_dir 同名**。
- 消费方：`write_rule_to_adp.py::build_item`（上传取真 ID）、
  `core/textsearch.py::_load_method_registry`（编辑器占位模板/参数位上下文）。
- 坑：label 归一化只去空白不去标点，BOM 与 `rule_model.txt` 的中文逗号/冒号须一致；
  同 label 重载（如 `addFeeAdjustmentRate` `_15` 三参/`_16` 四参）按第 4 列签名
  `类#方法#参数个数` 配对，签名行缺失时才回退 label 取第一个未用 ID；
  上传端 `find_registry_entry` 对多候选先按 `detailsList` 参数个数过滤再 label 消歧；
  Windows cmd 中文路径易乱码，建议在 IDE 内置终端执行。

## 已知边界（v1）

- LLM（整句意图解析/desc 生成修复）仅留接口，默认关闭；关闭后系统功能完整。
- 语义索引（embedding+RRF）默认关闭，用字符匹配 + BM25 + 频次。
- METHOD 自定义方法、NESTING/CALCULATE 不做自由构造。
- then_else 动作部分 v1 不编辑。
- 相似规则「从此续写」功能已移除（前端展示 + 后端 /similar /reference 接口已删除）。
