"""规则语料索引（M1.2b）。

索引对象 = 整条规则（ruleName + 去标签 ruleDesc + 结构化特征），共 12,050 条。

用途（注意：不是候选匹配目标）：
1. 相似规则检索 / 「从此续写」：用户编辑到一半，检索相似的整条规则，
   把该规则的 desc 解析回状态机槽位继续编辑（不接触规则树）。
2. LLM few-shot 上下文：给 LLM 提供相似规则作为示例。

desc 只是规则的文本示例，其内容（字段/运算符/值）都由 bom.json 语料 + 运算符 + 参数值
组成；索引的是「规则」这一整体，而非把 desc 当作独立检索目标。

存储：SQLite + 内存 BM25。语义（embedding）留扩展位。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Iterable, Optional

from .rule_tree import parse_rule_content, iter_clauses
from .value_dict import iter_sample_lines

_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>|<[^>]+>", re.S)


def strip_html(text: str) -> str:
    """剥除 ruleDesc 里的 HTML/<span> 标记，得到纯文本。"""
    if not text:
        return ""
    return _SPAN_RE.sub("", text).replace("&nbsp;", " ").strip()


def _rule_features(rc: dict) -> dict:
    """提取规则结构化特征（objects/fields/运算符集合/树深）。"""
    objects = set()
    fields = set()
    ops = set()
    depth = 0

    def walk(n, d=1):
        nonlocal depth
        if not isinstance(n, dict):
            return
        depth = max(depth, d)
        if n.get("nodeType") in ("FIELD",):
            if n.get("fieldPath"):
                fields.add(n["fieldPath"])
                first = n["fieldPath"].split(".")[0]
                if first not in ("规则预定义变量", "方法参数", "当前规则"):
                    objects.add(first)
        code = n.get("code") or n.get("expressionId")
        if code and code not in ("left", "right"):
            ops.add(code)
        for k in ("left", "right"):
            if isinstance(n.get(k), dict):
                walk(n[k], d + 1)
        pv = n.get("paramValue")
        if isinstance(pv, dict):
            for v in pv.values():
                if isinstance(v, dict):
                    walk(v, d + 1)
    walk(rc)
    return {"objects": sorted(objects), "fields": sorted(fields),
            "ops": sorted(ops), "depth": depth}


def build_corpus_index(samples_dir: str, db_path: str) -> dict:
    """扫描语料，写入规则索引表。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    DROP TABLE IF EXISTS rules;
    CREATE TABLE rules(
        rule_id TEXT PRIMARY KEY,
        rule_name TEXT,
        rule_desc TEXT,
        raw_desc TEXT,
        objects TEXT,
        fields TEXT,
        ops TEXT,
        depth INTEGER
    );
    CREATE INDEX idx_rules_name ON rules(rule_name);
    """)
    cur = conn.cursor()
    n = 0
    n_desc = 0
    for sample in iter_sample_lines(samples_dir):
        rule_node = sample.get("rule_node") or {}
        if_rule = sample.get("if_rule") or {}
        rid = (if_rule.get("ruleId") or rule_node.get("ruleId") or "")
        rname = (if_rule.get("ruleName") or rule_node.get("ruleName") or "")
        raw_desc = if_rule.get("ruleDesc") or ""
        rc = parse_rule_content(if_rule.get("ruleContent"))
        feats = _rule_features(rc) if rc else {"objects": [], "fields": [], "ops": [], "depth": 0}
        desc = strip_html(raw_desc)
        if desc:
            n_desc += 1
        cur.execute(
            "INSERT OR REPLACE INTO rules"
            "(rule_id,rule_name,rule_desc,raw_desc,objects,fields,ops,depth)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (rid, rname, desc, raw_desc,
             json.dumps(feats["objects"]), json.dumps(feats["fields"]),
             json.dumps(feats["ops"]), feats["depth"]))
        n += 1
    conn.commit()
    stat = {"rules": n, "with_desc": n_desc}
    conn.close()
    return stat


class RuleCorpus:
    """规则语料内存索引（BM25 + 特征过滤）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.rules: list[dict] = []
        self._bm25 = None

    def load(self) -> "RuleCorpus":
        if not os.path.exists(self.db_path):
            return self
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.rules = [dict(r) for r in conn.execute("SELECT * FROM rules")]
        conn.close()
        return self

    def _init_bm25(self):
        if self._bm25 is None:
            # 轻量 BM25（对规则 desc 做子串/关键词打分），语义留扩展位
            class _BM25:
                def __init__(self, rules):
                    self.rules = rules
                    self._index = None
                def search(self, query, top_k):
                    # 简化：子串+关键词打分
                    q = query
                    scored = []
                    for r in self.rules:
                        text = (r.get("rule_desc") or "") + " " + (r.get("rule_name") or "")
                        if q and q in text:
                            scored.append((1.0, r))
                        else:
                            score = sum(1 for tok in q if tok and tok in text)
                            if score:
                                scored.append((score * 0.5, r))
                    scored.sort(key=lambda x: -x[0])
                    return [r for _, r in scored[:top_k]]
            self._bm25 = _BM25(self.rules)
        return self._bm25

    def search(self, query: str, top_k: int = 8,
               obj_filter: Optional[str] = None,
               op_filter: Optional[str] = None) -> list[dict]:
        """检索相似规则（供「从此续写」/ few-shot）。"""
        bm25 = self._init_bm25()
        hits = bm25.search(query, top_k * 3)
        out = []
        for r in hits:
            objs = json.loads(r.get("objects") or "[]")
            ops = json.loads(r.get("ops") or "[]")
            if obj_filter and obj_filter not in objs:
                continue
            if op_filter and op_filter not in ops:
                continue
            out.append({
                "rule_id": r.get("rule_id"),
                "rule_name": r.get("rule_name"),
                "rule_desc": r.get("rule_desc"),
            })
            if len(out) >= top_k:
                break
        return out
