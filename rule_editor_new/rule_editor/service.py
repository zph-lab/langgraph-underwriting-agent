"""服务装配：初始化 BOM 目录，并暴露统一入口。

v2（重构）：放弃状态机引导式编辑，改为自由文本 + 空格分隔词检索。
"""
from __future__ import annotations

import os
from typing import Optional

from .config import load_config, get
from .core.textsearch import TextSearchService
from .etl.bom_catalog import BomCatalog


class EditorService:
    """一次初始化，供 API/测试复用。"""

    def __init__(self):
        load_config()
        self.cat = BomCatalog(get("etl", "bom_db"))
        self.cat.connect()
        self.cat.build_from_db()
        self.search = TextSearchService(self.cat)

    # ---------- 词检索 ----------
    def search_word(self, current_word: str = "", prev_word: str = "",
                    prevprev_word: str = "", limit: int = 50,
                    predefines: str = "") -> list[dict]:
        """检索当前词候选。

        current_word 非空 -> 路径 + 运算符/连接词。
        current_word 空 -> 根据 prev_word / prevprev_word 智能：
            上上一个词是运算符（prev 是手输值）-> 给连接词/运算符；
            上一词是运算符/连接词/如果 -> 给一级对象；
            上一词是 BOM 对象 -> 给运算符/连接词。
        predefines：编辑区预定义行（\n 分隔），解析成功后注入预定义词条
        （一级对象追加"规则预定义变量"，其下级别名/子字段进检索池）。
        """
        return self.search.search(current_word, prev_word, prevprev_word, limit, predefines)

    def search_predefine(self, kind: str = "type", word: str = "", limit: int = 50,
                         type_name: str = "", field: str = "") -> list[dict]:
        """预定义主句/条件子句候选检索。

        kind="type" -> BOM 中 type=list 节点的 actualTypeNames[0]（去重）；
        kind="path" -> type=list 节点组合路径（'root'的p1的p2）；
        kind="cond_field" -> 条件字段候选（type_name 对应列表节点的子字段）；
        kind="cond_op" -> 条件运算符候选（field 指定字段名，按字段类型给运算符）。
        type_name 非空时只返回该类型对应的列表路径（与"为一个"后的类型一一对应）。
        """
        return self.search.search_predefine(kind, word, limit, type_name, field)

    def annotate_predefine_line(self, line: str, predefines: str = "") -> list[dict]:
        """预定义行专用标注：主句模板切词 + "符合条件"子句校验（用于前端着色/标红）。
        predefines 用于识别表达式右值里引用的其它预定义别名（'前置赔付率' 之类）。"""
        return self.search.annotate_predefine_line(line, predefines)

    def search_methods(self, word: str = "", limit: int = 50) -> list[dict]:
        """then/else 动作候选：只给 BOM 方法节点（候选来源纯方法）。"""
        return self.search.search_methods(word, limit)

    def method_param_context(self, line: str = "", caret: int = -1,
                             limit: int = 50) -> dict:
        """then/else 参数位上下文：定位光标所在参数位并按类型给候选（field 给 BOM 路径）。"""
        return self.search.method_param_context(line, caret, limit)

    def predefine_param_context(self, line: str = "", caret: int = -1,
                                limit: int = 50, predefines: str = "") -> dict:
        """预定义右值方法实参位上下文：按 detailsList 参数类型给路径候选（2026-09-11）。

        predefines（2026-09-29）：全部预定义行，rvalue 候选混入本块别名及其子字段。
        """
        return self.search.predefine_param_context(line, caret, limit, predefines)

    def annotate_then_else_line(self, line: str) -> list[dict]:
        """then/else 行（'那么'/'否则' 开头）专用标注：整行一段灰显，不校验。"""
        return self.search.annotate_then_else_line(line)

    def stats(self) -> dict:
        return self.search.stats()

    def annotate(self, text: str = "", predefines: str = "") -> list[dict]:
        """词类型标注：解析整段文本，逐词标注类型（用于前端着色）。

        predefines：编辑区预定义行（\n 分隔），其词条参与标注/校验。
        """
        return self.search.annotate(text, predefines)

    def normalize_pasted_rule(self, text: str) -> str:
        """粘贴整理：主系统规则文案 → 编辑器格式（如果行补空格、那么行删序号）。"""
        return self.search.normalize_pasted_rule(text)

    def desc(self, text: str = "") -> str:
        """预览/校验文本（v2：自由文本即规则，直接返回）。"""
        return text or "（空）"
