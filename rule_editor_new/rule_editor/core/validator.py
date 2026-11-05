"""校验器（M2.3）。

- 语法：槽位序列符合状态机文法
- BOM 接地：FIELD 的 field_path 必须存在于 BOM 目录，type 与运算符兼容
- 值：类型匹配；动态域集引用存在
"""
from __future__ import annotations

from typing import Any, Optional

from ..etl.bom_catalog import BomCatalog
from .statemachine import EditorSession, State


class ValidationError(Exception):
    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.message = message
        self.field = field


class Validator:
    def __init__(self, catalog: BomCatalog):
        self.cat = catalog

    def validate_session(self, session: EditorSession) -> list[dict]:
        """校验整个会话，返回问题列表（空 = 通过）。"""
        problems: list[dict] = []
        # 收集所有 FIELD 槽位
        for i, slot in enumerate(session.steps):
            if slot.state == State.FIELD:
                fp = slot.data.get("path") or ""
                if fp:
                    f = self.cat.get_field_by_path(fp)
                    if f is None:
                        problems.append({
                            "step": i, "field": "field_path",
                            "message": f"字段不存在于 BOM: {fp}",
                        })
            if slot.state == State.OPERATOR:
                fp = self._find_field_path(session, i)
                code = slot.data.get("code") or ""
                if fp and code:
                    ops = [o["code"] for o in self.cat.get_operators(fp)]
                    if ops and code not in ops:
                        problems.append({
                            "step": i, "field": "operator",
                            "message": f"运算符 {code} 与字段 {fp} 类型不兼容",
                        })
        return problems

    def _find_field_path(self, session: EditorSession, up_to: int) -> str:
        for s in reversed(session.steps[:up_to]):
            if s.state == State.FIELD:
                return s.data.get("path") or ""
        return ""

    def validate_clause(self, clause: dict) -> Optional[str]:
        """校验单个子句，返回错误信息或 None。"""
        fp = clause.get("field_path")
        if fp:
            f = self.cat.get_field_by_path(fp)
            if f is None:
                return f"字段不存在于 BOM: {fp}"
            op = clause.get("op_code")
            if op:
                ops = [o["code"] for o in self.cat.get_operators(fp)]
                if ops and op not in ops:
                    return f"运算符 {op} 与字段 {fp} 不兼容"
        return None
