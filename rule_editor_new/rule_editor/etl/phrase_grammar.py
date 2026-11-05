"""短语文法表（M1.4）。

把「运算符短语 ↔ 状态机槽位」固化为模板，负责：
- 子句 → desc 纯文本渲染（字段/值用 BOM 接地的中文 label，不带 <span>）
- desc 纯文本 → 子句解析（整句模式 / 相似规则引用入口）

覆盖句式：
  - 保单的X 属于 动态域集的A的B,动态域集的A的C 中的一个
  - 保单的X 为 动态域集的A的B         （STR_EQ）
  - 保单的X 等于 <值>                 （EQ / DATE_EQ）
  - 保单的X 包含 <值> / 不包含 <值>
  - 保单的X 大于/小于/大于等于/小于等于 <值>
  - 保单的X 为 是/否                   （BOOLEAN_BELONG_TO）
  - 保单的X 不为空 / 为空
  - 并且 / 或者 / 结束                （CONNECTOR）
"""
from __future__ import annotations

import re
from typing import Any

# 运算符 code -> 渲染模板（{obj} 表示对象前缀如"保单的"，{field} 字段label，{value} 值）
# value_mode: single / list / bool / none
OPERATOR_RENDER: dict[str, dict[str, Any]] = {
    "STR_BELONG_TO":   {"tmpl": "{obj}{field} 属于 {value} 中的一个",
                        "value_mode": "list"},
    "STR_NOT_BELONG_TO": {"tmpl": "{obj}{field} 不属于 {value} 中的一个",
                          "value_mode": "list"},
    "STR_EQ":          {"tmpl": "{obj}{field} 是 {value}", "value_mode": "single"},
    "EQ":              {"tmpl": "{obj}{field} 等于 {value}", "value_mode": "single"},
    "NE":              {"tmpl": "{obj}{field} 不等于 {value}", "value_mode": "single"},
    "STR_INCLUDE":     {"tmpl": "{obj}{field} 包含 {value}", "value_mode": "single"},
    "STR_NOT_INCLUDE": {"tmpl": "{obj}{field} 不包含 {value}", "value_mode": "single"},
    "STR_NOT_EMPTY":   {"tmpl": "{obj}{field} 不为空", "value_mode": "none"},
    "STR_EMPTY":       {"tmpl": "{obj}{field} 为空", "value_mode": "none"},
    "STR_START_WITH":  {"tmpl": "{obj}{field} 以 {value} 开头", "value_mode": "single"},
    "STR_END_WITH":    {"tmpl": "{obj}{field} 以 {value} 结尾", "value_mode": "single"},
    "GE":   {"tmpl": "{obj}{field} 大于等于 {value}", "value_mode": "single"},
    "GT":   {"tmpl": "{obj}{field} 大于 {value}", "value_mode": "single"},
    "LE":   {"tmpl": "{obj}{field} 小于等于 {value}", "value_mode": "single"},
    "LT":   {"tmpl": "{obj}{field} 小于 {value}", "value_mode": "single"},
    "DATE_GE": {"tmpl": "{obj}{field} 大于等于 {value}", "value_mode": "single"},
    "DATE_GT": {"tmpl": "{obj}{field} 大于 {value}", "value_mode": "single"},
    "DATE_LE": {"tmpl": "{obj}{field} 小于等于 {value}", "value_mode": "single"},
    "DATE_LT": {"tmpl": "{obj}{field} 小于 {value}", "value_mode": "single"},
    "DATE_EQ": {"tmpl": "{obj}{field} 等于 {value}", "value_mode": "single"},
    "BOOLEAN_BELONG_TO": {"tmpl": "{obj}{field} 为 {value}", "value_mode": "bool"},
    "OBJECT_NOT_EMPTY": {"tmpl": "{obj}{field} 不为空", "value_mode": "none"},
    "OBJECT_EMPTY":     {"tmpl": "{obj}{field} 为空", "value_mode": "none"},
    "STR_NE":           {"tmpl": "{obj}{field} 不是 {value}", "value_mode": "single"},
    # ---- 运算符924 档1：集合(list)系 + 区间 + 字符串否定式 ----
    "COLLECTION_IS_EMPTY":  {"tmpl": "{obj}{field} 为空",   "value_mode": "none"},
    "COLLECTION_NOT_EMPTY": {"tmpl": "{obj}{field} 不为空", "value_mode": "none"},
    "COLLECTION_ANY_STR":   {"tmpl": "{obj}{field} 包含 {value}",
                             "value_mode": "single"},
    "COLLECTION_NOT_IN_STR": {"tmpl": "{obj}{field} 不包含 {value}",
                              "value_mode": "single"},
    "COLLECTION_ANY_BELONG_TO": {"tmpl": "{obj}{field} 部分属于 {value}",
                                 "value_mode": "single"},
    "COLLECTION_ALL_BELONG_TO": {"tmpl": "{obj}{field} 全部属于 {value}",
                                 "value_mode": "single"},
    "COLLECTION_ANY_CONTAINS": {"tmpl": "{obj}{field} 部分包含 {value}",
                                "value_mode": "single"},
    "COLLECTION_ALL_CONTAINS": {"tmpl": "{obj}{field} 全部包含 {value}",
                                "value_mode": "single"},
    "COLLECTION_NOT_IN_CONTAINS": {"tmpl": "{obj}{field} 不包含集合 {value}",
                                   "value_mode": "single"},
    # 运算符924：区间句式「在 起 和 止 之间」（值内部仍用 "起,止" 规范形态，渲染时转 "起 和 止"）
    "NUM_BETWEEN":  {"tmpl": "{obj}{field} 在 {value} 之间", "value_mode": "range"},
    "DATE_BETWEEN": {"tmpl": "{obj}{field} 在 {value} 之间", "value_mode": "range"},
    "TIME_BETWEEN": {"tmpl": "{obj}{field} 在 {value} 之间", "value_mode": "range"},
    "STR_NOT_START_WITH": {"tmpl": "{obj}{field} 不以 {value} 开头",
                           "value_mode": "single"},
    "STR_NOT_END_WITH":   {"tmpl": "{obj}{field} 不以 {value} 结尾",
                           "value_mode": "single"},
    "METHOD": {"tmpl": "{field}({value})", "value_mode": "none"},
}

# 对象前缀
OBJ_PREFIX = {"保单": "保单的", "动态域集": "动态域集的", "结果": "结果的"}

# 值渲染：动态域集枚举 label（已是完整中文，如"动态域集的业务员归属团队类型的综合销售团队"）
def render_value(clause: dict) -> str:
    mode = OPERATOR_RENDER.get(clause["op_code"], {}).get("value_mode", "single")
    if mode == "list":
        labels = [v.get("label") or "" for v in clause.get("values", [])]
        return ",".join(x for x in labels if x)
    if mode == "bool":
        return clause.get("value_label") or clause.get("value") or ""
    if mode == "range":
        # 内部规范形态 "起,止" → 展示形态「在 起 和 止 之间」的 "起 和 止"
        v = str(clause.get("value") or "")
        if "," in v:
            s, e = v.split(",", 1)
            return f"{s.strip()} 和 {e.strip()}"
        return v
    return str(clause.get("value") or "")


def render_clause(clause: dict) -> str:
    """子句 -> desc 纯文本。

    对象前缀支持多级（如"保单的车辆信息的"）：用 clause['obj_label_path']。
    """
    meta = OPERATOR_RENDER.get(clause["op_code"])
    if not meta:
        return ""
    tmpl = meta["tmpl"]
    # 多级对象前缀
    obj_path = clause.get("obj_label_path") or []
    if obj_path:
        obj = "".join(f"{x}的" for x in obj_path)
    else:
        obj = OBJ_PREFIX.get(clause.get("obj_label") or "", "")
    field = clause.get("field_label") or ""
    # "内容长度"一元函数：在字段后以空格分隔追加"的内容长度"，
    # 使渲染文本「保单的代理人 的内容长度 大于 5」可被再编辑/上传解析回环。
    if clause.get("len_fn") and field:
        field = field + " 的内容长度"
    value = render_value(clause)
    out = tmpl.format(obj=obj, field=field, value=value)
    # 清理多余空白
    return re.sub(r"\s+", " ", out).strip()


# ---- 反向解析：desc 文本 -> 槽位 ----
# 运算符短语（用于整句切分）
OPERATOR_PHRASES = [
    "属于", "其中一个", "不包含集合", "不包含", "包含", "大于等于", "小于等于",
    "大于", "小于", "等于", "不为空", "为空", "开头", "结尾",
    "部分属于", "全部属于", "部分包含", "全部包含", "介于", "不是", "不以",
]
_OP_REGEX = "|".join(re.escape(p) for p in
                     ["属于", "大于等于", "小于等于", "大于", "小于",
                      "等于", "包含", "不包含", "不为空", "为空"])


def split_into_clauses(desc: str) -> list[tuple[str, str]]:
    """把整句 desc 切成 (连接符, 子句文本) 列表。

    连接符：并且 / 或者（用于状态机 CONNECTOR 槽位）。
    """
    # 用 并且/或者 切分，同时保留连接符
    pattern = re.compile(r"(并且|或者|，并且|,并且)")
    parts = []
    last_end = 0
    for m in pattern.finditer(desc):
        clause_txt = desc[last_end:m.start()].strip().strip("，, ")
        conn = m.group(1).replace("，", "").replace(",", "").strip()
        if clause_txt:
            parts.append((conn, clause_txt))
        last_end = m.end()
    tail = desc[last_end:].strip().strip("，, ")
    if tail:
        parts.append(("结束", tail))
    return parts
