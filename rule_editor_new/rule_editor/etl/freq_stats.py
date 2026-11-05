"""状态机条件频次统计（M1.3）。

对每条规则的 ruleContent 树按状态机序列展开，统计（主键用 field_path，与 bom 对齐）：
  P(OBJECT|IF后)          —— IF 后首对象分布
  P(FIELD|OBJECT)         —— 某对象下常用字段
  P(OPERATOR|FIELD)       —— 某字段常用运算符
  P(CONNECTOR|VALUE后)    —— 值后是继续(并且/或者)还是结束

schema:
  freq_object_after_if(obj_field_path, count)
  freq_field_given_object(obj_field_path, field_path, count)
  freq_op_given_field(field_path, op_code, count)
  freq_connector_after_value(field_path, op_code, conn, count)
"""
from __future__ import annotations

import os
import sqlite3

from .rule_tree import parse_rule_content, iter_clauses, iter_clauses_with_connects
from .value_dict import iter_sample_lines


def build_freq_stats(samples_dir: str, db_path: str) -> dict:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    DROP TABLE IF EXISTS freq_object_after_if;
    DROP TABLE IF EXISTS freq_field_given_object;
    DROP TABLE IF EXISTS freq_op_given_field;
    DROP TABLE IF EXISTS freq_connector_after_value;
    CREATE TABLE freq_object_after_if(
        obj_field_path TEXT PRIMARY KEY, count INTEGER
    );
    CREATE TABLE freq_field_given_object(
        obj_field_path TEXT, field_path TEXT, count INTEGER,
        PRIMARY KEY(obj_field_path, field_path)
    );
    CREATE TABLE freq_op_given_field(
        field_path TEXT, op_code TEXT, count INTEGER,
        PRIMARY KEY(field_path, op_code)
    );
    CREATE TABLE freq_connector_after_value(
        field_path TEXT, op_code TEXT, conn TEXT, count INTEGER,
        PRIMARY KEY(field_path, op_code, conn)
    );
    """)

    # 内存聚合
    obj_if: dict[str, int] = {}
    fld_obj: dict[tuple, int] = {}
    op_fld: dict[tuple, int] = {}
    conn_val: dict[tuple, int] = {}
    n_rules = 0

    for sample in iter_sample_lines(samples_dir):
        if_rule = sample.get("if_rule") or {}
        rc = parse_rule_content(if_rule.get("ruleContent"))
        if rc is None:
            continue
        n_rules += 1
        clauses = list(iter_clauses(rc))

        # IF 后首对象
        if clauses:
            first = clauses[0]
            if first["obj_field_path"]:
                obj_if[first["obj_field_path"]] = obj_if.get(first["obj_field_path"], 0) + 1
            else:
                obj_if[""] = obj_if.get("", 0) + 1

        # 字段→对象
        for c in clauses:
            if c["obj_field_path"] and c["field_path"]:
                k = (c["obj_field_path"], c["field_path"])
                fld_obj[k] = fld_obj.get(k, 0) + 1

        # 运算符→字段
        for c in clauses:
            if c["field_path"] and c["op_code"]:
                k = (c["field_path"], c["op_code"])
                op_fld[k] = op_fld.get(k, 0) + 1

        # 连接符：相邻子句之间的逻辑连接（子句后是 并且/或者/结束）
        for c, conn_label in iter_clauses_with_connects(rc):
            if c["field_path"] and c["op_code"]:
                k = (c["field_path"], c["op_code"], conn_label)
                conn_val[k] = conn_val.get(k, 0) + 1

    cur = conn.cursor()
    cur.executemany("INSERT OR REPLACE INTO freq_object_after_if VALUES(?,?)",
                    obj_if.items())
    cur.executemany("INSERT OR REPLACE INTO freq_field_given_object VALUES(?,?,?)",
                    [(*k, v) for k, v in fld_obj.items()])
    cur.executemany("INSERT OR REPLACE INTO freq_op_given_field VALUES(?,?,?)",
                    [(*k, v) for k, v in op_fld.items()])
    cur.executemany("INSERT OR REPLACE INTO freq_connector_after_value VALUES(?,?,?,?)",
                    [(*k, v) for k, v in conn_val.items()])
    conn.commit()
    stat = {"rules": n_rules}
    conn.close()
    return stat
