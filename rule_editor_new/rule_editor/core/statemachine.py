"""状态机引擎（M2.1）。

引导式编辑（对象逐级下钻）：
    START → IF(如果) → OBJECT(一级对象) → OBJECT_PATH(对象内下钻：
      子对象 继续下钻 / 叶子字段 → FIELD)
    → OPERATOR → VALUE → CONNECTOR(并且/或者/结束) → OBJECT → …

EditorSession 持有一次编辑会话的完整状态：
- steps: 已选槽位列表，每个 {state, label, ...}
- 当前活跃状态 current_state
- 上下文（当前对象、当前字段）供候选生成器使用
- 支持 back() 回退、build_desc() 输出 BOM 接地纯文本 desc

槽位（state）：
    START, IF, OBJECT, OBJECT_PATH, FIELD, OPERATOR, VALUE, CONNECTOR, DONE
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class State(str, Enum):
    START = "START"
    IF = "IF"
    OBJECT = "OBJECT"          # 选一级对象（保单/结果/动态域集）
    OBJECT_PATH = "OBJECT_PATH"  # 对象路径内下钻（子对象+叶子字段混合候选）
    FIELD = "FIELD"            # 已锁定一个叶子字段，选运算符
    OPERATOR = "OPERATOR"
    VALUE = "VALUE"
    CONNECTOR = "CONNECTOR"
    DONE = "DONE"


# 转移表（advance 里按 slot.state 计算下一状态，这里给出常规后继）
TRANSITIONS: dict[State, list[State]] = {
    State.START: [State.IF],
    State.IF: [State.OBJECT],
    State.OBJECT: [State.OBJECT_PATH],      # 选完一级对象 -> 进入对象内下钻
    State.OBJECT_PATH: [State.OBJECT_PATH, State.FIELD],  # 子对象继续 / 字段选运算符
    State.FIELD: [State.OPERATOR],
    State.OPERATOR: [State.VALUE],
    State.VALUE: [State.CONNECTOR],
    State.CONNECTOR: [State.OBJECT, State.DONE],
    State.DONE: [State.DONE],
}


def _next_state(s: State) -> State:
    return TRANSITIONS.get(s, [State.DONE])[0]


class Slot:
    """一个已选槽位。"""

    def __init__(self, state: State, label: str = "", **kw):
        self.state = state
        self.label = label
        self.data: dict[str, Any] = kw

    def to_dict(self) -> dict:
        return {"state": self.state.value, "label": self.label, **self.data}


class EditorSession:
    """规则编辑会话（一次编辑 = 一个实例）。"""

    def __init__(self):
        self.steps: list[Slot] = []
        self._state: State = State.START
        # 值下钻（动态域集枚举浏览）：不改变主状态流，仅在 VALUE 状态下切换候选
        self._value_drilling = False
        self._value_drill_path = ""
        # 多值累积（STR_BELONG_TO 属于...中的一个）
        self._pending_values: list[dict] = []
        # 预定义变量：{变量名: 绑定的 bom 对象 fieldPath}
        self.predefines: dict[str, str] = {}

    # ---------- 预定义变量 ----------
    def add_predefine(self, name: str, bound_path: str) -> None:
        """新增/更新一个预定义变量：name 等价绑定到 bom 对象 bound_path。"""
        self.predefines[name] = bound_path

    def remove_predefine(self, name: str) -> None:
        self.predefines.pop(name, None)

    # ---------- 状态 ----------
    @property
    def state(self) -> State:
        return self._state

    def current(self) -> State:
        return self._state

    # ---------- 值下钻 ----------
    @property
    def value_drilling(self) -> bool:
        return self._value_drilling

    @property
    def value_drill_path(self) -> str:
        return self._value_drill_path

    def start_value_drill(self, path: str) -> None:
        """开始/更新动态域集值下钻路径。"""
        self._value_drilling = True
        self._value_drill_path = path

    def finish_value_drill(self) -> None:
        """选中枚举值后结束值下钻。"""
        self._value_drilling = False
        self._value_drill_path = ""

    # ---------- 多值累积（STR_BELONG_TO 属于...中的一个） ----------
    @property
    def pending_values(self) -> list[dict]:
        """当前子句已累积选中的值（STR_BELONG_TO 多值）。"""
        if self._pending_values is None:
            self._pending_values = []
        return self._pending_values

    def add_pending_value(self, value: dict) -> None:
        """追加一个值到当前子句。"""
        if self._pending_values is None:
            self._pending_values = []
        if value.get("label") not in [v.get("label") for v in self._pending_values]:
            self._pending_values.append(value)

    def clear_pending_values(self) -> None:
        """清空（子句提交/回退时）。"""
        self._pending_values = []

    # ---------- 上下文（供候选生成） ----------
    @property
    def current_object(self) -> Optional[dict]:
        """当前选中的 OBJECT 槽位（返回 {label, field_path, path}）。"""
        for s in reversed(self.steps):
            if s.state == State.OBJECT:
                return {
                    "label": s.label,
                    "field_path": s.data.get("field_path") or s.data.get("path") or "",
                    "path": s.data.get("path") or s.data.get("field_path") or "",
                }
        return None

    @property
    def current_object_path(self) -> Optional[dict]:
        """当前对象下钻路径：{field_path: 'appbase.vehicleInfo', labels: ['保单','车辆信息']}。

        bom 的 field_path 是自根到节点的完整路径，故取**最后一个** object 类槽位的
        完整 field_path 即为当前下钻位置。labels 为该路径上各层对象的中文名。
        """
        seg_paths: list[str] = []
        seg_labels: list[str] = []
        for s in self.steps:
            if s.state == State.OBJECT:
                seg_paths.append(s.data.get("field_path") or s.data.get("path") or "")
                seg_labels.append(s.label)
            elif s.state == State.OBJECT_PATH and (s.data.get("kind") or "object") == "object":
                seg_paths.append(s.data.get("field_path") or s.data.get("path") or "")
                seg_labels.append(s.label)
        if not seg_paths:
            return None
        # 去重：field_path 是全路径，可能出现重复前缀（如 appbase + appbase.vehicleInfo）
        # 取最后一个非空路径作为当前下钻位置
        cur_path = next((p for p in reversed(seg_paths) if p), "")
        return {
            "field_path": cur_path,
            "labels": seg_labels,
        }

    @property
    def current_field(self) -> Optional[dict]:
        for s in reversed(self.steps):
            if s.state == State.FIELD:
                return {
                    "label": s.label,
                    "field_path": s.data.get("field_path") or s.data.get("path") or "",
                    "path": s.data.get("path") or s.data.get("field_path") or "",
                    "field_id": s.data.get("field_id") or "",
                    "type": s.data.get("type") or "",
                }
        return None

    @property
    def current_operator(self) -> Optional[dict]:
        for s in reversed(self.steps):
            if s.state == State.OPERATOR:
                return {
                    "label": s.label,
                    "code": s.data.get("code") or "",
                    "value": s.data.get("value"),
                }
        return None

    @property
    def last_clause(self) -> Optional[dict]:
        """最近一个完整子句（FIELD+OPERATOR+VALUE 完整）。"""
        return self.steps[-1].data.get("clause") if self.steps else None

    @property
    def clauses(self) -> list[dict]:
        """已完成的子句序列（每个含 field/operator/value）。"""
        out = []
        for s in self.steps:
            if s.state == State.VALUE and s.data.get("clause"):
                out.append(s.data["clause"])
        return out

    # ---------- 转移 ----------
    def advance(self, slot: Slot) -> State:
        """提交一个槽位并推进状态机。下一状态由 slot.state 决定。"""
        # 起始必须选 IF
        if self._state == State.START and slot.state != State.IF:
            raise ValueError("必须先选择『如果』开始")
        # CONNECTOR 槽位：且/或 -> OBJECT，结束 -> DONE
        if slot.state == State.CONNECTOR:
            conn = (slot.data.get("conn") or slot.label or "")
            self.steps.append(slot)
            self._state = State.OBJECT if conn in ("并且", "或者") else State.DONE
            return self._state
        # DONE
        if slot.state == State.DONE:
            self.steps.append(slot)
            self._state = State.DONE
            return self._state
        # OBJECT_PATH 槽位：子对象继续下钻 / 叶子字段切运算符
        if slot.state == State.OBJECT_PATH:
            kind = slot.data.get("kind") or "object"
            self.steps.append(slot)
            if kind == "field":
                self._state = State.FIELD
            else:
                self._state = State.OBJECT_PATH
            return self._state
        # OBJECT（一级对象）-> 进入对象内下钻
        if slot.state == State.OBJECT:
            self.steps.append(slot)
            self._state = State.OBJECT_PATH
            return self._state

        self.steps.append(slot)
        self._state = _next_state(slot.state)
        return self._state

    def back(self) -> State:
        """回退一步（同时清除值下钻/多值累积等临时状态）。

        值下钻（已选值但未提交）时按 Backspace：先退出值下钻，不删除已选槽位；
        多值累积中按 Backspace：先清除累积值，不删除运算符。
        """
        if self.value_drilling:
            # 值下钻中：先退出下钻，回到空输入的值选择
            self.finish_value_drill()
            return self._state
        if self.pending_values:
            self.clear_pending_values()
            return self._state
        if not self.steps:
            self._state = State.START
            return self._state
        self.steps.pop()
        if not self.steps:
            self._state = State.START
            return self._state
        last = self.steps[-1].state
        if last == State.IF:
            self._state = State.OBJECT
        elif last == State.OBJECT:
            self._state = State.OBJECT_PATH
        elif last == State.OBJECT_PATH:
            self._state = State.OBJECT_PATH
        elif last == State.FIELD:
            self._state = State.OPERATOR
        elif last == State.OPERATOR:
            self._state = State.VALUE
        elif last == State.VALUE:
            self._state = State.CONNECTOR
        elif last == State.CONNECTOR:
            self._state = State.OBJECT
        else:
            self._state = State.DONE
        return self._state

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "state": self._state.value,
            "steps": [s.to_dict() for s in self.steps],
            "clauses": self.clauses,
            "current_object": self.current_object,
            "current_field": self.current_field,
            "current_operator": self.current_operator,
            "pending_values": self.pending_values,
            "value_drilling": self.value_drilling,
            "predefines": self.predefines,
        }

    def desc(self) -> str:
        """输出 BOM 接地的纯文本 desc（由 desc_codec 渲染）。"""
        from .desc_codec import render_session
        return render_session(self)
