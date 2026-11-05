"""值词典挖掘（M1.2）。

扫描全部 *_samples.jsonl，从 ruleContent 树收集：
- 每个 (field_path, op_code) → 用到的值集合（CUSTOM 字面量 / 动态域集枚举 label），
  按出现频次排序。
- 字段实际使用过的运算符频次（用于运算符兼容表的经验排序）。

**主键统一用 field_path**（样本与 bom.json 两套数据中 field_path 完全一致，
field_id 两套不一致，不可用于跨数据接地）。

schema:
  field_values(field_path, op_code, value_text, value_field_id, count)
  field_ops_count(field_path, op_code, count)
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterable

from .rule_tree import parse_rule_content, iter_clauses


def iter_sample_lines(samples_dir: str) -> Iterable[dict]:
    for fn in os.listdir(samples_dir):
        if not fn.endswith("_samples.jsonl"):
            continue
        if fn == "predefine_samples.jsonl":
            continue  # 预定义样本汇总文件，非原始分片，避免被二次加载
        p = os.path.join(samples_dir, fn)
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except Exception:
            continue


def build_value_dict(samples_dir: str, db_path: str) -> dict:
    """扫描语料，写入值词典 + 字段运算符频次，返回统计信息。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    DROP TABLE IF EXISTS field_values;
    DROP TABLE IF EXISTS field_ops_count;
    CREATE TABLE field_values(
        field_path TEXT, op_code TEXT,
        value_text TEXT, value_field_id TEXT, count INTEGER DEFAULT 0,
        PRIMARY KEY(field_path, op_code, value_text)
    );
    CREATE TABLE field_ops_count(
        field_path TEXT, op_code TEXT, count INTEGER DEFAULT 0,
        PRIMARY KEY(field_path, op_code)
    );
    """)

    # 内存聚合
    val_cnt: dict[tuple, int] = {}
    op_cnt: dict[tuple, int] = {}
    n_rules = 0
    n_clauses = 0

    for sample in iter_sample_lines(samples_dir):
        if_rule = sample.get("if_rule") or {}
        rc = parse_rule_content(if_rule.get("ruleContent"))
        if rc is None:
            continue
        n_rules += 1
        for c in iter_clauses(rc):
            n_clauses += 1
            fp = c["field_path"]
            if not fp:
                continue
            key = (fp, c["op_code"])
            op_cnt[key] = op_cnt.get(key, 0) + 1
            if c.get("values"):
                for v in c["values"]:
                    vkey = (fp, c["op_code"],
                            v.get("label") or v.get("field_path") or "",
                            v.get("field_id") or "")
                    val_cnt[vkey] = val_cnt.get(vkey, 0) + 1
            elif c.get("value") is not None:
                vkey = (fp, c["op_code"],
                        str(c["value"]), c.get("value_field_id") or "")
                val_cnt[vkey] = val_cnt.get(vkey, 0) + 1

    # 写入
    cur = conn.cursor()
    cur.executemany(
        "INSERT OR REPLACE INTO field_values(field_path,op_code,value_text,value_field_id,count)"
        " VALUES(?,?,?,?,?)",
        [(*k, cnt) for k, cnt in val_cnt.items()])
    cur.executemany(
        "INSERT OR REPLACE INTO field_ops_count(field_path,op_code,count)"
        " VALUES(?,?,?)",
        [(*k, cnt) for k, cnt in op_cnt.items()])
    conn.commit()

    stat = {
        "rules": n_rules,
        "clauses": n_clauses,
        "unique_values": len(val_cnt),
        "unique_field_ops": len(op_cnt),
    }
    conn.close()
    return stat


def load_value_dict(db_path: str) -> dict:
    """加载值词典到内存：{field_path: {op_code: [(value_text, value_field_id, count)]}}"""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict[str, list]] = {}
    for r in conn.execute(
            "SELECT * FROM field_values ORDER BY field_path, op_code, count DESC"):
        out.setdefault(r["field_path"], {}).setdefault(r["op_code"], []).append(
            (r["value_text"], r["value_field_id"], r["count"]))
    conn.close()
    return out


def load_field_ops_count(db_path: str) -> dict:
    """加载字段运算符频次：{field_path: {op_code: count}}"""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT * FROM field_ops_count"):
        out.setdefault(r["field_path"], {})[r["op_code"]] = r["count"]
    conn.close()
    return out
