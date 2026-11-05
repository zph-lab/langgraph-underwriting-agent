"""自由文本编辑的词检索服务（重构版）。

放弃状态机引导式编辑，改为纯文本编辑器 + 空格分隔词检索：
- 规则是一段自由文本，如：`保单的个团标志 是 动态域集的个团标志的团单`
- 语义单元（对象路径 / 运算符 / 连接词 / 值）之间用空格分隔
- **按下空格**（进入新词槽位）→ 只给"运算符 + 连接词"候选
- **输入文字**（正在输入当前词）→ 根据当前词检索"多级对象路径"候选，
  同时附带匹配的运算符 / 连接词
- 候选只是提示，用户可不选，自由输入文本

bom 处理：把所有多级对象路径（任意深度）全部提取为检索表：
    `保单`、`保单的个团标志`、`动态域集的个团标志`、`动态域集的个团标志的团单` …
每个条目 = 从根到该节点的 label 用"的"连接。
"""
from __future__ import annotations

import json
import functools
import os
import re
import sqlite3
from typing import Any, Optional

from ..config import get
from ..etl.bom_catalog import BomCatalog


# 空输入候选的方法垫底段上限（2026-10-08）：仅作用于"空词弹一级对象候选"
# 分支（如果/连接词后续写位）——一级对象/预定义根/连接词之后追加最多 100 条
# BOM 方法候选；非空词检索等其他候选逻辑保持原口径不动。
EMPTY_METHOD_TAIL = 100

# 纯英文候选降权（2026-09-21）：**末级名**完全不含中文的字段候选（abs()、
# c2ClassService、getXxx() 等英文名节点，BOM 中 471+ 条）在检索词不含英文字母时
# 整体沉底——中文拼词/空词弹候选不再被英文字段挤占头部；检索词含字母（用户
# 有意搜英文字段/方法）则不降权。按末级名判断：路径前缀"动态域集的帮助"带
# 中文不影响降权（用户看到的是叶子名是英文）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 预定义右值字面常量（2026-09-21）：`指定'别名'为 1232` 直接把常量绑定到预定义
# 变量，不做 BOM 路径校验（此前数字/字符串常量落入路径档，误报
# "对象字段路径不存在"红字并拦截上传）。支持：数字、true/false、
# 日期(时间)、双引号字符串。单引号是别名/root 定界符，不作字符串字面量。
_PD_LITERAL_RE = re.compile(
    r'^(?:[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?'
    r'|true|false|True|False|TRUE|FALSE'
    r'|\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?'
    r'|"[^"]*")$'
)

# 方法头尾部数字槽位括号（如 帮助.向下取整(0)、…取大值(0,1)）：ADP 手写预定义把
# 实参直接填进这层括号，故另登记一个"去槽位"方法头变体（见 _ensure_pd_scan_index）
_PD_SLOT_TAIL_RE = re.compile(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)$")


def _en_pen(word: str, node: str) -> int:
    """排序档位：1=末级名纯英文候选沉底，0=正常。档位置于排序键首位。"""
    if not node or _CJK_RE.search(node):
        return 0
    if not _LATIN_RE.search(node):
        return 0                     # 纯数字/符号不算"英文字段"
    if _LATIN_RE.search(word or ""):
        return 0                     # 检索词含字母：有意搜英文名，不降权
    return 1


# 运算符候选（全量，按固定顺序）
OPERATOR_CANDIDATES: list[dict] = [
    {"label": "属于...中的一个", "code": "STR_BELONG_TO", "kind": "operator"},
    {"label": "不属于...中的一个", "code": "STR_NOT_BELONG_TO", "kind": "operator"},
    {"label": "是", "code": "STR_EQ", "kind": "operator"},
    {"label": "等于", "code": "EQ", "kind": "operator"},
    {"label": "包含", "code": "STR_INCLUDE", "kind": "operator"},
    {"label": "不包含", "code": "STR_NOT_INCLUDE", "kind": "operator"},
    {"label": "大于等于", "code": "GE", "kind": "operator"},
    {"label": "大于", "code": "GT", "kind": "operator"},
    {"label": "小于等于", "code": "LE", "kind": "operator"},
    {"label": "小于", "code": "LT", "kind": "operator"},
    {"label": "不为空", "code": "STR_NOT_EMPTY", "kind": "operator"},
    {"label": "为空", "code": "STR_EMPTY", "kind": "operator"},
    {"label": "以...开头", "code": "STR_START_WITH", "kind": "operator"},
    {"label": "以...结尾", "code": "STR_END_WITH", "kind": "operator"},
    # ---- 运算符924 档1：集合(list)系 + 区间 + 字符串否定式 ----
    {"label": "不是", "code": "STR_NE", "kind": "operator"},
    {"label": "不以...开头", "code": "STR_NOT_START_WITH", "kind": "operator"},
    {"label": "不以...结尾", "code": "STR_NOT_END_WITH", "kind": "operator"},
    # 区间系（运算符924 词形「在 起 和 止 之间」）：一条通用候选，
    # 上传端按字段类型自动选 NUM/DATE/TIME_BETWEEN；旧词形"介于 起,止"仍兼容解析
    {"label": "在...和...之间", "code": "DATE_BETWEEN", "kind": "operator"},
    {"label": "部分属于", "code": "COLLECTION_ANY_BELONG_TO", "kind": "operator"},
    {"label": "全部属于", "code": "COLLECTION_ALL_BELONG_TO", "kind": "operator"},
    {"label": "部分包含", "code": "COLLECTION_ANY_CONTAINS", "kind": "operator"},
    {"label": "全部包含", "code": "COLLECTION_ALL_CONTAINS", "kind": "operator"},
    {"label": "不包含集合", "code": "COLLECTION_NOT_IN_CONTAINS", "kind": "operator"},
]

# 一元运算符（空判断）：本身不带值，其后可直接接连接词（如「不为空 或者」），
# 连接词后也可直接接一元运算符（如「或者 为空」）。相邻词规则4需放行此类组合。
UNARY_OPERATORS: set[str] = {"不为空", "为空"}

# 多词（头...尾）运算符：形如「属于 [值] 中的一个」「以 [值] 开头」「以 [值] 结尾」，
# 头部运算符后必须跟一个值，再跟尾部运算符。头尾直接相连（中间缺值）即非法。
# 尾词 -> 期望的头部运算符（用于配对与错误提示）。
# 区间系（运算符924）「在 [起] 和 [止] 之间」为三段式：头部"在" + 中段"和" + 尾部"之间"，
# 起/止两个值位由 annotate 主循环按 between_phase 状态机做单值类型校验。
MULTI_OP_HEADS: set[str] = {"属于", "不属于", "以", "不以", "在"}
# 尾词 -> 合法头部集合（"不以"与"以"共用 开头/结尾 尾词）
MULTI_OP_TAILS: dict[str, set[str]] = {
    "中的一个": {"属于", "不属于"}, "开头": {"以", "不以"}, "结尾": {"以", "不以"},
    "之间": {"在"},
}
BETWEEN_HEAD = "在"          # 区间句式头词
BETWEEN_MID = "和"           # 区间句式中段词（独立词类 operator_mid）
BETWEEN_TAIL = "之间"        # 区间句式尾词
BETWEEN_LEGACY = "介于"      # 旧词形（"介于 起,止"单 token），兼容解析不再作候选

# 内容长度修饰符：作用在字段（string）上的一元"长度"函数（非终结运算符），
# 其后须再接一个数值比较符 + 整数，如「保单的代理人 的内容长度 大于 5」，
# 上传发 STR_LENGTH（EXPRESSION 包裹字段 param1）。它是"修饰符"而非运算符，
# 不进 OPERATOR_CANDIDATES（避免被当成"运算符→值"的终结运算符而截断两段式）。
LEN_MODIFIERS: set[str] = {"的内容长度", "内容长度"}
# 修饰符之后可选的数值比较符（长度结果恒为整数）
LEN_COMPARISON_CANDIDATES: list[dict] = [
    {"label": "大于", "code": "GT", "kind": "operator"},
    {"label": "小于", "code": "LT", "kind": "operator"},
    {"label": "大于等于", "code": "GE", "kind": "operator"},
    {"label": "小于等于", "code": "LE", "kind": "operator"},
    {"label": "等于", "code": "EQ", "kind": "operator"},
    {"label": "不等于", "code": "NE", "kind": "operator"},
]

# 算术运算符（"如果"条件/预定义右值专用词类，2026-09-24 引入）：
# 数值操作数之间的四则运算，候选主词形为中文 加/减/乘/除（desc 规范文本、
# 生成产物 label 均以中文为准）；符号 + - * / 与全角（＋－＊／×÷）保留为
# 手输兼容别名——用户直接敲符号同样识别为 arith 词类（标紫、参与检索匹配），
# 上传时生成端照常解析（词形归一为中文输出）。code 对齐
# ADD/SUB/MULTIPLY/DIVIDE，与生成端 desc_to_rulecontent.ARITH_OPS 双口径
# 同映射，_parse_left_arith/_parse_operand 原生支持
# "字段 减 字段 大于 值"两侧算术链，编辑器检索/标注/校验三端共用本词表。
# 例外：预定义子句右值运算符维持符号口径（_PD_ARITH_OPS 仅认 + - * /）——
# 预定义行整行直发 ADP 解析，中文运算符词形不被接受，手输中文在该场景标红。
# 注意：生成端 _find_arith_op 对符号要求前侧为空白/行首（防日期 2026-09-22、
# 比值串 2/3、label 内"保额/限额(元)"被误切）；前端规范文本（renderWords）对
# arith 词保留前后空格（见 app.js spaced），手输无空格形（金额+100/+100）由
# _segment_words 的符号边界+前置符号拆分兜住。
ARITH_CANDIDATES: list[dict] = [
    {"label": "加", "code": "ADD", "kind": "arith", "alias": "+"},
    {"label": "减", "code": "SUB", "kind": "arith", "alias": "-"},
    {"label": "乘", "code": "MULTIPLY", "kind": "arith", "alias": "*"},
    {"label": "除", "code": "DIVIDE", "kind": "arith", "alias": "/"},
]
# 一切算术词形 → 半角规范符号（内部比较键；含中文主词形与全角变体，×÷ 归 */）
ARITH_NORMALIZE: dict[str, str] = {
    "+": "+", "＋": "+", "加": "+",
    "-": "-", "－": "-", "减": "-",
    "*": "*", "＊": "*", "×": "*", "乘": "*",
    "/": "/", "／": "/", "÷": "/", "除": "/",
}
# 符号类词形（作分词边界/前置拆分用；中文 加/减/乘/除 不做边界，防
# "乘客人数"这类未注册 label 被从中间切开）
ARITH_SYM_CHARS: set[str] = set("+-*/＋－＊／×÷")
# 正负号类（可能与负数字面量粘连，拆分需上下文判定）
ARITH_SIGN_CHARS: set[str] = set("+-＋－")
ARITH_WORDS: set[str] = (
    {c["label"] for c in ARITH_CANDIDATES}
    | {c["alias"] for c in ARITH_CANDIDATES}
    | set(ARITH_NORMALIZE)
)

# 连接词候选
CONNECTOR_CANDIDATES: list[dict] = [
    {"label": "并且", "code": "AND", "kind": "connector"},
    {"label": "或者", "code": "OR", "kind": "connector"},
]

# 规则起始/固定词（自由文本可选的引导词）
KEYWORD_CANDIDATES: list[dict] = [
    {"label": "如果", "code": "IF", "kind": "keyword"},
    {"label": "那么", "code": "THEN", "kind": "keyword"},
    {"label": "否则", "code": "ELSE", "kind": "keyword"},
]


class TextSearchService:
    """词检索服务：多级对象路径索引 + 当前词检索。"""

    # n-gram 权重：n 越大权重越大（1-gram 保证召回，2/3-gram 提升精确）
    NGRAM_WEIGHTS: dict[int, int] = {1: 1, 2: 2, 3: 3}

    def __init__(self, cat: BomCatalog):
        self.cat = cat
        # 多级对象路径条目：{label_path, field_path, kind, leaf_type, node_label}
        self.path_index: list[dict] = []
        self._build_path_index()
        # 所有路径 label 集合（用于语义分词：优先匹配完整字段路径，含内部空格）
        self._label_set: set[str] = {item["label"] for item in self.path_index if item.get("label")}
        # n-gram 兜底索引（精确匹配 0 候选时启用）
        self._ng_doc_grams: list[dict[int, set]] = []   # 每条目 {n: gram集合}
        self._ng_inverted: dict[str, set[int]] = {}      # gram -> 条目索引集合
        self._build_ngram_index()
        # 预定义主句索引：BOM 中 type=list 节点（懒加载，见 _ensure_list_index）
        self._list_index: Optional[list[dict]] = None
        # 预定义词条（规则模式注入，见 predefine_lex / _set_pd_lex）：
        # 每次 search/annotate 调用按编辑区预定义行重建
        self._pd_lex: list[dict] = []
        self._pd_by_label: dict[str, dict] = {}
        # then/else 动作方法索引：BOM 方法节点全量（含同名重载/多路径）。
        # cat.methods 由 BomCatalog 从 DB 直接收集（field_by_path 按 field_path 覆盖会丢重载）。
        # 方法 label 为参数化模板（0/1/2… 为参数槽位，对应 detailsList.paramId）。
        # 方法完整路径 label（父链+方法，如 动态域集的帮助帮助.0与1相差的天数()），
        # 供 predefine 场景以带前缀形态检索/标注匹配
        self._method_full_labels: dict[str, set[str]] = {}
        # 带"的"上级对象展示形态（样本/desc 成品：动态域集的帮助的帮助.取大值(0,1)，
        # 拼接处带"的"；path_index label 是 ADP methodName 形态"帮助帮助"不带），
        # 供 predefine 右值方法候选显示/模板插入使用
        self._method_disp_labels: dict[str, list[str]] = {}
        for item in self.path_index:
            fp2 = item.get("field_path") or ""
            if fp2.endswith("()") and item.get("label"):
                lp = item["label"]
                bucket = self._method_full_labels.setdefault(fp2, set())
                bucket.add(lp)
                nl = item.get("node_label") or ""
                if nl and lp.endswith(nl) and len(lp) > len(nl):
                    disp = lp[:-len(nl)] + "的" + nl
                    bucket.add(disp)   # 参与检索匹配与标注扫描
                    dl = self._method_disp_labels.setdefault(fp2, [])
                    if disp not in dl:
                        dl.append(disp)
        self._method_index: list[dict] = []
        for f in self.cat.methods:
            label = f.get("label") or ""
            fp = f.get("field_path") or ""
            # 参数槽位 = label 中"孤立单数字"（0/1/2…，前后非数字，如 '：0,' '为:1'）；
            # 险别代码等连续数字串（061002）不算槽位。
            nums = [int(x) for x in re.findall(r"(?<!\d)\d(?!\d)", label) if x.isdigit()]
            self._method_index.append({
                "label": label,
                "field_path": fp,
                "class_path": f.get("class_path") or "",
                "field_id": f.get("field_id") or "",
                # 方法返回类型（BOM type 列：void/string/double…），候选框类型标签用
                "type": (f.get("type") or "").strip().lower(),
                "param_count": (max(nums) + 1) if nums else 0,
                "full_labels": sorted(self._method_full_labels.get(fp, ())),
                "disp_labels": list(self._method_disp_labels.get(fp, ())),
            })
        # then/else 方法注册表（bom_methods_<samples_dir>.json，含 expressionArray）懒加载缓存
        self._method_reg: Optional[list[dict]] = None
        # 方法参数类型（bom.json detailsList，fieldPath/label → [type…]）懒加载缓存
        self._method_types: Optional[dict] = None
        # 预定义右值方法扫描：head 首字符 → [(head, it)] 分桶索引。
        # 定制正则首个必选 token 恒为 re.escape(head[0])，p 须以该字符开头才可能命中，
        # 故按首字符分桶可将全表扫描（车险数千方法 ~9.6s）降到极少候选。
        # 启动期构建，避免服务起来后第一个按键承担一次性建索引开销（车险 ~1s）。
        self._pd_scan_index: Optional[dict] = None
        self._pd_scan_fallback: Optional[list] = None
        self._pd_scan_memo: Optional[dict] = None
        self._pd_label_map_maxlen: int = 0
        self._ensure_pd_scan_index()
        # 参数位候选：按类型族分桶的非方法路径池（懒建，见 _ensure_pd_type_pools）
        self._pd_type_pools: Optional[dict] = None

    def _load_method_registry(self) -> list[dict]:
        """懒加载方法注册表（含 expressionArray），用于渲染占位模板。"""
        if self._method_reg is not None:
            return self._method_reg
        self._method_reg = []
        try:
            import sys as _sys
            from ..config import project_root as _pr
            root = _pr()
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from desc_to_rulecontent.write_rule_to_adp import default_methods_registry
            self._method_reg = default_methods_registry() or []
        except Exception:
            self._method_reg = []
        return self._method_reg

    def _selector_catalog(self):
        from desc_to_rulecontent.selector_values import SelectorCatalog
        if not hasattr(self, '_selectors'):
            try:
                with open(get('data', 'bom_path'), encoding='utf-8') as stream:
                    self._selectors = SelectorCatalog(json.load(stream))
            except (OSError, ValueError, TypeError):
                # 字典不可用不影响原有基于 SQLite 的普通字段候选。
                self._selectors = SelectorCatalog({})
        return self._selectors

    def selector_context(self, line: str, caret: int) -> dict:
        """返回光标处字典值区间，供下拉整段回填；不跨条件检索字典。"""
        from desc_to_rulecontent.selector_values import SELECTOR_RE
        catalog = self._selector_catalog()
        caret = max(0, min(caret, len(line)))
        for match in SELECTOR_RE.finditer(line):
            start = match.end()
            while start < len(line) and line[start].isspace():
                start += 1
            tail = re.search(r'\s+(?:并且|或者)\s+|\s+[)）](?=\s|$)', line[start:])
            end = start + tail.start() if tail else len(line)
            if not (match.end() <= caret <= end):
                continue
            prefix = line[:match.start()].rstrip()
            fields = [(label, field) for label, field in catalog.by_label.items()
                      if prefix.endswith(label)]
            field = max(fields, key=lambda pair: len(pair[0]))[1] if fields else None
            options = catalog.options(field)
            return {'active': True, 'start': start, 'end': end, 'multiple': match[2] == '多选',
                    'codeService': (field or {}).get('codeService'), 'options': options,
                    'raw': line[start:end].strip(),
                    'error': '' if options else '当前字段没有可用的 codeService 字典选项'}
        return {'active': False}

    # ---------- 预定义右值方法（2026-09-11）：参数类型 + call 模板 ----------
    def _ensure_method_types(self) -> None:
        """懒加载 BOM 方法参数类型（bom.json detailsList → 按 paramId 序排 type 列表）。

        双 key 索引：fieldPath 与 label（含尾部"()"完整形态）；同名重载（同 key
        不同 detailsList）存多个类型列表，渲染时按 label 槽位数取最贴近的一组。
        """
        if self._method_types is not None:
            return
        self._method_types = {}
        bom_path = get("data", "bom_path")
        if not bom_path or not os.path.exists(bom_path):
            return
        try:
            with open(bom_path, encoding="utf-8") as f:
                bom = json.load(f)
        except Exception:
            return

        def param_sort_key(pid: str):
            m = re.search(r"(\d+)$", pid or "")
            return (int(m.group(1)) if m else 0, pid or "")

        def walk(node: dict) -> None:
            if not isinstance(node, dict):
                return
            fp = node.get("fieldPath") or node.get("field_path") or ""
            label = node.get("label") or ""
            if (fp.endswith("()") or node.get("dataType") == "method") and label:
                dl = node.get("detailsList") or []
                types = [d.get("type") or "" for d in sorted(
                    (d for d in dl if isinstance(d, dict)),
                    key=lambda d: param_sort_key(d.get("paramId") or ""))]
                for key in {fp, label}:
                    if key:
                        lst = self._method_types.setdefault(key, [])
                        if types and types not in lst:
                            lst.append(types)
            for ch in node.get("children") or []:
                if isinstance(ch, dict):
                    walk(ch)

        walk(bom)

    @staticmethod
    def _strip_tail_empty_parens(label: str) -> str:
        """剥方法 label 尾部的空"()"后缀（签名括号/槽位保留）：帮助.取大值(0,1)() → 帮助.取大值(0,1)。"""
        return label[:-2] if label.endswith("()") else (label or "")

    def render_call_template(self, label: str, types: Optional[list[str]]) -> str:
        """预定义右值方法模板：label 剥尾"()" + "(<类型> , <类型>)"。

        如 帮助.0与1相差的天数() + [date, date] → 帮助.0与1相差的天数(<date> , <date>)
        与 desc 成品形态一致（方法名槽位保留，实参集中于尾部括号）；
        类型缺失的参数位回退 <text>；无参方法原样返回 label。
        """
        head = self._strip_tail_empty_parens(label)
        ts = [t for t in (types or []) if t]
        if not ts:
            return label or head
        return head + "(" + " , ".join("<%s>" % t for t in ts) + ")"

    def _call_types_for(self, it: dict) -> Optional[list[str]]:
        """方法条目 → 一组参数类型（detailsList 多组时按 label 槽位数取最贴近的一组）。"""
        self._ensure_method_types()
        label = it.get("label") or ""
        cands = ((self._method_types or {}).get(it.get("field_path") or "")
                 or (self._method_types or {}).get(label)) or []
        if not cands:
            return None
        pc = it.get("param_count") or 0
        return next((x for x in cands if len(x) == pc), cands[0])

    @staticmethod
    @functools.lru_cache(maxsize=65536)
    def _pd_method_head_re(head: str) -> "re.Pattern":
        """方法头 → 宽松匹配正则。

        head 中"数字槽位/签名段"（如 (0,1)）必选保留；非数字注记段（如 (浮点数)，
        平台渲染的返回类型说明）做成可选——手打/编辑器插入可不带注记。
        """
        # 2026-09-22：可选组一律用占有量词 ?+（Python≥3.11）。长 label 有几十个
        # 相邻可选"的/空白"组，普通 ? 在后文失配时回溯枚举全部切分方式 →
        # 指数级灾难性回溯（'保' 桶 2907 个 head，单行扫描可卡死数十秒）。
        # ?+ = 每组至多贪婪一次且不还手，可消费字符集不变（语义等价），匹配线性。
        pat = []
        for seg in re.split(r"(\([^()]*\))", head):
            if seg.startswith("(") and seg.endswith(")"):
                if re.fullmatch(r"\d+(?:\s*,\s*\d+)*", seg[1:-1]):
                    pat.append(re.escape(seg))
                else:
                    pat.append("(?:%s)?+" % re.escape(seg))
            else:
                # 平台渲染在 label 拼接处可插"的"（BOM 拼接 label 帮助帮助.X，
                # desc 成品可为 帮助的帮助.X）→ 每个字符后容忍一个可选"的"段；
                # label 自身的"的"也做成可选（可省）。贪婪消费原文，
                # token 拼接仍=原文（head 取 m.group(0)）。
                chars = []
                for ch in seg:
                    if ch == "的":
                        chars.append("(?:\\s*的\\s*)?+")
                    else:
                        chars.append(re.escape(ch) + "(?:\\s*的\\s*)?+")
                pat.append("".join(chars))
        return re.compile("".join(pat))

    # ---------- 全/半角括号等价（2026-09-15） ----------
    # 中文输入法手打方法括号易成全角（如 取得结果（）），而 BOM 路径 key 为半角；
    # 同时 BOM 自身有 700+ 合法 label 含全角括号（如 【车上人员责任险（驾驶员）】…），
    # 不能全局归一化文本。策略：精确匹配优先，未命中时按括号等价类重试（只放宽不改写）。
    @staticmethod
    def _paren_trans(s: str) -> str:
        """全角括号→半角（仅供比较用，返回展示文本不做此转换）。"""
        return s.replace("\uff08", "(").replace("\uff09", ")")

    # ---------- 路径比较键（2026-09-21） ----------
    # BOM 有 750+ 合法 label 含内部空格（如 "保单的险别0, 险别()的不计免赔"），
    # 而手打/候选插入的右值常被剥掉空格（'保单'的险别0, 险别()的不计免赔 去空格后
    # 与原文不等），旧逻辑"查询侧去空格、label 侧原样"两侧口径不一致 → 误报
    # "对象字段路径不存在"。统一用一个比较键：剥单引号 + 去所有空白 + 全角标点归一，
    # 两侧同键比较（只放宽比较，不改写展示文本）。
    _PUNCT_TRANS = str.maketrans({"\uff08": "(", "\uff09": ")", "\uff0c": ",",
                                  "\u3001": ",", "\uff1a": ":", "\uff1b": ";"})

    @staticmethod
    def _path_key(s: str) -> str:
        """路径比较键：剥单引号、去所有空白、全角括号/逗号/顿号/冒号/分号→半角。"""
        s = re.sub(r"\s+", "", (s or "").replace("'", "").replace("'", ""))
        return s.translate(TextSearchService._PUNCT_TRANS)

    def _label_keys(self) -> set:
        """_label_set 的比较键镜像（懒建缓存）。"""
        t = getattr(self, "_label_set_keys_cache", None)
        if t is None:
            t = self._label_set_keys_cache = {self._path_key(s) for s in self._label_set}
        return t

    def _label_set_trans(self) -> set:
        """_label_set 的括号等价镜像（懒建缓存）。"""
        t = getattr(self, "_label_set_tr", None)
        if t is None:
            t = self._label_set_tr = {self._paren_trans(s) for s in self._label_set}
        return t

    def _find_list_node(self, pk: str) -> Optional[dict]:
        """列表路径命中：精确匹配优先，失败按比较键（空格/全半角）等价重试。"""
        node = next((it for it in self._list_index if it["key"] == pk), None)
        if node is None:
            k = self._path_key(pk)
            node = next((it for it in self._list_index
                         if self._path_key(it["key"]) == k), None)
        return node

    def _match_pd_method(self, path: str) -> Optional[tuple[str, str]]:
        """预定义右值方法匹配：path 以"方法label(实参…)"开头 → (方法头原文, 其后文本)。"""
        r = self._pd_method_scan((path or "").strip())
        return (r[0], r[1]) if r else None

    @staticmethod
    def _pd_head_prefilter(head: str, p: str) -> bool:
        """必选字符序快速前判（仅用于首字符分桶命中、head[0] 非"的"/"(" 的 head）。

        head 的必选字符 = 首个"("括号段前、非"的"的字符（head 中"的"→可选组；
        遇"("保守放行交定制正则精判）。定制正则逐字为 必选字+`(?:\\s*的\\s*)?`，
        故必选字符须自 p[0] 起依序可定位、字符间仅可跳"的"/空白；
        不符则正则必不中——跳过编译/匹配（误放由正则终判，保守安全）。
        """
        i = 0
        n = len(p)
        for ch in head:
            if ch == "(":
                return True
            if ch == "的":
                continue
            if i >= n:
                return False
            if p[i] == ch:
                i += 1
                continue
            j = i
            while j < n and p[j] != ch:
                if p[j] != "的" and not p[j].isspace():
                    return False
                j += 1
            if j >= n:
                return False
            i = j + 1
        return True

    def _ensure_pd_scan_index(self) -> None:
        """懒建 head 前 3 必选字符分桶索引（2026-09-22 细化，_pd_b3/_pd_b2/_pd_b1）。

        "的"是 head 每个字符后的可选组，必选字符 = head 剥"的"、遇"("截断后的
        序列。首字符桶（'保' 2907 个"保单的…"方法头）在 _pd_expr_parse 逐字符位
        试探时被整体扫过 → 千万级 Python 循环、单行卡死数十秒。按必选前缀长度
        分 3 档桶，扫描端算出 p 的必选前缀后按 3→2→1 档逐级取桶并集
        （head 必选前缀比 p 短时落在短档，仍可被前缀命中）。
        head 以"的"或"("开头时，首个必选 token 不确定（前导可选组），
        无法用 head[0] 锁桶、也不适用前判，归入 fallback 全扫（实际几乎不存在）。
        """
        if self._pd_scan_index is not None:
            return
        b3: dict = {}
        b2: dict = {}
        b1: dict = {}
        fb: list = []
        for it in self._method_index:
            for cand in {it.get("label") or "", *(it.get("full_labels") or ())}:
                head = self._strip_tail_empty_parens(cand)
                if not head:
                    continue
                # 2026-09-21：ADP 手写样本把实参直接填进数字槽位括号
                # （帮助.向下取整( 表达式 )），而编辑器模板形态保留槽位再追加
                # 实参括号（…取大值(0,1)(1 , 2)）。两种都登记：额外索引一个
                # "去尾部数字槽位"变体；扫描取消费最长者，模板形态优先不受影响。
                heads = [head]
                h2 = _PD_SLOT_TAIL_RE.sub("", head)
                if h2.strip() and h2 != head:
                    heads.append(h2)
                for h in heads:
                    if h[0] in ("(", "的"):
                        fb.append((h, it))
                        continue
                    k = [h[0]]
                    for ch2 in h[1:]:
                        if ch2 == "的":
                            continue
                        if ch2 in "(（":
                            break
                        k.append(ch2)
                        if len(k) == 3:
                            break
                    key = "".join(k)
                    {1: b1, 2: b2, 3: b3}[len(key)].setdefault(key, []).append((h, it))
        self._pd_scan_index = {"b3": b3, "b2": b2, "b1": b1}
        self._pd_scan_fallback = fb

    def _pd_method_scan(self, p: str) -> Optional[tuple[str, str, int, dict]]:
        """memo 壳：_pd_expr_parse 对同一 txt 的同一后缀串可能重复试探
        （算术式里 " - '当前险别'的保额/限额…" 同形片段多处出现），按 p 缓存。
        2026-09-22 车险示例实测单行词法 25s+ → 缓存后 <1s。"""
        if not p:
            return None
        memo = self._pd_scan_memo
        if memo is None:
            memo = self._pd_scan_memo = {}
        try:
            return memo[p]
        except KeyError:
            pass
        r = self._pd_method_scan_core(p)
        if len(memo) > 60000:
            memo.clear()
        memo[p] = r
        return r

    def _pd_method_scan_core(self, p: str) -> Optional[tuple[str, str, int, dict]]:
        """同 _match_pd_method，但输入须已 strip，返回 (方法头, tail, 消费长度, 方法条目)。

        方法头 = label 剥尾部空"()"（签名/槽位保留，如 帮助.取大值(0,1)），
        非数字括号注记段（(浮点数)）可选跳过；同时支持完整路径形态
        （动态域集的帮助帮助.0与1相差的天数(…)）。多个方法命中时取消费最长者
        （区分 0与1相差的天数 与 …天数(浮点数) 两个变体）。未命中返回 None。
        tail 保持原文（含方法头后的空白）——调用方按"方法头 + 其余"切词，
        拼接需与原行严格相等；实参段整段灰显（无需括号配平）。

        性能（车险数千 head 全表正则 ~9.6s/次）：先按首字符分桶，
        桶内再过 _pd_head_prefilter 必选字符前判，仅极少数进入正则编译/匹配。
        """
        if not p:
            return None
        self._ensure_pd_scan_index()
        best: Optional[tuple[str, str, int, dict]] = None
        best_end = 0
        # p 的必选字符前缀（≤3 字，剥"的"/空白、遇括号截断）——与登记端同口径；
        # head 必选前缀比 p 前缀短（括号截断/短头）时落在 b2/b1 短档，按前缀并集取回
        k = [p[0]]
        for ch2 in p[1:]:
            if ch2 == "的" or ch2.isspace():
                continue
            if ch2 in "(（":
                break
            k.append(ch2)
            if len(k) == 3:
                break
        key = "".join(k)
        idxs = self._pd_scan_index
        cands: list = []
        if len(key) >= 3:
            cands = list(idxs["b3"].get(key, ()))
        if len(key) >= 2:
            cands += list(idxs["b2"].get(key[:2], ()))
        cands += list(idxs["b1"].get(key[:1], ()))
        for head, it in cands:
            if not self._pd_head_prefilter(head, p):
                continue
            m = self._pd_method_head_re(head).match(p)
            if not m:
                continue
            tail = p[m.end():]
            # 方法头与实参括号间容忍编辑辅助空格
            if not tail.lstrip().startswith("("):
                continue
            if m.end() > best_end:
                best = (m.group(0), tail, m.end(), it)
                best_end = m.end()
        for head, it in self._pd_scan_fallback:
            m = self._pd_method_head_re(head).match(p)
            if not m:
                continue
            tail = p[m.end():]
            if not tail.lstrip().startswith("("):
                continue
            if m.end() > best_end:
                best = (m.group(0), tail, m.end(), it)
                best_end = m.end()
        if best_end > 0:
            return best
        # 2026-09-21：容忍右值开头的 'root'的 引号段。字段路径右值按 ADP 语法根对象
        # 带引号（样本 118/118），用户从字段候选接续输入方法时会把引号段一起带进来；
        # 而方法头是**去引号**的 BOM 拼接形态，首字符分桶取不到 "'" → 方法匹配整体
        # 失败 → 误报"对象字段路径不存在"。此处去引号重试并把消费长度映射回原文
        # （引号段比原文多 2 字符），使标注/实参位检索/词条注册共用同一套方法识别。
        rm = re.match(r"^'([^']*)'的", p)
        if not rm:
            return None
        full = rm.group(1) + "的" + p[rm.end():]
        fs = self._pd_method_scan(full)
        if fs is None:
            return None
        end = fs[2] + rm.end() - len(rm.group(1)) - 1
        return p[:end], p[end:], end, fs[3]

    # ---------- 预定义方法实参位上下文（2026-09-11） ----------
    _PD_NUMERIC_TYPES = {"double", "float", "integer", "int", "long", "bigint",
                         "bigdecimal", "number", "currency", "short", "byte"}
    _PD_DATE_TYPES = {"date", "datetime", "time", "timestamp"}
    _PD_STRING_TYPES = {"string", "text", "char", "varchar", "str", "enum"}
    _PD_LIST_TYPES = {"list", "array", "set"}
    _PD_FRAG_DELIMS = "+-*/(),，、;； \t"

    @classmethod
    def _pd_type_family(cls, ptype: str) -> str:
        """detailsList 参数 type → 候选过滤族（未知/对象类返回 ""=不过滤）。"""
        t = (ptype or "").strip().lower()
        if t in cls._PD_NUMERIC_TYPES:
            return "numeric"
        if t in cls._PD_DATE_TYPES:
            return "date"
        if t in cls._PD_STRING_TYPES:
            return "string"
        if t in cls._PD_LIST_TYPES:
            return "list"
        return ""

    @staticmethod
    def _pd_quote_label(label: str) -> str:
        """路径 label 转预定义右值引号形态：保单的X → '保单'的X（已带引号原样）。"""
        parts = (label or "").split("的", 1)
        if len(parts) == 2 and parts[0] and not parts[0].startswith("'"):
            return "'%s'的%s" % (parts[0], parts[1])
        return label or ""

    @staticmethod
    def _pd_subseq(sub: str, s: str) -> bool:
        """非连续字符子序列匹配："交强保费" ⊑ "交强险应缴保费"（模糊兜底）。"""
        it = iter(s)
        return all(ch in it for ch in sub)

    def _ensure_pd_type_pools(self) -> None:
        """懒建按类型族分桶的非方法路径池（参数位候选用，2026-09-14）。

        旧实现召回走 _match_paths（空词/局部词只按 label 浅前缀取头部），
        深层数值字段（如 '保单'的临时信息的精算信息的交强险应缴保费，type=double）
        进不了池，参数位空词/输"保费"都检索不到。此处一次遍历 path_index 分桶：
          numeric/date/string → 该族类型 ∪ 类型未知叶子（排尾沉底，宁多勿误杀）；
          all                 → 全量非方法（参数类型未知、不做族过滤时用）。
        条目预生成带引号 label 与匹配键（lb_key/node_key/depth），击键时族内线性打分。
        """
        if self._pd_type_pools is not None:
            return
        fam_map = {"numeric": self._PD_NUMERIC_TYPES,
                   "date": self._PD_DATE_TYPES,
                   "string": self._PD_STRING_TYPES}
        pools: dict[str, list] = {k: [] for k in ("numeric", "date", "string", "all")}
        seen_lb: set[str] = set()
        for item in self.path_index:
            fp = item.get("field_path") or ""
            if fp.endswith("()"):
                continue                    # 方法节点不进实参路径池（实参要的是值）
            label = item.get("label") or ""
            if "的" not in label:
                continue                    # 一级对象不进值池（与 field_path 候选口径一致）
            t = (item.get("type") or "").strip().lower()
            if (item.get("kind") or "") == "object" and not t:
                fams: tuple[str, ...] = ("all",)   # 类型未知的对象节点只进无过滤池
            else:
                fams = tuple(f for f, allow in fam_map.items()
                             if not t or t in allow)
            lb = self._pd_quote_label(label)
            if not lb or lb in seen_lb:
                continue
            seen_lb.add(lb)
            entry = {"label": lb, "kind": "pfieldpath", "field_path": fp,
                     "type": t,
                     "lb_key": lb.replace("'", "").replace(" ", "")
                                .replace("\u3000", ""),
                     "node_key": (item.get("node_label") or "").replace(" ", ""),
                     "depth": label.count("的")}
            for fam in ("all", *fams):
                pools[fam].append(entry)
        self._pd_type_pools = pools

    def _pd_param_candidates(self, family: str, word: str, limit: int,
                             ptype: str = "") -> list[dict]:
        """方法实参位候选：类型族分桶内召回，按（匹配强度, 类型精确度, 层级）打分排序。

        2026-09-14：先族内召回再打分（旧实现先浅层召回再过滤，深层 numeric/date
        字段永远进不来）。类型档位：参数声明类型精确同型（<double>→type=double）2 >
        同族 1 > 类型未知 0。匹配档位：完全 > 全路径前缀 > 末级名前缀 > 包含 >
        非连续子序列（模糊）；空词（占位符原位）时按（类型, 浅层级）给头部。
        """
        if family == "list":
            self._ensure_list_index()
            pool = [{"label": it["label"], "kind": "ppath", "type": "",
                     "field_path": it.get("field_path") or "",
                     "lb_key": (it["label"] or "").replace("'", "").replace(" ", ""),
                     "node_key": "", "depth": (it.get("label") or "").count("的")}
                    for it in self._list_index if it.get("label")]
        else:
            self._ensure_pd_type_pools()
            pool = (self._pd_type_pools or {}).get(family or "all") or []
        allow = {"numeric": self._PD_NUMERIC_TYPES, "date": self._PD_DATE_TYPES,
                 "string": self._PD_STRING_TYPES}.get(family)
        pt = (ptype or "").strip().lower()
        # 检索词剥引号/空白：参数片段常为 '保单'的X 形态，而池匹配键无引号
        wl = (word or "").replace("'", "").replace(" ", "").replace("\u3000", "")
        scored = []
        for c in pool:
            t = c.get("type") or ""
            t_exact = 2 if (pt and t == pt) else (1 if (allow and t in allow) else 0)
            if not wl:
                s = 0.5
            elif c["lb_key"] == wl:
                s = 1.0
            elif c["lb_key"].startswith(wl):
                s = 0.95
            elif c["node_key"] and c["node_key"].startswith(wl):
                s = 0.9
            elif wl in c["lb_key"]:
                s = 0.6
            elif len(wl) >= 2 and self._pd_subseq(wl, c["lb_key"]):
                s = 0.45
            else:
                continue
            pen = _en_pen(word, c.get("node_key") or "")
            key = ((pen, -s, -t_exact, c["depth"], c["lb_key"]) if wl
                   else (pen, -t_exact, c["depth"], c["lb_key"]))
            scored.append((key, c))
        scored.sort(key=lambda x: x[0])
        return [{"label": c["label"], "kind": c["kind"],
                 "field_path": c["field_path"], "type": c.get("type") or ""}
                for _, c in scored[:limit]]

    @classmethod
    def _pd_frag_start(cls, txt: str, upto: int) -> int:
        """光标位参数片段起点：向左到运算符/括号/逗号/空白；引号段整体不拆
        （'保单'的临时… 中光标停在"的"后 → 片段含整个带引号前缀，替换不吞引号）。"""
        j = upto
        while j > 0:
            ch = txt[j - 1]
            if ch in cls._PD_FRAG_DELIMS:
                return j
            if ch == "'":
                if txt[:j - 1].count("'") % 2 == 0:
                    j -= 1            # 未闭合开引号：纳入片段继续
                    continue
                k = txt.rfind("'", 0, j - 1)
                if k < 0:
                    return j
                j = k                 # 闭引号：跳到对应开引号继续
                continue
            j -= 1
        return 0

    @classmethod
    def _pd_operand_tail(cls, txt: str) -> str:
        """右值算术表达式"当前操作数"片段：从右向左到最近一个「独立算术
        运算符」（+-*/ 及全角 ＋－＊／×÷，紧邻空白或位于片段首尾；引号段
        内不参与）截断，返回其后的文本；无运算符 → 原样返回（整段即单
        操作数）。2026-09-23：'X'的A - 'Y'的B 这类右值整段直查必 0 候选
        （去空格后减号粘在词中间），检索只认光标所在操作数——判定口径
        与前端 pdRvalueSpan 的独立运算符边界一致（label 内部的 保额/限额
        无邻空白不误切），候选检索与选后替换区间两处对齐。"""
        if not txt:
            return ""
        t = txt.replace("\u2018", "'").replace("\u2019", "'")
        ops = set("+-*/＋－＊／×÷")
        cut = -1
        inq = False
        n = len(t)
        for i, ch in enumerate(t):
            if ch == "'":
                inq = not inq
                continue
            if inq or ch not in ops:
                continue
            prev_ws = (i == 0) or t[i - 1].isspace()
            nxt = t[i + 1] if i + 1 < n else ""
            if prev_ws or nxt == "" or nxt.isspace():
                cut = i
        return t.strip() if cut < 0 else t[cut + 1:].strip()

    def predefine_param_context(self, line: str = "", caret: int = -1,
                                limit: int = 50, predefines: str = "") -> dict:
        """预定义 指定''为 右值：光标在方法实参区 → 按参数类型给路径候选。

        返回 stage：
          "param"    光标在某参数位 → {param_index, param_kind, word, start, end,
                     candidates}；start/end 为当前占位符/文本片段在整行中的区间
                     （选中候选后仅替换该片段，表达式已输入的 a+b*c 其余部分不动）；
          "rvalue"   非方法调用或光标在实参区外 → candidates 即 field_path 混合
                     候选（原"为"后检索逻辑，前端直接渲染、不做区间替换）；
          "fallback" 主句不成立/光标在主句内 → 前端回退原有 fetchPredefine 流程。
        predefines（2026-09-29）：编辑区全部预定义行（\n 分隔）。rvalue 位候选
        混入本块已定义别名（列表别名同时展开其元素类型子字段，label 带
        `'别名'的` 前缀），支持表达式里 `'当前险别'的保额/限额(元)` 式下钻。
        """
        raw = (line or "").replace("\u2018", "'").replace("\u2019", "'")
        caret = len(raw) if caret < 0 else min(max(caret, 0), len(raw))
        n0 = len(raw) - len(raw.lstrip())
        if caret < n0:
            return {"stage": "fallback", "candidates": []}
        r = raw[n0:]
        c = caret - n0
        pm = re.match(r"^预定义\s*[：:]?\s*", r)
        pd_off = pm.end() if pm else 0
        r = r[pd_off:]
        c -= pd_off
        m = re.match(r"^指定'([^']*)'\s*为(?!一个)", r)
        if not m or c < m.end():
            return {"stage": "fallback", "candidates": []}
        rhs = r[m.end():]
        rc = c - m.end()
        base = n0 + pd_off + m.end()   # rhs 索引 → 整行绝对偏移的加数

        # 本块已定义别名混池（2026-09-29）：排除当前行自身别名（自引用无意义）；
        # 列表别名（指定'X'为一个T来自(in)…）同时展开元素类型一层子字段
        cur_alias = (m.group(1) or "").strip()
        alias_pool: list[dict] = []
        if (predefines or "").strip():
            # 空 predefines 不调 _set_pd_lex：避免把共享词法状态清掉（老调用方）
            self._set_pd_lex(predefines)
            for pd in self.parse_predefine_lines(predefines):
                a = (pd.get("alias") or "").strip()
                if not a or a == cur_alias or any(x.get("alias") == a
                                                  for x in alias_pool):
                    continue
                alias_pool.append({"label": "'%s'" % a, "kind": "pdalias",
                                   "node": a, "pd": True, "alias": a})
                if pd.get("is_list"):
                    for ch in pd["node"].get("children") or []:
                        cl = (ch.get("label") or "").strip()
                        if cl:
                            alias_pool.append({
                                "label": "'%s'的%s" % (a, cl),
                                "kind": "pfieldpath", "node": cl,
                                "type": ch.get("type") or "", "pd": True,
                                "alias": a})
        alias_pool = [{"label": it["label"], "kind": it["kind"],
                       "node": it.get("node") or "", "type": it.get("type") or "",
                       "pd": True} for it in alias_pool]

        def _rvalue(word: str) -> dict:
            return {"stage": "rvalue",
                    "candidates": self.search_predefine(
                        "field_path", word, limit=limit, alias_pool=alias_pool)}

        def _resolve(txt: str, tc: int, dep: int) -> Optional[dict]:
            """txt 以内是方法调用且光标在实参括号区 → 参数位信息（局部索引）。"""
            h = self._pd_method_scan(txt.strip())
            if h is None:
                return None
            _head, tail, end, it = h
            o0 = len(txt) - len(txt.lstrip())
            ao = o0 + end + (len(tail) - len(tail.lstrip()))
            if ao >= len(txt) or txt[ao] != "(":
                return None
            depth = 0
            close = -1
            inq = False
            for i in range(ao, len(txt)):
                ch = txt[i]
                if ch == "'":
                    inq = not inq
                    continue
                if inq:
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        close = i
                        break
            if close < 0:
                close = len(txt)       # 输入中未闭合：实参区延到行尾
            if not (ao < tc <= close + 1):
                return None            # 光标在方法头区/括号后 → 上层走 rvalue
            slots: list[tuple[int, int]] = []
            s = ao + 1
            depth = 1
            inq = False
            for i in range(ao + 1, close):
                ch = txt[i]
                if ch == "'":
                    inq = not inq
                    continue
                if inq:
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch in ",，" and depth == 1:
                    slots.append((s, i))
                    s = i + 1
            slots.append((s, close))
            trim = []
            for a, b in slots:
                seg = txt[a:b]
                trim.append((a + (len(seg) - len(seg.lstrip())),
                             a + max(len(seg.rstrip()), len(seg) - len(seg.lstrip()))))
            slot_i = next((idx for idx, (a, b) in enumerate(trim) if tc <= b),
                          len(trim) - 1)
            s0, e0 = trim[slot_i]
            # 嵌套方法调用（取大值(取小值(a,b), 4)）：光标落在内层 → 递归下钻
            if dep < 3 and s0 <= tc <= e0 and e0 - s0 > 0:
                sub = _resolve(txt[s0:e0], tc - s0, dep + 1)
                if sub:
                    sub["fs"] += s0
                    sub["fe"] += s0
                    return sub
            segt = txt[s0:e0]
            local = min(max(tc - s0, 0), len(segt))
            fs = fe = -1
            word = ""
            for mm in re.finditer(r"<[^<>]*>", segt):
                if mm.start() <= local <= mm.end():
                    fs, fe = mm.start(), mm.end()
                    break
            if fs < 0:
                fs = self._pd_frag_start(segt, local)
                fe = local
                word = segt[fs:fe]
            types = self._call_types_for(it) or []
            return {"fs": s0 + fs, "fe": s0 + fe, "word": word,
                    "index": slot_i, "types": types}

        def _clause_ended(txt: str) -> bool:
            """2026-09-29：右值已到子句结束分号 → 本条预定义完成，不再弹候选。
            裸分号=分号前引号族（单/双，弯引号已归一）各自配对为偶数；方法实参
            字符串里的 "a;b" 不误判。与前端 pdClauseEnd 同口径。"""
            for i, ch in enumerate(txt):
                if ch not in ";；":
                    continue
                p = txt[:i]
                if p.count("'") % 2 == 0 and p.count('"') % 2 == 0:
                    return True
            return False

        if _clause_ended(rhs[:max(rc, 0)]):
            return {"stage": "none", "candidates": []}

        res = _resolve(rhs, rc, 0)
        if res is None:
            return _rvalue(rhs[:max(rc, 0)])
        t = res["types"][res["index"]] if res["index"] < len(res["types"]) else ""
        fam = self._pd_type_family(t)
        return {
            "stage": "param",
            "param_index": res["index"],
            "param_kind": t or "any",
            "word": res["word"],
            "start": base + res["fs"],
            "end": base + res["fe"],
            "candidates": self._pd_param_candidates(fam, res["word"], limit,
                                                    ptype=(t or "").strip().lower()),
        }

    # ---------- 构建多级对象路径索引 ----------
    def _build_path_index(self) -> None:
        """提取所有多级对象路径（任意深度），label 用"的"连接。

        覆盖：每个 object 节点 + 每个叶子字段，label 路径从根到该节点。
        例如：
            appbase -> 保单
            appbase.vhlInfo -> 保单的车辆信息
            appbase.vhlInfo.vnature -> 保单的车辆信息的车辆使用性质
        同时把动态域集（值枚举来源）下的枚举值也纳入，供"值"检索。
        """
        seen_paths: set[str] = set()
        index: list[dict] = []

        def label_segment(o: dict) -> str:
            return o.get("label") or ""

        # 递归展开，记录每个节点的 label 前缀
        # 用 field_path 的前缀关系构建 label 路径
        # 1) 对象节点：所有 object
        # 2) 叶子字段：所有 field
        # 构建 label 前缀映射：field_path -> label 路径（用"的"连接）
        label_path_by_fp: dict[str, str] = {}

        def resolve_label_path(fp: str, node_label: str) -> str:
            """根据 field_path 前缀 + label 前缀，拼出完整"的"路径。

            方法节点（field_path 以 "()" 结尾）复刻 ADP methodName 拼接规则：
            父链 label 用"的"连接后，方法 label 直接追加（不加"的"）。
            如：动态域集的帮助 + 帮助:0包含1中的一个()
              → 动态域集的帮助帮助:0包含1中的一个()
            与 desc_to_rulecontent 的 mlabel 规则、样本 methodName 三者保持一致。
            """
            # 从已知前缀逐层往上找 label
            parts = fp.split(".")
            labels: list[str] = []
            cur = ""
            for i, seg in enumerate(parts):
                cur = ".".join(parts[: i + 1]) if not i else cur + "." + seg
                # 查找该层对象的 label
                if i == len(parts) - 1:
                    labels.append(node_label)
                else:
                    o = self.cat.object_by_path.get(cur) or self.cat.field_by_path.get(cur)
                    labels.append(o.get("label") if o else seg)
            ls = [l for l in labels if l]
            if fp.endswith("()") and len(ls) >= 2:
                return "的".join(ls[:-1]) + ls[-1]
            return "的".join(ls)

        # 对象节点：全部纳入（含方法节点如 "getCoverageXXX()"，不做过滤）
        for o in self.cat.object_by_path.values():
            fp = o.get("field_path") or ""
            if not fp:
                continue
            lp = resolve_label_path(fp, label_segment(o))
            if not lp:
                continue
            index.append({
                "label": lp, "field_path": fp,
                "kind": "object",
                "field_id": o.get("field_id") or "",
                "node_label": label_segment(o),
            })

        # 叶子字段：全部纳入（含方法节点如 "设置XXX为()"，不做过滤）
        for f in self.cat.field_by_path.values():
            fp = f.get("field_path") or ""
            if not fp:
                continue
            flabel = f.get("label") or ""
            lp = resolve_label_path(fp, flabel)
            if not lp:
                continue
            index.append({
                "label": lp, "field_path": fp,
                "kind": "field",
                "field_id": f.get("field_id") or "",
                "node_label": f.get("label") or "",
                "type": f.get("type") or "",
                "is_enum": bool(f.get("is_enum")),
            })

        # 去重（相同 label 路径可能指向相同节点）
        seen: set[str] = set()
        deduped = []
        for item in index:
            key = (item["label"], item["field_path"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        # 按"的"层级浅->深，同层按 label 排序
        deduped.sort(key=lambda x: (x["label"].count("的"), x["label"]))
        self.path_index = deduped

        # 2026-10-08：空输入候选的方法垫底段——方法节点全集预排序
        # （口径与 _match_paths 空词一致：中文名优先/纯英文名沉底、
        # 浅层优先、同层按 label），检索时只切前 EMPTY_METHOD_TAIL 条
        meths = [it for it in deduped
                 if (it.get("field_path") or "").endswith("()")]
        meths.sort(key=lambda it: (
            _en_pen("", it.get("node_label") or ""),
            it["label"].count("的"), it["label"]))
        self._empty_methods = meths

    # ---------- n-gram 兜底索引 ----------
    @staticmethod
    def _ngrams(text: str, n: int) -> set[str]:
        """切分字符 n-gram（保留顺序的连续子串）。"""
        if not text:
            return set()
        if len(text) < n:
            return set()
        return {text[i:i + n] for i in range(len(text) - n + 1)}

    def _build_ngram_index(self) -> None:
        """对每个路径条目建 1/2/3-gram 集合 + 倒排索引。

        文档文本 = label（"的"路径）+ node_label（末级名），均用于匹配；
        1-gram 保证即使词序/字形打乱也能召回，2/3-gram 提供精确度。
        """
        self._ng_doc_grams = []
        self._ng_inverted = {}
        for i, item in enumerate(self.path_index):
            text = f"{item.get('label') or ''} {item.get('node_label') or ''}"
            grams: dict[int, set] = {}
            for n in self.NGRAM_WEIGHTS:
                gs = self._ngrams(text, n)
                grams[n] = gs
                for g in gs:
                    self._ng_inverted.setdefault(g, set()).add(i)
            self._ng_doc_grams.append(grams)

    def _ngram_search(self, query: str, top_k: int = 8) -> list[dict]:
        """n-gram 混合加权兜底检索。

        query 与每个条目做**加权 n-gram 命中率**打分：
            score = sum_n( w_n * |NG_n(q) ∩ NG_n(d)| ) / sum_n( w_n * |NG_n(q)| )
        - 分子：query 的 n-gram 有多少也出现在条目里（按 n 加权，n 越大权重越大）
        - 分母：query 自身 n-gram 的加权总数（归一化，长 query 不天然占便宜）
        用倒排索引快速召回候选，避免全量遍历。过滤得分过低项。
        """
        if not query:
            return []
        q_grams: dict[int, set] = {}
        for n in self.NGRAM_WEIGHTS:
            q_grams[n] = self._ngrams(query, n)

        # 倒排召回：任一 query n-gram 出现的条目
        cand: set[int] = set()
        for n, gs in q_grams.items():
            for g in gs:
                cand.update(self._ng_inverted.get(g, set()))

        # 分子分母权重预计算
        w = self.NGRAM_WEIGHTS
        denom = sum(w[n] * len(q_grams[n]) for n in w if q_grams[n])
        if denom <= 0:
            return []

        scored: list[tuple[float, dict]] = []
        for i in cand:
            dg = self._ng_doc_grams[i]
            numer = 0
            for n, gs in q_grams.items():
                inter = len(gs & dg.get(n, set()))
                numer += w[n] * inter
            score = numer / denom
            if score < 0.20:
                continue
            item = self.path_index[i]
            scored.append(
                (1 if (item.get("field_path") or "").endswith("()") else 0,
                 _en_pen(query, item.get("node_label") or ""), score, item))
        scored.sort(key=lambda x: (x[0], x[1], -x[2], x[3]["label"]))
        out = []
        for *_x, item in scored[:top_k]:
            is_m = (item.get("field_path") or "").endswith("()")
            out.append({
                "label": item.get("label") or "",
                "field_path": item.get("field_path") or "",
                "field_id": item.get("field_id") or "",
                "kind": "method" if is_m else (item.get("kind") or "field"),
                "node_label": item.get("node_label") or "",
                "type": item.get("type") or "",
                "source": "fuzzy_ngram",
            })
        return out

    # ---------- 检索 ----------
    def search(self, current_word: str, prev_word: str = "",
               prevprev_word: str = "", limit: int = 50,
               predefines: str = "") -> list[dict]:
        """检索当前词候选。

        current_word 为空（刚按空格）-> 根据上一个词（prev_word）及上上个词智能给候选：
            - prev 前面是运算符（如"是 123455"中 prev=123455 是值）-> 给连接词/运算符
            - prev 是运算符 / 连接词 / 如果 / 空（首词前）-> 给 BOM 一级对象
            - prev 是 BOM 对象/字段路径 -> 给运算符 + 连接词（保持原有）
        非空 -> 匹配的多级对象路径 + 匹配的运算符/连接词。
        predefines：编辑区预定义行（\n 分隔）。解析成功后注入预定义词条：
            一级对象候选追加"规则预定义变量"；其下级（别名/子字段）进检索池，
            输"规则预定义变量的..."前缀即可逐级下钻（与 BOM 对象交互一致）。
        """
        self._set_pd_lex(predefines)
        w = (current_word or "").strip()
        if w:
            previous = self._find_path_item((prev_word or '').strip())
            if previous and ('属于' in w or w.startswith('不属')):
                return [c for c in self._field_operators(previous) if w in c['label']][:limit]
        if not w:
            # 上一词是"内容长度"修饰符 → 当前应选数值比较符（两段式第二段）
            if (prev_word or "").strip() in LEN_MODIFIERS:
                return [dict(c) for c in LEN_COMPARISON_CANDIDATES]
            # 上一词是算术运算符 → 当前是"右操作数"位置：给一级对象/预定义下钻入口
            # （数值字段靠继续输词过滤，见非空分支的 _numeric_hits；数字直接手输）
            if self._is_arith(prev_word):
                return self._top_objects() + self._pd_roots()
            # 上一词若是"值"（其前面紧跟一个运算符）→ 值之后期待连接词/运算符；
            # 该值是数值操作数（数字/数值字段/别名）时追加算术运算符候选，
            # 支持"保费 大于 保额 加 "这类右侧算术链
            if self._is_operator(prevprev_word):
                out = self._operators_and_connectors()
                import re as _re
                pw = (prev_word or "").strip()
                prev_v = self._find_path_item(pw)
                if (prev_v is not None and self._is_arith_operand_item(prev_v)) \
                        or _re.match(self.NUMBER_RE, pw):
                    out = out + self._arith_candidates()
                return out
            # 上一词是运算符 → 当前是"值"位置：按上一个字段的类型给值候选
            if self._is_operator(prev_word):
                # 该运算符的操作数是"内容长度"修饰符 → 比较值是整数，自由输入（不弹对象）
                if (prevprev_word or "").strip() in LEN_MODIFIERS:
                    return []
                field_item = self._find_path_item(prevprev_word)
                return self._value_candidates(field_item)
            # 上一词是 BOM 字段 → 给该字段类型的兼容运算符 + 连接词
            prev_item = self._find_path_item(prev_word)
            if prev_item and prev_item.get("kind") == "field":
                return self._field_operators(prev_item)
            # 上一词是 BOM 对象 → 给连接词 + 全量运算符
            if prev_item:
                return self._operators_and_connectors()
            # 否则（连接词/如果/空）期待一级对象（含预定义一级"规则预定义变量"）
            # 2026-09-23：条件起始位（"如果"后、"如果"块内换行续写空行）追加
            # 并且/或者 连接词候选，置于一级对象之后——"如果"块多行正文
            # 折叠后即为"条件1 并且 条件2"的合法行文，用户可候选直选以连接词开头。
            conns = [{
                "label": c["label"], "code": c["code"],
                "kind": c["kind"], "field_path": "", "source": "static",
            } for c in CONNECTOR_CANDIDATES]
            # 2026-10-08：表尾追加方法垫底段（≤EMPTY_METHOD_TAIL 条），
            # 只影响空输入候选，检索词非空时逻辑不变
            return (self._top_objects() + self._pd_roots() + conns
                    + self._empty_word_methods(EMPTY_METHOD_TAIL))

        # 上一词是"内容长度"修饰符、当前正输入比较符 → 只给数值比较符候选
        if (prev_word or "").strip() in LEN_MODIFIERS:
            return self._match_len_comp_ops(w)
        # 上一词是算术运算符、当前正输入右操作数 → 数值字段/别名候选
        # （类型未知叶子与对象节点沉底可下钻），附带收束链后的比较运算符候选
        # （如"保额 加 保"→保费类数值字段在前；"保额 加 大于"→仍可见运算符）
        if self._is_arith(prev_word):
            return self._match_ops_conns(w) + self._numeric_hits(w, limit)
        # 值位置（prev 是运算符、prevprev 是字段）：当前输入的是"值"，按字段类型处理，
        # 不弹对象字段路径候选（避免输入数字 0 却弹出一堆含 0 的字段，误导用户）。
        if self._is_operator(prev_word):
            # 该运算符的操作数是"内容长度"修饰符 → 比较值是整数，自由输入
            if (prevprev_word or "").strip() in LEN_MODIFIERS:
                return []
            field_item = self._find_path_item(prevprev_word)
            value_cands = self._value_candidates_nonempty(field_item, w)
            # 值位置：只有 string 字段可继续给对象/枚举候选，numeric/date 直接返回空
            if value_cands is not None:
                return value_cands

        # 选运算符阶段（prev 是字段、当前词是运算符名）：只给运算符/连接词候选，
        # 不匹配对象字段（避免输入"大于"混入含"大于"的字段）。
        # prev 是数值字段时算术运算符（加/减/乘/除，含符号/全角别名）一并参与前缀匹配。
        prev_item = self._find_path_item(prev_word)
        if prev_item and prev_item.get("kind") == "field":
            with_arith = self._pd_type_family(
                (prev_item.get("type") or "").lower()) == "numeric"
            return (self._match_ops_conns(w, include_arith=with_arith)
                    + self._match_keywords(w))

        hits = self._match_paths(w, limit * 2)
        # 附带匹配的运算符/连接词/关键词（若当前词匹配）；运算符优先（用户输入运算符名期望先见运算符）
        op_hits = self._match_ops_conns(w)
        kw_hits = self._match_keywords(w)
        merged = op_hits + kw_hits + hits[:limit]
        # 精确匹配 0 候选 → n-gram 混合加权模糊兜底（容忍记错名/多字/顺序不同）
        if not merged:
            return self._ngram_search(w, limit)
        return merged[:limit]

    # ---------- 预定义主句检索（指定'别名'为一个X来自(in) 路径） ----------
    def _ensure_list_index(self) -> None:
        """懒加载 BOM 中 type=list 节点索引。

        每条目：
          label:      展示形态，root 带引号，如 '保单'的人身险信息的xxx险别列表
          key:        匹配键（去空格、剥"业务模型对象的"前缀），与生成端 _bom_composed_norm 同规则
          type_name:  actualTypeNames[0]（列表元素类型中文名）
        """
        if self._list_index is not None:
            return
        self._list_index = []
        bom_path = get("data", "bom_path")
        if not bom_path or not os.path.exists(bom_path):
            return
        with open(bom_path, encoding="utf-8") as f:
            bom = json.load(f)
        prefix = "业务模型对象的"

        def walk(node: dict, anc: list[str]) -> None:
            label = node.get("label") or ""
            composed = ("的".join(anc) + "的" if anc else "") + label
            if composed.startswith(prefix):
                composed = composed[len(prefix):]
            if node.get("type") == "list":
                atn = (node.get("actualTypeNames") or [""])[0]
                parts = composed.split("的", 1)
                shown = ("'" + parts[0] + "'的" + parts[1]) if len(parts) > 1 else ("'" + composed + "'")
                self._list_index.append({
                    "label": shown,
                    "key": self._path_key(composed),
                    "type_name": (atn or "").strip(),
                    # 一层子字段（列表元素类型的字段），供规则模式三级词条用
                    "children": [
                        {"label": (ch.get("label") or "").strip(),
                         "type": (ch.get("type") or "").strip()}
                        for ch in (node.get("children") or [])
                        if isinstance(ch, dict) and (ch.get("label") or "").strip()
                    ],
                })
            for ch in node.get("children") or []:
                if isinstance(ch, dict):
                    walk(ch, anc + [label])

        walk(bom, [])

    # 操作数片段首尾分隔符清理（2026-09-29 表达式场景）：BOM label 不会以
    # 运算符/括号/逗号开头，片段头部粘连的 `( `、`* ` 等只能是表达式分隔符；
    # 尾部只剥"未配对"右括号（限额(元) 开闭平衡保留，`4 ) *` 收尾的 ) 剥掉）。
    _PD_LEAD_DELIM_RE = re.compile(r"^[+\-*/%（）()\s,，、]+")

    @classmethod
    def _pd_strip_delims(cls, seg: str) -> str:
        seg = seg.strip()
        seg = cls._PD_LEAD_DELIM_RE.sub("", seg)
        while seg and seg[-1] in "）)" and (
                seg.count("(") + seg.count("（")) < (seg.count(")") + seg.count("）")):
            seg = seg[:-1].rstrip()
        return seg

    def search_predefine(self, kind: str, word: str = "", limit: int = 50,
                         type_name: str = "", field: str = "",
                         alias_pool: "list[dict] | None" = None) -> list[dict]:
        """预定义主句/条件子句候选检索。

        kind="type": BOM 中 type=list 节点的 actualTypeNames[0]（去重，保首现顺序）
        kind="path": type=list 节点的组合路径（'root'的p1的p2）
        kind="cond_field": 条件字段候选——type_name 对应列表节点的 children（BOM 子字段）
        kind="cond_op": 条件运算符候选——按字段类型（数值加大于/小于等，其余 是/不是/包含）
        type_name 非空时（kind="path"）：只返回该类型对应的列表路径
        （"为一个"后的类型与列表路径一一对应），类型不在索引中时回退全量。
        检索策略与规则模式 _match_paths 一致：完全相等 > 前缀 > 包含；空词给全量前 limit。
        匹配时忽略单引号（用户输入 '保单'的人 与 保单的人 等价）。
        """
        self._ensure_list_index()
        if kind == "field_path":
            # 2026-09-23：算术右值（'X'的A - 'Y'的B）整段直查时运算符粘在
            # 检索词中间必 0 候选 → 只检索光标所在操作数（_rvalue 与前端
            # fetchPredefine("field_path") 兜底共用本口径）
            # 2026-09-29：表达式括号/运算符上下文——操作数头部粘连的 `( `/`* `
            # 等分隔符一并剥除，否则 `( '当前险别'的保额` 这类片段 0 精确命中、
            # 全掉进模糊兜底出垃圾候选
            word = self._pd_strip_delims(self._pd_operand_tail(word))
        # 匹配统一去引号/空格（BOM label 可能自带空格，与生成端 key 规则一致）；
        # 全角括号转半角：候选池 label 均为 BOM 原文，用户手打（）不应导致 0 候选
        wl = self._paren_trans(
            (word or "").strip().replace("'", "").replace("'", "").replace(" ", ""))
        if kind == "cond_field":
            # 条件字段候选：该类型列表节点的子字段
            tn = (type_name or "").replace(" ", "")
            node = next((it for it in self._list_index
                         if (it.get("type_name") or "").replace(" ", "") == tn), None)
            pool = [{"label": c.get("label") or "", "kind": "pcondfield",
                    "type": c.get("type") or ""}
                    for c in (node.get("children") if node else [])]
        elif kind == "cond_op":
            # 条件运算符候选：按字段类型——数值型加比较运算符，其余 是/不是/包含
            tn = (type_name or "").replace(" ", "")
            node = next((it for it in self._list_index
                         if (it.get("type_name") or "").replace(" ", "") == tn), None)
            ftype = ""
            if node:
                ftype = next((c.get("type") or "" for c in node.get("children", [])
                              if c.get("label") == field), "").lower()
            ops = ["大于", "小于", "等于", "不等于"] if ftype in (
                "double", "int", "integer", "float", "decimal", "number") else []
            pool = [{"label": o, "kind": "pcondop"}
                    for o in ops + list(self.COND_OPERATORS)]
        elif kind == "field_path":
            # 非列表预定义路径候选：BOM 对象/叶子字段路径（'root'的p1的p2），排除方法节点。
            # 用户输入 指定'别名'为 → 直接选对象字段路径（非列表，规则里当普通字段用）。
            # 2026-09-11：混入 BOM 方法候选（kind="method"，call 式模板）——
            # 预定义右值支持方法调用（指定'X'为 帮助.0与1相差的天数(<date> , <date>)）。
            # 与路径候选统一打分排序，同分方法优先；空词时方法限半量防挤占路径候选。
            pool = []
            for item in self.path_index:
                if (item.get("field_path") or "").endswith("()"):
                    continue
                label = item.get("label") or ""
                if "的" not in label:
                    continue
                parts = label.split("的", 1)
                pool.append({"label": "'%s'的%s" % (parts[0], parts[1]),
                             "kind": "pfieldpath",
                             "type": item.get("type") or "",
                             "node": item.get("node_label") or ""})
            # 2026-09-29：本块已定义的预定义别名混池——表达式/右值里引用
            # `'当前险别'的…` 时别名根与其元素类型子字段作为一等候选参与打分
            # （pd=True 优先于 BOM 路径与方法），否则只能靠模糊兜底出无关垃圾
            if alias_pool:
                seen_lb = {it["label"].replace("'", "").replace(" ", "")
                           for it in pool}
                for it in alias_pool:
                    k = it["label"].replace("'", "").replace(" ", "")
                    if k and k not in seen_lb:
                        seen_lb.add(k)
                        pool.append(it)
            # 方法限半量（空/非空词一致）：防"保费"这类通用词被数百 setter
            # 方法挤满槽位，字段路径永远看不到
            m_lim = max(limit // 2, 3)
            for h in self.search_methods(word, limit=m_lim, template_style="call"):
                pool.append({"label": h["label"], "kind": "method",
                             "template": h["template"], "field_path": h["field_path"],
                             "type": h.get("type") or "",
                             "param_count": h["param_count"]})
            scored = []
            for it in pool:
                lb = it["label"]
                lb_key = lb.replace("'", "").replace(" ", "")
                s = 0.0
                if not wl:
                    s = 0.5
                elif lb_key == wl:
                    s = 1.0
                elif lb_key.startswith(wl):
                    s = 0.95
                elif wl in lb_key:
                    s = 0.6
                if s > 0:
                    if it["kind"] == "method":
                        # 方法候选按展示名末级判断：addCpInfos() 等英文方法名
                        # 同样沉底，"取大值(0,1)" 等中文方法名不受影响
                        pen = _en_pen(wl, lb.replace(" ", "").split("的")[-1])
                    else:
                        pen = _en_pen(wl, it.get("node") or "")
                    rk = -1 if it.get("pd") else (
                        0 if it["kind"] == "method" else 1)
                    scored.append((pen, -s, rk, lb_key, it))
            scored.sort(key=lambda x: x[:4])
            res = [it for *_x, it in scored[:limit]]
            # 纯数字检索词（`/ 500000`、`- 4` 常量位）：精确 0 命中即空，
            # 不跑模糊——否则 n-gram 按"50""4"召回含数字的动态域值刷屏
            if (wl and len(res) < limit
                    and not re.fullmatch(r"[\d.]+", wl)):
                res = self._pd_fuzzy_fill(res, wl, limit)
            return res
        elif kind == "type":
            seen: set[str] = set()
            pool: list[dict] = []
            for it in self._list_index:
                t = it.get("type_name") or ""
                if t and t not in seen:
                    seen.add(t)
                    pool.append({"label": t, "kind": "ptype"})
        else:
            items = self._list_index
            tn = (type_name or "").replace(" ", "")
            if tn:
                exact = [it for it in items
                         if (it.get("type_name") or "").replace(" ", "") == tn]
                items = exact or items  # 手输类型不在索引时回退全量，避免卡死
            pool = [{"label": it["label"], "kind": "ppath"} for it in items]
        scored = []
        for it in pool:
            lb = it["label"]
            lb_key = lb.replace("'", "").replace(" ", "")
            s = 0.0
            if not wl:
                s = 0.5
            elif lb_key == wl:
                s = 1.0
            elif lb_key.startswith(wl):
                s = 0.95
            elif wl in lb_key:
                s = 0.6
            if s > 0:
                scored.append((_en_pen(wl, lb_key.split("的")[-1]),
                               -s, lb_key, it))
        scored.sort(key=lambda x: x[:3])
        return [it for *_x, it in scored[:limit]]

    def _pd_fuzzy_fill(self, res: list[dict], wl: str, limit: int) -> list[dict]:
        """指定''为 右值候选的模糊补位（2026-09-14）。

        精确池只做 完全/前缀/包含，输漏字、顺序不同或跨层级拼词（如"精算应缴"
        记成"应缴精算"）即 0 候选。字段路径走 n-gram 倒排召回（与规则模式
        search() 的 _ngram_search 兜底同口径），方法候选走非连续字符子序列；
        仅补足 limit 空缺，并对精确池已展示的 label 去重。
        """
        def _key(lb: str) -> str:
            return lb.replace("'", "").replace(" ", "").replace("\u3000", "")

        have = {_key(it["label"]) for it in res}
        out = list(res)
        # 模糊字段召回后按层级浅→深重排取位：深层长 label（含 getInstance() 的
        # 六七年链）gram 命中面天然占便宜，直接按分数排会刷屏挤掉可用浅路径
        fill: list[tuple[int, int, str]] = []
        for g in self._ngram_search(wl, max((limit - len(out)) * 8, 16)):
            fp = g.get("field_path") or ""
            label = g.get("label") or ""
            if fp.endswith("()") or "的" not in label:
                continue
            parts = label.split("的", 1)
            q = "'%s'的%s" % (parts[0], parts[1])
            k = _key(q)
            if k in have:
                continue
            have.add(k)
            fill.append((_en_pen(wl, g.get("node_label") or ""),
                         label.count("的"), -len(label), q, g.get("type") or ""))
        for t in sorted(fill, key=lambda x: x[:4])[:limit - len(out)]:
            out.append({"label": t[3], "kind": "pfieldpath",
                        "type": t[4], "source": "fuzzy"})
        # 方法候选：精确打分全无（右值混池里 method 计数为 0）时子序列兜底
        if len(out) < limit and not any(it.get("kind") == "method" for it in out):
            n_m = 0
            for it in self._method_index:
                if len(out) >= limit or n_m >= max(limit // 2, 3):
                    break
                disp = next(iter(it.get("disp_labels") or ()), it["label"])
                dk = disp.replace(" ", "")
                if _key(dk) in have or not self._pd_subseq(wl, dk):
                    continue
                have.add(_key(dk))
                out.append({"label": disp, "kind": "method",
                            "template": self.render_call_template(
                                disp, self._call_types_for(it)),
                            "field_path": it["field_path"],
                            "type": it.get("type") or "",
                            "param_count": it["param_count"], "source": "fuzzy"})
                n_m += 1
        return out

    # ---------- 预定义词条注入规则模式（生成端原生形态） ----------
    # 一级对象固定名：与生成端 fieldPath 前缀"规则预定义变量."一致
    PD_ROOT = "规则预定义变量"
    # 预定义行解析：指定'别名'为一个 X来自(in) 'root'的路径（容忍编辑辅助空格）；
    # 路径后允许"符合条件：…"过滤子句或";"结尾（与生成端正则终止口径一致，样本 59% 带"符合条件"）
    PREDEFINE_LINE_RE = re.compile(
        r"^指定'([^']+)'为一个\s*(\S*)\s*来自\s*[（(]\s*in\s*[）)]\s+(.+?)\s*"
        r"(?:符合条件[：:]?.*|;.*)?$"
    )

    # "符合条件"子句运算符全集（样本统计：是 76% / 包含 25% / 是…中的一个 3%）
    COND_OPERATORS = ("是", "不是", "包含")
    # 条件子句解析：符合条件：当前这个 <类型名>的<字段><运算符>"<值>"
    # （"当前这个"指代刚定义的列表元素；值可带/不带引号）
    PREDEFINE_COND_RE = re.compile(
        r"符合条件[：:]\s*当前这个\s*(\S+)\s*的([^是属于包含；;\"]+?)"
        r"(是|不是|包含)\s*[\"“]?([^\"”；;]*)"
    )

    def parse_predefine_condition(self, line: str) -> dict | None:
        """解析预定义行"符合条件"子句 → {type_name, field, op, value}；无子句返回 None。

        兼容中文弯引号（先归一化再匹配）。
        边界：条件字段名含"是/不是/包含"时解析歧义，与生成端正则口径一致（排除）。
        """
        ln = (line or "").replace("\u2018", "'").replace("\u2019", "'")
        m = self.PREDEFINE_COND_RE.search(ln)
        if not m:
            return None
        return {
            "type_name": m.group(1).strip(),
            "field": m.group(2).strip(),
            "op": m.group(3),
            "value": m.group(4).strip(),
        }

    def _pd_out(self, words: list[dict], pref: str) -> list[dict]:
        """给已切词结果补"预定义"引导词前缀（若无前缀/空词原样返回）。"""
        if pref and words:
            return [{"text": pref, "type": "pd_keyword", "valid": True, "error": ""}] + words
        return words

    # ==================== 预定义右值表达式词法器（2026-09-22） ====================
    # 为什么必须词典驱动而非正则/逐字符扫：BOM 字段 label 内部会自带逗号、斜杠、
    # 括号（新车险915 实测存在 "销售费用0, 销售费用类型的费用比率"、"保额/限额(元)"、
    # "年折前保费(元)"、"保费计算信息0, 险别"），而算术表达式恰好用同形字符做运算符
    # 与实参分隔符。旧实现（_pd_frag_start 的 delimiter 集、predefine_param_context 的
    # 逐字符切槽）必然把这些 label 从中间截断，表现为：
    #   ① label 内逗号被当实参分隔符 → 槽位数/期望类型错位，候选池类型不对；
    #   ② 光标停在 label 中段选候选 → 替换区间落在 label 内部（生成 "保额/<候选>"）；
    #   ③ 右值直接是算术表达式（无方法包裹）整段判不出 → 标红"路径不存在"、词条不
    #      注册、上传预检拦截（交接文档 6.6 记录的 19 例即此）。
    # 本词法器先把「完整 BOM label / 方法头 / 引号段」整体吃进，剩下的字符才是
    # 运算符/括号/逗号，歧义一次消除。标注着色、实参位定位、词条注册、测试报文收集
    # 共用这一口径（单一真源，与 _pd_rvalue_parts 的既定策略一致）。
    _PD_ARITH_OPS = "+-*/"
    _PD_OP_TRANS = {"＋": "+", "－": "-", "＊": "*", "／": "/", "×": "*", "÷": "/"}
    # 切点字符集：路径/别名候选只能在这些字符前截断（= 运算符/括号/逗号/分号/空白/引号）
    _PD_CUT_CHARS = set("+-*/(),，、;； \t\r\n'\"“”（）＋－＊／×÷")
    _PD_NUM_RE = re.compile(r"\d+(?:\.\d+)?|\.\d+")
    # 日期字面量须先于数字试探：实参里的 2021-06-15 若按数字+运算符切会得到
    # num-op-num-op-num（假语法错误 + 类型误判 double）
    _PD_DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
                             r"(?:\s+\d{1,2}:\d{1,2}(?::\d{1,2})?)?")
    # 词法 kind → 标注 type（复用既有配色，不新增前端类：算符/括号紫、字面量灰、
    # 路径绿、别名绿、上级对象蓝、方法名绿、空格浅灰）
    _PD_EXPR_TYPE = {"rootq": "pd_keyword", "path": "pd_path", "alias": "pd_alias",
                     "num": "pd_value", "str": "pd_value", "date": "pd_value",
                     "ph": "pd_value", "op": "pd_op", "lparen": "pd_op",
                     "rparen": "pd_op", "argsep": "pd_keyword", "semi": "pd_keyword",
                     "space": "pd_draft", "method": "methodtext",
                     "unknown": "pd_path"}

    def _ensure_pd_label_map(self) -> dict:
        """label 比较键 → path_index 条目（词法器词典）。
        同键多条目（方法重载、带/不带上级对象两种形态）优先保留有 type 的那条——
        表达式类型推断只认字段类型，方法条目另有 _pd_by_label 通道。"""
        m = getattr(self, "_pd_label_map", None)
        if m is None:
            m = {}
            for it in self.path_index:
                lab = it.get("label") or ""
                if not lab:
                    continue
                k = self._path_key(lab)
                if not k:
                    continue
                cur = m.get(k)
                if cur is None or (not (cur.get("type") or "") and (it.get("type") or "")):
                    m[k] = it
            self._pd_label_map = m
            self._pd_label_map_maxlen = max((len(k) for k in m), default=0)
        return m

    def _pd_alias_path_map(self, predefines: str) -> dict:
        """预定义别名子路径词典：key = `别名`的`相对路径`（比较键形态）。
        列表/对象型别名（'当前险别'）被右值引用时写的是 ` '当前险别'的保额/限额(元) `，
        BOM 词典里没有这种以别名为根的 label，必须从词条索引（predefine_lex）派生，
        否则整段判为"路径不存在"。裸别名（`'前置赔付率'`、`前置赔付率`）不放进来——
        由 aliases 集合在引号/兜底分支识别，避免与"别名整体当操作数"的着色混淆。"""
        m: dict = {}
        if not (predefines or "").strip():
            return m
        # 2026-09-22：按 predefines 原文 memo——该函数会触发整段预定义全量词法
        # （predefine_lex），标注一次请求里多行右值各引用一次时成本 ×行数 放大；
        # 结果只依赖 predefines 文本 + BOM 索引（实例内静态），可安全缓存。
        cache = getattr(self, "_pd_alimap_cache", None)
        if cache is None:
            cache = self._pd_alimap_cache = {}
        hit = cache.get(predefines)
        if hit is not None:
            return hit
        cache[predefines] = m = self._pd_alias_path_map_build(predefines)
        if len(cache) > 16:
            cache.clear()
            cache[predefines] = m
        return m

    def _pd_alias_path_map_build(self, predefines: str) -> dict:
        m: dict = {}
        root = self.PD_ROOT
        for it in self.predefine_lex(predefines):
            fp = it.get("field_path") or ""
            if not fp.startswith(root + "."):
                continue
            parts = fp.split(".")
            if len(parts) < 3:
                continue
            alias = parts[1]
            lab = it.get("label") or ""
            pre = root + "的" + alias + "的"
            if not lab.startswith(pre):
                continue
            key = self._path_key(alias + "的" + lab[len(pre):])
            if key:
                m.setdefault(key, {**it, "is_pd": True, "pd_alias": alias})
        return m

    def _pd_path_scan(self, txt: str, i: int, extra: Optional[dict] = None):
        """从 i 起贪心匹配一个完整 BOM 路径/对象 label（'root'的 引号形态亦可）。
        返回 (end, entry)：end 为切片终点（已剥尾随空白，可直接 txt[i:end]）；
        找不到任何可命中的切点返回 None。
        命中取最长（"…费用比率" 与 "…费用比" 同时存在时必须取长，否则短路匹配
        会把长 label 打断）；切点只允许落在结构字符前，因此 label 内部的运算符
        会被完整吃进、label 之后的运算符绝不会被误吃。
        extra：别名子路径词典（BOM 词典未命中时兜底，命中条目带 is_pd 标记）。

        实现（2026-09-22）：_path_key 是逐字符归一（剥引号/去空白/全角平移，
        不删"的"）→ 单调，可前向累积归一流、每个切点查词典取最长命中。旧实现
        "由远及近"对每个切点整段重归一，300 字长行逐字符位试探 → O(n³) 数十秒。
        累积超过最长键即停（比它长的候选不可能命中，与原全扫严格等价）。"""
        mp = self._ensure_pd_label_map()
        n = len(txt)
        maxlen = self._pd_label_map_maxlen
        if extra:
            for kx in extra:
                if len(kx) > maxlen:
                    maxlen = len(kx)
        if maxlen < 2:
            return None
        key = ""                   # 增量拼接的归一流（+= 在 CPython 摊还 O(1)；
        # 旧写法每切点 "".join(acc) 整段重建 = O(n) × O(n) 切点 = O(n²)，
        # 前向化后剩下的最后热点，2026-09-22 faulthandler 栈实锤在 join 行）
        best = None
        end_eff = i              # = i + len(txt[i:e].rstrip())：剥尾随空白后的终点
        cut = self._PD_CUT_CHARS
        trans = self._PUNCT_TRANS
        # 引号平衡计数（2026-09-22）：引号也是切点字符，成对引号内已积累到闭合
        # 引号处的 key 才准查词典。否则 `'保单'的…错词` 会在"保单"+闭引号处
        # 提前命中 root 对象"保单"——半截引号对当完整路径收尾，扫描错位到引号
        # 中间，后续整段错词被配成"别名"假绿（实测打错路径不标红的根因）。
        qcnt = 0
        for e in range(i + 1, n + 1):
            c = txt[e - 1]
            if c.isspace():
                pass                     # rstrip 语义：空白不进 key 也不进 cand 长度
            elif c == "'" or c == "'":
                end_eff = e              # 引号剥出 key，但 cand 保留（非空白不 rstrip）
                qcnt += 1
            else:
                key += c.translate(trans)
                end_eff = e
            if len(key) > maxlen:
                break
            if len(key) > 1 and qcnt % 2 == 0 and (e == n or txt[e] in cut):
                ent = mp.get(key)
                if ent is None and extra:
                    ent = extra.get(key)
                if ent is not None:
                    best = (end_eff, ent)
        return best

    @staticmethod
    def _pd_issue(issues: list, level: str, s: int, e: int, msg: str):
        issues.append({"level": level, "start": s, "end": e, "msg": msg})

    def _pd_alias_names(self, predefines: str) -> list:
        """整段预定义文本里已定义的别名集合（供词法器识别 '别名' 引用）。
        只用正则收集名字、不做右值解析——避免 annotate→parse_predefine_lines→
        annotate 的递归，也天然容忍"后定义的别名先被引用"（ADP 侧按序求值，
        编辑器只负责别认识不识别得出来）。"""
        if not (predefines or "").strip():
            return []
        return [m for m in re.findall(r"指定\s*['']([^'']+)['']\s*为", predefines) if m.strip()]

    def _pd_expr_parse(self, txt: str, base_off: int = 0, aliases=None,
                       finished: bool = False, alias_paths: Optional[dict] = None) -> dict:
        """预定义右值表达式解析（词法器，单一真源）。

        入参 txt 为右值区原文（含前导空格），base_off 为 txt[0] 在整行中的偏移
        （返回的 start/end 一律绝对化，标注与前端区间替换直接用）。
        aliases=None 表示"不知道别名集合"，此时别名引用不做存在性判定（不误报）。
        finished=True 表示该子句已写完（行尾有 ; 或右值后还有后续内容）——未闭合
        括号/悬空运算符/非法操作数才升格为 error；编辑过程中只给 warning，不标红
        不拦上传（与"参数位只查空"的既定不拦人策略一致）。

        返回 {form, tokens, frames, refs, issues, type, ok, complex}：
          tokens 拼接后与 txt 逐字符相等；kind ∈ space|rootq|path|alias|num|str|
                 date|ph|op|lparen|rparen|argsep|semi|method|unknown
          frames 方法调用帧（head 区间 + 实参槽区间 + 声明类型），实参位定位用；
                 槽位在 token 流上切，天然免疫 label 内逗号
          refs   表达式引用到的 BOM 路径/别名（词条注册、测试报文骨架收集用）
          form   empty|const|path|alias|method|arith（按 depth=0 顶层结构判）
          complex 是否含真实运算结构（顶层运算符 / 嵌套方法调用 / 实参区内运算符）
                 ——调用方据此决定"走新着色"还是"保持旧版整段着色"，保证 145 句
                 ADP 样本的既有标注口径零变化
        """
        self._ensure_pd_label_map()
        ali: dict = {}
        if aliases is not None:
            for a in aliases:
                k = self._path_key(a)
                if k:
                    ali[k] = (a or "").strip()
        toks: list = []
        frames: list = []
        stack: list = []
        refs: list = []
        issues: list = []
        n = len(txt)
        after_operand = False   # 上一 token 是完整操作数 → 现在期望运算符/右括号

        # 相邻操作数缺运算符检测的 token 分类（2026-09-29）：
        #   _ADJ_START 可开启一个操作数的 token；_ADJ_PREV 以一个操作数收尾的
        #   token。方法名后紧跟左括号是实参区（help取整(1)），故 lparen 的相邻
        #   判定池不含 method。
        _ADJ_START = ("path", "alias", "num", "str", "date", "ph", "method", "lparen")
        _ADJ_PREV = ("path", "alias", "num", "str", "date", "ph", "rparen")

        def emit(kind, s, e, valid=True, error="", type_="", entry=None):
            t = {"kind": kind, "text": txt[s:e], "start": base_off + s,
                 "end": base_off + e, "valid": valid, "error": error,
                 "type": type_, "depth": len(stack)}
            # 两操作数直接相邻（如删掉 A 与 B 之间的 * 后 `…保费A …保费B`）
            # 旧版静默放行——这里统一判"缺少运算符"：操作数起点 token / 分组
            # 左括号若前面（跳过空格）紧挨另一操作数即成立。只在子句写完
            # （finished）时置红拦上传，编辑中不标红（与悬空运算符/未闭合
            # 括号同一口径，边打长字段名不误杀）；issue 恒 warning 级，不翻转
            # expr["ok"]（2026-09-22 口径：词条注册不受影响）。前一项已红不叠加。
            if valid and kind in _ADJ_START:
                pv = next((x for x in reversed(toks)
                           if x["kind"] not in ("space", "rootq")), None)
                prev_set = _ADJ_PREV if kind == "lparen" else _ADJ_PREV + ("method",)
                if pv is not None and pv.get("valid") is not False \
                        and pv["kind"] in prev_set:
                    msg = ("左括号前缺少运算符（+ - * /）" if kind == "lparen"
                           else "与前一项之间缺少运算符（+ - * /）")
                    self._pd_issue(issues, "warning", base_off + s,
                                   base_off + e, msg)
                    if finished:
                        t["valid"] = False
                        t["error"] = msg
            if entry is not None:
                t["entry"] = entry
            toks.append(t)
            return t

        i = 0
        while i < n:
            ch = txt[i]
            # --- 空白 ---
            if ch.isspace():
                j = i
                while j < n and txt[j].isspace():
                    j += 1
                emit("space", i, j)
                i = j
                continue
            # --- 左括号：紧跟方法头的即"实参区"，否则为表达式分组括号 ---
            if ch in "(（":
                prev = next((t for t in reversed(toks) if t["kind"] != "space"), None)
                is_m = prev is not None and prev["kind"] == "method"
                stack.append({"method": is_m,
                              "entry": ((prev or {}).get("entry") or {}),
                              "types": (self._call_types_for(((prev or {}).get("entry") or {}))
                                        if is_m else []) or [],
                              "argc": 0, "arg_start": base_off + i + 1, "args": [],
                              "head_start": (prev or {}).get("start", base_off + i),
                              "lparen": base_off + i, "depth": len(stack)})
                emit("lparen", i, i + 1)
                after_operand = False
                i += 1
                continue
            # --- 右括号 ---
            if ch in ")）":
                if stack:
                    fr = stack.pop()
                    fr["args"].append((fr["arg_start"], base_off + i))
                    fr["end"] = base_off + i + 1
                    if fr["method"] and not fr["types"] and all(
                            not txt[max(0, a - base_off):max(0, b - base_off)].strip()
                            for a, b in fr["args"]):
                        fr["args"] = []      # 零参调用 getInstance()：不留虚槽
                    frames.append(fr)
                    emit("rparen", i, i + 1)
                else:
                    emit("rparen", i, i + 1, valid=False,
                         error="右括号多余：没有对应的左括号")
                    self._pd_issue(issues, "error", base_off + i, base_off + i + 1,
                                   "右括号多余：没有对应的左括号")
                after_operand = True
                i += 1
                continue
            # --- 逗号：方法帧内=实参分隔；别处只在 warning 级提示 ---
            if ch in ",，、":
                fr = stack[-1] if stack else None
                if fr is not None and fr["method"]:
                    fr["args"].append((fr["arg_start"], base_off + i))
                    fr["argc"] += 1
                    fr["arg_start"] = base_off + i + 1
                    emit("argsep", i, i + 1)
                else:
                    # 词典已把真 label 整体吃进，走到这里的逗号必非 label 内字符
                    emit("argsep", i, i + 1)
                    self._pd_issue(issues, "warning", base_off + i, base_off + i + 1,
                                   "逗号只在方法实参之间有意义")
                after_operand = False
                i += 1
                continue
            if ch in ";；":
                emit("semi", i, i + 1)
                after_operand = False
                i += 1
                continue
            # --- 编辑器模板留下的未填占位符 <double> ---
            if ch == "<":
                m = re.match(r"<[^<>\n]{0,30}>", txt[i:])
                if m:
                    emit("ph", i, i + m.end(),
                         type_=m.group(0)[1:-1].strip())   # <double> → double
                    after_operand = True
                    i += m.end()
                    continue
            # --- 字符串字面量 ---
            if ch in "\"“":
                m = re.match(r'"[^"\n]*"?|“[^”\n]*”?', txt[i:])
                if m:
                    emit("str", i, i + m.end(), type_="string")
                    after_operand = True
                    i += m.end()
                    continue
                # 落单引号：并入下面的兜底分支处理
            # --- 日期字面量（先于数字试探） ---
            m = self._PD_DATE_RE.match(txt, i)
            if m and m.end() > i:
                emit("date", i, m.end(), type_="date")
                after_operand = True
                i = m.end()
                continue
            # --- 方法调用（词典最长头；方法头与实参括号间容忍空格） ---
            scan = self._pd_method_scan(txt[i:])
            if scan:
                head_full, _tail0, end, it = scan
                seg = txt[i:i + end]
                rq = re.match(r"^\s*('[^']*'的)", seg)
                cut = i + (rq.end() if rq else 0)
                if rq:
                    emit("rootq", i, cut)
                # 尾随的"("不并入方法名 token，留给左括号分支建实参帧
                body_end = (i + end - 1) if txt[i + end - 1] in "(（" else (i + end)
                emit("method", cut, body_end, type_="method", entry=it)
                after_operand = False
                i = body_end
                continue
            # --- 完整 BOM 路径 / 对象（词典最长匹配，别名子路径兜底） ---
            p = self._pd_path_scan(txt, i, alias_paths)
            if p:
                end, ent = p
                seg = txt[i:end]
                rq = re.match(r"^\s*('[^']*'的)", seg)
                cut = i + (rq.end() if rq else 0)
                if rq:
                    emit("rootq", i, cut)
                t = emit("path", cut, end, type_=(ent.get("type") or "").strip(),
                         entry=ent)
                refs.append({"kind": "pd" if ent.get("is_pd") else "bom",
                             "text": seg.strip(),
                             "start": base_off + i, "end": base_off + end,
                             "type": t["type"],
                             "field_path": ent.get("field_path") or "",
                             "alias": ent.get("pd_alias") or "",
                             "label": (ent.get("label") or "").strip()})
                after_operand = True
                i = end
                continue
            # --- 数字（含"期望操作数"位置的一元正负号并入） ---
            if ch.isdigit() or (ch == "." and re.match(r"\.\d", txt[i:i + 2])):
                m = self._PD_NUM_RE.match(txt, i)   # Pattern.match(s, pos)：span 已是绝对下标
                if m:
                    emit("num", i, m.end(), type_="double")
                    after_operand = True
                    i = m.end()
                    continue
            # --- 算术运算符 ---
            if ch in self._PD_ARITH_OPS or ch in self._PD_OP_TRANS:
                op = self._PD_OP_TRANS.get(ch, ch)
                if not after_operand:
                    if op in "+-" and re.match(r"\d", txt[i + 1:i + 2] or ""):
                        # 一元正负号紧贴数字 → 并入同一个数字 token（-0.005 是一个操作数）
                        m = re.match(r"[+-]?\d+(?:\.\d+)?", txt[i:])
                        if m:
                            emit("num", i, i + m.end(), type_="double")
                            after_operand = True
                            i += m.end()
                            continue
                    msg = "运算符位置错误：缺少左操作数"
                    emit("op", i, i + 1, valid=not finished, error=(msg if finished else ""))
                    self._pd_issue(issues, "error" if finished else "warning",
                                   base_off + i, base_off + i + 1, msg)
                else:
                    nxt = txt[i + 1:i + 2]
                    msg = "" if nxt and nxt not in ")]" else "运算符后面缺少操作数"
                    # 子句结尾停在运算符 = 还在输入，不报错
                    at_end = not txt[i + 1:].strip() or txt[i + 1:].strip() in (";", "；")
                    bad = bool(msg) and not at_end
                    emit("op", i, i + 1, valid=not bad, error=(msg if bad else ""))
                    if msg:
                        self._pd_issue(issues, "error" if bad else "warning",
                                       base_off + i, base_off + i + 1, msg)
                    after_operand = False
                i += 1
                continue
            # --- 引号段：'别名' 引用 / 'root'的 但路径不成立 ---
            if ch == "'":
                q = re.match(r"'([^'\n]*)'", txt[i:])
                if q:
                    body = q.group(1)
                    k = self._path_key(body)
                    follows_de = txt[i + q.end():i + q.end() + 1] == "的"
                    if k in ali and not follows_de:
                        ty = self._pd_alias_type(ali[k])
                        emit("alias", i, i + q.end(), type_=ty)
                        refs.append({"kind": "alias", "text": txt[i:i + q.end()],
                                     "start": base_off + i, "end": base_off + i + q.end(),
                                     "type": ty, "label": ali[k]})
                        after_operand = True
                        i += q.end()
                        continue
                    if not follows_de:
                        # 带引号但不是已定义别名 → 标红（2026-09-22 升级：前端
                        # annotate 恒传编辑区全部预定义行 predefines，"未定义"可确证；
                        # 旧口径只 warning 不标红，而 warning 在 words 渲染层无消费方
                        # = 完全静默放行）。token 置红走预览标红+上传拦截；
                        # issue 保持 warning 级：不改 expr["ok"]，词条注册口径不变
                        emit("alias", i, i + q.end(), valid=False,
                             error="引用了未定义的预定义别名 '%s'" % body)
                        self._pd_issue(issues, "warning", base_off + i,
                                       base_off + i + q.end(),
                                       "引用了未定义的预定义别名 '%s'" % body)
                        after_operand = True
                        i += q.end()
                        continue
                    # 带引号且后跟"的" → 意图是对象字段路径，词典没匹配上即非法路径
                    # 2026-09-22 修：q 匹配的是 txt[i:] 切片，q.end() 是相对偏移，
                    # 漏加 i → j<i → 末尾 i=j 把扫描指针倒拨回行首，长行含未匹配
                    # 'X'的 段时主循环无限震荡（实测 623 圈 × 19 token，即 UI 卡死）
                    j = i + q.end()
                    while j < n and txt[j] not in self._PD_CUT_CHARS:
                        j += 1
                    self._pd_emit_bad_operand(txt, i, j, n, finished, toks, issues,
                                              base_off, emit, alias=ali.get(k))
                    after_operand = True
                    i = j
                    continue
            # --- 兜底：到下一个结构字符的整段 = 无法识别的操作数 ---
            j = i
            while j < n and txt[j] not in self._PD_CUT_CHARS:
                j += 1
            if j == i:
                j = i + 1
            bare = txt[i:j].strip()
            k = self._path_key(bare)
            if bare and k in ali:
                ty = self._pd_alias_type(ali[k])
                emit("alias", i, j, type_=ty)
                refs.append({"kind": "alias", "text": bare,
                             "start": base_off + i, "end": base_off + j,
                             "type": ty, "label": ali[k]})
                after_operand = True
                i = j
                continue
            self._pd_emit_bad_operand(txt, i, j, n, finished, toks, issues,
                                      base_off, emit)
            after_operand = True
            i = j

        # 括号未闭合：栈帧按"到文末"收尾（实参位定位仍可用），是否报错看是否写完
        for fr in list(stack):
            fr["args"].append((fr["arg_start"], base_off + n))
            fr["end"] = base_off + n
            fr["unclosed"] = True
            frames.append(fr)
            self._pd_issue(issues, "error" if finished else "warning",
                           fr["lparen"], fr["lparen"] + 1, "括号未闭合")
            # 2026-09-29：只发 issue 不标 token 红 → words 渲染层无消费方 = 预览
            # 完全静默、上传不拦（与"右括号多余"直接置红不对称）。子句写完时
            # 把对应左括号置红；编辑中保持不红（继续打右括号即恢复）。
            if finished:
                lt = next((t for t in toks if t["kind"] == "lparen"
                           and t["start"] == fr["lparen"]
                           and t.get("valid") is not False), None)
                if lt is not None:
                    lt["valid"] = False
                    lt["error"] = "括号未闭合：缺少对应的右括号"

        # 实参类型 vs 占位符声明类型校验（2026-09-22）：方法帧槽位（如
        # …保留1位小数(<double> , <integer>) 用户把占位符换成实参后）与 detailsList
        # 声明类型族不符 → 该槽 token 置 valid=False（预览标红、复制/上传拦截前端
        # 现成）。只出 warning 级 issue：不改变 expr["ok"]，零影响词条注册
        # （predefine_lex "method 未写完且 ok → 不注册"）等既有口径。
        arg_bad = self._pd_check_arg_types(toks, frames, issues)

        d0 = [t for t in toks if t["depth"] == 0 and
              t["kind"] in ("path", "alias", "num", "str", "date", "ph", "method")]
        ops0 = [t for t in toks if t["depth"] == 0 and t["kind"] == "op"]
        if not d0:
            form = "empty"
        elif ops0 or len(d0) > 1:
            form = "arith"
        elif d0[0]["kind"] == "method":
            form = "method"
        elif d0[0]["kind"] in ("num", "str", "date"):
            form = "const"
        elif d0[0]["kind"] == "alias":
            form = "alias"
        else:
            form = "path"
        # 括号配对有错（多余右括号/未闭合左括号已置红）时强制走逐 token 着色：
        # 简单右值（无 depth0 运算符的 单路径/常量/裸括号组）原本走快速模板分支，
        # 红括号会被 shown/extra 切段吞掉、既预览不红也不拦上传（2026-09-29）。
        # 合法括号（方法实参区、成对分组）不置红 → 不改变原路由，零误伤。
        # form=empty（depth0 无操作数）右值即"整体括号组 (A)"或中间态：快速分支的
        # shown 切段会丢首括号并把括号组当路径误标红"对象字段路径不存在"
        # （2026-09-29），一律走逐 token 着色。
        complex_ = (form in ("arith", "alias", "empty")) or \
            bool([t for t in toks if t["kind"] == "op" and t["depth"] >= 1]) or \
            bool([t for t in toks if t["kind"] == "method" and t["depth"] >= 1]) or \
            any(t["kind"] in ("lparen", "rparen") and t.get("valid") is False
                for t in toks)
        return {"form": form, "tokens": toks, "frames": frames, "refs": refs,
                "issues": issues, "type": self._pd_expr_type(d0, bool(ops0)),
                "ok": not any(x["level"] == "error" for x in issues),
                "complex": complex_, "text": txt, "base_off": base_off,
                "d0": d0, "ops0": ops0, "arg_bad": arg_bad}

    def _pd_emit_bad_operand(self, txt, i, j, n, finished, toks, issues, base_off,
                             emit, alias=None):
        """词典没匹配上的操作数：子句末尾未写完 → 灰显（正在输入，不判错）；
        否则红标 + 错误。旧口径对未匹配右值一律标红"对象字段路径不存在"，但表达式
        里用户是逐 token 边打边选候选的，末尾片段每次都红一下反而干扰。"""
        bare = txt[i:j].strip()
        tail_open = (not finished) and (j >= n or not txt[j:].strip() or
                                        txt[j:].strip() in (";", "；"))
        if tail_open and bare:
            emit("space", i, j)     # 未写完的片段：灰显，等用户继续
            return
        if alias:
            # 同带引号未定义别名口径（2026-09-22）：token 标红，issue 留 warning
            emit("alias", i, j, valid=False, error="引用了未定义的预定义别名")
            self._pd_issue(issues, "warning", base_off + i, base_off + j,
                           "引用了未定义的预定义别名")
            return
        msg = ("对象字段路径不存在" if ("的" in bare or bare.startswith("'"))
               else "无法识别的操作数（应为对象字段路径、预定义别名或数字）")
        emit("unknown", i, j, valid=False, error=msg)
        self._pd_issue(issues, "error", base_off + i, base_off + j, msg)

    # ---------- 方法实参 × 占位符声明类型 校验（2026-09-22） ----------
    _PD_FAM_CN = {"numeric": "数值", "string": "文本", "date": "日期", "list": "列表"}
    _PD_OPERAND_KINDS = ("path", "alias", "num", "str", "date", "ph", "method")

    def _pd_operand_type(self, t: dict) -> str:
        """操作数 token → 类型字符串（""=未知，不判）。"""
        k = t.get("kind")
        if k in ("path", "alias"):
            return (t.get("type") or "").strip().lower()
        if k == "num":
            return "double"
        if k == "str":
            return "string"
        if k == "date":
            return "date"
        if k == "method":
            return ((t.get("entry") or {}).get("returnType") or "").strip().lower()
        return ""

    def _pd_slot_type(self, slot: list) -> "str | None":
        """实参槽 token 列表 → 实参类型（""=无法判定；None=应跳过校验）。"""
        ops = [t for t in slot if t.get("kind") in self._PD_OPERAND_KINDS]
        if any(t.get("valid") is False for t in slot) or not ops:
            return None      # 槽内已有红词（语法错/路径不存在）→ 不叠加类型错
        if any(t.get("kind") == "ph" for t in ops):
            return None      # 占位符未填（<double>）→ 输入中，不判
        has_op = any(t.get("kind") == "op" for t in slot)
        if not has_op and len(ops) == 1:
            return self._pd_operand_type(ops[0])
        # 算术组合（如 字段A / 字段B - 5）：数值族/未知类型操作数 → double，
        # 出现文本/日期/嵌套方法参与运算 → 不判（宁漏勿误杀）
        if any(t.get("kind") == "method" for t in ops):
            return ""
        for t in ops:
            ty = self._pd_operand_type(t)
            if ty and self._pd_type_family(ty) != "numeric":
                return ""
        return "double"

    def _pd_check_arg_types(self, toks: list, frames: list, issues: list) -> bool:
        """方法帧实参槽 vs detailsList 声明类型逐位比对（词法器统一入口）。

        判定口径（保守，防编辑中误报）：
          - types 缺失/槽数超出声明数/槽为空或含未填占位符/槽内已有非法 token → 跳过；
          - 实参类型按 _pd_slot_type 推断，声明与实参类型族（numeric/string/date）
            任一侧未知 → 跳过；同族细型（<integer> 填 double 字段）→ 放行（数值可互转）；
          - 族不符 → 该槽操作数 token 全部置 valid=False + 中文错误说明。
        返回是否存在不匹配（标注层据此决定是否改用逐 token 着色露出红槽）。
        """
        bad = False
        for fr in frames:
            if not fr.get("method"):
                continue
            types = fr.get("types") or []
            if not types:
                continue      # 声明类型未知（BOM 无 detailsList）→ 全槽不判
            for idx, (a, b) in enumerate(fr.get("args") or []):
                if idx >= len(types):
                    break
                decl = (types[idx] or "").strip()
                dfam = self._pd_type_family(decl)
                if not dfam:
                    continue
                slot = [t for t in toks
                        if t["start"] >= a and t["end"] <= b
                        and t["kind"] != "space"]
                act = self._pd_slot_type(slot)
                if act is None:
                    continue
                afam = self._pd_type_family(act)
                if not afam or afam == dfam:
                    continue
                txts = "".join(t["text"] for t in slot).strip()
                msg = ("参数类型不符：该位置声明为 <%s>（应为%s），"
                       "实填『%s』是%s" % (decl, self._PD_FAM_CN.get(dfam, dfam),
                                          txts, self._PD_FAM_CN.get(afam, afam)))
                for t in slot:
                    if t["kind"] not in ("rootq", "lparen", "rparen", "argsep"):
                        t["valid"] = False
                        t["error"] = msg
                self._pd_issue(issues, "warning", a, b, msg)
                bad = True
        return bad

    def _pd_alias_type(self, name: str) -> str:
        """已定义预定义别名的类型（_pd_lex 为词条 list，同 path_index 条目结构）。"""
        key = self._path_key(name)
        for e in (getattr(self, "_pd_lex", None) or []):
            if not isinstance(e, dict):
                continue
            if self._path_key(e.get("label") or e.get("raw_label") or "") == key:
                return (e.get("type") or "").strip()
            if (e.get("node_label") or "") == (name or ""):
                return (e.get("type") or "").strip()
        return ""

    def _pd_expr_type(self, d0: list, has_op: bool) -> str:
        """表达式结果类型推断：单操作数取其类型；含运算符时——所有操作数都是
        数值族/类型未知 → double，出现字符串/日期参与运算 → 返回 ""（不猜）。"""
        if not d0:
            return ""
        if not has_op and len(d0) == 1:
            t = d0[0]
            if t["kind"] == "method":
                return ((t.get("entry") or {}).get("returnType") or "").strip()
            return t.get("type") or ""
        for t in d0:
            ty = (t.get("type") or "").strip()
            if ty and self._pd_type_family(ty) != "numeric":
                return ""
        return "double"

    def _pd_rvalue_parts(self, ln: str, predefines: str = "") -> Optional[dict]:
        """非列表预定义主句右值解析（**标注与词条注册共用同一口径**，2026-09-21）。

        ln：剥掉"预定义"前缀、弯引号已归一化的行文本，须以 `指定'别名'为` 开头。
        返回 None 表示主句未成形（输入中：既不校验也不注册词条）。

        右值四种形态（前三种为 ADP 真实样本 3396 句实测口径，见交接文档）：
          1) 对象字段路径 `'保单'的X`——root 带单引号是 ADP 语法（样本 118/118 带引号）；
             用户手打不带引号（`保单的X`）同样识别（比较键本就剥引号）
          2) 方法调用 `保单的…设置X为(实参)`、`帮助.向下取整(实参)`——ADP 样本里方法
             右值 212/212 **不带** root 引号；但用户从字段候选接续输入时会带出
             `'保单'的` 前缀，两种形态均由 _pd_method_scan 统一识别（引号容忍内置）
          3) 字面常量 `1232` / `true` / `"abc"` / 日期（仅在无 root 引号段时豁免）
          4) 算术表达式（2026-09-22，后端给的示例集主形态）：
             `指定'纯风险保费'为( '当前险别'的保额/限额(元) / 500000 - 4 ) * ( …A - …B )
              * ( 1 - … / 500000 * 0.005 ) + …A`——实参区内同样允许嵌表达式与嵌套方法。
             由 _pd_expr_parse 词典驱动切分，结果放 r["expr"]（form/type/refs/issues）。
        predefines：整段预定义原文（可空），用于识别 `'别名'` 跨行引用。
        """
        fm = re.match(r"^(指定)('([^']*)')(\s*为\s*)('([^']*)'的)?([^;]*?)\s*;?\s*$", ln)
        if not fm or not fm.group(4):
            return None
        body = ln[fm.end(4):]
        # 路径在"符合条件"或";"处终止：后续内容不是字段名，不能并入路径
        # （否则 "字段 符合条件…" 整体当路径命中失败 → 误标红）
        sp = re.split(r"(符合条件[：:]?|;)", body, 1)
        rp = sp[0]
        after = "".join(sp[1:]) if len(sp) > 1 else ""
        root_q, root, path = "", "", rp
        rm2 = re.match(r"^('([^']*)'的)", rp)
        if rm2:
            root_q, root, path = rm2.group(1), rm2.group(2), rp[rm2.end():]
        # 表达式解析（右值第四形态，2026-09-22）：整段右值一律过词法器。调用方按
        # expr["complex"] 决定是否改用逐 token 着色——简单路径 / 纯方法调用形态
        # form 仍是 path/method 且 complex=False，旧标注与词条注册口径零变化。
        # finished：子句写完（右值后还有内容，或整行以 ; 收尾）才允许判 error，
        # 编辑中的悬空运算符 / 未闭合括号只给 warning（不标红、不拦上传）。
        ali_names = (self._pd_alias_names(predefines)
                     if (predefines or "").strip() else None)
        expr = self._pd_expr_parse(
            rp, base_off=len(ln) - len(rp), aliases=ali_names,
            alias_paths=(self._pd_alias_path_map(predefines) if ali_names else None),
            finished=bool(after.strip()) or ln.rstrip().endswith((";", "；")))
        r = {"head": fm.group(1), "alias": fm.group(2), "wei": fm.group(4),
             "root_q": root_q, "root": root, "path": path, "after": after,
             "mhit": None, "method_fp": "", "matched": False, "expr": expr,
             "shown": path, "extra": "", "lit_ok": False, "path_ok": False}
        # ① 方法调用（2026-09-11）：方法头 + 实参括号，不做字段路径校验（方法参数
        #    校验属 then/else 管线），保证候选插入后预览不误标红。
        #    root 引号段一并交给 _pd_method_scan（其内部容忍 'root'的 前缀）——
        #    标注 / 实参位检索 / 词条注册三处共用同一套方法识别，不再各写一份
        scan = self._pd_method_scan(rp.strip())
        if scan is not None:
            head_full, tail = scan[0], scan[1]
            if root_q and head_full.startswith(root_q):
                head = head_full[len(root_q):]   # 引号段单独成词（分词/着色口径不变）
            else:
                root_q, head = "", head_full
            r["root_q"] = root_q
            r["mhit"] = (head, tail)
            r["method_fp"] = (scan[3] or {}).get("field_path") or ""
            return r
        # ② 对象字段路径：路径本体也可能带空格尾巴（如 "E-cargo业务类型 123" 的手输
        #    残留）：取能命中 BOM label 的最长前缀，其余词灰显不并入路径。
        #    BOM 字段 label 本身允许含内部空格，故从右往左在词边界截断试匹配。
        prefix = path.rstrip()
        # 别名子路径兜底（2026-09-22）：右值 `'<别名>'的X` 里 X 不是 BOM label，而是
        # 预定义词条派生的别名子路径（口径=词法器 alias_paths 同源词典）。不走这份
        # 词典的话，单条别名路径判 simple 落回本分支，词法器明明命中却误标"路径不存在"。
        alimap = self._pd_alias_path_map(predefines) if root else None
        while True:
            pk0 = (root + "的" + prefix) if root else prefix
            pk = self._path_key(pk0)
            if pk in self._label_keys() or (alimap and pk in alimap):
                r["matched"] = True
                break
            pos = prefix.rfind(" ")
            if pos < 0:
                break
            prefix = prefix[:pos]
        if r["matched"]:
            shown, extra = prefix, path[len(prefix):]
            if extra and not extra.strip():
                shown, extra = path, ""      # 仅尾随空格并入路径（不影响命中）
            r["shown"], r["extra"] = shown, extra
        else:
            r["shown"], r["extra"] = path, ""
        # ③ 字面常量：路径未命中且无 root 段 → 常量绑定合法放行，不报"对象字段路径不存在"
        r["lit_ok"] = (not r["matched"] and not root_q
                       and bool(_PD_LITERAL_RE.match(path.strip())))
        r["path_ok"] = r["matched"] or not r["shown"].strip()
        return r

    def _pd_expr_words(self, ex: dict) -> list[dict]:
        """表达式 token → 标注词。
        text 逐 token 拼接 == 右值原文（**不注入任何空格**：上传用规范文本口径不变）；
        token 自带的空白成 pd_draft（浅灰不可见），运算符/括号成 pd_op（紫），
        字面量成 pd_value（灰），路径/别名成 pd_path/pd_alias（绿），方法名 methodtext。
        非法 token（valid=False）一律红 + title 说明——旧版把整段实参灰显零校验，
        少括号 / 打错字段名要等 ADP 侧报错才发现，现在当场可见。"""
        ws = []
        for t in ex.get("tokens") or []:
            if not t.get("valid"):
                ws.append({"text": t["text"], "type": "pd_path", "valid": False,
                           "error": t.get("error") or "表达式语法错误"})
                continue
            w = {"text": t["text"],
                 "type": self._PD_EXPR_TYPE.get(t.get("kind"), "pd_draft"),
                 "valid": True, "error": ""}
            # 2026-09-29：右值里的别名引用词（带引号/裸形均 kind=alias）打
            # pd_ref 标记——与主句定义词同为 pd_alias，routes 重复定义校验
            # 若不区分，`指定'446'为 '123'` 的引用会被数成第二次定义而误报
            if t.get("kind") == "alias":
                w["pd_ref"] = True
            ws.append(w)
        return ws

    def annotate_predefine_line(self, line: str, predefines: str = "") -> list[dict]:
        """预定义行专用标注：主句模板切词 + "符合条件"子句校验。

        返回 [{text, type, valid, error}]，text 拼接 == 归一化后的行文本：
            pd_keyword  预定义/指定/为一个/来自(in)/符合条件：/当前这个/;（固定引导词）
            pd_alias    '别名'     pd_type  元素类型 X   pd_path  'root'的路径
            pd_field    条件字段   pd_op    条件运算符     pd_value 条件值
        支持可选"预定义"行首引导词（2026-09-03，类似"如果"之于规则行）：
        `预定义 指定'别名'…`，解析时剥"预定义 "前缀、返回时作为 pd_keyword 词补回。
        旧格式 `指定'别名'…`（无前缀）同样支持。
        校验（主句宽松、条件严格）：
            - 主句已完结（有条件子句或;结尾）但路径未命中 _list_index → pd_path 标红
            - 条件类型名 ≠ 主句类型名 → 条件 pd_type 标红
            - 条件字段不在该列表 children → pd_field 标红
            - 条件运算符不在 COND_OPERATORS → pd_op 标红
            - 行以;结尾但字段/运算符/值缺失 → 对应词标红
        """
        self._ensure_list_index()
        raw = (line or "").strip()
        ln = raw.replace("\u2018", "'").replace("\u2019", "'")
        # 可选"预定义"引导词前缀：预定义 指定'别名'…（剥前缀切词，返回时补回）
        pref = ""
        pm = re.match(r"^(预定义)\s*", ln)
        if pm:
            pref = pm.group(0)
            ln = ln[len(pref):]
        m = re.match(r"^(指定'[^']*'为一个\s*)(.*?)(来自\s*[（(]\s*in\s*[）)]\s*)(.*)$", ln)
        if not m and not re.match(r"^指定'[^']*'为一个", ln):
            # 非列表形式：指定'别名'为 对象字段路径 / 方法调用 / 字面常量
            # 解析与命中判定统一走 _pd_rvalue_parts（与 parse_predefine_lines 词条
            # 注册同源），杜绝"预览全绿、规则行引用却找不到别名"这类两口径分叉
            pr = self._pd_rvalue_parts(ln, predefines)
            if pr:
                out = [
                    {"text": pr["head"], "type": "pd_keyword", "valid": True, "error": ""},
                    {"text": pr["alias"], "type": "pd_alias", "valid": True, "error": ""},
                    {"text": pr["wei"], "type": "pd_keyword", "valid": True, "error": ""},
                ]
                ex = pr.get("expr") or {}
                if ex.get("complex"):
                    # 表达式右值（算术/嵌套方法/别名引用）：逐 token 着色 + 结构化
                    # 校验，不再整段"路径不存在"标红（2026-09-22）
                    out.extend(self._pd_expr_words(ex))
                    return self._pd_out(out + ([{"text": pr["after"],
                                                 "type": "pd_draft", "valid": True,
                                                 "error": ""}]
                                                if pr["after"].strip() else []), pref)
                if pr["mhit"] and (ex.get("arg_bad") or any(
                        t.get("valid") is False for t in ex.get("tokens") or [])):
                    # 实参有问题（类型不符 arg_bad，或槽内含无法识别的操作数——
                    # 后者 _pd_slot_type 见红词即跳过类型判定，arg_bad 恒 False）：
                    # 整段绿 methodtext 会吞掉红槽，
                    # 改逐 token 着色（方法名仍绿、括号紫，错误槽红+title 悬停提示）。
                    # tokens 拼接 == 右值原文（root_q/尾随空格均在内），不再重复
                    # 输出 pr["root_q"]，口径与 complex 分支一致。
                    out.extend(self._pd_expr_words(ex))
                    if pr["after"].strip():
                        out.append({"text": pr["after"], "type": "pd_draft",
                                    "valid": True, "error": ""})
                    return self._pd_out(out, pref)
                if pr["root_q"]:
                    out.append({"text": pr["root_q"], "type": "pd_keyword", "valid": True, "error": ""})
                if pr["mhit"]:
                    head, tail = pr["mhit"]
                    out.append({"text": head, "type": "methodtext", "valid": True, "error": ""})
                    if tail.strip():
                        out.append({"text": tail, "type": "pd_draft", "valid": True, "error": ""})
                elif pr["lit_ok"]:
                    out.append({"text": pr["path"], "type": "pd_value", "valid": True, "error": ""})
                elif pr["shown"].strip():
                    out.append({"text": pr["shown"], "type": "pd_path",
                                "valid": pr["path_ok"],
                                "error": "" if pr["path_ok"] else "对象字段路径不存在"})
                if pr["extra"].strip():
                    out.append({"text": pr["extra"], "type": "pd_draft", "valid": True, "error": ""})
                if pr["after"].strip():
                    out.append({"text": pr["after"], "type": "pd_draft", "valid": True, "error": ""})
                return self._pd_out(out, pref)
            # 主句未成形（输入中）：整行一段，不校验
            return [{"text": raw, "type": "pd_draft", "valid": True, "error": ""}]
        if not m:
            # 列表主句输入中（已以"指定'别名'为一个"开头，但"来自(in)"未输完/未输）：
            # 整行 draft 不校验（防 m 为 None 崩溃，也避免对中间态误报）
            return [{"text": raw, "type": "pd_draft", "valid": True, "error": ""}]
        head, xtype, tail = m.group(1), m.group(2), m.group(4)
        hm = re.match(r"^(指定)('([^']*)')(为一个\s*)$", head)
        tname = xtype.strip()
        # 路径：'root'的路径体（root 带引号；未输引号时整体视为路径体）
        rm = re.match(r"^('([^']*)')(.*)$", tail)
        if rm:
            root_q, root, cut_part = rm.group(1), rm.group(2), rm.group(3)
        else:
            root_q, root, cut_part = "", "", tail
        cut = re.split(r"符合条件|;", cut_part, 1)[0]
        rest = cut_part[len(cut):]
        finished = bool(rest.strip())   # 主句已完结（有条件子句或;结尾）
        # 缺 root 路径词：来自(in) 后无任何路径内容（含直接接 符合条件/;）→ 语义不完整报错
        missing_root = not (root_q + cut).strip()
        pk = self._path_key(root + cut)
        node = self._find_list_node(pk)
        # 来自(in) 左右两侧均须完全命中 BOM（与"如果"行字段口径一致）：
        #   左（元素类型名）：须为某 type=list 节点的 actualTypeNames[0]（空=输入中不拦）
        tname = xtype.strip()
        tk = self._path_key(tname)
        type_ok = (not tk) or any(self._path_key(it["type_name"]) == tk
                                  for it in self._list_index)
        #   右（root 路径）：须精确命中某 type=list 节点的 key；不匹配一律标红
        #   （不做前缀豁免——路径为候选整段插入，正常点选即完整命中，截断即红）
        path_ok = node is not None
        out = [
            {"text": hm.group(1), "type": "pd_keyword", "valid": True, "error": ""},
            {"text": hm.group(2), "type": "pd_alias", "valid": True, "error": ""},
            {"text": hm.group(4), "type": "pd_keyword", "valid": True, "error": ""},
            {"text": xtype, "type": "pd_type",
             "valid": type_ok,
             "error": "" if type_ok else "元素类型不存在（BOM 中无此列表类型）"},
            {"text": m.group(3), "type": "pd_keyword",
             "valid": not missing_root,
             "error": "" if not missing_root else "来自(in)后缺少 root 路径（如 来自(in) '保单'的XX）"},
        ]
        if not missing_root:
            out.append({"text": root_q + cut, "type": "pd_path",
                        "valid": path_ok,
                        "error": "" if path_ok else "列表路径不存在"})
        if not rest.strip():
            return self._pd_out(out, pref)
        # ---- 条件子句切词 + 校验（分组保证 text 拼接 == 原文） ----
        cm = re.match(
            r"^(符合条件[：:]\s*)"
            r"(当前这个\s*)"
            r"(\S+)"
            r"(\s*的\s*)"
            r"([^\s是属于；;\"]*)"
            r"(\s*)"
            r"((?:是|不是|包含)?)"
            r"(\s*)"
            r"(\"[^\"]*\"?|[^\";]*)"
            r"(\s*;?)\s*$", rest)
        if not cm:
            # 条件子句未成形（输入中）：整段灰显不校验
            out.append({"text": rest, "type": "pd_draft", "valid": True, "error": ""})
            return self._pd_out(out, pref)
        cond_tn, field, op, val = cm.group(3), cm.group(5), cm.group(7), cm.group(9)
        done = bool((cm.group(10) or "").strip())
        children = (node.get("children") if node else []) or []
        err_tn = "" if self._path_key(cond_tn) == self._path_key(tname) \
            else "条件类型名与主句不一致"
        if field:
            fk = self._path_key(field)
            ok_f = any(self._path_key(c.get("label") or "") == fk for c in children)
            err_f = "" if ok_f else f"字段『{field}』不在{tname}中"
        elif done:
            err_f = "缺少条件字段"
        else:
            err_f = ""
        if op:
            err_o = "" if op in self.COND_OPERATORS else "运算符应为 是/不是/包含"
        elif done:
            err_o = "缺少运算符"
        else:
            err_o = ""
        val_clean = val.strip().strip('"“”')
        err_v = "" if (val_clean or not done) else "缺少条件值"
        out += [
            {"text": cm.group(1), "type": "pd_keyword", "valid": True, "error": ""},
            {"text": cm.group(2), "type": "pd_keyword", "valid": True, "error": ""},
            {"text": cond_tn, "type": "pd_type", "valid": not err_tn, "error": err_tn},
            {"text": cm.group(4), "type": "pd_keyword", "valid": True, "error": ""},
            {"text": field + cm.group(6), "type": "pd_field",
             "valid": not err_f, "error": err_f},
            {"text": op + cm.group(8), "type": "pd_op",
             "valid": not err_o, "error": err_o},
            {"text": val, "type": "pd_value", "valid": not err_v, "error": err_v},
            {"text": cm.group(10), "type": "pd_keyword", "valid": True, "error": ""},
        ]
        return self._pd_out(out, pref)

    def parse_predefine_lines(self, text: str) -> list[dict]:
        """解析编辑区预定义行 → [{alias, type_name, path_key, node, is_list}]。

        支持形式（右值解析与 annotate_predefine_line 共用 _pd_rvalue_parts）：
          - 列表：指定'别名'为一个X来自(in) 'root'的路径（path 命中 _list_index）
          - 非列表·路径：指定'别名'为 'root'的路径（root 引号可省，命中 BOM 字段）
          - 非列表·方法：指定'别名'为帮助.向下取整(实参)（is_method，类型取方法返回类型）
          - 非列表·常量：指定'别名'为 1232 / true / "abc"（is_const，类型不强校验）
        未命中（路径不存在/主句未输完）的行不注册词条。
        兼容中文弯引号（''）与半角直引号（'）。
        """
        self._ensure_list_index()
        out: list[dict] = []
        for ln in (text or "").split("\n"):
            ln = ln.strip()
            # 可选"预定义"引导词前缀（与编辑器行文一致）：预定义 指定'别名'…
            pm = re.match(r"^预定义\s*", ln)
            if pm:
                ln = ln[len(pm.group(0)):]
            if not ln.startswith("指定"):
                continue
            # 归一化中文弯引号 → 半角直引号（两种引号输入都能命中）
            ln = ln.replace("\u2018", "'").replace("\u2019", "'")
            m = self.PREDEFINE_LINE_RE.match(ln)
            if m:
                alias = m.group(1).strip()
                tname = m.group(2).strip()
                pk = self._path_key(m.group(3))
                node = self._find_list_node(pk)
                if node:
                    out.append({"alias": alias, "type_name": tname, "path_key": pk,
                                "node": node, "is_list": True})
                continue
            # 非列表：指定'别名'为 对象字段路径 / 方法调用 / 字面常量
            # 复用 _pd_rvalue_parts（与标注同一口径）：旧写法只认带引号 root 的
            # `'root'的路径`，导致①手输无引号路径预览绿却不注册词条，②方法型/
            # 常量型预定义（ADP 样本 202 例）别名一律注册不上。
            pr = self._pd_rvalue_parts(ln)
            if not pr:
                continue
            alias = pr["alias"].strip("'")
            ex = pr.get("expr") or {}
            if ex.get("form") in ("arith", "alias"):
                # 表达式右值（2026-09-22，后端示例集主形态）：别名照样注册词条，
                # 否则规则行 `如果 '纯风险保费' > 100` 解析不出该别名（旧口径整行不
                # 注册，且预览还标红"对象字段路径不存在"、上传预检直接拦死）。
                # type 取表达式推断结果（全数值族 → double；含字符串/日期参与 → 空
                # 即不强校验，与对象节点口径一致）；refs 留给测试报文骨架收集。
                out.append({
                    "alias": alias, "type_name": "", "path_key": "",
                    "node": {"label": (ex.get("text") or "").strip(),
                             "type": ex.get("type") or "", "children": []},
                    "is_list": False, "is_expr": True,
                    "refs": ex.get("refs") or [],
                    "expr_error": not ex.get("ok", True),
                })
                continue
            if pr["mhit"]:
                # 右值是方法调用：别名 = 方法结果值。类型取 BOM 方法节点返回类型
                # （void setter/取不到 → 空，视作不强校验，与对象节点口径一致）
                fp = pr.get("method_fp") or ""
                it = next((x for x in self.path_index
                           if (x.get("field_path") or "") == fp), None) if fp else None
                rt = (it or {}).get("type") or ""
                out.append({
                    "alias": alias, "type_name": "",
                    "path_key": self._path_key(pr["mhit"][0]),
                    "node": {"label": pr["mhit"][0],
                             "type": "" if rt == "void" else rt,
                             "children": []},
                    "is_list": False, "is_method": True,
                })
                continue
            if pr["lit_ok"]:
                # 常量绑定（指定'X'为 1232 / true / "abc"）：别名可被规则行直接引用
                out.append({"alias": alias, "type_name": "", "path_key": "",
                            "node": {"label": pr["path"].strip(), "type": "",
                                     "children": []},
                            "is_list": False, "is_const": True})
                continue
            if not (pr["path_ok"] and pr["shown"].strip()):
                continue
            pk = self._path_key((pr["root"] + "的" if pr["root"] else "") + pr["shown"])
            item = next((it for it in self.path_index
                         if self._path_key(it.get("label") or "") == pk), None)
            if item:
                out.append({
                    "alias": alias, "type_name": "", "path_key": pk,
                    "node": {"label": item.get("label") or "",
                             "type": item.get("type") or "",
                             "children": []},
                    "is_list": False,
                })
        return out

    def predefine_lex(self, predefines: str) -> list[dict]:
        """预定义词条（生成端原生形态），注入规则模式检索/校验：

        - 规则预定义变量                    （一级对象，field_path 无点）
        - 规则预定义变量的<别名>             （二级对象，field_path 一点）
        - 规则预定义变量的<别名>的<子字段>    （三级字段，type=子字段类型）
        词条结构与 path_index 一致，source="predefine" 供前端标注。
        """
        out: list[dict] = []
        if not (predefines or "").strip():
            return out
        pds = self.parse_predefine_lines(predefines)
        if not pds:
            # 无任何解析成功的预定义：不注入词条（一级"规则预定义变量"也不出现）
            return out
        out.append({
            "label": self.PD_ROOT, "field_path": self.PD_ROOT,
            "kind": "object", "node_label": self.PD_ROOT,
            "type": "", "is_enum": False, "source": "predefine",
        })
        for pd in pds:
            alias = pd["alias"]
            fp2 = self.PD_ROOT + "." + alias
            if pd.get("is_list"):
                # 列表：二级为集合对象，三级为子字段
                out.append({
                    "label": self.PD_ROOT + "的" + alias, "field_path": fp2,
                    "kind": "object", "node_label": alias,
                    "type": "", "is_enum": False, "source": "predefine",
                })
                for ch in pd["node"].get("children") or []:
                    cl = ch.get("label") or ""
                    if not cl:
                        continue
                    out.append({
                        "label": self.PD_ROOT + "的" + alias + "的" + cl,
                        "field_path": fp2 + "." + cl,
                        "kind": "field", "node_label": cl,
                        "type": ch.get("type") or "", "is_enum": False,
                        "source": "predefine",
                    })
            else:
                # 非列表：直接绑定对象/叶子字段，二级即字段
                out.append({
                    "label": self.PD_ROOT + "的" + alias, "field_path": fp2,
                    "kind": "field", "node_label": alias,
                    "type": pd["node"].get("type") or "", "is_enum": False,
                    "source": "predefine",
                })
        return out

    def _set_pd_lex(self, predefines: str) -> None:
        """按编辑区预定义行重建预定义词条表（search/annotate 入口调用）。"""
        self._pd_lex = self.predefine_lex(predefines)
        self._pd_by_label = {it["label"]: it for it in self._pd_lex}

    def _pd_roots(self) -> list[dict]:
        """一级对象候选追加：规则预定义变量（有解析成功的预定义时）。"""
        return [it for it in self._pd_lex if it["field_path"] == self.PD_ROOT]

    def _empty_word_methods(self, n: int) -> list[dict]:
        """空输入候选表尾的方法垫底段（2026-10-08，≤n 条）。

        池由 _build_path_index 预生成并排序（口径同 _match_paths 空词：
        中文方法名优先、纯英文名沉底、浅层优先），此处仅切前 n 条、
        出口标 kind=method 供前端类型标签与选中替换逻辑复用。
        """
        pool = getattr(self, "_empty_methods", None) or []
        return [{**it, "kind": "method"} for it in pool[:max(n, 0)]]

    # ------------------------------------------------------------------
    # 算术运算符（"如果"条件，词形=符号 + - * /，中文/全角为兼容别名；
    # 生成端 ARITH_OPS 同口径）辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _is_arith(word: str) -> bool:
        """该词是否算术运算符（加/减/乘/除 主词形及 + - * / 与全角兼容变体）。"""
        return (word or "").strip() in ARITH_WORDS

    def _arith_candidates(self) -> list[dict]:
        """算术运算符候选（附 field_path/source，与其它静态候选结构一致）。"""
        return [{**c, "field_path": "", "source": "static"} for c in ARITH_CANDIDATES]

    def _is_arith_operand_item(self, item: dict | None) -> bool:
        """算术操作数类型判定：数值类型字段/别名=可；类型未知=宽容放行；
        明确非数值（string/date/boolean/list）=不可（候选位不弹、校验位标红）。"""
        if not item:
            return False
        ty = (item.get("type") or "").lower()
        if not ty:
            return True
        return self._pd_type_family(ty) == "numeric"

    def _numeric_hits(self, word: str, limit: int) -> list[dict]:
        """数值操作数候选：路径匹配结果按操作数类型过滤——
        数值类型字段优先，类型未知叶子/对象节点（可继续下钻）沉底。"""
        hits = self._match_paths(word, max(limit * 4, 40))
        keep: list[dict] = []
        unk: list[dict] = []
        for it in hits:
            ty = (it.get("type") or "").lower()
            # 方法候选（_match_paths 出口已标 method）与对象容器同档沉底
            if it.get("kind") in ("object", "method") or not ty:
                unk.append(it)
            elif self._pd_type_family(ty) == "numeric":
                keep.append(it)
        return (keep + unk)[:limit]

    def _is_operator(self, word: str) -> bool:
        """判断词是否是运算符（含"..."占位符，如"属于...中的一个"）。"""
        w = (word or "").strip()
        if not w:
            return False
        from desc_to_rulecontent.selector_values import SELECTOR_OPS, normalize_selectors
        if normalize_selectors(w) in SELECTOR_OPS:
            return True
        # 区间句式「在 起 和 止 之间」各独立词与旧词形"介于"均算运算符词位
        if w in (BETWEEN_HEAD, BETWEEN_MID, BETWEEN_TAIL, BETWEEN_LEGACY):
            return True
        for op in OPERATOR_CANDIDATES:
            # 运算符 label 含 "..." 占位（如"以...开头"），填入时去掉了"..."，
            # 故比较时也去掉"..."来匹配已填入的词
            if op["label"].replace("...", "") == w.replace("...", ""):
                return True
            if op["label"] == w:
                return True
        return False

    def _find_path_item(self, word: str) -> dict | None:
        """在 path_index 中精确查找 label==word 的条目（对象或字段）。

        返回条目 dict（含 kind / field_path / type / node_label），找不到返回 None。
        """
        p = (word or "").strip()
        if not p:
            return None
        for item in self.path_index:
            if item["label"] == p:
                return item
        # 预定义词条兜底（规则预定义变量...），BOM 未命中时生效
        item = self._pd_by_label.get(p)
        if item:
            return item
        hit = self._match_parameterized_path(p)
        return hit[1] if hit and hit[0] == len(p) else None

    def _match_parameterized_path(self, text: str, pos: int = 0, require_boundary: bool = True):
        if '(' not in text[pos:] and '（' not in text[pos:]:
            return None
        index = getattr(self, '_parameterized_paths', None)
        if index is None:
            from desc_to_rulecontent.parameterized_paths import ParameterizedPaths
            self._ensure_method_types()
            methods = {}
            for method in list(self.cat.methods) + list(self.cat.object_by_path.values()):
                fp = method['field_path']
                if not fp.endswith('()'):
                    continue
                variants = self._method_types.get(fp) or []
                types = variants[0] if variants else ['string']
                methods[fp] = {'detailsList': [{'type': t} for t in types]}
            index = self._parameterized_paths = ParameterizedPaths(
                {item['label']: item for item in self.path_index}, methods)
        return index.match(text, pos, require_boundary)

    # boolean 字段的布尔运算符拆分：把"为 是/否（成立/不成立）"拆成可直接填入的具体选项
    BOOLEAN_OPTIONS: list[str] = ["为 是", "为 否", "为 成立", "为 不成立"]

    def _value_candidates(self, field_item: dict | None) -> list[dict]:
        """值位置候选：按前一个字段的类型给对应类型的值候选。

        - string 字段 → 一级对象（结果/保单/动态域集）作为值入口，可继续下钻
        - boolean 字段 → 是/否/成立/不成立
        - numeric / date 字段 → 数字/日期（自由输入，无预设候选，返回空让用户手输）
        """
        if field_item is None:
            return self._top_objects() + self._pd_roots()
        ftype = (field_item.get("type") or "").lower()
        if ftype == "boolean":
            out = []
            for v in self.BOOLEAN_OPTIONS:
                # 去掉"为 "前缀，值就是"是/否/成立/不成立"
                label = v.replace("为 ", "")
                out.append({
                    "label": label, "code": "BOOLEAN_BELONG_TO",
                    "kind": "value", "field_path": "", "source": "static",
                })
            return out
        if ftype in ("string", "str"):
            # 字符串值：给一级对象作为值候选入口（用户可继续下钻到具体值）
            return self._top_objects() + self._pd_roots()
        if self._pd_type_family(ftype) == "numeric":
            # 数值字段：值也可以是"另一个数值字段/预定义别名"（字段比字段、
            # 字段+算术表达式），给一级对象/预定义下钻入口；数字仍可直接手输
            return self._top_objects() + self._pd_roots()
        # 日期：自由输入（无预设候选）
        return []

    def _value_candidates_nonempty(self, field_item: dict | None, current_word: str) -> list[dict] | None:
        """值位置且当前词非空（用户在输入值）时的候选。

        - numeric / date 字段 → 返回 []（自由输入数字/日期，不弹对象字段候选）
        - boolean 字段 → 校验当前词是否为合法布尔值，返回 []（不再弹候选）
        - string 字段 → 返回 None（走正常路径检索，值可能是对象字段/枚举）
        - field_item 无法识别 → 返回 None（走正常检索，避免误拦）
        """
        if field_item is None:
            return None
        ftype = (field_item.get("type") or "").lower()
        if ftype in ("string", "str"):
            return None  # 字符串值：继续走路径检索，允许选对象字段/枚举值
        # 数值字段：值也可以是"另一个数值字段/预定义别名"（字段比字段、算术链
        # 中段"大于 保额 加 "），检索数值操作数候选；但正在手输纯数字时不弹
        # （避免"输 0 弹一堆含 0 字段"的误导，保持旧口径）
        if self._pd_type_family(ftype) == "numeric":
            import re as _re
            wd = current_word.strip()
            if _re.match(self.NUMBER_RE, wd) or (wd and wd.lstrip("-.").isdigit()):
                return []
            return self._numeric_hits(wd, 50)
        # date / boolean：值是具体日期/布尔，不弹对象字段候选
        return []

    def _field_operators(self, item: dict) -> list[dict]:
        """给 BOM 字段类型兼容的运算符（连接词最前）+ 该字段兼容运算符。

        用 bom_catalog.get_operators(field_path) 取该字段类型的兼容运算符。
        boolean 字段特殊处理：把"为 是/否（成立/不成立）"拆成"为 是/为 否"等
        可直接填入的选项（无需手动输值）。
        """
        out = []
        for c in CONNECTOR_CANDIDATES:
            out.append({
                "label": c["label"], "code": c["code"],
                "kind": c["kind"], "field_path": "", "source": "static",
            })
        ftype = (item.get("type") or "").lower()
        if ftype == "boolean":
            # 布尔字段：给拆分后的"为 是/为 否/..."具体选项
            for label in self.BOOLEAN_OPTIONS:
                out.append({
                    "label": label, "code": "BOOLEAN_BELONG_TO",
                    "kind": "operator", "field_path": "", "source": "static",
                })
            return out
        ops = self.cat.get_operators(item.get("field_path") or "")
        from desc_to_rulecontent.selector_values import SELECTOR_OPS
        field = self._selector_catalog().fields.get(item.get('field_path'))
        if field and field.get('codeService'):
            out.extend({'label': label, 'code': code, 'kind': 'operator',
                        'field_path': '', 'source': 'static'}
                       for label, code in SELECTOR_OPS.items())
        if not ops and item.get("source") == "predefine":
            # 预定义字段不在语料运算符映射中：借用同类型 BOM 字段的运算符集
            pftype = (item.get("type") or "").lower()
            for it in self.path_index:
                if it.get("kind") == "field" and (it.get("type") or "").lower() == pftype:
                    ops = self.cat.get_operators(it.get("field_path") or "")
                    if ops:
                        break
        for op in ops:
            out.append({
                "label": op["label"], "code": op.get("code") or op["label"],
                "kind": "operator", "field_path": "", "source": "static",
            })
        # string 字段额外给"内容长度"修饰符（两段式：内容长度 → 比较符 → 数字）
        if ftype in ("string", "str"):
            out.append({
                "label": "的内容长度", "code": "",
                "kind": "operator", "field_path": "", "source": "static",
            })
        # 数值字段追加算术运算符（加/减/乘/除）：支持"数值字段 加 数值字段|数字"
        # 算术表达式，词形与生成端 ARITH_OPS 一致；放在比较运算符之后，
        # 不打乱"字段→比较运算符"主流程候选顺序
        if self._pd_type_family(ftype) == "numeric":
            out.extend(self._arith_candidates())
        return out

    def _match_len_comp_ops(self, word: str) -> list[dict]:
        """内容长度修饰符之后、正在输入比较符时的候选（只匹配数值比较符）。"""
        w = (word or "").strip()
        out = []
        for op in LEN_COMPARISON_CANDIDATES:
            if w in op["label"] or op["label"].startswith(w):
                out.append(dict(op))
        return out

    def annotate_then_else_line(self, line: str) -> list[dict]:
        """then/else 行（'那么'/'否则' 开头）标注：方法匹配 + 参数空校验（2026-09-02）。

        只拦结构性错误（整行一段返回，前端标红 + title=error）：
        - 行首（去引导词后）未匹配到 BOM 方法 → "未匹配到 BOM 方法"
        - 方法匹配但某参数位为空（trim 后空 / 纯空白）→ "参数 paramX 为空"
        参数值不做类型校验：占位符（<string>/<费用率>/<核保结果>）非空不拦截，
        字面量/表达式/字段引用均放行（与转换端"参数位不拦截"口径一致）。
        """
        raw = (line or "").strip()
        if not raw:
            return [{"text": raw, "type": "thenelse", "valid": True, "error": ""}]
        # 拆引导词（那么/否则，含其后的空格）与动作文本
        mg = re.match(r"^(那么|否则)\s*", raw)
        g = mg.group(0) if mg else ""
        t = raw[len(g):]
        if not t:   # 只有引导词（编辑中）：不校验
            return [{"text": raw, "type": "thenelse", "valid": True, "error": ""}]
        try:
            import sys as _sys
            from ..config import project_root as _pr
            root = _pr()
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from desc_to_rulecontent.write_rule_to_adp import (
                effective_parse_basis, extract_params, method_match_score,
                split_template, template_head_has_slots)
        except Exception:
            return [{"text": raw, "type": "thenelse", "valid": True, "error": ""}]
        # 方法匹配：模板固定文本可定位总长 + 模板全长择优（重名首段等长的方法
        # 由更完整命中区分，如 setMapXomStringAttribute 与 …ForDeveloper）
        best = None
        for it in self._method_index:
            segs, _ = split_template(it["label"])
            if not segs or not segs[0]:
                continue
            p = segs[0]
            if p and t.startswith(p):
                key = (method_match_score(t, segs),
                       len("".join(s for s in segs if s)))
                if best is None or key > best[0]:
                    best = (key, it)
        if best is None:
            return [{"text": raw, "type": "thenelse", "valid": False,
                     "error": "未匹配到 BOM 方法（文本应以方法模板开头，可从'那么'候选选方法）"}]
        _, it = best
        # 参数切分基准：注册表 expressionArray 正常时优先；畸形（text 段残留数字
        # 槽位）/缺失 → BOM label 数字槽位（BOM 原生参数位标记）。
        expr = self._method_expr_array(it)
        segs, slots, _types, keep, descs = effective_parse_basis(it["label"], expr)
        params = extract_params(t, segs, slots, keep, descs)
        # 参数空检查：trim 后为空（空串/纯空白）→ 标红；占位符/任意非空值不拦
        for k, v in params.items():
            if isinstance(v, str) and not v.strip():
                return [{"text": raw, "type": "thenelse", "valid": False,
                         "error": "参数 %s 为空，请为该位填值（字段/文本/数字均可）" % k}]
        # 合法：方法模板固定文本标绿（methodtext），参数值段灰（thenelse）。
        # 固定段统一取切分基准 segments（正常条目=text 段；畸形/缺失=label 槽位段）。
        text_seqs = [s for s in segs if s]
        words: list[dict] = []
        if g:
            words.append({"text": g, "type": "methodtext", "valid": True, "error": ""})
        pos = 0
        for seg in text_seqs:
            # 定位锚点：去前导标点/空白（用户常省略逗号，如 "0.05 规则名称为:X"），
            # 命中段按 t 原样文本着绿，段前剩余归入前参数段
            core = seg.strip().lstrip(",，、;；:： ")
            if not core:
                continue
            idx = t.find(core, pos)
            if idx >= 0:
                if idx > pos:
                    words.append({"text": t[pos:idx], "type": "thenelse",
                                  "valid": True, "error": ""})
                words.append({"text": t[idx:idx + len(core)], "type": "methodtext",
                              "valid": True, "error": ""})
                pos = idx + len(core)
            # text 段缺失（用户改写/省略）：跳过，剩余并入后续参数段
        if pos < len(t):
            words.append({"text": t[pos:], "type": "thenelse", "valid": True, "error": ""})
        return words if words else [{"text": raw, "type": "thenelse", "valid": True, "error": ""}]

    def _pick_registry_entry(self, cands: list, label: str):
        """methodId 前缀同源多候选（方法重载）→ 按 label 与 expressionArray text 段
        一致性择优（委托 write_rule_to_adp.pick_registry_entry），失败回退第一条。"""
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        try:
            import sys as _sys
            from ..config import project_root as _pr
            root = _pr()
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from desc_to_rulecontent.write_rule_to_adp import pick_registry_entry
            return pick_registry_entry(cands, label)
        except Exception:
            return cands[0]

    def _method_expr_array(self, it: dict) -> Optional[list]:
        """BOM 方法条目 → 注册表对应 expressionArray（无则 None）。

        前缀命中的多条目（方法重载，methodId 仅 ADP 序号不同）按 label 消歧，
        否则重载方法的参数切分/定位会取到另一个重载的模板结构。
        """
        reg = self._load_method_registry()
        if not reg:
            return None
        cls = (it.get("class_path") or "").rsplit(".", 1)[-1]
        mname = (it.get("field_path") or "").rsplit(".", 1)[-1].rstrip("()")
        derived = "%s_%s" % (cls, mname)
        hits = [x for x in reg
                if (x.get("methodId") or "") == derived
                or (x.get("methodId") or "").startswith(derived + "_")]
        x = self._pick_registry_entry(hits, it.get("label") or "")
        return (x.get("expressionArray") or None) if x else None

    def _method_template(self, it: dict) -> Optional[str]:
        """候选方法 → 占位模板（注册表 expressionArray 渲染，text 原样 + input 语义/类型占位）。

        匹配须带 `_` 序号边界（setMapXomStringAttribute 不误配 ForDeveloper 条目）；
        前缀同源多条目（方法重载，methodId 仅序号不同）按 label 消歧（_pick_registry_entry）；
        expressionArray 缺失或畸形（text 段未拆参数位、渲染结果残留数字槽位）时，
        回退 BOM label 数字槽位 → 参数占位（'帮助.设置对象0…1的值为2' → '…<string>…'），
        保证候选模板不再出现裸数字槽位 + 占位符堆尾。
        """
        label = (it.get("label") or "").rstrip("()")
        cls = (it.get("class_path") or "").rsplit(".", 1)[-1]
        mname = (it.get("field_path") or "").rsplit(".", 1)[-1].rstrip("()")
        derived = "%s_%s" % (cls, mname)
        try:
            import sys as _sys
            from ..config import project_root as _pr
            root = _pr()
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from desc_to_rulecontent.write_rule_to_adp import (
                degenerate_label_template, render_placeholder_template,
                template_head_has_slots)
        except Exception:
            return degenerate_label_template(label) if label else None
        reg = self._load_method_registry()
        if reg:
            hits = [x for x in reg
                    if (x.get("methodId") or "") == derived
                    or (x.get("methodId") or "").startswith(derived + "_")]
            x = self._pick_registry_entry(hits, label)
            if x:
                arr = x.get("expressionArray") or []
                tpl = render_placeholder_template(arr) if arr else None
                if tpl and not template_head_has_slots(tpl):
                    return tpl   # 正常模板（参数已内联）
                # 畸形/缺失 → label 槽位兜底，类型取 expressionArray 的 returnType
                types = [nd.get("returnType") for nd in arr
                         if isinstance(nd, dict) and nd.get("type") == "input"]
                return degenerate_label_template(label, types) if label else tpl
        return degenerate_label_template(label) if label else None

    def search_methods(self, word: str = "", limit: int = 50,
                       template_style: str = "expr") -> list[dict]:
        """then/else 动作候选：只给 BOM 方法节点（候选来源纯方法，不含普通字段）。

        匹配：完全相等 > 前缀 > 包含（label 与完整路径 label 均参与）；空词给全量（limit 个）。
        param_count：方法 label 中占位数字个数（参数槽位数）。
        template：template_style="expr"（默认，then/else）→ 注册表 expressionArray 渲染的
        内联占位模板；"call"（predefine 右值）→ label + "(<类型> , <类型>)" 调用式模板
        （detailsList 参数类型，缺失位回退 <text>）。
        """
        w = (word or "").strip()
        # 2026-09-23：与路径侧统一比较键 _path_key。预定义右值检索把"为"后到光标
        # 整段原样传进来（predefine_param_context stage=rvalue），手打词常剥空格/
        # 带引号（"销售费用0,销售费用类型" vs label "销售费用0, 销售费用类型…"），
        # 旧版原文直比 label 导致含内部空格的方法永远配不上、方法候选整体缺席
        # （字段路径池已剥空格能配上，用户只见路径不见方法）。只放宽比较，不改展示。
        wk = self._path_key(w)
        seen: set[str] = set()
        out = []
        for it in self._method_index:
            label = it["label"]
            if not label:
                continue
            lk = self._path_key(label)
            fulls = it.get("full_labels") or ()
            if wk and not (lk.startswith(wk) or wk in lk
                           or any(self._path_key(fl).startswith(wk)
                                  or wk in self._path_key(fl) for fl in fulls)):
                continue
            if template_style == "call":
                # 预定义右值：候选与插入模板用带上级对象的样本形态
                # （动态域集的帮助的帮助.取大值(0,1)(<double> , <double>)），
                # 无完整路径时回退短 label
                disp = next(iter(it.get("disp_labels") or ()), label)
                # 2026-09-23：call 形态去重键改用展示 label——同名不同上级对象的
                # 重载（getFeeInfoRate 在保单/关联保单/上年保单等 5 个对象上各一
                # 条）展示名不同、语义不同，全该露出；短 label 去重会把 5 条砍成
                # 1 条（用户只见"含车损关联保单"版）。同 disp 多 Java 方法名变体
                # 仍只留一条，与 then/else 池同口径。
                if disp in seen:
                    continue
                seen.add(disp)
                template = self.render_call_template(disp, self._call_types_for(it))
            else:
                if label in seen:   # 同 label 多路径（appbase.result.xxx / result.xxx）只留一个
                    continue
                seen.add(label)
                disp = label
                template = self._method_template(it)
            out.append({
                "label": disp,
                "field_path": it["field_path"],
                "class_path": it["class_path"],
                "param_count": it["param_count"],
                "kind": "method",
                # 返回类型标签（2026-09-29：那么/否则与预定义右值方法候选标类型）
                "type": it.get("type") or "",
                "source": "bom_method",
                "template": template,
            })
        # 纯英文方法名（addCpInfos() 等，展示名末级无中文）沉底；
        # 检索词含字母（有意搜英文名）不降权。空词稳定排序仅做降权档。
        def _leaf(x: dict) -> str:
            return x["label"].replace(" ", "").split("的")[-1]
        if w:
            out.sort(key=lambda x: (_en_pen(w, _leaf(x)),
                                    self._path_key(x["label"]) != wk,
                                    not self._path_key(x["label"]).startswith(wk),
                                    x["label"]))
        else:
            out.sort(key=lambda x: _en_pen(w, _leaf(x)))
        return out[:limit]

    # ---------- then/else 参数位上下文检索（2026-09-03） ----------
    # 方法模板 = expressionArray 的 text/input 交替结构；参数位分两类：
    #   field：该位应填 BOM 对象/字段路径（含动态域集枚举项），给对象路径候选；
    #   text：纯文本字面量/数字，自由输入，不给候选。
    # 分类与候选源全部由当前 BOM/注册表推导（不写死 BOM 名/字段名），换 BOM 表可复用。
    def method_param_context(self, line: str = "", caret: int = -1,
                             limit: int = 50) -> dict:
        """then/else 行（编辑中）上下文检索：定位光标所处参数位并给该位候选。

        line：当前整行文本（可含'那么/否则'引导词）；caret：光标在该行内的偏移。
        返回：
          stage="method" -> 未匹配到方法（仍在输方法名），candidates 为方法候选；
          stage="param"  -> 已定位到参数位，{param_index, param_kind, param_ptype,
                            start, end, word, candidates}；参数位声明类型
                            （占位符 <double>/<string>… → detailsList → returnType，
                            与预定义实参位同一链路 _call_types_for/_pd_param_candidates）
                            时按类型族过滤给路径候选（空词也弹头部）；类型未知时
                            仅 field 位有词才给候选；
          stage="none"   -> 无法定位（光标在固定文本/模板外），不弹候选。
        start/end 为参数值在整行（含引导词）中的区间，供前端整段替换。
        """
        raw = line or ""
        caret = len(raw) if caret < 0 else min(caret, len(raw))
        try:
            import sys as _sys
            from ..config import project_root as _pr
            root = _pr()
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from desc_to_rulecontent.write_rule_to_adp import (
                effective_parse_basis, extract_params, method_match_score,
                split_template, segments_from_expression_array)
        except Exception:
            return {"stage": "none", "candidates": []}
        # 剥引导词（那么/否则，含其后的空格），偏移随之前移
        mg = re.match(r"^(那么|否则)\s*", raw)
        pref = len(mg.group(0)) if mg else 0
        t = raw[pref:]
        caret -= pref
        if caret < 0:
            caret = 0
        if not t.strip():
            # 只有引导词：给全量方法候选（与按空格进入 then 一致）
            return {"stage": "method", "candidates": self.search_methods("", limit)}
        # 方法匹配：同标注端——固定文本可定位总长 + 模板全长择优
        best = None
        for it in self._method_index:
            segs, _ = split_template(it["label"])
            if not segs or not segs[0]:
                continue
            p = segs[0]
            if p and t.startswith(p):
                key = (method_match_score(t, segs),
                       len("".join(s for s in segs if s)))
                if best is None or key > best[0]:
                    best = (key, it)
        if best is None:
            # 方法名输入中：按已输入字检索方法候选
            head = t[:caret].strip()
            w = head.split()[-1] if head else ""
            return {"stage": "method", "candidates": self.search_methods(w, limit)}
        _, it = best
        expr = self._method_expr_array(it)
        # 参数位定位基准：expressionArray 畸形/缺失 → BOM label 槽位段
        # （畸形条目不再整体回退 stage=method，参数位区间/整段替换照常可用）
        segs, slots, _types, _keep, _descs = effective_parse_basis(it["label"], expr)
        inputs = [n for n in (expr or [])
                  if isinstance(n, dict) and n.get("type") == "input"]
        n_params = len(slots) if slots else len(inputs)
        if not n_params:
            return {"stage": "none", "candidates": []}
        # 固定段定位：各非空固定段按序真实出现在文本中，
        # 否则判定仍处于"输入方法名/模板未插入"阶段 → 弹方法候选。
        seg_pos: list[tuple[int, int]] = []
        pos = 0
        template_ok = True
        for d in [s for s in segs if s]:
            idx = t.find(d, pos)
            if idx < 0:
                template_ok = False
                break
            seg_pos.append((idx, idx + len(d)))
            pos = idx + len(d)
        if not template_ok or not seg_pos:
            head = t[:caret].strip()
            w = head.split()[-1] if head else ""
            return {"stage": "method", "candidates": self.search_methods(w, limit)}
        # 参数 k 值区间 = [seg_pos[k-1].end, seg_pos[k].start 或行尾]（含端点：
        # 光标停在参数值尾、紧贴下一固定文本起点时仍视为正在编辑该参数）。
        # 光标严格落在固定文本段内部（非端点）→ 不在任何参数位，不弹。
        for (a, b) in seg_pos:
            if a < caret < b:
                return {"stage": "none", "candidates": []}
        chosen = None
        for k in range(1, n_params + 1):
            s = seg_pos[k - 1][1] if k - 1 < len(seg_pos) else 0
            e = seg_pos[k][0] if k < len(seg_pos) else len(t)
            if s <= caret <= e:
                chosen = k
                break
        if chosen is None:
            return {"stage": "none", "candidates": []}
        node = inputs[chosen - 1] if chosen <= len(inputs) else {}
        desc = node.get("desc") or ""
        # field/text 判定：codeService 非空 或 desc 是无尖括号的完整路径提示 → field；
        # 其余（<占位符>、<数字>…）→ text。不依赖任何 BOM 字样。
        has_code_svc = bool((node.get("codeService") or "").strip())
        is_path_hint = ("<" not in desc and ">" not in desc and desc.strip())
        param_kind = "field" if (has_code_svc or is_path_hint) else "text"
        # 参数值已输入部分：光标落在占位符 <...> 内视为未开始输入（word 空）
        raw_cur = t[s:caret]
        if "<" in raw_cur or ">" in raw_cur:
            word = ""
        else:
            word = raw_cur.strip()
        # ---- 参数位声明类型（2026-09-28 与预定义实参位口径统一）----
        # 类型取自：占位符若恰是类型词（<double>/<string>…）→ BOM detailsList
        # （_call_types_for 与预定义同一链路）→ expressionArray input returnType。
        # then/else 模板占位符多为语义词（<参数1>）或类型缺失回退（<text>），
        # 一律视同未知。预定义口径：参数位总有候选——有类型按族过滤，
        # 未知给全量路径头部（空词亦然，原位替换占位符）。
        ph_m = re.search(r"<([^<>]*)>", t[s:e])
        ph_type = (ph_m.group(1).strip().lower() if ph_m else "")
        if ph_type and not self._pd_type_family(ph_type) and ph_type not in ("boolean",):
            ph_type = ""                       # 语义词/未知占位符不参与类型判定
        tlist = self._call_types_for(it) or []
        decl = ((tlist[chosen - 1] or "").strip().lower()
                if chosen - 1 < len(tlist) else "")
        ptype = ph_type or decl or (node.get("returnType") or "").strip().lower()
        if self._pd_type_family(ptype) or ptype == "boolean":
            # 类型已知：按族过滤（boolean 无专属池，走 all 池精确排尾即可）
            cands = self._pd_param_candidates(self._pd_type_family(ptype),
                                              word, limit, ptype=ptype)
            # 池 label 是预定义右值带引号形态（'保单'的X）；then/else 动作
            # 参数为无引号路径（保单的X），剥引号后填入
            cands = [dict(c, label=(c.get("label") or "").replace("'", ""))
                     for c in cands]
        else:
            # 类型未知：与预定义 fam="" 同口径给全量路径池（空词也弹头部）。
            # boolean 型 detailsList 常标 string 之外的杂值，保守归未知。
            cands = self._pd_param_candidates("", word, limit)
            cands = [dict(c, label=(c.get("label") or "").replace("'", ""))
                     for c in cands]
            if not cands and param_kind == "field" and word:
                # 路径池未命中（词深/拼写偏差）：ngram 兜底，与旧 field 位行为一致
                cands = self._ngram_search(word, limit)
        return {
            "stage": "param",
            "method_label": it.get("label") or "",
            "param_index": chosen,
            "param_kind": param_kind,
            "param_ptype": ptype or "any",
            "word": word,
            "start": pref + s,
            "end": pref + e,
            "candidates": cands[:limit],
        }

    def _top_objects(self) -> list[dict]:
        """空输入、上一词是运算符/连接词/如果时：给 BOM 一级对象候选（保单/结果/动态域集）。"""
        out = []
        for o in self.cat.get_top_objects():
            label = o.get("label") or ""
            if not label:
                continue
            out.append({
                "label": label,
                "field_path": o.get("field_path") or "",
                "field_id": o.get("field_id") or "",
                "kind": "object",
                "node_label": label,
                "source": "bom",
            })
        return out

    def _operators_and_connectors(self) -> list[dict]:
        """刚按空格、上一词是 BOM 对象或值：给连接词（最前）+ 运算符候选。"""
        out = []
        for c in CONNECTOR_CANDIDATES:
            out.append({
                "label": c["label"], "code": c["code"],
                "kind": c["kind"], "field_path": "", "source": "static",
            })
        for op in OPERATOR_CANDIDATES:
            out.append({
                "label": op["label"], "code": op["code"],
                "kind": op["kind"], "field_path": "", "source": "static",
            })
        return out

    def _match_paths(self, word: str, limit: int) -> list[dict]:
        """按当前词前缀/子串匹配多级对象路径候选。

        匹配策略（字符匹配）：
        - 整词完全相等 -> 最高
        - label_path 以 word 开头 -> 高（如输入"保单的个团"匹配"保单的个团标志"）
        - label 以 word 开头 / word 在 label 中 -> 中
        - node_label 匹配 -> 中
        词内含"的"时，优先匹配完整 label_path 前缀。
        """
        wl = word
        scored: list[tuple[float, dict]] = []
        # 检索池叠加预定义词条（规则预定义变量及其下级别名/子字段）
        for item in self.path_index + self._pd_lex:
            label = item["label"]
            node = item["node_label"]
            score = 0.0
            if label == wl:
                score = 1.0
            elif label.startswith(wl):
                # 前缀匹配（含"的"），深度浅者优先
                score = 0.95 - 0.005 * label.count("的")
            elif "的" in wl and label.startswith(wl):
                score = 0.9
            elif node.startswith(wl) or node == wl:
                score = 0.8
            elif wl in label:
                score = 0.6 - 0.005 * label.count("的")
            elif wl in node:
                score = 0.5
            if score > 0:
                # BOM 方法节点（field_path 尾 "()"）整体沉底：如果行以字段/
                # 对象为主，方法即使高分也排所有非方法之后（2026-09-29）
                scored.append((
                    1 if (item.get("field_path") or "").endswith("()") else 0,
                    _en_pen(wl, node), score, item))
        scored.sort(key=lambda x: (x[0], x[1], -x[2], x[3]["label"]))
        out = []
        for *_x, item in scored[:limit]:
            # 候选出口改标 kind=method 供前端类型标签（索引构建层保持
            # object/field，校验/运算符推荐口径不受影响）
            if (item.get("field_path") or "").endswith("()"):
                item = {**item, "kind": "method"}
            out.append(item)
        return out

    def _match_keywords(self, word: str) -> list[dict]:
        """匹配规则固定词（如"如果"）。"""
        out = []
        for kw in KEYWORD_CANDIDATES:
            if kw["label"].startswith(word) or word in kw["label"]:
                out.append({
                    "label": kw["label"], "code": kw["code"],
                    "kind": kw["kind"], "field_path": "", "source": "static",
                })
        return out

    def _match_ops_conns(self, word: str, include_arith: bool = False) -> list[dict]:
        """匹配运算符/连接词（当前词为运算符/连接词前缀时）。

        include_arith=True 时同时匹配算术运算符（加/减/乘/除）——仅在
        数值操作数之后的运算符选择位开启，普通比较位不弹，避免干扰主流程。
        用户输入匹配按"中文主词形前缀 + 符号规范形归一"口径（输入 加/×/+ 均命中
        同一候选，候选 label 恒为中文词形）。
        """
        out = []
        for op in OPERATOR_CANDIDATES:
            if word in op["label"] or op["label"].startswith(word):
                out.append({
                    "label": op["label"], "code": op["code"],
                    "kind": op["kind"], "field_path": "", "source": "static",
                })
        for c in CONNECTOR_CANDIDATES:
            if word in c["label"] or c["label"].startswith(word):
                out.append({
                    "label": c["label"], "code": c["code"],
                    "kind": c["kind"], "field_path": "", "source": "static",
                })
        if include_arith:
            norm = ARITH_NORMALIZE.get(word, word)
            for a in ARITH_CANDIDATES:
                al = a.get("alias", "")
                if word and (word in a["label"] or a["label"].startswith(word)
                             or norm == al):
                    out.append({**a, "field_path": "", "source": "static"})
        return out

    def preview(self, text: str) -> str:
        """预览：规范化文本（候选填入时由前端直接插入，这里仅作回显校验）。"""
        return text

    def stats(self) -> dict:
        return {"path_entries": len(self.path_index)}

    # ---------- 词类型标注 + 值类型校验（合法性检验） ----------
    NUMBER_RE = r"^[-+]?\d+(\.\d+)?$"   # 容忍显式正负号（"大于 -5"取负值）
    DATE_RE = r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$"
    # 分词整体扫描：系统日期字段带时间（"2026-09-20 00:00:00"），复制粘贴进来
    # 不得被空格拆成"日期"+"时间"两段（时间孤儿非数字非日期落字段档标红）；
    # _segment_words 命中即整体成词，DATE_RE 随后匹配（2026-10-08）。
    _DATETIME_SCAN = re.compile(
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"              # 日期
        r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"  # 可选时间（空格/T 分隔，含毫秒）
    )

    def _is_operand_token(self, tok: str | None) -> bool:
        """token 是否可作算术左操作数（字段/BOM 条目/数字/算术词）——
        仅用于 _segment_words 决定 +/- 粘连数字时是否拆为独立算术运算符。"""
        if not tok:
            return False
        if tok in ARITH_WORDS:
            return True
        import re as _re
        if _re.match(self.NUMBER_RE, tok):
            return True
        return self._find_path_item(tok) is not None

    def _segment_words(self, text: str) -> list[str]:
        """语义分词：优先匹配完整 BOM 路径（含内部空格的字段路径作为整体），
        其余按空格分成普通词。用于保留对象字段自带空格、删掉语义单元间空格。

        算术符号扩展（2026-09-24）：
        - 贪心 label 匹配的右边界除空格外还接受算术符号（"金额+100"→金额/+）；
        - 词首若为算术符号且前词可作操作数（+/-）或符号本身非正负号（* / 等），
          拆为独立运算符 token（"金额 -100"→金额/-/100）；"大于 -100"前词是运算符
          不拆，保留带符号数字（负值比较）。
        中文算术别名（加/减/乘/除）不做边界/拆分——维持"两侧须空白"旧口径，
        防止"乘客人数"等未注册词被从中间切开。

        返回 token 列表，每个 token 是完整字段路径 或 单个普通词（不含前后空格）。
        """
        if not text:
            return []
        from desc_to_rulecontent.selector_values import SELECTOR_RE
        selector = SELECTOR_RE.search(text)
        if selector:
            op = selector[1] + '（' + selector[2] + '）'
            tail = text[selector.end():]
            boundary = re.search(r'\s+(?:并且|或者)\s+|\s+[)）](?=\s|$)', tail)
            end = boundary.start() if boundary else len(tail)
            value = tail[:end].strip()
            return (self._segment_words(text[:selector.start()]) + [op]
                    + ([value] if value else []) + self._segment_words(tail[end:]))
        # 词典并入预定义词条 label（处理子字段 label 含内部空格的贪心匹配）
        ls = self._label_set | set(self._pd_by_label)
        n = len(text)
        i = 0
        result: list[str] = []
        while i < n:
            while i < n and text[i] == " ":
                i += 1
            if i >= n:
                break
            ch = text[i]
            bound = self._match_parameterized_path(text, i)
            if bound:
                result.append(text[i:bound[0]])
                i = bound[0]
                continue
            # 词首算术符号独立成 token（正负号仅在可作左操作数时才拆）
            if ch in ARITH_SYM_CHARS and (
                    ch not in "+-＋－"
                    or self._is_operand_token(result[-1] if result else None)):
                result.append(ch)
                i += 1
                continue
            # 贪心最长匹配完整路径 label（含内部空格），保证词边界；
            # 符号字符也算边界（"金额+100"里 label 止于 +）
            matched: str | None = None
            for j in range(n, i, -1):
                cand = text[i:j]
                if cand in ls and (j == n or text[j] == " "
                                   or text[j] in ARITH_SYM_CHARS):
                    matched = cand
                    break  # j 从大到小，第一个匹配即最长
            if matched:
                result.append(matched)
                i += len(matched)
            else:
                # 日期时间字面量整体成词（系统日期带时间"2026-09-20 00:00:00"，
                # 空格不得把时间拆成孤儿 token；边界须为空格或行尾，2026-10-08）
                dm = self._DATETIME_SCAN.match(text, i)
                if dm and (dm.end() == n or text[dm.end()] == " "):
                    result.append(dm.group(0))
                    i = dm.end()
                    continue
                # 普通词：到下一个空格或结尾
                j = i
                while j < n and text[j] != " ":
                    j += 1
                result.append(text[i:j])
                i = j
        return result

    # ---------- 粘贴整理：主系统规则文案 → 编辑器格式（2026-09-29） ----------
    # 主系统规则库展示文本直接复制过来："如果"行语义单元粘连（字段属于值
    # 中的一个/内容长度等于0），"那么"行带 1. 2. 3. 序号；编辑器折叠与标注
    # 需要连接词空格、不接受序号。整理原则=**最小干预**：只在明确的锚点词
    # （运算符/连接词/内容长度修饰符）处补空格断开，其余行内排版原样保留
    # （方法实参"（ 保单的关联保单，nclaimamount ）"内部空格、路径自带空格
    # 均不动）；锚点跳过 BOM label 命中区，防"是否续保"类字段被单字运算符
    # （是/以/在）从中间切开。"预定义"行、引导词行、空行一律原样。
    _PASTE_LEAD_RE = re.compile(r"^(预定义|如果|那么|否则)\s*[：:]?\s*(.*)$", re.S)
    _PASTE_ORD_RE = re.compile(r"^\s*\d+\s*[.、)）]\s*")
    _PASTE_CONN_RE = re.compile(r"^(并且|或者)")

    def _paste_anchor_table(self) -> dict:
        """锚点词表（首字符分桶、按长度降序保证"不包含"先于"包含"试中）。"""
        t = getattr(self, "_paste_anchor_c", None)
        if t is None:
            from desc_to_rulecontent.selector_values import SELECTOR_OPS
            words: set[str] = set(MULTI_OP_HEADS) | set(MULTI_OP_TAILS) \
                | set(UNARY_OPERATORS) | set(LEN_MODIFIERS)
            words |= set(SELECTOR_OPS)
            for c in OPERATOR_CANDIDATES:
                lab = c["label"]
                if "..." in lab:
                    # 多词运算符拆头/尾（"在...和...之间"的中段"和"不作锚点：
                    # 它高频出现在值/字段文本里，切开反而破坏取值）
                    words |= {w for w in lab.split("...") if w and w != BETWEEN_MID}
                else:
                    words.add(lab)
            # "为"（2026-09-29 补）：主系统"字段为 值"判断句的运算符展示词，
            # 不在 OPERATOR_CANDIDATES（上传端另有前缀识别），粘贴需断开
            words |= {"不等于", "并且", "或者", "为"}
            t = self._paste_anchor_c = {
                w[0]: sorted((x for x in words if x[0] == w[0]),
                             key=len, reverse=True)
                for w in words}
        return t

    def _paste_label_dict(self):
        """label 保护区词典（与 _segment_words 同源）+ 最长 label 长度。"""
        merged = self._label_set | set(self._pd_by_label)
        key = len(merged)
        if getattr(self, "_paste_ls_c", None) is None or \
                getattr(self, "_paste_ls_n", -1) != key:
            self._paste_ls_c = merged
            self._paste_ls_n = key
            self._paste_ls_max = max((len(s) for s in merged), default=0)
        return self._paste_ls_c, self._paste_ls_max

    def _paste_single_cut_ok(self, txt: str, i: int, anchors: dict,
                             ls, maxlen: int, prev_operand_end: bool) -> bool:
        """单字锚点（是/为/在…）粘连形态下是否可安全切分（2026-09-29）。

        主系统文案运算符前后无空格（"二级机构是动态域集…"），仅靠
        "下一字符为空格/引号"永远切不开。允许切分的结构信号：
        行尾/空白/引号/数字紧邻其后；锚点前恰为完整操作数结尾
        （label 或引号段被贪心消费完，"字段 是 …"形态）；锚点前回看
        最近非空白字符为右括号（方法调用实参组")"收尾后紧跟运算符，
        "…1() ( 1,2 ) 是非自然人…"，2026-10-08）；其后紧跟
        已知 label 开头（值/对象引用）；或其后是另一单字锚词且随即
        收尾（"商业险为是"）。label 内部"是/为"由 label 保护区先手
        整体消费，不会走到这里被误切。
        """
        n = len(txt)
        sep = " \t'\"“”‘’"
        nxt = txt[i + 1] if i + 1 < n else ""
        if not nxt or nxt in sep or nxt.isdigit():
            return True
        if prev_operand_end:
            return True
        # 回看最近非空白：右括号闭合一个实参/括号操作数 → 本锚点是运算符
        k = i - 1
        while k >= 0 and txt[k] in " \t":
            k -= 1
        if k >= 0 and txt[k] in ")]）":
            return True
        for j in range(min(n, i + 1 + maxlen), i + 1, -1):
            if txt[i + 1:j] in ls:
                return True
        if nxt in anchors:
            for a2 in anchors[nxt]:
                if len(a2) == 1 and txt.startswith(a2, i + 1):
                    p2 = i + 2
                    if p2 >= n or txt[p2] in sep or txt[p2].isdigit():
                        return True
        return False

    def _norm_if_line(self, line: str) -> str:
        """单条"如果"行：锚点处补空格，其余原文保留（含行内既有空白）。"""
        from desc_to_rulecontent.selector_values import normalize_selectors
        txt = normalize_selectors((line or "").strip())
        if not txt:
            return line
        ls, maxlen = self._paste_label_dict()
        anchors = self._paste_anchor_table()
        n = len(txt)
        segs: list[str] = []
        cur: list[str] = []
        i = 0
        prev_end = False   # 上一消费段是否为完整操作数结尾（label/引号段）
        paren_block = 0    # 已切开的全角实参块深度（指纹"）（"开、配对企业）
        while i < n:
            ch = txt[i]
            if ch in "‘“'\"":
                # 引号段整体保留：值文本里可能出现运算符字样，不切开
                close = {"‘": "’", "“": "”", "'": "'", '"': '"'}[ch]
                j = txt.find(close, i + 1)
                j = n if j < 0 else j + 1
                cur.append(txt[i:j])
                prev_end = True
                i = j
                continue
            bound = self._match_parameterized_path(txt, i, require_boundary=False)
            if bound:
                cur.append(txt[i:bound[0]])
                prev_end = True
                i = bound[0]
                continue
            hit = 0
            for j in range(min(n, i + maxlen), i, -1):
                if txt[i:j] in ls:
                    hit = j
                    break
            if hit:
                cur.append(txt[i:hit])
                prev_end = True
                i = hit
                continue
            if (ch == "（" and txt[i - 1:i] in (")", "）")
                    and paren_block == 0) or (ch == "）" and paren_block > 0):
                # 全角方法实参块粘连（2026-10-08）：主系统文案
                # "方法()（实参1，实参2）为是"里，实参括号与"()"、实参、
                # 后续运算符全部无空格粘连；annotate 只认**独立成词**的括号
                # （PAREN_CHARS），不切开则 方法头+实参块 整体落入"值"档，
                # 报"语法错误: 值不能裸用（前面缺少运算符）"整行标红。
                # 切开还原编辑器规范形态"方法() （ 实参 ） 为 是"（与 ADP
                # 样本、括号上下文栈口径一致）。触发条件刻意收窄为
                # **右括号紧接"（"**这一实参块指纹（paren_block 深度自管）：
                # 普通值里的全角括号（"深圳分公司（直销）"、区间/括注）
                # 前一字符不是括号，不拆、整串保留为值；含括号的 BOM
                # label 也已被上方贪心匹配整体消费。半角括号不动。
                s = "".join(cur)
                if s:
                    segs.append(s)
                    cur.clear()
                segs.append(ch)
                paren_block += 1 if ch == "（" else -1
                prev_end = False
                i += 1
                continue
            word = next((a for a in anchors.get(ch, ())
                         if txt.startswith(a, i) and
                         (len(a) > 1 or self._paste_single_cut_ok(
                             txt, i, anchors, ls, maxlen, prev_end))),
                        None)
            if word:
                s = "".join(cur)
                if s:
                    segs.append(s)
                    cur.clear()
                segs.append(word)
                prev_end = False
                i += len(word)
                continue
            cur.append(ch)
            prev_end = False
            i += 1
        s = "".join(cur)
        if s:
            segs.append(s)
        out = ""
        for sg in segs:
            if not sg.strip():
                out += sg          # 原空白段：不叠加，保留原排版
                continue
            if out and not out[-1].isspace() and not sg[0].isspace():
                out += " "
            out += sg
        return out

    def normalize_pasted_rule(self, text: str) -> str:
        """整段粘贴整理：按引导词分块——如果行补空格、那么/否则行删序号、
        预定义与其后续行原样；行内引导词（"那么：1. xxx"）拆成两行规范形态。
        无引导词的散段按行首特征兜底（指定→预定义块、并且/或者→如果行）。"""
        if not (text or "").strip():
            return text
        block = ""
        out: list[str] = []
        for ln in text.split("\n"):
            s = ln.strip()
            if not s:
                out.append(ln)
                continue
            m = self._PASTE_LEAD_RE.match(s)
            if m:
                lead, rest = m.group(1), m.group(2)
                block = {"预定义": "pd", "如果": "if",
                         "那么": "then", "否则": "else"}[lead]
                if not rest:
                    out.append(ln)
                else:
                    tail = (self._norm_if_line(rest) if block == "if"
                            else self._PASTE_ORD_RE.sub("", rest, count=1)
                            if block in ("then", "else") else rest)
                    out.append(lead + "：\n" + tail)
                continue
            if not block:
                if s.startswith("指定"):
                    block = "pd"
                elif self._PASTE_CONN_RE.match(s):
                    block = "if"
            if block == "if":
                out.append(self._norm_if_line(ln))
            elif block in ("then", "else"):
                out.append(self._PASTE_ORD_RE.sub("", ln, count=1))
            else:
                out.append(ln)
        return "\n".join(out)

    def annotate(self, text: str, predefines: str = "") -> list[dict]:
        """解析整段文本，按空格分隔逐词标注类型 + 校验值类型。

        返回 [{text, type, valid, error}]，type 取值：
            keyword / connector / operator / arith / field / object / number / boolean / value / unknown
        校验："字段 运算符 值"三段中，值的类型必须匹配字段类型：
            - 数字值 → 数值字段（double/int）
            - 布尔值（是/否/成立/不成立）→ boolean 字段
            - 日期值 → date/datetime 字段
            - 字符串值 → string 字段 / 枚举字段 / 值为对象字段
        predefines：编辑区预定义行（\n 分隔），解析成功后其词条参与标注/校验
        （"规则预定义变量的<别名>的<子字段>"识别为合法对象/字段）。
        """
        self._set_pd_lex(predefines)
        words = self._segment_words(text)
        from desc_to_rulecontent.selector_values import SELECTOR_OPS
        out: list[dict] = []
        cur_field: dict | None = None   # 当前正在等待运算符/值的字段
        awaiting_value = False          # 是否在值位置（字段+运算符之后）
        cur_op_code = ""                # 当前运算符 code（介于系值位需按起,止两段校验）
        # 区间句式状态机（运算符924「在 起 和 止 之间」三段式）：
        # 0=非区间；1=「在」后待起始值；2=起始值已给待「和」；3=「和」后待结束值；
        # 4=结束值已给待「之间」；行尾未闭合由下方兜底标红头词
        between_phase = 0
        ctx_stack: list[tuple] = []     # 括号块外层上下文栈：左括号压入，右括号恢复
        for i, w in enumerate(words):
            t = self._annotate_word(w, words, i)
            valid = True
            error = ""
            if w in SELECTOR_OPS:
                t = 'operator'
            elif i and words[i - 1] in SELECTOR_OPS:
                t = 'value'
                catalog = self._selector_catalog()
                field = catalog.fields.get((cur_field or {}).get('field_path'))
                try:
                    catalog.values(field, words[i - 1], w)
                except ValueError as exc:
                    valid, error = False, str(exc)
            # 更新上下文
            if t == "paren":
                # 括号块内是独立上下文（如 Helper 方法参数列表）：
                # 左括号保存并清空外层状态，右括号恢复——
                # 防止块内参数污染块外的 cur_field（如"方法() （ 参数 ） 为 是"）
                if w in self.LEFT_PARENS:
                    ctx_stack.append((cur_field, awaiting_value))
                    cur_field, awaiting_value = None, False
                elif ctx_stack:
                    cur_field, awaiting_value = ctx_stack.pop()
                # 多余右括号（栈空）由 _check_paren_balance 标红，此处忽略
            elif t in ("field", "object"):
                if awaiting_value:

                    # 处于值位置的字段引用（如"项目中类 是 是否4S店销售"），
                    # 校验其类型与左字段类型是否一致，且不把它当作新的左字段。
                    # 区间句式起/止值位同构（"在 字段X 和 字段Y 之间"）。
                    valid, error = self._check_value_type(cur_field, w, "field")
                    awaiting_value = False
                    if between_phase == 1:
                        between_phase = 2
                    elif between_phase == 3:
                        between_phase = 4
                else:
                    cur_field = self._find_path_item(w)
                    awaiting_value = False
                    # 新条款左字段开启：放弃未闭合的旧区间句式（悬空不再误延），
                    # 其结构缺失由上传转换端 ^在…和…之间$ 硬校验兜底
                    between_phase = 0
            elif t == "operator":
                awaiting_value = True
                cur_op_code = self._op_code_for_word(w)
                if w == BETWEEN_HEAD:
                    between_phase = 1
                else:
                    if between_phase in (1, 2, 3, 4) and valid:
                        valid, error = False, (
                            "语法错误: 区间句式『在 起 和 止 之间』尚未完整"
                            f"（缺『和』/结束值/『之间』），不能接『{w}』")
                    between_phase = 0
            elif t == "operator_mid":
                # 区间句式中段"和"：只允许出现在「在 起始值」之后
                if between_phase == 2:
                    between_phase = 3
                    awaiting_value = True
                    cur_op_code = "DATE_BETWEEN"
                else:
                    valid, error = False, (
                        "语法错误: 区间句式『在 起 和 止 之间』——『在』后缺少起始值"
                        if between_phase == 1 else
                        "语法错误: 『和』仅用于区间句式"
                        "『在 起 和 止 之间』的中段（起始值之后）")
                    awaiting_value = False
            elif t == "operator_tail" and w == BETWEEN_TAIL:
                # 区间句式尾词"之间"：须在结束值之后闭合
                if between_phase == 4:
                    between_phase = 0
                    awaiting_value = False
                    cur_field = None
                    cur_op_code = ""
                else:
                    _need = {1: "起始值、『和』与结束值", 2: "『和』与结束值",
                             3: "结束值"}.get(between_phase,
                                              "『在』、起始值、『和』与结束值")
                    valid, error = False, (
                        f"语法错误: 『之间』前缺少{_need}"
                        "（句式『在 起 和 止 之间』）")
                    between_phase = 0
                    awaiting_value = False
            elif t == "arith":
                # 算术运算符（+ - * / 及别名）：不改变值等待状态——操作数成对合法性
                # 由 _check_arith_rules 后置校验；值位字段后的算术（"是 B + C"）
                # 视同表达式延续，不再按"两个连续对象字段"误判。
                pass
            elif t in ("number", "boolean", "value", "date") and awaiting_value:
                # 校验值类型 vs 当前字段类型；区间句式「在 起 和 止 之间」的
                # 起/止各自为单值按字段类型校验；旧词形"介于 起,止"为单 token，
                # 须拆两段分别校验（整串按单值校验必误报类型不匹配）
                if between_phase == 1:
                    valid, error = self._check_value_type(cur_field, w, t)
                    between_phase = 2
                elif between_phase == 3:
                    valid, error = self._check_value_type(cur_field, w, t)
                    between_phase = 4
                elif cur_op_code.endswith("_BETWEEN"):
                    valid, error = self._check_between_value(cur_field, w)
                else:
                    if not (i and words[i - 1] in SELECTOR_OPS):
                        valid, error = self._check_value_type(cur_field, w, t)
                awaiting_value = False
            elif t in ("connector", "keyword"):
                cur_field = None
                awaiting_value = False
            out.append({"text": w, "type": t, "valid": valid, "error": error})
        # 区间句式行尾悬空兜底：「在 起 和 止 之间」写到一半未闭合（如"保费 在 5"），
        # 把最后一个仍合法的区间头词"在"标红，提示结构不完整
        if between_phase:
            for _j in range(len(out) - 1, -1, -1):
                _wd = out[_j]
                if (_wd["type"] == "operator" and _wd["text"] == BETWEEN_HEAD
                        and _wd["valid"]):
                    _wd["valid"] = False
                    _wd["error"] = ("语法错误: 区间句式『在 起 和 止 之间』不完整"
                                    "（缺『和』/结束值/『之间』）")
                    break
        # 相邻词语法校验（连接词/对象字段/数字/运算符相邻规则）
        self._check_adjacent_rules(out)
        # 算术运算符（+ - * / 及别名）操作数配对与类型校验
        self._check_arith_rules(out)
        # 括号配对校验
        self._check_paren_balance(out)
        return out

    def _check_adjacent_rules(self, words: list[dict]) -> None:
        """相邻词语法校验，违反则把违规词标红（valid=False, error="语法错误:..."）。

        规则：
        1. 不能连续两个连接词
        2. 不能连续两个对象字段/字段（除非前一词以逗号","结尾——如"保单的个团标志,"）
        3. 不能连续两个数字
        4. 连接词不能与运算符相邻（运算符 连接词 / 连接词 运算符；一元空运算符除外）
        5. 不能连续两个运算符（如"大于 等于"）
        6. 字段后不能直接接连接词（应接运算符/一元运算符，如"保单 并且"非法）
        7. 值不能裸用：字符串/数字前必须是运算符（豁免：左括号后首个参数、逗号后参数）
        8. 二元运算符不能悬空结尾（运算符后必须跟值，如"保费合计 大于"非法）
        11. 字段/对象不能被两个运算符夹在中间（"运算符 字段 运算符"非法）
        """
        VAL_TYPES = {"number", "boolean", "value", "date"}
        n = len(words)
        # 首词即为孤立尾部运算符（如句首就是"中的一个"）
        if n > 0 and words[0]["type"] == "operator_tail":
            words[0]["valid"] = False
            _heads = '/'.join(sorted(MULTI_OP_TAILS.get(words[0]['text'], ())))
            words[0]["error"] = f"语法错误: 『{words[0]['text']}』缺少前置『{_heads}』和值"
        # 首词即为裸值（字符串/数字不能作为句首，前面缺少运算符）
        if n > 0 and words[0]["type"] in ("value", "number"):
            words[0]["valid"] = False
            words[0]["error"] = "语法错误: 值不能裸用（前面缺少运算符）"
        for i in range(1, n):
            prev = words[i - 1]
            cur = words[i]
            pt, ct = prev["type"], cur["type"]
            ptext, ctext = prev["text"], cur["text"]
            # prev 的上一个词（用于判断 prev 是否处于"值位置"，即其前是运算符）
            pptext = words[i - 2]["text"] if i >= 2 else ""
            # 1. 连续连接词
            if pt == "connector" and ct == "connector":
                cur["valid"] = False
                cur["error"] = "语法错误: 不能连续两个连接词"
            # 2. 连续对象字段/字段（除非前词以逗号结尾）
            elif pt in ("field", "object") and ct in ("field", "object"):
                if not ptext.rstrip().endswith(","):
                    cur["valid"] = False
                    cur["error"] = "语法错误: 不能连续两个对象字段（除非以,间隔）"
            # 3. 连续数字
            elif pt == "number" and ct == "number":
                cur["valid"] = False
                cur["error"] = "语法错误: 不能连续两个数字"
            # 3b. 连续布尔值（与连续数字同构；如"为 是 是"）
            elif pt == "boolean" and ct == "boolean":
                cur["valid"] = False
                cur["error"] = "语法错误: 不能连续两个布尔值"
            # 4. 连接词与运算符相邻（二元运算符后须接值，不能再接连接词）
            #    但一元空运算符（不为空/为空）本身不带值，其后/其前接连接词均合法，放行。
            elif (pt == "connector" and ct == "operator") or (pt == "operator" and ct == "connector"):
                if ctext in UNARY_OPERATORS or ptext in UNARY_OPERATORS:
                    continue
                cur["valid"] = False
                cur["error"] = "语法错误: 连接词不能与运算符相连"
            # 5. 连续两个运算符（如"大于 等于"）
            elif pt == "operator" and ct == "operator":
                cur["valid"] = False
                cur["error"] = "语法错误: 不能连续两个运算符"
            # 6. 字段后直接接连接词（字段后应接运算符，不能直接接连接词）
            #    豁免：当该字段处于"值位置"（其前一个词是运算符，如"是 结果的保费计算信息"），
            #    它是运算符后的值引用（字段兼作值），其后接"并且/或者"合法，不报错；
            #    其前是算术运算符（算术链尾操作数，如"大于 保额 加 100 并且"）同理豁免。
            elif pt in ("field", "object") and ct == "connector":
                if self._is_operator(pptext) or self._is_arith(pptext):
                    continue
                cur["valid"] = False
                cur["error"] = "语法错误: 字段后缺少运算符（不能直接接连接词）"
            # 7. 值不能裸用：字符串/数字的前一个词必须是运算符。
            #    豁免（Helper 方法参数列表，如"（ A , B , C ）"）：
            #    - 左括号后的第一个参数
            #    - 逗号（独立成词或附着在词尾）之后的后续参数
            elif ct in ("value", "number"):
                if pt in ("operator", "operator_mid"):
                    # operator_mid："在 起 和 止"的结束值前是区间中段词"和"，合法
                    pass
                elif pt == "arith":
                    # 算术运算符后紧跟数字/字符串 → 算术表达式的操作数（"保额 加 100"）
                    pass
                elif pt == "paren" and ptext in self.LEFT_PARENS:
                    pass
                elif pt == "punct" or ptext.endswith(","):
                    pass
                else:
                    cur["valid"] = False
                    cur["error"] = "语法错误: 值不能裸用（前面缺少运算符）"
            # 11. 字段/对象被两个运算符夹在中间（"运算符 字段 运算符"非法，
            #     如"项目中类 是 意外医疗保险 是 apPremiumA1"，值以字段身份出现在两运算符间）。
            #     cur(ct) 是被夹的词，prev(pt) 是前一运算符；后一词若仍是运算符则非法。
            if ct in ("field", "object") and self._is_operator(ptext):
                nxt = words[i + 1] if i + 1 < n else None
                if nxt and nxt["type"] == "operator":
                    cur["valid"] = False
                    cur["error"] = "语法错误: 字段不能被两个运算符夹在中间（应为值，请检查是否漏写连接词或括号）"
            # 8. 二元运算符悬空结尾（运算符后必须跟值）
            if i == n - 1 and ct == "operator" and ctext not in UNARY_OPERATORS:
                cur["valid"] = False
                cur["error"] = "语法错误: 运算符缺少后续值"
            # 9. 多词（头...尾）运算符：头部与尾部直接相连（中间缺值）
            #    如"属于 中的一个" → 标红尾部"中的一个"
            elif pt == "operator" and ct == "operator_tail":
                if ptext in MULTI_OP_HEADS and ctext in MULTI_OP_TAILS and ptext in MULTI_OP_TAILS[ctext]:
                    cur["valid"] = False
                    cur["error"] = f"语法错误: 『{ptext}』与『{ctext}』之间缺少值"
            # 10. 孤立尾部运算符（前置不是合法的值/字段，缺头部与值）
            #     如"中的一个"单独出现、或前词非值（"并且 中的一个"）
            elif ct == "operator_tail":
                if pt not in ("value", "field", "object", "number", "boolean", "date"):
                    cur["valid"] = False
                    _heads = '/'.join(sorted(MULTI_OP_TAILS.get(ctext, ())))
                    cur["error"] = f"语法错误: 『{ctext}』缺少前置『{_heads}』和值"
            # 12. 值/区间尾词收尾后紧跟字段/对象 = 新条件缺连接词（与生成端口径一致：
            #     "字段 运算符 值"后只能接连接词/右括号/行尾，直接出现字段说明
            #     两个完整条件连写，生成器会静默吞掉后一条，必须在此拦截）。
            #     operator_tail（结尾/开头/中的一个/之间）同样完成条件闭合
            #     （"以 X 结尾 字段…"缺连接词与"值 字段…"同性质）。
            #     豁免：前词以逗号/顿号收尾视为枚举续写（"属于 A, B, C"）。
            if (pt in VAL_TYPES or pt == "operator_tail") and ct in ("field", "object") \
                    and not ptext.rstrip().endswith((",", "，", "、")):
                if cur.get("valid") is not False:
                    cur["valid"] = False
                    cur["error"] = "语法错误: 两个完整条件之间缺少连接词（并且/或者）"

    def _check_arith_rules(self, words: list[dict]) -> None:
        """算术运算符（加/减/乘/除 及独立成词的 + - * / 全角兼容形）配对与操作数类型校验。

        算术链文法：操作数 (算术运算符 操作数)*，操作数=数值字段/预定义别名/数字/
        括号表达式块（2026-09-30：「（ A + B ） * C」的括号块整体视作一个操作数，
        与生成端 desc_to_rulecontent 括号块解析口径一致）。
        - 句首/句尾即为算术运算符 → 缺操作数；
        - 算术运算符相邻（两侧都缺操作数）→ 报错；
        - 运算符/连接词/引导词等紧邻算术运算符的操作数位 → 缺操作数；
        - 括号块为空（）→ 缺操作数；
        - 操作数为明确非数值类型（string/date/boolean/list 字段或对象节点）
          → 仅支持数值类型操作数；类型未知（动态域/预定义未声明）宽容放行。
        报错统一落在算术运算符词上（预览标红 + 上传硬校验拦截）。
        """
        n = len(words)
        for i, wd in enumerate(words):
            if wd["type"] != "arith":
                continue
            err = ""
            for idx, name in ((i - 1, "左"), (i + 1, "右")):
                ok, msg = self._arith_operand_err(wd, words, idx, name)
                if not ok:
                    err = msg
                    break
            if err:
                wd["valid"] = False
                wd["error"] = err

    def _resolve_arith_operand(self, words: list[dict], idx: int,
                               name: str) -> tuple[int | None, str]:
        """把算术运算符某一侧紧邻的词归位到真正的操作数 token（2026-09-30）。

        紧邻词是括号块端点时整块跳过：左操作数="…）"回溯到配对的"（"、
        右操作数="（…"前进到配对的"）"，返回块内首/末实词下标；嵌套
        （（A）+B）逐层剥壳。块内无实词（空括号）→ (None, "empty")；
        括号未配对（回溯/前进找不到闭合端）→ (None, "unmatched")，
        该情形交给 _check_paren_balance 标红括号本身，算术侧不再叠加报错。
        其余情况原样返回该下标。
        """
        n = len(words)
        if not 0 <= idx < n:
            return idx, ""
        w = words[idx]
        if w["type"] != "paren":
            return idx, ""
        # 孤立括号端点（自身无配对）：交给 _check_paren_balance 标红，算术侧不叠加。
        # 右括号→向前深度扫描找开括号；左括号→向后深度扫描找闭括号。
        if w["text"] in self.RIGHT_PARENS:
            depth, j = 0, idx
            while j >= 0:
                if words[j]["type"] == "paren":
                    if words[j]["text"] in self.RIGHT_PARENS:
                        depth += 1
                    else:
                        depth -= 1
                        if depth == 0:
                            break
                j -= 1
            if j < 0:
                return None, "unmatched"
        else:
            depth, j = 0, idx
            while j < n:
                if words[j]["type"] == "paren":
                    if words[j]["text"] in self.LEFT_PARENS:
                        depth += 1
                    else:
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            if j >= n:
                return None, "unmatched"
        if name == "左" and w["text"] in self.RIGHT_PARENS:
            depth, j = 0, idx
            while j >= 0:
                if words[j]["type"] == "paren" and words[j]["text"] in self.RIGHT_PARENS:
                    depth += 1
                elif words[j]["type"] == "paren" and words[j]["text"] in self.LEFT_PARENS:
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            if j < 0:
                return None, "unmatched"
            k = j + 1
            while k < idx and words[k]["type"] in ("punct", "keyword"):
                k += 1
            if k >= idx:
                return None, "empty"
            return self._resolve_arith_operand(words, k, "左")
        if name == "右" and w["text"] in self.LEFT_PARENS:
            depth, j = 0, idx
            while j < n:
                if words[j]["type"] == "paren" and words[j]["text"] in self.LEFT_PARENS:
                    depth += 1
                elif words[j]["type"] == "paren" and words[j]["text"] in self.RIGHT_PARENS:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                return None, "unmatched"
            k = j - 1
            while k > idx and words[k]["type"] in ("punct", "keyword"):
                k -= 1
            if k <= idx:
                return None, "empty"
            return self._resolve_arith_operand(words, k, "右")
        return idx, ""

    def _arith_operand_err(self, op_word: dict, words: list[dict], idx: int,
                           name: str) -> tuple[bool, str]:
        """算术运算符某一侧操作数合法性（先做括号块归位）：返回 (ok, 错误信息)。"""
        op = op_word["text"]
        idx, skip = self._resolve_arith_operand(words, idx, name)
        if skip == "unmatched":
            return True, ""      # 括号不配对由 _check_paren_balance 负责标红
        if skip == "empty":
            return False, (f"语法错误: 算术运算符『{op}』的{name}操作数是空括号"
                           "（（ ）内缺少表达式）")
        side = words[idx] if 0 <= idx < len(words) else None
        t, txt = side["type"], side["text"]
        if t == "number":
            return True, ""
        if t == "field":
            item = self._find_path_item(txt)
            if item is None:
                return True, ""   # 宽容：分词歧义查不到条目时不误杀
            ty = (item.get("type") or "").lower()
            if not ty:
                return True, ""   # 类型未知（动态域/预定义未声明）宽容放行
            if self._pd_type_family(ty) == "numeric":
                return True, ""
            return False, ("语法错误: 算术运算仅支持数值类型操作数，"
                           f"{name}操作数『{txt}』类型『{ty}』")
        if t == "arith":
            return False, f"语法错误: 不能连续两个算术运算符（『{op}』与『{txt}』之间缺操作数）"
        return False, (f"语法错误: 算术运算符『{op}』缺少{name}操作数"
                       f"（两侧须为数值字段或数字，实际是『{txt}』）")

    def _check_paren_balance(self, words: list[dict]) -> None:
        """括号配对校验（半角/全角，仅独立成词的括号参与）。

        规则：
        - 计数：遇左括号 +1，遇右括号 -1；计数 < 0（右括号多于左括号）→ 标红该右括号。
        - 遍历结束后计数 > 0（左括号未闭合）→ 标红每一个未闭合的左括号。
        """
        depth = 0
        unclosed_left: list[int] = []   # 未闭合左括号的词索引
        for idx, w in enumerate(words):
            if w["type"] != "paren":
                continue
            if w["text"] in self.LEFT_PARENS:
                depth += 1
                unclosed_left.append(idx)
            else:  # 右括号
                if depth <= 0:
                    w["valid"] = False
                    w["error"] = "语法错误: 右括号没有匹配的左括号"
                else:
                    depth -= 1
                    # 弹出一个与之配对的左括号记录
                    if unclosed_left:
                        unclosed_left.pop()
        # 遍历结束仍有未闭合的左括号
        if depth > 0:
            for idx in unclosed_left:
                words[idx]["valid"] = False
                words[idx]["error"] = "语法错误: 左括号没有匹配的右括号"

    def _check_value_type(self, field_item: dict | None, value: str, value_type: str) -> tuple[bool, str]:
        """校验值类型是否匹配字段类型。返回 (valid, error)。"""
        if field_item is None:
            return True, ""
        # 对象节点（容器，无具体类型）不作值类型强校验，值可为任意文本/枚举
        if field_item.get("kind") == "object" or not (field_item.get("type") or ""):
            return True, ""
        ftype = (field_item.get("type") or "").lower()
        import re
        # 值可能是对象字段（如动态域集枚举值路径）→ 看它对应字段类型
        if value_type == "value":
            v_item = self._find_path_item(value)
            if v_item:
                value_type = "field"
        # boolean 值匹配 boolean 字段
        if value_type == "boolean":
            if ftype == "boolean":
                return True, ""
            return False, f"布尔值『{value}』与字段类型『{ftype}』不匹配，应为布尔"
        # 数字值匹配数值字段，也匹配 string 字段（数值型 string，如保额/金额以字符串存储）
        if value_type == "number":
            if ftype in ("double", "int", "integer", "long", "big_decimal", "string", "str"):
                return True, ""
            return False, f"数字『{value}』与字段类型『{ftype}』不匹配，应为数值"
        # 日期值匹配日期字段
        if value_type == "value" and re.match(self.DATE_RE, value):
            if ftype in ("date", "datetime", "time"):
                return True, ""
            return False, f"日期『{value}』与字段类型『{ftype}』不匹配，应为日期"
        # 字符串值 / 对象字段值：匹配 string 字段、枚举字段、或任何类型（对象字段作为值）
        if value_type in ("value", "field"):
            # 值是对象字段路径（值引用了另一字段）：先比较左右字段类型，
            # 类型不一致（如 string 字段 = boolean 字段）应报错。
            if value_type == "field":
                v_item = self._find_path_item(value)
                vtype = (v_item.get("type") or "").lower() if v_item else ""
                # 右侧是对象容器（kind==object，type 可能为空）→ 类型宽松放行
                if v_item and v_item.get("kind") == "object":
                    return True, ""
                # 同类型放行
                if vtype == ftype:
                    return True, ""
                # 数值左字段：字段比字段须同为数值族——镜像候选端口径
                # （_value_candidates_for 数值字段值位只弹 _numeric_hits 候选，
                # string/date/list 型字段本就不出现在候选里，手输也应拦截）。
                if self._pd_type_family(ftype) == "numeric" and vtype:
                    if self._pd_type_family(vtype) != "numeric":
                        return False, (f"值字段『{value}』类型『{vtype}』与数值字段"
                                       f"『{field_item.get('node_label', '')}』不匹配"
                                       f"（数值比较须同为数值类型）")
                    return True, ""
                # 数值型与数值型 string 互容（string 存金额、左侧非数值族的场景）
                if vtype in ("double", "int", "integer", "long", "big_decimal") and ftype in ("double", "int", "integer", "long", "big_decimal", "string", "str"):
                    return True, ""
                return False, f"值字段『{value}』类型『{vtype or '未知'}』与字段『{field_item.get('node_label','')}』类型『{ftype}』不匹配"
            # 普通字符串值
            if ftype in ("string", "str"):
                return True, ""
            # 枚举字段的值可以是任意（枚举来自动态域集）
            if field_item.get("is_enum"):
                return True, ""
            return False, f"字符串『{value}』与字段类型『{ftype}』不匹配"
        return True, ""

    @staticmethod
    def _op_code_for_word(w: str) -> str:
        """运算符词 → code（兼容已填入的去'...'形态）。

        区间系特判：新词形头词"在"与旧词形"介于"统一返回 DATE_BETWEEN
        触发 BETWEEN 值校验分支（实际 NUM/DATE/TIME 由上传端按字段类型选）。
        """
        if w in (BETWEEN_HEAD, BETWEEN_LEGACY):
            return "DATE_BETWEEN"
        for op in OPERATOR_CANDIDATES:
            if w == op["label"] or w == op["label"].replace("...", ""):
                return op["code"]
        return ""

    def _check_between_value(self, field_item: dict | None,
                             value: str) -> tuple[bool, str]:
        """介于系（*_BETWEEN）值校验：'起,止'（逗号/顿号/分号/'和'分隔）
        拆两段分别按字段类型校验——整体串直接按单值校验必误报（如 double
        字段填 '5,10' 被当字符串判'与字段类型不匹配'）。"""
        parts = [p.strip() for p in re.split(r"[，,、;；]|和", value) if p.strip()]
        if len(parts) != 2:
            return False, ("区间值『" + value + "』格式不正确，"
                           "『介于』须填 起,止 两个值（如 5,10）")
        for p in parts:
            pt = self._annotate_word(p, [p], 0)
            if pt not in ("number", "date", "value"):
                return False, f"区间值『{p}』不合法，起止须为数值/日期"
            ok, err = self._check_value_type(field_item, p, pt)
            if not ok:
                return False, err
        return True, ""

    # 括号字符（半角 + 全角）。仅当整词独立成 token（左右空格隔离）时才识别为括号，
    # 字段内部的括号（如"数量（间）"）不在此列。
    PAREN_CHARS: set[str] = {"(", ")", "（", "）"}
    LEFT_PARENS: set[str] = {"(", "（"}
    RIGHT_PARENS: set[str] = {")", "）"}

    def _annotate_word(self, w: str, words: list[str], i: int) -> str:
        # 括号（仅独立成词，左右空格隔离时）
        if w in self.PAREN_CHARS:
            return "paren"
        # 独立成词的逗号（半角/全角）：参数/列表分隔符，不参与"值不能裸用"校验
        if w in (",", "，"):
            return "punct"
        # 连接词
        if w in ("并且", "或者"):
            return "connector"
        # 引导词（如果）
        for kw in KEYWORD_CANDIDATES:
            if w == kw["label"]:
                return "keyword"
        # 布尔运算符前缀"为"：前一词是 boolean 字段 → operator
        # 若前一词是右括号，先回溯跳过配对的参数括号块（如"方法() （ 参数 ） 为"），
        # 定位括号块之前的词——参数列表不改变"为/是"所依附的 boolean 字段上下文
        j = i - 1
        if j >= 0 and words[j] in self.RIGHT_PARENS:
            depth = 0
            while j >= 0:
                if words[j] in self.RIGHT_PARENS:
                    depth += 1
                elif words[j] in self.LEFT_PARENS:
                    depth -= 1
                    if depth == 0:
                        j -= 1
                        break
                j -= 1
        prev = words[j] if j >= 0 else ""
        prev_item = self._find_path_item(prev) if prev else None
        is_after_boolean_field = bool(prev_item and (prev_item.get("type") or "").lower() == "boolean")
        # "为"是判断词：前面是任意 BOM 字段/方法（不限 boolean）→ 运算符前缀。
        # 类型是否匹配由值校验负责（如 double 方法后"为 是"报"布尔值与字段类型不匹配"，
        # 而非把"为"当值报"值不能裸用"）
        if w == "为" and prev_item:
            return "operator"
        # 布尔值（是/否/成立/不成立）：
        # 前一词是"为" / 前一词是运算符（值位置） / 前一词是 boolean 字段 → 布尔值
        if w in ("是", "否", "成立", "不成立"):
            # 前一词是"为"（布尔运算符前缀）→ 布尔值
            if prev == "为":
                return "boolean"
            # 前一词是运算符（值位置，如"大于 是"）→ 布尔值
            if self._is_operator(prev):
                return "boolean"
            # 前一词直接是 boolean 字段（"为"被省略）→ 布尔值
            if is_after_boolean_field:
                return "boolean"
            # 否则"是"作为 string 运算符
            if w == "是":
                return "operator"
            return "boolean"
        # 区间句式（运算符924「在 起 和 止 之间」）独立成词的头/中/尾：
        # "在" ∈ MULTI_OP_HEADS、"之间" ∈ MULTI_OP_TAILS 由下方自动识别；
        # "和"为中段（新词类 operator_mid），"介于"为旧词形（介于 起,止）兼容识别
        if w == BETWEEN_MID:
            return "operator_mid"
        if w == BETWEEN_LEGACY:
            return "operator"
        # 多词（头...尾）运算符的头部与尾部
        # 属于 / 以 → 运算符头；中的一个 / 开头 / 结尾 → 运算符尾（独立类型，便于配对校验）
        if w in MULTI_OP_HEADS:
            return "operator"
        if w in MULTI_OP_TAILS:
            return "operator_tail"
        # 内容长度修饰符：独立类型 len_func（既非运算符也非值），
        # 使「字段 的内容长度 比较符 数字」通过相邻词校验且不被误标红。
        if w in LEN_MODIFIERS:
            return "len_func"
        # BOM 对象/字段（精确命中）
        # 处于"值位置"（前一个词是运算符，如"是 结果的保费计算信息"）时也标为 field/object，
        # 保留其字段身份与着色；是否合法的判断交由 _check_adjacent_rules 处理
        # （如"运算符 字段 运算符"由规则 11 判定其是否被两运算符夹在中间）。
        item = self._find_path_item(w)
        if item:
            return "field" if item.get("kind") == "field" else "object"
        # 算术运算符：独立成词的 + - * /（及 加减乘除/全角兼容别名）→ arith
        # （词形口径与生成端 ARITH_OPS 一致）。放在 BOM 精确匹配之后——真实同名
        # 字段优先；贪心分词保证"驾乘险/保额/限额(元)"这类 label 内部的字或符号
        # 不会被切成运算符，独立成词即用户有意写运算。
        if w in ARITH_WORDS:
            return "arith"
        # 日期值（含数字，优先匹配日期格式再数字）
        import re
        if re.match(self.DATE_RE, w):
            return "value"
        # 数字
        if re.match(self.NUMBER_RE, w):
            return "number"
        # 运算符（含已填入的去"..."运算符）
        for op in OPERATOR_CANDIDATES:
            if w == op["label"] or w == op["label"].replace("...", ""):
                return "operator"
        # 值 / 未知
        return "value" if w else "unknown"
