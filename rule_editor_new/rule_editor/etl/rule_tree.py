"""规则树解析器（共享工具）。

把 ruleContent（JSON 树字符串）解析为标准化的「子句序列」，供：
- 状态机条件频次统计（P(FIELD|OBJECT) 等）
- 值词典挖掘（CUSTOM 字面量、动态域集枚举引用）
- desc_codec（desc ⇄ 状态机槽位）

子句结构：
    {obj_label, obj_field_path, field_label, field_id, field_path, field_type,
     op_code, op_label, value, value_field_id, value_label, values: [..]}
"""
from __future__ import annotations

import json
from typing import Any, Iterator

# 运算符 code -> (标准中文短语文案, 值处理方式)
# value_mode: "single"(单值) / "list"(多个动态域集引用) / "bool"(是/否)
OPERATOR_META: dict[str, dict[str, Any]] = {
    "STR_BELONG_TO": {"label": "属于...中的一个", "value_mode": "list"},
    "STR_NOT_BELONG_TO": {"label": "不属于...中的一个", "value_mode": "list"},
    "STR_EQ":        {"label": "是",             "value_mode": "single"},
    "EQ":            {"label": "等于",           "value_mode": "single"},
    "NE":            {"label": "不等于",         "value_mode": "single"},
    "STR_INCLUDE":   {"label": "包含",           "value_mode": "single"},
    "STR_NOT_INCLUDE": {"label": "不包含",       "value_mode": "single"},
    "STR_NOT_EMPTY": {"label": "不为空",         "value_mode": "none"},
    "STR_EMPTY":     {"label": "为空",           "value_mode": "none"},
    "STR_START_WITH": {"label": "以...开头",     "value_mode": "single"},
    "STR_END_WITH":  {"label": "以...结尾",      "value_mode": "single"},
    "GE": {"label": "大于等于", "value_mode": "single"},
    "GT": {"label": "大于",     "value_mode": "single"},
    "LE": {"label": "小于等于", "value_mode": "single"},
    "LT": {"label": "小于",     "value_mode": "single"},
    "DATE_GE": {"label": "大于等于", "value_mode": "single"},
    "DATE_GT": {"label": "大于",     "value_mode": "single"},
    "DATE_LE": {"label": "小于等于", "value_mode": "single"},
    "DATE_LT": {"label": "小于",     "value_mode": "single"},
    "DATE_EQ": {"label": "等于",     "value_mode": "single"},
    "BOOLEAN_BELONG_TO": {"label": "为", "value_mode": "bool"},
    "OBJECT_NOT_EMPTY": {"label": "不为空", "value_mode": "none"},
    "OBJECT_EMPTY":     {"label": "为空",   "value_mode": "none"},
    "STR_NE":          {"label": "不是",   "value_mode": "single"},
    # ---- 运算符924 档1：集合(list)系 + 区间 + 字符串否定式 ----
    "COLLECTION_IS_EMPTY":  {"label": "为空",       "value_mode": "none"},
    "COLLECTION_NOT_EMPTY": {"label": "不为空",     "value_mode": "none"},
    "COLLECTION_ANY_STR":   {"label": "包含",       "value_mode": "single"},
    "COLLECTION_NOT_IN_STR": {"label": "不包含",    "value_mode": "single"},
    "COLLECTION_ANY_BELONG_TO":   {"label": "部分属于",   "value_mode": "single"},
    "COLLECTION_ALL_BELONG_TO":   {"label": "全部属于",   "value_mode": "single"},
    "COLLECTION_ANY_CONTAINS":    {"label": "部分包含",   "value_mode": "single"},
    "COLLECTION_ALL_CONTAINS":    {"label": "全部包含",   "value_mode": "single"},
    "COLLECTION_NOT_IN_CONTAINS": {"label": "不包含集合", "value_mode": "single"},
    "NUM_BETWEEN":  {"label": "在...和...之间", "value_mode": "range"},
    "DATE_BETWEEN": {"label": "在...和...之间", "value_mode": "range"},
    "TIME_BETWEEN": {"label": "在...和...之间", "value_mode": "range"},
    "STR_NOT_START_WITH": {"label": "不以...开头", "value_mode": "single"},
    "STR_NOT_END_WITH":   {"label": "不以...结尾", "value_mode": "single"},
}

# code 别名归一（有些树里 expressionId 全路径）
def norm_operator(code: str | None) -> str | None:
    if not code:
        return None
    if code in OPERATOR_META:
        return code
    # 去掉全路径前缀，取最后一段
    return code


def parse_rule_content(content: str) -> dict | None:
    """解析 ruleContent 字符串为 dict；失败返回 None。"""
    if not content:
        return None
    if isinstance(content, dict):
        return content
    try:
        return json.loads(content)
    except Exception:
        return None


def _field_of(node: dict) -> dict | None:
    """提取 FIELD 节点信息。"""
    if not isinstance(node, dict):
        return None
    if node.get("nodeType") == "FIELD":
        return {
            "label": node.get("label") or "",
            "field_id": node.get("fieldId") or "",
            "field_path": node.get("fieldPath") or "",
            "type": node.get("type") or node.get("returnType") or "",
            "value": node.get("value"),
        }
    return None


def _value_of(node: dict, op_code: str) -> dict:
    """从运算符节点提取值信息。"""
    meta = OPERATOR_META.get(op_code, {"label": op_code, "value_mode": "single"})
    mode = meta["value_mode"]

    # range 模式（在 起 和 止 之间）：param2=起 param3=止 → 内部规范形态 "起,止"
    if mode == "range":
        pv = node.get("paramValue") or {}

        def _lit(n) -> str:
            if isinstance(n, dict):
                v = n.get("value") or n.get("label")
                if v in (None, "") and isinstance(
                        n.get("paramValue"), (str, int, float)):
                    v = n.get("paramValue")
                return "" if v is None else str(v)
            return "" if n is None else str(n)

        s, e = _lit(pv.get("param2")), _lit(pv.get("param3"))
        val = f"{s},{e}" if (s or e) else None
        return {"value": val, "value_field_id": "", "value_label": val,
                "value_field_path": "", "values": []}

    # BOOLEAN_BELONG_TO: paramValue.param2 = {label:是, key:true}
    if mode == "bool":
        pv = node.get("paramValue") or {}
        p2 = pv.get("param2") or {}
        return {
            "value": p2.get("label") or p2.get("key"),
            "value_field_id": p2.get("fieldId") or "",
            "value_label": p2.get("label"),
            "value_field_path": p2.get("fieldPath") or "",
            "values": [],
        }

    # list 模式：param2 是数组（动态域集引用）
    if mode == "list":
        pv = node.get("paramValue") or {}
        vals = pv.get("param2")
        values = []
        if isinstance(vals, list):
            for v in vals:
                if isinstance(v, dict):
                    values.append({
                        "label": v.get("label") or "",
                        "field_id": v.get("fieldId") or "",
                        "field_path": v.get("fieldPath") or "",
                    })
                elif v == ",":
                    continue
                else:
                    values.append({"label": str(v), "field_id": "", "field_path": ""})
        else:
            values = []
        return {
            "value": None,
            "value_field_id": "",
            "value_label": None,
            "value_field_path": "",
            "values": values,
        }

    # single / none：COMPARISON 的 right 是 CUSTOM 或 FIELD；EXPRESSION 的 param2 是值
    pv = node.get("paramValue")
    right = node.get("right")

    def extract(n) -> dict:
        if not isinstance(n, dict):
            return {"value": n, "value_field_id": "",
                    "value_label": n, "value_field_path": "", "values": []}
        if n.get("nodeType") == "FIELD":
            return {
                "value": n.get("label"),
                "value_field_id": n.get("fieldId") or "",
                "value_label": n.get("label"),
                "value_field_path": n.get("fieldPath") or "",
                "values": [],
            }
        if n.get("nodeType") == "CUSTOM":
            # ADP 字面量可能存于 value/label，也可能直接是 paramValue 标量
            # （如 NE 的 right={nodeType:CUSTOM, paramValue:"0"}）
            val = n.get("value") or n.get("label")
            if val in (None, "") and isinstance(n.get("paramValue"), (str, int, float, bool)):
                val = n.get("paramValue")
            return {"value": val,
                    "value_field_id": "", "value_label": val,
                    "value_field_path": "", "values": []}
        return {"value": None, "value_field_id": "",
                "value_label": None, "value_field_path": "", "values": []}

    if isinstance(pv, dict) and "param2" in pv:
        return extract(pv["param2"])
    if right is not None:
        return extract(right)
    return {"value": None, "value_field_id": "",
            "value_label": None, "value_field_path": "", "values": []}


def _left_field_of(node: dict) -> dict | None:
    """提取运算符节点的被比较字段（left / paramValue.param1）。
    并穿透"内容长度"一元函数（left=EXPRESSION STR_LENGTH/SIZE_OF_LIST → param1 字段），
    修复此前 left=EXPRESSION 时字段整个丢失的盲点。"""
    if not isinstance(node, dict):
        return None
    left = node.get("left")
    if isinstance(left, dict):
        if left.get("nodeType") == "FIELD":
            return _field_of(left)
        if left.get("nodeType") == "EXPRESSION" \
                and left.get("expressionId") in ("STR_LENGTH", "SIZE_OF_LIST"):
            p1 = (left.get("paramValue") or {}).get("param1")
            if isinstance(p1, dict) and p1.get("nodeType") == "FIELD":
                return _field_of(p1)
    pv = node.get("paramValue")
    if isinstance(pv, dict):
        p1 = pv.get("param1")
        if isinstance(p1, dict) and p1.get("nodeType") == "FIELD":
            return _field_of(p1)
    return None


def _is_len_expr(node: dict) -> bool:
    """COMPARISON 左操作数是否为"内容长度"一元函数（STR_LENGTH/SIZE_OF_LIST）。"""
    if not isinstance(node, dict):
        return False
    left = node.get("left")
    return (isinstance(left, dict) and left.get("nodeType") == "EXPRESSION"
            and left.get("expressionId") in ("STR_LENGTH", "SIZE_OF_LIST"))


def iter_clauses(node: dict, path: str = "") -> Iterator[dict]:
    """把一棵规则树展开为子句序列。

    返回顺序即状态机选择顺序（不含 LOGICAL 连接符，连接符由 tree 结构推导）。
    """
    if not isinstance(node, dict):
        return
    node_type = node.get("nodeType")
    code = norm_operator(node.get("code") or node.get("expressionId"))

    if node_type == "LOGICAL":
        for child in (node.get("left"), node.get("right")):
            if isinstance(child, dict):
                yield from iter_clauses(child, path)
        return

    # EXPRESSION / COMPARISON / METHOD 叶子
    if code and code in OPERATOR_META:
        field = _left_field_of(node)
        value = _value_of(node, code)
        clause = {
            "obj_label": _obj_label(field),
            "obj_field_path": _obj_field_path(field),
            "field_label": field["label"] if field else "",
            "field_id": field["field_id"] if field else "",
            "field_path": field["field_path"] if field else "",
            "field_type": field["type"] if field else "",
            "op_code": code,
            "op_label": OPERATOR_META[code]["label"],
            **value,
        }
        # 左操作数被"内容长度"一元函数包裹 → 渲染时字段后插"的内容长度"
        if _is_len_expr(node):
            clause["len_fn"] = True
        yield clause
        return

    # 其他（METHOD 自定义方法）：作为 METHOD 子句保留
    if node_type == "METHOD" or (node_type == "EXPRESSION" and not code):
        yield {
            "obj_label": "", "obj_field_path": "",
            "field_label": node.get("methodName") or node.get("label") or "",
            "field_id": node.get("fieldId") or "",
            "field_path": node.get("methodPath") or node.get("path") or "",
            "field_type": node.get("returnType") or "",
            "op_code": "METHOD", "op_label": "调用方法",
            "value": None, "value_field_id": "", "value_label": None,
            "value_field_path": "", "values": [],
        }


def _obj_label(field: dict | None) -> str:
    """从 field_path 推导对象 label（取第一段，不依赖固定名称）。

    非 bom 顶层（如"规则预定义变量"、"方法参数"）返回空，避免污染对象频次统计。
    """
    return _obj_field_path(field)


def _obj_field_path(field: dict | None) -> str:
    """字段所属顶层对象路径（用于状态机对象频次统计）。

    取 field_path 的第一段作为顶层对象（结构通用，不依赖固定名称）。
    排除非 bom 顶层（"规则预定义变量"、"方法参数"等）。
    """
    if not field:
        return ""
    p = field.get("field_path") or ""
    if not p:
        return ""
    first = p.split(".")[0]
    if first in ("规则预定义变量", "方法参数", "当前规则"):
        return ""
    return first


def iter_clauses_with_connects(node: dict):
    """遍历树，产出 (子句, 之后连接符)。连接符 ∈ {并且, 或者, 结束}。

    规则：
    - 叶子子句（独立）：之后连接符 = "结束"
    - LOGICAL 左子树中的叶子：其后连接符 = 该 LOGICAL 的 label
    - LOGICAL 右子树中的叶子：其后连接符 = 由外层决定（若无外层则"结束"）
    """
    def walk(n, after_conn):
        """after_conn: 当前节点整体完成后的连接符（供右子树使用）。"""
        if not isinstance(n, dict):
            return
        if n.get("nodeType") == "LOGICAL":
            lab = n.get("label") or "并且"
            left = n.get("left")
            right = n.get("right")
            if isinstance(left, dict):
                walk(left, lab)
            elif isinstance(left, list):
                for it in left:
                    for c in iter_clauses(it):
                        yield (c, lab)
            if isinstance(right, dict):
                walk(right, after_conn)
            elif isinstance(right, list):
                for it in right:
                    for c in iter_clauses(it):
                        yield (c, after_conn)
            return
        # 叶子（EXPRESSION/COMPARISON/METHOD）
        for c in iter_clauses(n):
            yield (c, after_conn)

    yield from walk(node, "结束")


def _leaf_clauses(n: dict) -> list[dict]:
    """收集 n 下所有叶子子句（按 iter_clauses 顺序）。"""
    return list(iter_clauses(n))


def connectives(tree: dict) -> list[str]:
    """返回规则树的逻辑连接符序列（如 ['并且','或者']）。"""
    out = []

    def walk(n):
        if not isinstance(n, dict):
            return
        if n.get("nodeType") == "LOGICAL":
            lab = n.get("label") or ""
            out.append(lab)
            walk(n.get("left"))
            walk(n.get("right"))
        # 非 LOGICAL 不递归下钻连接符
    walk(tree)
    return out
