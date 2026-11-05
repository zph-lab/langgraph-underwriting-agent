"""Embedding 接口（v1 跳过向量，仅留接口）。

语义检索在 v1 中默认关闭（config.search.use_semantic=false）。
接入时实现：
    class Embedder:
        def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
        def embed_query(self, q: str) -> list[float]: ...
"""
from __future__ import annotations


class BaseEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, q: str) -> list[float]:
        raise NotImplementedError

    @property
    def dim(self) -> int:
        raise NotImplementedError


class DummyEmbedder(BaseEmbedder):
    """占位：不做真实 embedding。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def embed_query(self, q: str) -> list[float]:
        return [0.0]

    @property
    def dim(self) -> int:
        return 1


def get_embedder(name: str = "dummy") -> BaseEmbedder:
    if name in ("dummy", "none", ""):
        return DummyEmbedder()
    # 本地 bge-m3 等：可在此接入 sentence-transformers / FlagEmbedding
    raise NotImplementedError(f"embedder {name} not implemented (v1 only dummy)")
