"""候选生成器（M2.2）。

两种模式（共用同一候选池）：
- 模式 A：空输入 · 状态主动提示。候选集与排序完全由「当前状态 + 已选上下文」决定，
  核心排序器是语料条件频次表（P(FIELD|OBJECT)、P(OP|FIELD)、值词典、P(连接|值后)）。
- 模式 B：输入文字 · 对模式 A 候选池做字符匹配过滤/重排（第一级），
  第二级 BM25 兜底由 rag/search.py 提供（语义关闭时跳过）。

任何候选都 BOM 接地：FIELD/VALUE 返回 {label, field_path, field_id, type}。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any, Optional

from ..etl.bom_catalog import BomCatalog
from .statemachine import EditorSession, State


class RuleService:
    """聚合 BOM 目录、值词典、频次表，提供候选。"""

    def __init__(self, bom_catalog: BomCatalog, value_dict: dict,
                 freq_db: str):
        self.cat = bom_catalog
        self.value_dict: dict[str, dict[str, list]] = value_dict
        self.freq_db = freq_db

    # ---------- 频次表查询（线程安全：每次新建连接，用完即关） ----------
    def _freq_rows(self, sql: str, params: tuple = ()):
        """执行查询并返回 rows 列表。每次独立连接，避免跨线程复用问题。"""
        if not self.freq_db:
            return []
        with closing(sqlite3.connect(self.freq_db)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        return rows

    def object_after_if(self, limit: int = 10) -> list[dict]:
        rows = self._freq_rows(
            "SELECT obj_field_path, count FROM freq_object_after_if "
            "ORDER BY count DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            o = self.cat.get_object_by_path(r["obj_field_path"]) or {}
            out.append({
                "label": o.get("label") or r["obj_field_path"],
                "field_path": r["obj_field_path"],
                "type": o.get("type") or "object",
                "count": r["count"],
                "source": "freq",
            })
        return out

    def fields_given_object(self, obj_path: str, limit: int = 20) -> list[dict]:
        rows = self._freq_rows(
            "SELECT field_path, count FROM freq_field_given_object "
            "WHERE obj_field_path=? ORDER BY count DESC LIMIT ?",
            (obj_path, limit))
        out = []
        for r in rows:
            f = self.cat.get_field_by_path(r["field_path"]) or {}
            out.append({
                "label": f.get("label") or r["field_path"],
                "field_path": r["field_path"],
                "field_id": f.get("field_id") or "",
                "type": f.get("type") or "",
                "count": r["count"],
                "source": "freq",
            })
        return out

    # ---------- 候选主入口 ----------
    def suggest(self, session: EditorSession, query: str = "", limit: int = 8) -> list[dict]:
        st = session.state
        query = (query or "").strip()

        # 多级路径整体解析：输入含"的"的完整路径（如"保单的投保人的客户代码"）
        # 时，尝试按 label 逐段在 bom 中定位，直接命中候选（任意深度）。
        if query and "的" in query and st in (State.OBJECT, State.OBJECT_PATH, State.FIELD):
            resolved = self._resolve_path_candidates(query)
            if resolved:
                return resolved

        # 状态 = 下一个待填槽位类型
        if st == State.START:
            pool = self._cand_if()
        elif st == State.IF:
            pool = self._cand_object(session)
        elif st == State.OBJECT:
            pool = self._cand_object(session)   # 一级对象
        elif st == State.OBJECT_PATH:
            pool = self._cand_object_path(session)  # 子对象 + 叶子字段混合下钻
        elif st == State.OPERATOR:
            pool = self._cand_operator(session)  # 待填运算符
        elif st == State.VALUE:
            pool = self._cand_value(session)     # 待填值
        elif st == State.CONNECTOR:
            pool = self._cand_connector(session)  # 并且/或者/结束
        else:
            pool = []

        # 模式 B：输入文字则过滤/重排
        if query:
            pool = self._filter_by_text(pool, query)

        # 运算符/连接符是"小候选集"且必须全量可选（自由编辑原则：语料只排序不限制）。
        # 对这些状态不截断，保证用户能选到任何合法运算符/值，即使语料从未出现。
        if st in (State.OPERATOR, State.CONNECTOR):
            return pool
        # VALUE：有输入（模式B）时全池过滤检索；空输入（模式A）时截断展示
        if st == State.VALUE:
            if query:
                return pool
            return pool[:limit]
        return pool[:limit]

    def _resolve_path_candidates(self, query: str) -> list[dict] | None:
        """多级路径整体解析：按 label 逐段定位 bom 节点（任意深度）。

        输入"保单的投保人的客户代码" -> 返回命中字段候选。
        返回 None 表示无法解析（走普通匹配）。
        """
        segs = [s for s in query.split("的") if s]
        if len(segs) < 2:
            return None
        # 预定义变量路径
        if segs[0] == "规则预定义变量":
            return None  # 由预定义机制处理
        # 从根的一级对象开始逐段定位
        cur_label = segs[0]
        obj = next((o for o in self.cat.get_top_objects() if o.get("label") == cur_label), None)
        if not obj:
            return None
        cur_path = obj["field_path"]
        # 逐段下钻（中间段是对象）
        for seg in segs[1:-1]:
            sub = next((o for o in self.cat.direct_child_objects(cur_path)
                        if o.get("label") == seg), None)
            if not sub:
                return None
            cur_path = sub["field_path"]
        # 最后一段：匹配叶子字段
        last = segs[-1]
        f = next((x for x in self.cat.direct_child_fields(cur_path)
                  if x.get("label") == last), None)
        if f:
            return [{
                "label": query, "field_path": f["field_path"],
                "field_id": f.get("field_id") or "", "type": f.get("type") or "",
                "count": 0, "source": "bom", "state": "FIELD",
                "kind": "field", "match_text": query,
            }]
        # 最后一段也匹配对象（可下钻）
        sub = next((o for o in self.cat.direct_child_objects(cur_path)
                    if o.get("label") == last), None)
        if sub:
            return [{
                "label": query, "field_path": sub["field_path"],
                "field_id": sub.get("field_id") or "", "type": sub.get("type") or "object",
                "count": 0, "source": "bom", "state": "OBJECT_PATH",
                "kind": "object", "match_text": query,
            }]
        return None

    # ---------- 模式 A 候选池 ----------
    def _cand_if(self) -> list[dict]:
        return [{"label": "如果", "state": "IF", "kind": "if",
                 "field_path": "", "source": "static"}]

    def _cand_object(self, session: EditorSession) -> list[dict]:
        # 顶层对象 + 高频子对象
        tops = []
        for o in self.cat.get_top_objects():
            tops.append({
                "label": o["label"], "field_path": o["field_path"],
                "type": o["type"] or "object", "count": 0, "source": "bom",
                "is_top": True,
            })
        freq = self.object_after_if(limit=10)
        # 合并：顶层优先，其次高频；过滤空路径
        seen = set()
        out = []
        for c in tops:
            if c["field_path"] and c["field_path"] not in seen:
                seen.add(c["field_path"])
                out.append(c)
        for c in freq:
            if c["field_path"] and c["field_path"] not in seen:
                seen.add(c["field_path"])
                out.append(c)

        # 展开所有一级对象的直接子对象 + 直接叶子字段，支持"X的"整体检索（结构统一，不依赖名称）。
        # 保单/结果/动态域集等所有一级对象逻辑完全一致。
        for top in self.cat.get_top_objects():
            tlabel = top["label"]
            tpath = top["field_path"]
            # 直接子对象（可继续下钻）
            for o in self.cat.direct_child_objects(tpath)[:30]:
                ofp = o.get("field_path") or ""
                if not ofp or ofp in seen:
                    continue
                seen.add(ofp)
                match_text = tlabel + "的" + (o.get("label") or ofp)
                out.append({
                    "label": match_text, "field_path": ofp,
                    "field_id": o.get("field_id") or "",
                    "type": o.get("type") or "object", "count": 0,
                    "source": "bom", "state": "OBJECT_PATH",
                    "kind": "object", "is_top_child": True,
                    "match_text": match_text,
                })
            # 直接叶子字段（选中即切运算符），match_text 含前缀，如"保单的个团标志"
            for f in self.cat.direct_child_fields(tpath)[:30]:
                fp = f["field_path"]
                if not fp or fp in seen:
                    continue
                seen.add(fp)
                match_text = tlabel + "的" + (f.get("label") or fp)
                out.append({
                    "label": match_text, "field_path": fp,
                    "field_id": f.get("field_id") or "",
                    "type": f.get("type") or "", "count": 0,
                    "source": "bom", "state": "FIELD",
                    "kind": "field", "is_top_child": True,
                    "match_text": match_text,
                })

        # 预定义变量入口：规则预定义变量（与保单/结果/动态域集并列）
        if session and session.predefines:
            out.append({
                "label": "规则预定义变量", "field_path": "规则预定义变量",
                "type": "object", "count": 0, "source": "predefine",
                "is_top": True, "predefine_root": True,
            })
        return out

    def _cand_object_path(self, session: EditorSession) -> list[dict]:
        """对象内下钻候选：直接子对象 + 当前层叶子字段 混合。

        - 子对象（kind=object）：可继续下钻
        - 叶子字段（kind=field）：选中即切到运算符
        语料高频优先（子对象按子树字段频次聚合），但覆盖全部合法节点（自由编辑）。
        """
        op = session.current_object_path
        if not op:
            # 兜底：从第一个顶层对象开始下钻
            obj = session.current_object
            first_top = next((o["field_path"] for o in self.cat.get_top_objects()), "")
            obj_path = obj.get("field_path") if obj else first_top
        else:
            obj_path = op["field_path"]

        # 规则预定义变量虚拟对象下钻：分发到预定义候选
        if obj_path == "规则预定义变量" or obj_path.startswith("规则预定义变量."):
            return self._cand_predefine(session, obj_path)

        # 对象路径前缀（如"保单的投保人的"），供整体检索（输入"保单的投保人"也能命中）
        obj_prefix_text = ""
        if op and op.get("labels"):
            obj_prefix_text = "".join(f"{x}的" for x in op["labels"])

        # 语料字段频次（该对象直接子字段）
        freq_map = {f["field_path"]: f for f in self.fields_given_object(obj_path, limit=200)}

        out = []
        seen = set()
        # 1. 直接子对象（可继续下钻）
        for o in self.cat.direct_child_objects(obj_path):
            ofp = o.get("field_path") or ""
            if ofp in seen:
                continue
            seen.add(ofp)
            # 子对象频次 = 其下所有字段频次之和（聚合，作为排序参考）
            agg = sum(f["count"] for f in self.fields_given_object(ofp, limit=200))
            out.append({
                "label": o.get("label") or ofp,
                "field_path": ofp,
                "field_id": o.get("field_id") or "",
                "type": o.get("type") or "object",
                "count": agg,
                "source": "bom",
                "state": "OBJECT_PATH",
                "kind": "object",
                "object_path": obj_path,
                "match_text": obj_prefix_text + (o.get("label") or ofp),
            })
        # 2. 当前层直接叶子字段
        for f in self.cat.direct_child_fields(obj_path):
            fp = f["field_path"]
            if fp in seen:
                continue
            seen.add(fp)
            freq = freq_map.get(fp)
            out.append({
                "label": f["label"] or fp,
                "field_path": fp,
                "field_id": f.get("field_id") or "",
                "type": f.get("type") or "",
                "count": freq["count"] if freq else 0,
                "source": "freq" if freq else "bom",
                "state": "FIELD",
                "kind": "field",
                "object_path": obj_path,
                "match_text": obj_prefix_text + (f["label"] or fp),
            })
        # 排序：子对象优先，其次字段按频次
        out.sort(key=lambda x: (0 if x["kind"] == "object" else 1, -x["count"]))
        return out

    def _cand_predefine(self, session: EditorSession, obj_path: str) -> list[dict]:
        """规则预定义变量虚拟对象下钻。

        obj_path == "规则预定义变量"          -> 列出所有预定义变量名（可继续下钻）
        obj_path == "规则预定义变量.{name}"   -> 列出该变量绑定 bom 对象的下钻候选
                                                   （下级字段与绑定对象一致，fieldPath 改写）
        """
        prefix = "规则预定义变量."
        if obj_path == "规则预定义变量":
            # 第一级：预定义变量名
            out = []
            for name in session.predefines:
                bound = session.predefines[name]
                out.append({
                    "label": name, "field_path": prefix + name,
                    "field_id": "", "type": "object", "count": 0,
                    "source": "predefine", "state": "OBJECT_PATH",
                    "kind": "object", "predefine_name": name,
                    "bound_path": bound,
                })
            return out

        # 第二级及以上：预定义变量绑定的 bom 对象下钻候选
        name = obj_path[len(prefix):].split(".")[0]
        bound = session.predefines.get(name)
        if not bound:
            return []
        out = []
        # 1. 绑定对象的直接子对象（继续下钻）
        for o in self.cat.direct_child_objects(bound):
            ofp = o.get("field_path") or ""
            new_path = prefix + name + "." + ofp.split(".")[-1]
            out.append({
                "label": o.get("label") or ofp, "field_path": new_path,
                "field_id": o.get("field_id") or "",
                "type": o.get("type") or "object", "count": 0,
                "source": "predefine", "state": "OBJECT_PATH",
                "kind": "object",
            })
        # 2. 绑定对象的直接字段（选中切运算符）
        for f in self.cat.direct_child_fields(bound):
            fp = f["field_path"]
            leaf = fp.split(".")[-1]
            new_path = prefix + name + "." + leaf
            out.append({
                "label": f["label"] or leaf, "field_path": new_path,
                "field_id": f.get("field_id") or "", "type": f.get("type") or "",
                "count": 0, "source": "predefine",
                "state": "FIELD", "kind": "field",
                "predefine_name": name, "bound_field": fp,
            })
        # 排序：对象优先，字段其次
        out.sort(key=lambda x: (0 if x["kind"] == "object" else 1, -x.get("count", 0)))
        return out

    def _cand_operator(self, session: EditorSession) -> list[dict]:
        """运算符候选：类型运算符 + 通用运算符集，保证自由编辑（语料只排序不限制）。

        任何字段都可选通用运算符（等于/为空/不为空/包含/属于等），
        即使该字段类型/语料未匹配过这些运算符。
        """
        field = session.current_field or {}
        field_path = field.get("field_path") or ""
        field_type = (field.get("type") or "").lower()

        # 1. 类型运算符（来自 bom_catalog type 兼容表 + 语料频次）
        type_ops = self.cat.get_operators(field_path) if field_path else []
        freq_map = {o["code"]: o.get("count", 0) for o in type_ops}

        # 2. 通用运算符（对任意字段补充，实现自由编辑：语料只排序不限制）
        common_ops = [
            {"code": "EQ", "label": "等于"},
            {"code": "STR_EQ", "label": "是"},
            {"code": "STR_BELONG_TO", "label": "属于...中的一个"},
            {"code": "STR_INCLUDE", "label": "包含"},
            {"code": "STR_NOT_INCLUDE", "label": "不包含"},
            {"code": "STR_EMPTY", "label": "为空"},
            {"code": "STR_NOT_EMPTY", "label": "不为空"},
            {"code": "STR_START_WITH", "label": "以...开头"},
            {"code": "STR_END_WITH", "label": "以...结尾"},
        ]
        if field_type in ("date", "datetime"):
            common_ops += [
                {"code": "DATE_GT", "label": "大于"},
                {"code": "DATE_LT", "label": "小于"},
                {"code": "DATE_GE", "label": "大于等于"},
                {"code": "DATE_LE", "label": "小于等于"},
                {"code": "DATE_BETWEEN", "label": "在...和...之间"},
            ]
        elif field_type in ("int", "integer", "double", "long", "big_decimal", "number"):
            common_ops += [
                {"code": "GT", "label": "大于"},
                {"code": "LT", "label": "小于"},
                {"code": "GE", "label": "大于等于"},
                {"code": "LE", "label": "小于等于"},
                {"code": "NUM_BETWEEN", "label": "在...和...之间"},
            ]
        elif field_type in ("list", "array", "collection"):
            # 运算符924 档1：列表字段 COLLECTION_* 系
            common_ops += [
                {"code": "COLLECTION_NOT_EMPTY", "label": "不为空"},
                {"code": "COLLECTION_IS_EMPTY", "label": "为空"},
                {"code": "COLLECTION_ANY_STR", "label": "包含"},
                {"code": "COLLECTION_NOT_IN_STR", "label": "不包含"},
                {"code": "COLLECTION_ANY_BELONG_TO", "label": "部分属于"},
                {"code": "COLLECTION_ALL_BELONG_TO", "label": "全部属于"},
                {"code": "COLLECTION_ANY_CONTAINS", "label": "部分包含"},
                {"code": "COLLECTION_ALL_CONTAINS", "label": "全部包含"},
                {"code": "COLLECTION_NOT_IN_CONTAINS", "label": "不包含集合"},
            ]

        # 合并去重：类型运算符在前（带频次），通用运算符补缺（count=0，可选）
        seen = set()
        merged = []
        for op in list(type_ops) + common_ops:
            code = op["code"]
            if code in seen:
                continue
            seen.add(code)
            merged.append({"code": code, "label": op["label"],
                           "count": freq_map.get(code, 0)})
        # 排序：语料频次高优先，count=0 的排后但仍可选
        merged.sort(key=lambda x: -x["count"])

        return [{
            "label": o["label"], "code": o["code"], "count": o["count"],
            "state": "OPERATOR", "kind": "operator", "field_path": field_path,
            "source": "freq" if o["count"] > 0 else "bom",
        } for o in merged]

    def _cand_value(self, session: EditorSession) -> list[dict]:
        field = session.current_field or {}
        operator = session.current_operator or {}
        field_path = field.get("field_path") or ""
        op_code = operator.get("code") if operator else ""

        # 值下钻模式（已在动态域集内浏览）
        if session.value_drilling:
            return self._cand_value_drill(session)

        # 无需值的运算符（为空/不为空 等）：直接给连接符候选
        if op_code in ("STR_EMPTY", "STR_NOT_EMPTY", "OBJECT_EMPTY",
                       "OBJECT_NOT_EMPTY", "COLLECTION_IS_EMPTY",
                       "COLLECTION_NOT_EMPTY"):
            out = [{
                "label": "（该运算符无需值，选择连接符结束）",
                "value": "", "count": 0, "state": "VALUE", "kind": "sep",
                "source": "info",
            }]
            out.extend(self._cand_connector(session))
            return out

        # 区间（在 起 和 止 之间）：起止值无法枚举，给输入提示
        if op_code and op_code.endswith("_BETWEEN"):
            out = [{
                "label": "（起值填在『在』后、止值填在『和』后，如：在 2020-01-01 和 2020-12-31 之间）",
                "value": "", "count": 0, "state": "VALUE", "kind": "sep",
                "source": "info",
            }]
            out.extend(self._cand_connector(session))
            return out

        out = []
        seen = set()
        need_enum = field_path and op_code in ("STR_BELONG_TO", "STR_EQ", "EQ")
        enum_source = self.cat.enum_source_path
        enum_label = self.cat.enum_source_label or (self.cat.enum_source_path or "值域")
        # 1. 值枚举来源入口（原"动态域集"）——放在最前，作为进入枚举浏览的入口
        if need_enum and enum_source:
            out.append({
                "label": enum_label, "value": "",
                "count": 0, "state": "VALUE", "kind": "dyn_root",
                "source": "bom", "drill_path": enum_source,
            })
            # 展开枚举来源直接枚举组，支持"{来源}的X"整体检索（如"动态域集的个团标志"）
            for g in self.cat.direct_child_objects(enum_source):
                gfp = g.get("field_path") or ""
                glabel = g.get("label") or gfp
                match_text = enum_label + "的" + glabel
                out.append({
                    "label": match_text, "value": "",
                    "field_path": gfp, "field_id": g.get("field_id") or "",
                    "count": 0, "state": "VALUE", "kind": "dyn_group",
                    "source": "bom", "drill_path": gfp,
                    "match_text": match_text,
                })
        # 2. 值词典（该字段+该运算符）——快捷选项
        field_vals = (self.value_dict.get(field_path) or {}).get(op_code, [])
        for vt, vid, cnt in field_vals[:10]:
            if vt and vt not in seen:
                seen.add(vt)
                out.append({
                    "label": vt, "value": vt, "field_id": vid,
                    "count": cnt, "state": "VALUE", "kind": "value",
                    "source": "value_dict",
                })
        # 3. BOOLEAN_BELONG_TO 的 是/否
        if op_code == "BOOLEAN_BELONG_TO":
            for v in ["是", "否"]:
                if v not in seen:
                    out.append({"label": v, "value": v, "count": 0,
                                "state": "VALUE", "kind": "value",
                                "source": "static"})
        # 4. 多值运算符（STR_BELONG_TO 属于...中的一个）：已选值后追加连接符（并且/或者/结束）
        if op_code == "STR_BELONG_TO" and session.pending_values:
            # 分隔提示
            out.append({
                "label": f"（已选 {len(session.pending_values)} 个值，选择下个值或结束）",
                "value": "", "count": 0, "state": "VALUE", "kind": "sep",
                "source": "info",
            })
            out.extend(self._cand_connector(session))
        return out

    def _cand_value_drill(self, session: EditorSession) -> list[dict]:
        """在值枚举来源内下钻选枚举值。当前路径 session.value_drill_path。"""
        enum_source = self.cat.enum_source_path or ""
        enum_label = self.cat.enum_source_label or "值域"
        path = session.value_drill_path or enum_source
        out = []
        seen = set()
        # 返回上一级入口
        if enum_source and path != enum_source:
            parent = ".".join(path.split(".")[:-1]) or enum_source
            out.append({
                "label": "↑ 返回上一级", "value": "",
                "count": 0, "state": "VALUE", "kind": "dyn_up",
                "source": "nav", "drill_path": parent,
            })
        # 枚举组（子对象，继续下钻）：match_text 含完整前缀，支持"动态域集的帮助"整体检索
        for o in self.cat.direct_child_objects(path):
            ofp = o.get("field_path") or ""
            if ofp in seen:
                continue
            seen.add(ofp)
            glabel = o.get("label") or ofp
            mt = f"{enum_label}的{glabel}" if enum_source and path == enum_source else glabel
            out.append({
                "label": glabel, "value": "",
                "field_path": ofp, "field_id": o.get("field_id") or "",
                "count": 0, "state": "VALUE", "kind": "dyn_group",
                "source": "bom", "drill_path": ofp, "match_text": mt,
            })
        # 枚举值（叶子，选中即 VALUE）：label 拼接"{枚举来源}的{枚举组}的"前缀，
        # 与值词典快捷项的完整描述保持一致。
        grp_obj = self.cat.get_object_by_path(path) if (enum_source and path != enum_source) else None
        grp_label = (grp_obj.get("label") if grp_obj else "") or ""
        for f in self.cat.direct_child_fields(path):
            fp = f["field_path"]
            if fp in seen:
                continue
            seen.add(fp)
            leaf_label = f.get("label") or fp
            full_label = f"{enum_label}的{grp_label}的{leaf_label}" if grp_label else leaf_label
            out.append({
                "label": full_label, "value": full_label,
                "field_path": fp, "field_id": f.get("field_id") or "",
                "count": 0, "state": "VALUE", "kind": "value",
                "source": "bom", "match_text": full_label,
            })
        return out

    def _cand_connector(self, session: EditorSession) -> list[dict]:
        # 并且 / 或者 / 结束，按语料统计排序
        field = session.current_field or {}
        op = session.current_operator or {}
        field_path = field.get("field_path") or ""
        op_code = op.get("code") if op else ""
        counts = {"并且": 0, "或者": 0, "结束": 0}
        if field_path and op_code:
            rows = self._freq_rows(
                "SELECT conn, count FROM freq_connector_after_value "
                "WHERE field_path=? AND op_code=?", (field_path, op_code))
            for r in rows:
                counts[r["conn"]] = counts.get(r["conn"], 0) + r["count"]
        items = [
            {"label": "并且", "conn": "并且", "count": counts["并且"],
             "state": "CONNECTOR", "kind": "connector", "source": "freq"},
            {"label": "或者", "conn": "或者", "count": counts["或者"],
             "state": "CONNECTOR", "kind": "connector", "source": "freq"},
            {"label": "结束", "conn": "结束", "count": counts["结束"],
             "state": "DONE", "kind": "done", "source": "freq"},
        ]
        items.sort(key=lambda x: -x["count"])
        return items

    # ---------- 模式 B 字符过滤 ----------
    def _filter_by_text(self, pool: list[dict], query: str) -> list[dict]:
        """对候选池做字符匹配过滤/重排（第一级）。

        优先用 match_text（完整对象路径前缀+label，如"保单的投保人的客户代码"）匹配，
        支持"保单的投保人"整体输入也能命中；无 match_text 时退回 label/path 匹配。
        """
        scored = []
        for c in pool:
            label = c.get("label") or ""
            path = c.get("field_path") or ""
            mt = c.get("match_text") or label
            score = 0.0
            if mt == query:
                score = 1.0
            elif mt.startswith(query):
                score = 0.9
            elif query in mt:
                score = 0.7
            elif query in label:
                score = 0.7
            elif query in path:
                score = 0.6
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: (-x[0], -x[1].get("count", 0)))
        return [c for _, c in scored]
