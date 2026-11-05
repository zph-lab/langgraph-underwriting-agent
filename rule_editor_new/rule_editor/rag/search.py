"""BM25 检索（M2.2 二级兜底）。

语义关闭时用 BM25（jieba 分词）对 BOM 字段 label / field_path 做兜底匹配，
供 FIELD / OBJECT 步骤在字符匹配失败时使用。语义（embedding+RRF）留扩展位。
"""
from __future__ import annotations

import math
from collections import Counter

import jieba

from ..etl.bom_catalog import BomCatalog


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t for t in jieba.lcut(text) if t.strip()]


class BM25Index:
    """针对 BOM 字段构建的小型 BM25 索引（内存）。"""

    def __init__(self, catalog: BomCatalog, k1: float = 1.5, b: float = 0.75):
        self.catalog = catalog
        self.k1 = k1
        self.b = b
        self.docs: list[dict] = []
        self.terms: list[list[str]] = []
        self.doc_len: list[int] = []
        self.idf: dict[str, float] = {}
        self._avgdl = 0.0
        self._built = False

    def build(self) -> None:
        fields = list(self.catalog.field_by_path.values())
        self.docs = fields
        self.terms = []
        self.doc_len = []
        df: Counter = Counter()
        for f in fields:
            text = f"{f.get('label') or ''} {f.get('field_path') or ''}"
            toks = _tokenize(text)
            self.terms.append(toks)
            self.doc_len.append(len(toks))
            for t in set(toks):
                df[t] += 1
        n = len(fields)
        self._avgdl = sum(self.doc_len) / n if n else 0.0
        self.idf = {t: math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
                    for t in df}
        self._built = True

    def search(self, query: str, top_k: int = 20) -> list[dict]:
        if not self._built:
            self.build()
        q_toks = _tokenize(query)
        if not q_toks:
            return []
        scores: list[tuple[float, int]] = []
        for i, terms in enumerate(self.terms):
            dl = self.doc_len[i]
            tf = Counter(terms)
            score = 0.0
            for t in q_toks:
                if t not in self.idf:
                    continue
                f = tf.get(t, 0)
                score += self.idf[t] * (f * (self.k1 + 1) /
                                        (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl)))
            if score > 0:
                scores.append((score, i))
        scores.sort(key=lambda x: -x[0])
        out = []
        for score, i in scores[:top_k]:
            d = self.docs[i]
            out.append({
                "label": d.get("label") or "",
                "field_path": d.get("field_path") or "",
                "field_id": d.get("field_id") or "",
                "type": d.get("type") or "",
                "bm25_score": round(score, 4),
                "source": "bm25",
            })
        return out
