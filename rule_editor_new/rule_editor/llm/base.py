"""LLM 统一接口（v1 仅 mock，留实现位）。

LLM 只做「自然语言 -> 结构化槽位 label 引用」的翻译，永远不直接产出 fieldId；
结果随后仍走两级匹配接地 + 校验器把关。
"""
from __future__ import annotations

from typing import Any, Optional


class LLMError(Exception):
    pass


class BaseLLM:
    """统一接口：chat(messages, schema) -> structured。"""

    def chat(self, messages: list[dict], schema: Optional[dict] = None,
             **kwargs) -> Any:
        raise NotImplementedError

    def complete(self, prompt: str, schema: Optional[dict] = None, **kwargs) -> Any:
        return self.chat([{"role": "user", "content": prompt}], schema, **kwargs)


class MockLLM(BaseLLM):
    """占位实现：不做真实调用，返回固定/空结果，并标注未启用。"""

    def __init__(self, reason: str = "LLM 未启用（v1）"):
        self.reason = reason

    def chat(self, messages, schema=None, **kwargs):
        raise LLMError(self.reason)


def get_llm(config: Optional[dict] = None) -> BaseLLM:
    """根据 config.llm 返回适配器。v1 默认 mock。"""
    cfg = config or {}
    provider = (cfg.get("provider") or "mock").lower()
    if provider in ("mock", "none", ""):
        return MockLLM()
    if provider in ("cloud", "openai", "anthropic"):
        # 云端 adapter 留扩展位
        raise NotImplementedError(
            f"cloud adapter ({provider}) not implemented in v1")
    if provider == "local":
        raise NotImplementedError("local lora adapter not implemented in v1")
    return MockLLM()
