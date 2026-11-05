"""BOM 目录构建（M1.1）。

读取 车险/bom.json（树状业务模型对象），展平成行写入 SQLite：
- 对象表（object/list 节点，可作为 OBJECT / 动态域集值域引用）
- 字段表（叶子字段，可作为 FIELD / 动态域集枚举值）
- 字段->运算符兼容表（由 type 推导）

schema:
  objects(id, field_id, label, field_path, type, class_path, depth, obj_path)
  fields(field_id, label, field_path, type, class_path, obj_path, depth,
         is_enum, enum_obj_field_path)
  field_operators(field_id, field_path, operator_code, operator_label, count)
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable

from ..config import project_root


# ---- type -> 兼容运算符（方案中的运算符短语 ↔ 代码映射）----
# operator_code 对齐语料 ruleContent 里的 code / expressionId
TYPE_OPERATORS: dict[str, list[dict[str, str]]] = {
    "string": [
        {"code": "STR_BELONG_TO", "label": "属于...中的一个"},
        {"code": "STR_NOT_BELONG_TO", "label": "不属于...中的一个"},
        {"code": "STR_EQ", "label": "是"},
        {"code": "EQ", "label": "等于"},
        {"code": "NE", "label": "不等于"},
        {"code": "STR_INCLUDE", "label": "包含"},
        {"code": "STR_NOT_INCLUDE", "label": "不包含"},
        {"code": "STR_NOT_EMPTY", "label": "不为空"},
        {"code": "STR_EMPTY", "label": "为空"},
        {"code": "STR_START_WITH", "label": "以...开头"},
        {"code": "STR_END_WITH", "label": "以...结尾"},
        {"code": "STR_NOT_START_WITH", "label": "不以...开头"},
        {"code": "STR_NOT_END_WITH", "label": "不以...结尾"},
        {"code": "STR_NE", "label": "不是"},
    ],
    # 列表字段：COLLECTION_* 系（运算符924 档1）
    "list": [
        {"code": "COLLECTION_NOT_EMPTY", "label": "不为空"},
        {"code": "COLLECTION_IS_EMPTY", "label": "为空"},
        {"code": "COLLECTION_ANY_STR", "label": "包含"},
        {"code": "COLLECTION_NOT_IN_STR", "label": "不包含"},
        {"code": "COLLECTION_ANY_BELONG_TO", "label": "部分属于"},
        {"code": "COLLECTION_ALL_BELONG_TO", "label": "全部属于"},
        {"code": "COLLECTION_ANY_CONTAINS", "label": "部分包含"},
        {"code": "COLLECTION_ALL_CONTAINS", "label": "全部包含"},
        {"code": "COLLECTION_NOT_IN_CONTAINS", "label": "不包含集合"},
    ],
    "boolean": [
        {"code": "BOOLEAN_BELONG_TO", "label": "为 是/否（成立/不成立）"},
    ],
    "double": [
        {"code": "GE", "label": "大于等于"},
        {"code": "GT", "label": "大于"},
        {"code": "LE", "label": "小于等于"},
        {"code": "LT", "label": "小于"},
        {"code": "EQ", "label": "等于"},
        {"code": "NE", "label": "不等于"},
        {"code": "NUM_BETWEEN", "label": "在...和...之间"},
    ],
    "int": [
        {"code": "GE", "label": "大于等于"},
        {"code": "GT", "label": "大于"},
        {"code": "LE", "label": "小于等于"},
        {"code": "LT", "label": "小于"},
        {"code": "EQ", "label": "等于"},
        {"code": "NE", "label": "不等于"},
        {"code": "NUM_BETWEEN", "label": "在...和...之间"},
    ],
    "date": [
        {"code": "DATE_GE", "label": "大于等于"},
        {"code": "DATE_GT", "label": "大于"},
        {"code": "DATE_LE", "label": "小于等于"},
        {"code": "DATE_LT", "label": "小于"},
        {"code": "DATE_EQ", "label": "等于"},
        {"code": "DATE_BETWEEN", "label": "在...和...之间"},
        {"code": "TIME_BETWEEN", "label": "在...和...之间"},
    ],
    "datetime": [
        {"code": "DATE_GE", "label": "大于等于"},
        {"code": "DATE_GT", "label": "大于"},
        {"code": "DATE_LE", "label": "小于等于"},
        {"code": "DATE_LT", "label": "小于"},
        {"code": "DATE_EQ", "label": "等于"},
        {"code": "DATE_BETWEEN", "label": "在...和...之间"},
    ],
}

# 兼容 type 归一化：把 bom 里的 type 映射到上述 key
TYPE_ALIAS = {
    "string": "string",
    "str": "string",
    "boolean": "boolean",
    "bool": "boolean",
    "double": "double",
    "int": "int",
    "integer": "int",
    "long": "double",
    "big_decimal": "double",
    "date": "date",
    "datetime": "datetime",
    "time": "date",
    "list": "list",
    "array": "list",
    "collection": "list",
    "object": None,
}

def _norm_type(t: str) -> str | None:
    if not t:
        return None
    return TYPE_ALIAS.get(t.lower())


class BomCatalog:
    """BOM 目录（内存中倒排 + SQLite 持久化）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # 内存索引（启动时 build_from_db 填充）
        # 统一以 field_path 为主键接地（样本与 bom.json 中 field_path 完全一致；
        # field_id 两套数据不一致，仅作内部辅助）。
        self.field_by_id: dict[str, dict] = {}
        self.field_by_path: dict[str, dict] = {}
        self.object_by_id: dict[str, dict] = {}
        self.object_by_path: dict[str, dict] = {}
        self.enum_groups: dict[str, list[dict]] = {}  # enum 组 field_path -> 枚举值字段
        self.operators_by_path: dict[str, list[dict]] = {}  # field_path -> 兼容运算符
        # 方法节点全量列表（field_path 以 "()" 结尾）。field_by_path 按 field_path 作 key，
        # 同名重载/多路径方法会互相覆盖，故这里单独保留全部方法行（供 then/else 动作引导）。
        self.methods: list[dict] = []
        self.field_id_to_path: dict[str, str] = {}
        self.object_id_to_path: dict[str, str] = {}
        # 顶层对象（根 children 的 object 节点，结构自动识别，不依赖名称）
        self.top_object_paths: list[str] = []
        # 值枚举来源对象（结构识别：子节点几乎全是"枚举组"的一级对象，如原"动态域集"）
        self.enum_source_path: str = ""
        self.enum_source_label: str = ""

    # ---------- 连接 ----------
    def connect(self) -> "BomCatalog":
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        return self

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _schema(self) -> None:
        c = self._conn
        c.executescript("""
        DROP TABLE IF EXISTS objects;
        DROP TABLE IF EXISTS fields;
        DROP TABLE IF EXISTS field_operators;
        CREATE TABLE objects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_id TEXT,
            label TEXT,
            field_path TEXT,
            type TEXT,
            class_path TEXT,
            depth INTEGER,
            obj_path TEXT
        );
        CREATE TABLE fields(
            field_id TEXT PRIMARY KEY,
            label TEXT,
            field_path TEXT,
            type TEXT,
            class_path TEXT,
            obj_path TEXT,
            depth INTEGER,
            is_enum INTEGER,
            enum_obj_field_path TEXT
        );
        CREATE TABLE field_operators(
            field_id TEXT,
            field_path TEXT,
            operator_code TEXT,
            operator_label TEXT,
            count INTEGER DEFAULT 0
        );
        CREATE INDEX idx_fields_path ON fields(field_path);
        CREATE INDEX idx_objects_path ON objects(field_path);
        CREATE INDEX idx_fop ON field_operators(field_id);
        """)

    # ---------- 结构识别 ----------
    def _identify_top_level(self, root: dict) -> None:
        """从根 children 自动识别顶层对象与值枚举来源对象（不依赖名称）。

        - 顶层对象：根 children 中 type=object 的节点
        - 值枚举来源（原"动态域集"）：子节点几乎全是"枚举组"（无更深对象、
          无叶子字段，子对象占比极高）的一级对象
        """
        children = root.get("children") or []
        self.top_object_paths = []
        enum_candidates = []
        for c in children:
            if c.get("type") not in ("object", "list"):
                continue
            fp = c.get("fieldPath") or ""
            if not fp:
                continue
            self.top_object_paths.append(fp)
            subs = c.get("children") or []
            # 枚举来源特征：子节点全是对象（枚举组），且几乎无叶子字段
            if subs:
                field_cnt = sum(1 for s in subs
                                if s.get("type") not in ("object", "list") and not s.get("children"))
                obj_cnt = len(subs) - field_cnt
                # 子对象占比极高且叶子字段极少 => 枚举来源
                if obj_cnt > 0 and field_cnt == 0 and obj_cnt / len(subs) > 0.9:
                    enum_candidates.append((fp, c.get("label") or "", obj_cnt))
        # 选子对象最多的作为枚举来源（如"动态域集"266 个 vs 保单 41 个）
        if enum_candidates:
            enum_candidates.sort(key=lambda x: -x[2])
            self.enum_source_path, self.enum_source_label, _ = enum_candidates[0]

    # ---------- 展平 bom.json ----------
    def build_from_bom(self, bom_path: str) -> int:
        """遍历 bom.json 树，写入 objects/fields/field_operators。"""
        self.connect()
        self._schema()
        with open(bom_path, encoding="utf-8") as f:
            root = json.load(f)

        # 结构识别顶层对象与枚举来源（不依赖名称）
        self._identify_top_level(root)

        cur = self._conn.cursor()
        obj_rows: list[tuple] = []
        field_rows: list[tuple] = []

        def is_object(node: dict) -> bool:
            return node.get("type") in ("object", "list") or bool(node.get("children"))

        def walk(node: dict, obj_path: str, depth: int) -> None:
            label = node.get("label") or ""
            field_id = node.get("fieldId") or ""
            field_path = node.get("fieldPath") or ""
            type_ = node.get("type") or ""
            class_path = node.get("classPath") or ""

            if is_object(node):
                # 对象 / 列表节点
                cur_obj_path = field_path or obj_path
                obj_rows.append((field_id, label, field_path, type_,
                                 class_path, depth, obj_path))
                # list 容器节点也是比较左值（档1 集合系运算符），运算符表同样按
                # type 挂载——is_object 分类下它们不走下方叶子分支
                if _norm_type(type_) == "list":
                    for op in TYPE_OPERATORS.get("list", []):
                        self.operators_by_path.setdefault(field_path, []).append({
                            "code": op["code"], "label": op["label"],
                        })
                self.object_id_to_path[field_id] = field_path
                self.object_by_id[field_id] = {
                    "field_id": field_id, "label": label,
                    "field_path": field_path, "type": type_,
                    "class_path": class_path, "depth": depth,
                    "obj_path": obj_path,
                }
                for ch in node.get("children") or []:
                    walk(ch, cur_obj_path, depth + 1)
            else:
                # 叶子字段
                # 值枚举来源（原"动态域集"）下的枚举值：字段所属对象即枚举组，
                # 其 children 为候选值。用结构识别的 enum_source_path，不依赖名称。
                enum_obj_field = ""
                is_enum = False
                if self.enum_source_path and obj_path.startswith(self.enum_source_path):
                    enum_obj_field = obj_path
                    is_enum = True
                field_rows.append((field_id, label, field_path, type_,
                                   class_path, obj_path, depth,
                                   1 if is_enum else 0, enum_obj_field))
                self.field_id_to_path[field_id] = field_path
                self.field_by_id[field_id] = {
                    "field_id": field_id, "label": label,
                    "field_path": field_path, "type": type_,
                    "class_path": class_path, "obj_path": obj_path,
                    "depth": depth, "is_enum": is_enum,
                    "enum_obj_field_path": enum_obj_field,
                }
                self.field_by_path[field_path] = self.field_by_id[field_id]
                # 运算符兼容表（type 推导，按 field_path 索引）
                norm = _norm_type(type_)
                for op in TYPE_OPERATORS.get(norm, []):
                    self.operators_by_path.setdefault(field_path, []).append({
                        "code": op["code"], "label": op["label"],
                    })

        # 顶层 5 对象
        for top in root.get("children", []):
            walk(top, "", 0)

        cur.executemany(
            "INSERT INTO objects(field_id,label,field_path,type,class_path,depth,obj_path)"
            " VALUES(?,?,?,?,?,?,?)", obj_rows)
        cur.executemany(
            "INSERT INTO fields(field_id,label,field_path,type,class_path,obj_path,depth,is_enum,enum_obj_field_path)"
            " VALUES(?,?,?,?,?,?,?,?,?)", field_rows)
        # 运算符表（field_id 取 bom 内部 id；若字段无 id 用 path 占位）
        op_rows = []
        for path, ops in self.operators_by_path.items():
            f = self.field_by_path.get(path, {})
            fid = f.get("field_id", "")
            for op in ops:
                op_rows.append((fid, path, op["code"], op["label"], 0))
        cur.executemany(
            "INSERT INTO field_operators(field_id,field_path,operator_code,operator_label,count)"
            " VALUES(?,?,?,?,?)", op_rows)

        self._conn.commit()
        return len(obj_rows) + len(field_rows)

    # ---------- 从 DB 加载内存索引 ----------
    def build_from_db(self) -> None:
        c = self._conn
        self.field_by_id.clear()
        self.field_by_path.clear()
        self.object_by_id.clear()
        self.object_by_path.clear()
        self.operators_by_path.clear()
        self.enum_groups.clear()
        self.field_id_to_path.clear()
        self.object_id_to_path.clear()
        self.top_object_paths = []
        self.enum_source_path = ""
        self.enum_source_label = ""
        self.methods = []

        for r in c.execute("SELECT * FROM fields"):
            d = dict(r)
            self.field_by_id[d["field_id"]] = d
            self.field_by_path[d["field_path"]] = d
            self.field_id_to_path[d["field_id"]] = d["field_path"]
            if d["is_enum"]:
                key = d["enum_obj_field_path"] or d["obj_path"]
                self.enum_groups.setdefault(key, []).append(d)
            # 方法节点全量收集（含同名重载/多路径，不覆盖）
            if (d["field_path"] or "").endswith("()"):
                self.methods.append(d)
        for r in c.execute("SELECT * FROM objects"):
            d = dict(r)
            self.object_by_id[d["field_id"]] = d
            self.object_by_path[d["field_path"]] = d
            self.object_id_to_path[d["field_id"]] = d["field_path"]
        for r in c.execute("SELECT * FROM field_operators ORDER BY count DESC"):
            # 以 field_path 为 key，与值词典/频次表对齐；count 高者优先
            self.operators_by_path.setdefault(r["field_path"], []).append(
                {"code": r["operator_code"], "label": r["operator_label"],
                 "count": r["count"]})

        # 结构识别：顶层对象（depth=0 的 object）+ 值枚举来源对象
        self._restore_top_level()

    def _restore_top_level(self) -> None:
        """从内存 object_by_path 恢复顶层对象与枚举来源（结构识别，不依赖名称）。"""
        # 顶层对象：obj_path 为空（根的直接子节点）的 object
        self.top_object_paths = []
        enum_candidates = []
        for o in self.object_by_path.values():
            if not o.get("obj_path") and o.get("field_path"):
                self.top_object_paths.append(o["field_path"])
                # 枚举来源特征：直接子对象占比极高且无叶子字段
                subs = self.direct_child_objects(o["field_path"])
                flds = self.direct_child_fields(o["field_path"])
                if subs and len(flds) == 0 and len(subs) / (len(subs) + len(flds)) > 0.9:
                    enum_candidates.append((o["field_path"], o.get("label") or "", len(subs)))
        if enum_candidates:
            enum_candidates.sort(key=lambda x: -x[2])
            self.enum_source_path, self.enum_source_label, _ = enum_candidates[0]

    # ---------- 查询 ----------
    def get_field_by_id(self, field_id: str) -> dict | None:
        return self.field_by_id.get(field_id)

    def get_field_by_path(self, path: str) -> dict | None:
        return self.field_by_path.get(path)

    def get_object_by_path(self, path: str) -> dict | None:
        return self.object_by_path.get(path)

    def fields_under_object(self, obj_path: str) -> list[dict]:
        """返回 obj_path 直接/间接子字段（用于自由编辑：候选须覆盖全部合法字段）。"""
        prefix = obj_path + "."
        return [f for f in self.field_by_path.values()
                if f["field_path"].startswith(prefix)]

    def direct_child_objects(self, obj_path: str) -> list[dict]:
        """obj_path 的直接子对象（仅下一层，不含更深）。"""
        prefix = obj_path + "."
        n = len(obj_path.split("."))
        out = []
        for o in self.object_by_path.values():
            fp = o.get("field_path") or ""
            if fp.startswith(prefix) and len(fp.split(".")) == n + 1:
                out.append(o)
        return out

    def direct_child_fields(self, obj_path: str) -> list[dict]:
        """obj_path 的直接叶子字段（仅下一层，不含更深）。"""
        prefix = obj_path + "."
        n = len(obj_path.split("."))
        out = []
        for f in self.field_by_path.values():
            fp = f.get("field_path") or ""
            if fp.startswith(prefix) and len(fp.split(".")) == n + 1:
                out.append(f)
        return out

    def get_operators(self, field_path: str) -> list[dict]:
        """返回字段兼容运算符（按语料实际使用频次优先）。"""
        return self.operators_by_path.get(field_path, [])

    def search_fields(self, text: str, limit: int = 20) -> list[dict]:
        """对 label / field_path 做子串 + 前缀匹配（字符匹配，无 LLM）。"""
        hits: list[tuple[float, dict]] = []
        text_l = text.lower()
        for f in self.field_by_id.values():
            label = f["label"] or ""
            path = f["field_path"] or ""
            score = 0.0
            if text and label == text:
                score = 1.0
            elif text and label.startswith(text):
                score = 0.9
            elif text and text in label:
                score = 0.7
            elif text and text in path:
                score = 0.6
            elif text and path.endswith(text):
                score = 0.65
            if score > 0:
                hits.append((score, f))
        hits.sort(key=lambda x: (-x[0], x[1]["field_path"]))
        return [h[1] for h in hits[:limit]]

    def get_top_objects(self) -> list[dict]:
        """顶层对象（根 children 的 object 节点，结构自动识别）。"""
        tops = []
        for p in self.top_object_paths:
            o = self.object_by_path.get(p)
            if o:
                tops.append(o)
        return tops

    def get_enum_values(self, obj_field_path: str) -> list[dict]:
        return self.enum_groups.get(obj_field_path, [])

    def close_ops(self):
        pass


def build_default() -> BomCatalog:
    """构建/加载默认 BOM 目录（供服务复用）。"""
    from ..config import get
    db_path = get("etl", "bom_db")
    cat = BomCatalog(db_path)
    cat.connect()
    return cat
