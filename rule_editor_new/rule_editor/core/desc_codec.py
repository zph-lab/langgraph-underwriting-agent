"""desc_codec（M2.3）：状态机选择序列 ⇄ desc 纯文本。

- render_session(session) -> desc：把已选槽位渲染为 BOM 接地的纯文本 desc（不带 <span>）。
- parse_desc(desc) -> 子句序列：整句模式/相似规则引用入口，用短语模板解析。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..etl.phrase_grammar import (
    OPERATOR_RENDER, OBJ_PREFIX, render_clause, split_into_clauses,
)
from .statemachine import EditorSession, Slot, State


def _obj_prefix(obj_labels: list[str]) -> str:
    """对象层级中文前缀，如 ['保单','验车信息'] -> '保单的验车信息的'。"""
    return "".join(f"{x}的" for x in obj_labels if x)


def render_session(session: EditorSession) -> str:
    """渲染会话为 desc 纯文本（实时累积：字段/运算符/值逐步渲染）。

    每个完整子句（FIELD+OPERATOR+VALUE）渲染为一行完整子句；
    未完成值/运算符的子句也实时渲染出已选部分，保证 desc 随时反映全部输入。
    """
    segments: list[dict] = []   # 每个元素: {text, kind}，kind ∈ {clause, field, op, conn}
    obj_labels: list[str] = []
    cur_field: dict = {}
    cur_op: dict = {}

    def flush_clause(value_text: str = "", pending_values: Optional[list] = None) -> None:
        """把当前构建中的子句输出为一段。

        pending_values: 多值运算符（属于...中的一个）已选的值列表（逗号分隔实时累积）。
        """
        prefix = _obj_prefix(obj_labels)
        field_label = cur_field.get("field_label", "")
        op_code = cur_op.get("op_code", "")
        op_label = cur_op.get("op_label", "")
        if pending_values:
            # 多值累积中：用 render_clause 保证语序正确（属于 值1,值2 中的一个）
            vals = [v.get("label", "") for v in pending_values if v.get("label")]
            clause = {
                "obj_label_path": list(obj_labels),
                "field_label": field_label,
                "field_id": cur_field.get("field_id", ""),
                "field_path": cur_field.get("field_path", ""),
                "field_type": cur_field.get("field_type", ""),
                "op_code": op_code,
                "op_label": op_label,
                "value": ",".join(vals),
                "value_label": ",".join(vals),
                "values": [{"label": v} for v in vals],
            }
            rendered = render_clause(clause)
            segments.append({"text": rendered or f"{prefix}{field_label} {op_label}", "kind": "incomplete"})
        elif value_text:
            clause = {
                "obj_label_path": list(obj_labels),
                "field_label": field_label,
                "field_id": cur_field.get("field_id", ""),
                "field_path": cur_field.get("field_path", ""),
                "field_type": cur_field.get("field_type", ""),
                "op_code": op_code,
                "op_label": op_label,
                "value": value_text,
                "value_field_id": "",
                "value_label": value_text,
                "value_field_path": "",
                "values": [{"label": value_text}] if op_code in
                          ("STR_BELONG_TO", "STR_NOT_BELONG_TO") else [],
            }
            segments.append({"text": render_clause(clause), "kind": "clause"})
        else:
            # 未完成值：渲染"对象路径.字段 运算符"
            text = f"{prefix}{field_label}" + (f" {op_label}" if op_label else "")
            segments.append({"text": text, "kind": "incomplete"})

    for s in session.steps:
        st = s.state
        if st == State.IF:
            segments.append({"text": "如果", "kind": "conn"})
        elif st == State.OBJECT:
            obj_labels = [s.label]
            cur_field = {}
            cur_op = {}
        elif st == State.OBJECT_PATH:
            if (s.data.get("kind") or "object") == "object":
                # label 可能含"的"（如"保单的投保人"整体候选），按"的"拆成多级对象
                for seg in s.label.split("的"):
                    if seg:
                        obj_labels.append(seg)
        elif st == State.FIELD:
            # 进入新子句，若上一子句未完成（值缺）先不 flush，直接更新字段
            cur_field = {
                "obj_label_path": list(obj_labels),
                "field_label": s.label,
                "field_id": s.data.get("field_id", ""),
                "field_path": s.data.get("path", ""),
                "field_type": s.data.get("type", ""),
            }
            cur_op = {}
        elif st == State.OPERATOR:
            cur_op = {
                "op_code": s.data.get("code", ""),
                "op_label": s.label,
            }
        elif st == State.VALUE:
            meta = OPERATOR_RENDER.get(cur_op.get("op_code", ""), {})
            value_text = str(s.data.get("value") or s.data.get("label") or "")
            flush_clause(value_text)
            cur_field = {}
            cur_op = {}
        elif st == State.CONNECTOR:
            if s.label in ("并且", "或者"):
                # 该子句如果值未填（异常路径），flush 出当前进度
                if cur_field.get("field_label"):
                    flush_clause()
                elif obj_labels:
                    # 已下钻对象但未选字段：渲染对象路径前缀（如"保单的投保人"）
                    segments.append({"text": "的".join(obj_labels), "kind": "incomplete"})
                cur_field = {}
                cur_op = {}
                segments.append({"text": s.label, "kind": "conn"})
    # 结尾：若还有未完成子句，flush 显示当前进度
    if cur_field.get("field_label"):
        flush_clause(pending_values=session.pending_values)
    elif obj_labels:
        # 已下钻对象但未选字段：渲染对象路径前缀
        segments.append({"text": "的".join(obj_labels), "kind": "incomplete"})
    return " ".join(seg["text"] for seg in segments if seg["text"]).strip()


def parse_desc(desc: str) -> list[dict]:
    """把整句 desc 切分为子句列表（用于相似规则引用/整句模式）。

    返回：[{obj, field_label, field_path, op_code, value, ...}]
    """
    out = []
    chunks = split_into_clauses(desc)
    for conn, clause_text in chunks:
        c = _parse_single_clause(clause_text)
        if c:
            c["conn"] = conn
            out.append(c)
    return out


def _parse_single_clause(text: str) -> Optional[dict]:
    """解析单个子句文本为槽位信息。"""
    text = text.strip()
    # 识别对象前缀
    obj_label = ""
    for o, prefix in OBJ_PREFIX.items():
        if text.startswith(prefix):
            obj_label = o
            text = text[len(prefix):]
            break
    # 识别运算符短语
    for code, meta in OPERATOR_RENDER.items():
        tmpl = meta["tmpl"]
        op_label = code  # fallback
        # 匹配运算符关键词
    # 用关键词匹配
    # 子串匹配、首个命中即止：否定式必须排在其肯定式之前，
    # 否则"不属于"会被"属于"、"不等于"会被"等于"、"不包含"会被"包含"抢先误配。
    # "不以 X 开头/结尾"：靠"不以"子串无法区分开/结尾，正则按尾词定 code
    m_notwith = re.search(r"不以(.*?)(开头|结尾)", text)
    if m_notwith:
        idx = text.find("不以")
        return {
            "obj_label": obj_label,
            "field_label": text[:idx].strip(),
            "op_code": "STR_NOT_START_WITH" if m_notwith.group(2) == "开头"
                       else "STR_NOT_END_WITH",
            "value": m_notwith.group(1).strip(),
            "conn": "结束",
        }
    # 区间句式（运算符924「在 起 和 止 之间」）：三段短语靠子串匹配无法定位，
    # 单独正则；value 归一为 "起,止"（与旧词形"介于 起,止"输出同构，下游不感知）
    m_between = re.search(r"在(.+?)和(.+?)之间", text)
    if m_between:
        idx = text.find("在")
        return {
            "obj_label": obj_label,
            "field_label": text[:idx].strip(),
            "op_code": "DATE_BETWEEN",
            "value": f"{m_between.group(1).strip()},{m_between.group(2).strip()}",
            "conn": "结束",
        }
    # 注意语序：长否定短语必须排在其短前缀短语之前
    # （"不包含集合"先于"不包含"，"部分/全部属于"先于"属于"，"不是"先于"是"）。
    op_map = [
        ("COLLECTION_NOT_IN_CONTAINS", "不包含集合"),
        ("STR_NOT_BELONG_TO", "不属于"),
        ("COLLECTION_ALL_BELONG_TO", "全部属于"),
        ("COLLECTION_ANY_BELONG_TO", "部分属于"),
        ("STR_BELONG_TO", "属于"),
        ("COLLECTION_ALL_CONTAINS", "全部包含"),
        ("COLLECTION_ANY_CONTAINS", "部分包含"),
        ("STR_NOT_INCLUDE", "不包含"), ("STR_INCLUDE", "包含"),
        ("GE", "大于等于"), ("LE", "小于等于"),
        ("NE", "不等于"), ("GT", "大于"), ("LT", "小于"), ("EQ", "等于"),
        ("STR_NE", "不是"), ("STR_EQ", "是"),
        ("STR_NOT_EMPTY", "不为空"), ("STR_EMPTY", "为空"),
        ("DATE_BETWEEN", "介于"),
        ("STR_START_WITH", "开头"), ("STR_END_WITH", "结尾"),
    ]
    op_code = None
    value = None
    for code, kw in op_map:
        if kw in text:
            op_code = code
            idx = text.find(kw)
            field_label = text[:idx].strip()
            if code in ("STR_BELONG_TO", "STR_NOT_BELONG_TO"):
                rest = text[idx + len(kw):].replace("中的一个", "").strip()
                value = rest
            else:
                value = text[idx + len(kw):].strip()
            break
    if op_code is None:
        return None
    return {
        "obj_label": obj_label,
        "field_label": field_label,
        "op_code": op_code,
        "value": value,
        "conn": "结束",
    }
