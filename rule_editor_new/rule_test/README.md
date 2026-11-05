# rule_test —— ADP 规则测试报文生成器

根据 ADP 规则的 `ruleContent`（编辑器 if 条件树，samples 行 `if_rule.ruleContent` 即现成）
生成**测试接口 `input` 骨架**：字段按 `field_path` 逐层嵌套、叶子值为 `null`，
命中值由测试者手工填写。与 `http://192.168.0.205:8902/test/getInputByRuleId` 返回
对齐（业务对象 `appbase.*` + 动态域集 `dynamicsAttributeList.*` 均收集，便于对照）。

## 目录

| 文件 | 作用 |
|---|---|
| `input_generator.py` | **报文生成器**：ruleContent → input 骨架 |
| `rule_evaluator.py` | **本地规则求值器**：填值后的报文 × ruleContent → 逐条件真值树 + 整体 pass（离线，不依赖接口） |
| `填值指南.md` | HHZ2026941103 填值指南：预定义变量挂载点映射、骨架填值对照表、命中/不命中示例 |
| `test_rule_evaluator.py` | 求值器用例矩阵（HHZ2026941103：正/反例、边界、双分支、同元素绑定、挂载点归一化） |
| `fetch_inputs.py` | 调 ADP `/test/getInputByRuleId` 抓真实骨架存档（用于对照/回归） |
| `_inputs.jsonl` | 已抓取的非车样例 input 骨架存档（生成器回归对照用） |
| `_compare.py` | 本地生成 vs 接口存档 字段一致性对照 |

## 用法

```bash
# 按 ruleId 从 samples 目录自动定位规则并生成
python rule_test/input_generator.py --rule-id YA_030080

# 直接给 ruleContent JSON + predefine 文本
python rule_test/input_generator.py \
  --rule-content '{"nodeType":"LOGICAL", ...}' \
  --predefine "指定'当前险别'为一个…来自(in) '保单'的人身险信息的…列表;"

# 抓取真实骨架（需要有效 adp-token）。默认分层扩样 ~96 条：
#   动态域优先（then/else 引用 dynamicsAttributeList.<域>，每域 --dyn-per-domain 条）
#   + then/else 非空规则 --n-then 条 + 各复杂度配额 method/predefine/deep/simple。
# --append 只追加本次新增（按 rule_id 去重，失败可续跑）。
python rule_test/fetch_inputs.py --token <ADP_TOKEN> \
  --n-simple 15 --n-deep 20 --n-pre 10 --n-method 15 --n-then 20 --dyn-per-domain 6 \
  [--limit 120] [--seed 42] [--append] [--out rule_test/_inputs.jsonl]

# 本地生成 vs 接口存档 对照（跑 _inputs.jsonl 全部样本，输出字段差异 + 类型汇总）
python rule_test/_compare.py [--input rule_test/_inputs.jsonl] [--detail]

# 抓取更大批次（例：各类型 30，动态域每域 10，共 ~150 条）
python rule_test/fetch_inputs.py --token <ADP_TOKEN> \
  --n-simple 30 --n-deep 30 --n-pre 20 --n-method 30 --n-then 30 --dyn-per-domain 10 \
  --append --out rule_test/_inputs.jsonl
```

> 注意：`--append` 只追加，绝不重写已有记录（曾因重写产生重复行）。
> `_compare.py` 会把「本地快照与接口字段集合不相交 / 一侧为空」的记录标记为
> 疑似服务端漂移（ruleId 在 ADP 上已不是快照内容），不计入生成器口径差异。

## 输出示例（input 骨架，叶子待用户填命中值）

```json
{
  "appbase": { "cchasubtype": null },
  "dynamicsAttributeList": { "CchasubType": { "StaticProperty_1900101": null } }
}
```

## 生成规则（对照 ADP 返回归纳）

1. 遍历 ruleContent 收集所有 **FIELD 节点** `fieldPath`（`appbase.*` / `dynamicsAttributeList.*`），
   按 `.` 逐层建嵌套骨架。
2. **METHOD 节点**：收集 `methodPath`（去尾部 `()`，如 `result.hasFeeRuleMatched`），
   **排除 `dynamicsAttributeList.Helper.*`** 辅助方法（接口对照：Helper 不进 input）。
3. **预定义引用展开**：规则里 `规则预定义变量.<别名>[.子字段]` 引用 → 解析 predefine 行
   （`指定'别名'为一个X来自(in) 'root'的…列表;` / `指定'别名'为'root'的字段`），
   输出别名绑定列表/字段的**真实 BOM field_path**（如 `appbase.productLifeInfo06.coverage060032List`）。
4. **then/else 方法参数**：收集 `fieldParsings`/`paramValue` 中 `${<field_path>}` 引用
   （排除 Helper，如 then 引用的动态域枚举 `dynamicsAttributeList.CUWReturnType.CUWReturnType_22` 会进 input）。

## 依赖与换表复用

- 复用 `rule_editor` 的 BomCatalog（`data/bom_catalog.sqlite`）做中文组合路径 → `field_path`
  映射；**不写死任何 BOM 字段/目录名**，换 BOM 表（车险/非车）改 `rule_editor/config.yaml`
  并重跑 ETL 即可复用。
- 生成前需已执行 `python -m rule_editor.etl.run_etl` 生成 sqlite 索引。
