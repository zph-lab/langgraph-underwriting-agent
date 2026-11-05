# rule_desc_to_content.py 技术文档

> 更新日期：2026-08-14 ｜ 文件：[rule_desc_to_content.py](rule_desc_to_content.py)
> 用途：把规则的**中文描述（ruleDesc）** + **BOM 词典** 转换成平台可用的 **ruleContent（规则 JSON）**

## 1. 概述

### 1.1 输入 / 输出

- 输入 1：`ruleDesc`——HTML 或纯文本的中文规则描述，如
  `保单的二级机构 是 动态域集的归属机构的四川 并且 ( 保单的投保日期 大于 2026-01-01 00:00:00 )`
- 输入 2：`bom.json`——规则库的业务对象模型（字段/方法/动态域值的树形结构）
- 可选输入：`predefine` 内容（"指定'别名'为一个…来自( in )…"格式的预定义变量声明）
- 输出：`ruleContent`——平台规则的 JSON（`COMPARISON` / `EXPRESSION` / `METHOD` / `NESTING` / `TREE_EX` / `LOGICAL` 等节点）

核心原则：**生成器不写死任何字段**，换规则库 = 换 `--bom` 参数；全程只依赖 desc + BOM，不依赖真实标签（gt）。

### 1.2 依赖

- Python 3（无第三方依赖）
- 评估脚本依赖 `evaluate_all_datas.py`（提供 `sig` / `sig_eq` / `first_diff`）

## 2. 整体流程

```
ruleDesc(HTML)
   │  plain_desc()：去 HTML 标签、空白归一化
   ▼
纯文本
   │  parse()：_parse_or → _parse_atom → _parse_clause
   ▼
语义树（and/or/not/cmp/expr/method… 的 dict）
   │  render()：_render 按 kind 序列化
   ▼
ruleContent（平台 JSON）
```

解析与渲染分离：`parse` 产出与运算符无关的语义节点，`render` 负责按平台模板输出节点结构。

## 3. 核心数据结构（`RuleGenerator.__init__`）

| 成员 | 说明 |
|---|---|
| `lexicon` | 组合标签（去 `业务模型对象的` 前缀）→ 字段节点，如 `保单的投保日期` |
| `mlabel_lex` | 方法标签 → 方法节点，如 `动态域集的帮助帮助:0包含1中的一个()` |
| `_trie` | 前缀 Trie 索引（lexicon + mlabel_lex），`_longest_label` 用 |
| `_lex_extra` | 按条叠加的预定义别名（构造期 + `generate_with_predefines` 临时加入） |
| `_bom_composed_norm` | 去空格组合标签 → 原始 BOM 节点（预定义路径查找用） |
| `dyn_label_index` | 动态域值"普通标签 → 节点列表"（多选/单选解析用） |
| `method_by_fieldpath` | 方法 fieldPath → 方法节点（exprNode 骨架用） |

### 3.1 fieldPath 兜底别名（2026-08-14 新增）

初始化时为每个字段额外注册 `父组合标签的英文字段名` 别名（如 `结果的nprmsum` → `结果的总保费` 节点），
命中别名时按 desc 原文渲染 label、fieldPath/field 取 BOM 节点。解决"desc 用英文字段名、BOM 用中文标签"的版本不一致场景。

## 4. 词表匹配

### 4.1 `_longest_label(text, pos)`——最长标签匹配

- 前缀 Trie 上沿文本逐字符下钻，取最深命中节点 = 最长标签（等价于原来的全表线性扫描，行为一致）
- 附加扫描 `_lex_extra`（预定义别名，数量很少）
- 复杂度：O(标签长度)，与词表大小无关

### 4.2 动态域值解析

- `_resolve_dyn_value`：按父标签匹配（`动态域集的<字段label>`）
- `_resolve_dyn_value_code`：优先按 codeService 定位动态域类（解决新疆 `XinJiang` 之类同名字段歧义）

## 5. 解析器

### 5.1 逻辑结构：左结合

同级 `并且 / 或者` 按文本顺序**左结合**解析（与平台 desc 解析一致；括号优先）。
`A 并且 B 或者 C 并且 D` → `((A∧B)∨C)∧D`，**不是** AND 优先（2026-08-13 按 gt 数据修正）。

### 5.2 `_scan_value`——值扫描

- 半角/全角括号 `()`/`（）` 配对计入深度
- 顶层遇到 `并且/或者` 且**两侧带空格**才终止（散文里的"或者"不误切）、遇到 `)`/`）` 终止

### 5.3 `_parse_method_args`——方法参数解析

- 括号内按**最长 BOM 标签顺序匹配**，参数值本身含 `，`（如 `动态域集的不浮动原因的01:过户车，不浮动`）不拆碎
- 全角括号配对（`（武汉）`、`（含）` 不提前闭合）
- 保留首尾/连续 `，` 产生的空参数占位（`（）` → 一个空串参数）

### 5.4 `OPERATORS` 运算符表

表顺序即匹配优先级（长运算符在前）。每个运算符对应一个处理器方法，返回 `(语义节点, 新位置)`。
新增运算符 = 表里加一行 + 写一个处理器。

| 运算符 | code / 节点 | 处理器 |
|---|---|---|
| `为是 / 为否` | BOOLEAN_BELONG_TO | `_op_boolean_belong` |
| `包含 / 不包含` | STR_INCLUDE / STR_NOT_INCLUDE | `_op_str_include` / `_op_str_not_include` |
| `属于（多选）/ 不属于（多选）` | STR_BELONG_TO_MUL / STR_NOT_BELONG_TO_MUL | `_op_belong_mul` / `_op_not_belong_mul` |
| `属于（单选）/ 不属于（单选）/ 属于` | STR_BELONG_TO / STR_NOT_BELONG_TO | `_op_str_belong` / `_op_str_not_belong` |
| `不为空 / 为空` | STR_NOT_EMPTY / STR_EMPTY（对象用 OBJECT_NOT_EMPTY） | `_op_not_empty` / `_op_empty` |
| `大于等于/小于等于/大于/小于/不等于/等于` | GT/GE/LT/LE/NE/EQ（日期 DATE_*） | `_op_cmp` |
| `是 / 不是` | STR_EQ / STR_NE | `_op_str_eq` |
| `以…结尾 / 以…开头` | STR_END_WITH / STR_START_WITH | `_op_end_with` |
| `内容长度 / 的内容长度` | SIZE_OF_LIST | 内联于 `_parse_clause` |
| `在X和Y之间` | DATE_BETWEEN | 内联于 `_parse_clause` |
| `"" 是 <字段>` 空串反写 | STR_EQ（左 CUSTOM `""`、右 FIELD） | 内联于 `_parse_clause` |

## 6. 语义节点（kind）与渲染映射

解析产物为 `{kind, field, value, op…}` 语义节点，`_render` 按 kind 输出平台节点：

| kind | 输出节点 |
|---|---|
| `and / or / not` | LOGICAL |
| `cmp` | COMPARISON（code 按 op + 字段类型，日期自动 DATE_*） |
| `str_eq / str_ne / str_eq_rev` | COMPARISON（STR_EQ / STR_NE） |
| `str_include(_list) / str_not_include(_list)` | EXPRESSION STR_INCLUDE / STR_NOT_INCLUDE |
| `str_belong / str_not_belong / belong_mul` | EXPRESSION STR_BELONG_TO(_MUL) 等 |
| `belong_single` | COMPARISON（右为 selector CUSTOM；值若为全路径 BOM 标签则右为 FIELD） |
| `boolean_belong` | EXPRESSION BOOLEAN_BELONG_TO |
| `not_empty / empty` | EXPRESSION STR_NOT_EMPTY / STR_EMPTY |
| `size_of_list` | EXPRESSION SIZE_OF_LIST |
| `date_between` | EXPRESSION DATE_BETWEEN |
| `str_end_with / str_start_with` | EXPRESSION STR_END_WITH / STR_START_WITH |
| 方法主体 | METHOD（参数含根对象 `保单` 时按数据集口径输出，当前与 813 一致用 FIELD） |
| 算术（含方法项） | NESTING + CALCULATE；含方法时 TREE_EX |

## 7. 关键算法细节

### 7.1 编码值截断 `_truncate_code_value`

非日期字段值形如 `047002 诉讼财产保全责任保险`（`^\d{4,}(?:\s+\S.*)?$`）或
`063043永安贝呵护…`（`^\d{4,}[\u4e00-\u9fff].*$`）时只保留编码段；STR_EQ / 单选 / 多选 key 均生效。

### 7.2 算术解析

- `_find_arith_op`：算术符（加/减/乘/除）前侧须为空格/数字/右括号、后侧须为空格/数字（避免 `驾乘` 的"乘"误判）
- 减运算符输出 `SUB`（平台实际使用），非历史遗留 `SUBTRACT`
- 方法调用可作算术项（`比率() 加 比率() 大于 X` → TREE_EX）

### 7.3 预定义变量

- 支持三种声明格式：`指定'X'为一个Y来自( in ) '根'的路径`（根可带/不带引号）、`指定'X'为'根'的路径`
- 根对象泛化（保单 / 结果 / 关联产品 / 核保节点 …）
- `generate_with_predefines(desc, contents, extra_entries=…)`：全局 predefine 条目预解析一次，避免 O(n²)

### 7.4 性能

| 优化 | 效果 |
|---|---|
| `_longest_label` 前缀 Trie | 词表匹配从 O(词表) → O(标签长度)；车险评估 ~10 分钟 → ~14 秒 |
| 全局 predefine 缓存 | 非车核保评估 432s → 57s |

## 8. 已知限制与平台口径差异

以下差异**无法用一个生成器同时满足两个数据集**，需按 BOM/版本统一口径：

- `内容长度`：813 的 gt 用 `SIZE_OF_LIST`，非车核保/核保生产用 `STR_LENGTH`
- 方法参数传根对象 `保单`：813 的 gt 用 FIELD，非车核保用 OBJECT
- STR_EQ 存编码 vs 存标签（`驾意险→17`、`传统直销业务→1900101` 等，desc 无法区分）
- 值两侧引号：813 保留、非车核保剥离
- 核保生产 `结果` 对象字段：fieldPath 兜底已解决大部分，剩余为本地 BOM 缺父级子树的字段（需换 BOM）

其它已知约束：

- fieldId 直接取 BOM 节点值，不校验（平台快照不一致）
- 重名组合标签按"先见先得"，不消歧
- 日期值为 `null` 时按 `"null"` 输出（真实值不在 desc 中）

## 9. 使用方式

```bash
# 单条生成
python -X utf8 rule_desc_to_content.py --bom ..\datas\813newdatas\bom.json \
  --desc "保单的二级机构 是 动态域集的归属机构的广西"

# 批量生成（--out 指定输出 jsonl）
python -X utf8 rule_desc_to_content.py --bom ..\datas\车险\bom.json \
  --samples ..\datas\车险\1_samples.jsonl --out out.jsonl

# 回归测试（与样例 ruleContent 比对）
python -X utf8 rule_desc_to_content.py --bom ..\datas\813newdatas\bom.json \
  --samples ..\datas\813newdatas\1_samples.jsonl --test
```

Python API：

```python
from rule_desc_to_content import RuleGenerator, plain_desc
gen = RuleGenerator("datas/车险/bom.json")
rc = gen.generate("保单的二级机构 是 动态域集的归属机构的四川")
```

## 10. 回归与验证

| 数据集 | 总数 | 成功 | 成功率 | 真失败 |
|---|---:|---:|---:|---:|
| 车险 | 12050 | 11967 | 99.31% | 1 |
| 非车核保 | 21098 | 20839 | 98.77% | 166 |
| 核保生产 | 13293 | 12763 | 96.01% | 170 |
| 813newdatas | 2701 | 2667 | 98.7% | 34 |
| 旧库（evaluate_all_datas） | 1024 | 1022 | 99.8% | 2 |

历次修复全程 0 回归；评估明细与剩余问题见各"评估问题总结"与 `results/`。
