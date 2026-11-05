# -*- coding: utf-8 -*-
"""本地规则求值器：离线验证 input 报文（填值后）能否命中规则的 if 条件树。

不依赖 ADP 接口。输入：
  - ruleContent（if 条件树 JSON，编辑器 AST：LOGICAL/COMPARISON/EXPRESSION/
    FIELD/METHOD/CUSTOM/NESTING/TREE_EX/CALCULATE）
  - input 报文（rule_test/input_generator.py 生成骨架 + 手工填值）

求值语义（对齐引擎）：
  1. LOGICAL(并且/或者) + COMPARISON(STR_EQ/LE/GE/DATE_GT/...) 递归求布尔值；
  2. EXPRESSION 全运算符（2026-09-07 补齐）：STR_BELONG_TO / STR_NOT_BELONG_TO /
     STR_BELONG_TO_MUL / STR_NOT_BELONG_TO_MUL（属于…中的一个，param2 为逗号
     分隔字面量串或字段节点列表）、BOOLEAN_BELONG_TO（为是/为否）、
     STR_INCLUDE / STR_NOT_INCLUDE（包含）、STR_EMPTY / STR_NOT_EMPTY（为空）、
     DATE_BETWEEN（介于）、STR_START_WITH / STR_END_WITH、SIZE_OF_LIST（长度，
     出现在 COMPARISON 左操作数）；
  3. 算术树：NESTING(nestingTree 二叉 CALCULATE) / TREE_EX(arr 交替) /
     CALCULATE，operator ∈ ADD/SUB/MULTIPLY/DIVIDE；
  4. 挂载点归一化：规则里 `规则预定义变量.<别名>.<字段>` 引用，按优先级定位绑定对象：
     a) input 原路径（规则预定义变量.<别名> 下）直取；
     b) predefine 文本经 input_generator.BomIndex 展开成真实 BOM 路径后取值（可选依赖）；
     c) 结构搜索：在报文里找"含全部叶子字段名的 dict / dict 列表"（如
        appbase.productLifeInfo06.coverage062008List）；
  5. 同元素绑定：绑定到列表的别名（如"当前险别"），对列表逐元素绑定求值，
     整树任一绑定组合为真即命中 —— 防止"跨元素拼条件"误判；
  6. 字段比字段（如 保单的二级机构 是 动态域集槽位）：两侧各自取值比较；
  7. 诊断三分：值命中 / UNFILLED(叶子 null) / MISSING(路径不存在)，可区分
     "值填错"与"挂载点错"。

输出：整体 pass + 逐条件缩进真值树。

用法:
    python rule_test/rule_evaluator.py --rule-content <file.json|'{...}'> \
        --input <file.json|'{...}'> [--predefine "指定'当前险别'…;"]

    from rule_test.rule_evaluator import parse, Evaluator, render_report
    ok, report = Evaluator(parse(rc), input).run()
"""
import argparse
import itertools
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PD_PREFIX = "规则预定义变量."

# EXPRESSION 运算符 → 中文标签（真值树/前端展示）
EXPR_LABELS = {
    "STR_BELONG_TO": "属于", "STR_NOT_BELONG_TO": "不属于",
    "STR_BELONG_TO_MUL": "属于(多)", "STR_NOT_BELONG_TO_MUL": "不属于(多)",
    "BOOLEAN_BELONG_TO": "为是/为否", "STR_INCLUDE": "包含",
    "STR_NOT_INCLUDE": "不包含", "STR_EMPTY": "为空", "STR_NOT_EMPTY": "不为空",
    "DATE_BETWEEN": "在…和…之间", "STR_START_WITH": "以…开头", "STR_END_WITH": "以…结尾",
    "SIZE_OF_LIST": "长度", "STR_LENGTH": "长度",
    # ---- 运算符924 档1 ----
    "STR_NOT_START_WITH": "不以…开头", "STR_NOT_END_WITH": "不以…结尾",
    "NUM_BETWEEN": "在…和…之间", "TIME_BETWEEN": "在…和…之间",
    "COLLECTION_IS_EMPTY": "为空", "COLLECTION_NOT_EMPTY": "不为空",
    "COLLECTION_ANY_STR": "包含", "COLLECTION_NOT_IN_STR": "不包含",
    "COLLECTION_ANY_BELONG_TO": "部分属于", "COLLECTION_ALL_BELONG_TO": "全部属于",
    "COLLECTION_ANY_CONTAINS": "部分包含", "COLLECTION_ALL_CONTAINS": "全部包含",
    "COLLECTION_NOT_IN_CONTAINS": "不包含集合",
}
# 布尔语义的 EXPRESSION（COMPARISON 的操作数位置出现时按布尔值参与比较）
EXPR_BOOLEAN = {"STR_BELONG_TO", "STR_NOT_BELONG_TO", "STR_BELONG_TO_MUL",
                "STR_NOT_BELONG_TO_MUL", "BOOLEAN_BELONG_TO", "STR_INCLUDE",
                "STR_NOT_INCLUDE", "STR_EMPTY", "STR_NOT_EMPTY",
                "DATE_BETWEEN", "STR_START_WITH", "STR_END_WITH",
                "STR_NOT_START_WITH", "STR_NOT_END_WITH",
                "NUM_BETWEEN", "TIME_BETWEEN",
                "COLLECTION_IS_EMPTY", "COLLECTION_NOT_EMPTY",
                "COLLECTION_ANY_STR", "COLLECTION_NOT_IN_STR",
                "COLLECTION_ANY_BELONG_TO", "COLLECTION_ALL_BELONG_TO",
                "COLLECTION_ANY_CONTAINS", "COLLECTION_ALL_CONTAINS",
                "COLLECTION_NOT_IN_CONTAINS"}


def _flat_set(v):
    """集合运算两侧统一扁平化为字符串集合：
    Literal 取 value、list/tuple 逐元素递归（dict 取 label/value/key）、
    逗号/顿号/分号串拆分。"""
    if v is None:
        return set()
    if isinstance(v, Literal):
        return _flat_set(v.value)
    if isinstance(v, str):
        return {x.strip() for x in _SEP_RE.split(v) if x.strip()}
    if isinstance(v, (list, tuple)):
        out = set()
        for x in v:
            out |= _flat_set(x)
        return out
    if isinstance(v, dict):
        return {str(v[k]) for k in ("label", "value", "key")
                if v.get(k) not in (None, "")}
    return {str(v)}


# ---------------------------------------------------------------- 表达式树

class Literal:
    """CUSTOM 常量操作数（paramValue + returnType）。"""

    def __init__(self, value, type_):
        self.raw = value
        self.type = type_ or ""
        if (self.type or "").lower() in ("double", "int", "integer", "number", "float"):
            try:
                self.value = float(value)
            except (TypeError, ValueError):
                self.value = value
        else:
            self.value = value

    def describe(self):
        return repr(self.value)


class FieldRef:
    """FIELD 操作数：fieldPath + 别名组信息。

    group：`规则预定义变量.<别名>.…` 引用时为别名，否则 None（业务对象/动态域集字段）。
    rest：剥离前缀后的剩余路径段（组字段=别名后字段段；普通字段=fieldPath 全段）。
    """

    def __init__(self, path, label, type_, group, rest):
        self.path = path
        self.label = label or path
        self.type = type_ or ""
        self.group = group
        self.rest = rest

    def describe(self):
        return self.label


class Cmp:
    def __init__(self, code, label, left, right):
        self.code = code or ""
        self.label = label or code or ""
        self.left = left
        self.right = right


class Expr:
    """EXPRESSION 节点：expressionId 分发的内建表达式（属于/包含/为空/介于…）。

    params：{paramId: 操作数}，值类型 Literal/FieldRef/Expr/Arith/list
    （list 为字段值列表或含 ',' 分隔项，见生成器 _belong_values 口径）。
    """

    def __init__(self, eid, params):
        self.eid = (eid or "").upper()
        self.params = params

    def describe(self):
        return EXPR_LABELS.get(self.eid, self.eid or "表达式")


class Arith:
    """算术节点：NESTING(nestingTree 二叉) / TREE_EX(arr 交替) / CALCULATE。"""

    def __init__(self, kind, tree):
        self.kind = kind
        self.tree = tree

    def describe(self):
        return "算术表达式"


class Logic:
    def __init__(self, op, label, left, right):
        self.op = op  # "AND" / "OR"
        self.label = label or op
        self.left = left
        self.right = right


class BoolSub:
    """操作数位置的布尔子树："（条件）等于 成立/不成立"转换器产物——
    外层 COMPARISON(EQ) 的 left 又是一棵 COMPARISON/LOGICAL。
    子树先求布尔值，再作为操作数参与外层比较。"""

    def __init__(self, expr):
        self.expr = expr

    def describe(self):
        return "（%s）" % _sub_text(self.expr)


def _sub_text(node):
    """子表达式树 → 可读文本（describe 用）。"""
    if isinstance(node, (Cmp, Logic)):
        return "%s %s %s" % (_sub_text(node.left), node.label,
                             _sub_text(node.right))
    if isinstance(node, BoolSub):
        return node.describe()
    if hasattr(node, "describe"):
        return node.describe()
    return str(node)


# ---------------------------------------------------------------- 解析

def _operand(node):
    """操作数节点 dict → Literal / FieldRef / Expr / Arith（裸值容错转 Literal）。"""
    if not isinstance(node, dict):
        return Literal(node, "string")
    nt = node.get("nodeType")
    if nt == "CUSTOM":
        pv = node.get("paramValue")
        if isinstance(pv, list):
            # selector-multiple：[{key,label,value},…] → 取 key 值列表参与 IN 比较
            return Literal([x.get("key") if isinstance(x, dict) else x
                            for x in pv],
                           node.get("returnType") or node.get("type"))
        if isinstance(pv, dict):
            # selector {key,label,value}：key 为实际参与比较的码值（缺省用 label）
            return Literal(pv.get("key") if pv.get("key") is not None
                           else pv.get("label"),
                           node.get("returnType") or node.get("type"))
        return Literal(pv, node.get("returnType") or node.get("type"))
    if "paramValue" not in node and not (node.get("fieldPath")
                                         or node.get("methodPath")) \
            and ("key" in node or "value" in node):
        # 裸 selector dict（BOOLEAN_BELONG_TO 的 param2 {label,value,key}）→ 值常量
        return Literal(node.get("value") if node.get("value") is not None
                       else node.get("key"),
                       node.get("returnType") or node.get("type"))
    if nt in ("COMPARISON", "LOGICAL"):
        # 操作数位置的布尔子树："（…）等于 成立/不成立"嵌套产物——
        # 子树整体先求布尔值，再作为操作数参与外层比较
        return BoolSub(parse(node))
    if nt == "EXPRESSION":
        pv = node.get("paramValue") or {}
        params = {k: _operand(v) if isinstance(v, dict) else _param_operand(v)
                  for k, v in pv.items()}
        return Expr(node.get("expressionId"), params)
    if nt in ("NESTING", "TREE_EX", "CALCULATE"):
        return Arith(nt, node)
    # FIELD / METHOD（自定义方法：本地不执行方法，把 methodPath 当字段路径，
    # 方法结果值由用户在填空表单里给出）
    fp = (node.get("fieldPath") or node.get("field_path")
          or node.get("methodPath") or "")
    label = node.get("label") or node.get("methodName")
    if fp.startswith(PD_PREFIX):
        # 规则预定义变量.<别名>[.<叶子>…]；两段 = 别名即叶子（指定'A费用上限'为…）
        parts = fp[len(PD_PREFIX):].split(".")
        group, rest = parts[0], parts[1:]
    else:
        group, rest = None, fp.split(".") if fp else []
    ref = FieldRef(fp, label,
                   node.get("returnType") or node.get("type"), group, rest)
    expr = node.get('exprNode') or {}
    params = expr.get('paramValue') or {}
    if params:
        args = [[k, v.get('fieldPath') or v.get('paramValue')]
                for k, v in sorted(params.items())]
        ref.result_key = fp + '|' + json.dumps(args, ensure_ascii=False, separators=(',', ':'))
    return ref


def _param_operand(v):
    """EXPRESSION paramValue 项（非 dict）：标量 / 列表（字段节点、',' 分隔项）。"""
    if isinstance(v, list):
        return [_operand(x) if isinstance(x, dict) else x for x in v]
    return Literal(v, "string")


def parse(node):
    """ruleContent 节点 → 表达式树（Logic/Cmp/Expr）。"""
    if not isinstance(node, dict):
        raise ValueError("ruleContent 节点必须是 dict: %r" % (node,))
    nt = node.get("nodeType")
    if nt == "LOGICAL":
        op = "AND" if node.get("label") == "并且" else "OR"
        return Logic(op, node.get("label"), parse(node["left"]), parse(node["right"]))
    if nt == "COMPARISON":
        return Cmp(node.get("code"), node.get("label"),
                   _operand(node["left"]), _operand(node["right"]))
    if nt == "EXPRESSION":
        return _operand(node)   # 顶层 EXPRESSION 条件（属于/为是/包含…）
    raise ValueError("未知 nodeType: %r" % (nt,))


def iter_field_refs(opd):
    """递归产出操作数内全部 FieldRef（含 Expr.params 列表与算术树叶子）。"""
    if isinstance(opd, FieldRef):
        yield opd
    elif isinstance(opd, Expr):
        for v in opd.params.values():
            items = v if isinstance(v, list) else [v]
            for x in items:
                yield from iter_field_refs(x)
    elif isinstance(opd, Arith):
        stack = []
        t = opd.tree
        if opd.kind == "NESTING":
            stack.append(t.get("nestingTree"))
        elif opd.kind == "TREE_EX":
            stack.extend(t.get("arr") or [])
        else:
            stack.append(t)
        while stack:
            n = stack.pop()
            if not isinstance(n, dict):
                continue
            if n.get("nodeType") == "CALCULATE":
                stack.append(n.get("left"))
                stack.append(n.get("right"))
            else:
                yield from iter_field_refs(_operand(n))
    elif isinstance(opd, BoolSub):
        yield from _iter_tree_refs(opd.expr)


def _iter_tree_refs(node):
    """遍历表达式树（Logic/Cmp/操作数混层）收集全部 FieldRef。"""
    if isinstance(node, (Cmp, Logic)):
        yield from iter_field_refs(node.left)
        yield from iter_field_refs(node.right)
    elif node is not None:
        yield from iter_field_refs(node)


def collect_groups(expr, out=None):
    """收集别名组：{别名: {叶子字段名,…}}。"""
    if out is None:
        out = {}
    if isinstance(expr, Logic):
        collect_groups(expr.left, out)
        collect_groups(expr.right, out)
    elif isinstance(expr, Cmp):
        for opd in (expr.left, expr.right):
            for fr in iter_field_refs(opd):
                # rest 为空（两段引用，别名即叶子）不进组绑定，取值走别名直取分支
                if fr.group and fr.rest:
                    out.setdefault(fr.group, set()).add(fr.rest[-1])
    elif isinstance(expr, Expr):
        for fr in iter_field_refs(expr):
            if fr.group and fr.rest:
                out.setdefault(fr.group, set()).add(fr.rest[-1])
    return out


# ---------------------------------------------------------------- 取值工具

def dig(obj, segs):
    """按段序列在嵌套 dict 中取值；路径不存在返回 (None, False)。"""
    cur = obj
    for s in segs:
        if isinstance(cur, dict) and s in cur:
            cur = cur[s]
        else:
            return None, False
    return cur, True


def _as_elems(v):
    """绑定对象形状归一：dict → [dict]；dict 列表 → 原列表；其余 None。"""
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list) and v and all(isinstance(e, dict) for e in v):
        return v
    return None


def _num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# 候选值串分隔符：中英文逗号 / 顿号 / 中英文分号（规则文本常用中文标点）
_SEP_RE = re.compile(r"[,，、;；]")


def _eq_loose(a, b):
    """宽容相等：布尔归一相等（(属于…) 等于 'false' 这类布尔字面量比较），
    字符串化相等，或两边均可数值化且数值相等
    （兼容机构号 0265220000000 字面量 vs 报文数字 265220000000 这类前导 0 差异）。"""
    ba, bb = _bool(a), _bool(b)
    if ba is not None and bb is not None:
        return ba == bb
    if _s(a) == _s(b):
        return True
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return False


def _bool(v):
    """宽容布尔化：bool/字符串 true-false-是-否/数字 1-0。不可判返回 None。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    t = _s(v).lower()
    if t in ("true", "1", "是"):
        return True
    if t in ("false", "0", "否"):
        return False
    return None


def _has_missing(rep):
    """报告子树里是否存在"取值缺失"条目（填空未完成提示用）。"""
    for c in rep.get("children", []) or []:
        if "取值缺失" in (c.get("label") or "") or _has_missing(c):
            return True
    return False


def _apply(code, a, b):
    """按比较 code 施加运算；返回 (bool, err)（err 为类型不匹配说明）。"""
    code = (code or "").upper()
    if code in ("STR_EQ", "EQ", "STR_EQUALS"):
        return _eq_loose(a, b), ""
    if code in ("STR_NE", "NE", "NOT_EQ"):
        return not _eq_loose(a, b), ""
    if code.startswith("DATE_") and code in ("DATE_EQ", "DATE_NE", "DATE_GT",
                                             "DATE_GE", "DATE_LT", "DATE_LE"):
        sa, sb = _s(a), _s(b)
        r = {"DATE_EQ": sa == sb, "DATE_NE": sa != sb, "DATE_GT": sa > sb,
             "DATE_GE": sa >= sb, "DATE_LT": sa < sb, "DATE_LE": sa <= sb}[code]
        return r, ""
    if code in ("LT", "LE", "GT", "GE", "NUM_EQ"):
        na, nb = _num(a), _num(b)
        if na is None or nb is None:
            return False, "类型错误: %r vs %r" % (a, b)
        return {"LT": na < nb, "LE": na <= nb, "GT": na > nb,
                "GE": na >= nb, "NUM_EQ": na == nb}[code], ""
    if code == "IN":
        if isinstance(b, (list, tuple, set)):
            return any(_eq_loose(a, x) for x in b), ""
        return _eq_loose(a, b), ""
    if code in ("STR_BELONG_TO", "STR_BELONG_TO_MUL"):
        # belong_single（selector 右操作数，_operand 已转 key 值）/
        # belong_mul（selector-multiple → key 列表）
        if isinstance(b, (list, tuple, set)):
            return any(_eq_loose(a, x) for x in b), ""
        return _eq_loose(a, b), ""
    if code in ("STR_NOT_BELONG_TO", "STR_NOT_BELONG_TO_MUL"):
        if isinstance(b, (list, tuple, set)):
            return not any(_eq_loose(a, x) for x in b), ""
        return not _eq_loose(a, b), ""
    if code == "CONTAINS":
        return _s(b) in _s(a), ""
    return False, "不支持的比较 code: %s" % code


# ---------------------------------------------------------------- 求值器

class Evaluator:
    def __init__(self, expr, input_data, predefine_text="", bom=None):
        """expr: parse() 输出；input_data: 填值后的报文 dict；
        predefine_text+bom: 可选，bom 需提供 resolve_pd_ref(fp, predefine) 接口
        （复用 rule_test.input_generator.BomIndex）。"""
        self.expr = expr
        self.input = input_data if input_data is not None else {}
        self.groups = collect_groups(expr)
        self.predefine_text = predefine_text or ""
        self.bom = bom
        self.warnings = []
        self.located = {}  # 别名 → ([元素…], 路径描述) 或 None

    # ---- 别名定位（挂载点归一化） ----

    def locate_groups(self):
        for alias, tails in self.groups.items():
            self.located[alias] = self._locate(alias, tails)
            if not self.located[alias]:
                self.warnings.append(
                    "别名'%s'未在报文中定位到绑定对象（叶子: %s）"
                    % (alias, ", ".join(sorted(tails))))

    def _locate(self, alias, tails):
        # a) input 原路径
        pd_root = self.input.get("规则预定义变量")
        if isinstance(pd_root, dict) and alias in pd_root:
            elems = _as_elems(pd_root[alias])
            if elems is not None:
                return elems, "%s%s" % (PD_PREFIX, alias)
        # b) predefine 文本 → BomIndex 展开 → BOM 路径
        if self.bom is not None and self.predefine_text:
            try:
                fp = self.bom.resolve_pd_ref(PD_PREFIX + alias + ".x", self.predefine_text)
            except Exception:
                fp = []
            if fp:
                v, found = dig(self.input, fp[0].split("."))
                elems = _as_elems(v) if found else None
                if elems is not None:
                    return elems, fp[0]
        # c) 结构搜索：找含全部叶子字段名的 dict / dict 列表
        cands = []

        def walk(v, path):
            if isinstance(v, dict):
                if tails and all(t in v for t in tails):
                    cands.append(([v], path))
                for k, vv in v.items():
                    walk(vv, path + "." + str(k) if path else str(k))
            elif isinstance(v, list):
                elems = _as_elems(v)
                if elems is not None and tails and all(
                        all(t in e for t in tails) for e in elems):
                    cands.append((elems, path or "<root>"))
                else:
                    for i, e in enumerate(v):
                        walk(e, "%s[%d]" % (path, i))

        walk(self.input, "")
        if cands:
            if len(cands) > 1:
                self.warnings.append(
                    "别名'%s'存在多个候选挂载点 %s，取第一个"
                    % (alias, [c[1] for c in cands]))
            return cands[0]
        return None

    # ---- 操作数取值 ----

    def _operand_value(self, opd, binding):
        """返回 (value, 描述, ok)。"""
        if isinstance(opd, Literal):
            return opd.value, opd.describe(), True
        if isinstance(opd, BoolSub):
            # 布尔子表达式（"（…）等于 成立/不成立"）：子树先求布尔值。
            # 子树内取值缺失按 False（与 _eval 顶层语义一致），属合法
            # False 参与比较 → ok=True
            tmp = {"children": []}
            ok = self._eval(opd.expr, binding, tmp)
            d = opd.describe() + ("=%s" % ok)
            if _has_missing(tmp):
                d += "（内部字段未填值，按False计）"
            return ok, d, True
        if isinstance(opd, Expr):
            # COMPARISON 的操作数位置出现 EXPRESSION：SIZE_OF_LIST / STR_LENGTH 取长度（值型），
            # 其余布尔运算符直接按求值布尔参与比较（如 (属于…中的一个) 等于 true）
            if opd.eid in ("SIZE_OF_LIST", "STR_LENGTH"):
                eid = opd.eid
                pv1 = opd.params.get("param1")
                v, d, ok = self._operand_value(pv1, binding)
                if not ok:
                    return None, "%s(%s)" % (eid, d), False
                if isinstance(v, list):
                    return len(v), "%s(%s)" % (eid, d), True
                if eid == "STR_LENGTH":
                    # 字符串长度：标量按 str 长度计（"内容长度"），缺失按失败
                    if v is None:
                        return None, "%s(%s)" % (eid, d), False
                    return len(str(v)), "%s(%s)" % (eid, d), True
                return None, "%s(%s)" % (eid, d), False
            ok, detail, failed = self._expr_eval(opd, binding)
            # 第三位 failed=取值缺失等求值失败；合法求出的 False 是有效布尔值
            # （(属于…中的一个) 等于 不成立 → 布尔 False 参与外层 EQ 比较）
            return ok, detail, not failed
        if isinstance(opd, Arith):
            return self._arith_value(opd, binding)
        fr = opd
        result_key = getattr(fr, 'result_key', None)
        if result_key:
            results = self.input.get('__method_results__') or {}
            if result_key not in results:
                return None, 'MISSING(%s：不同方法参数需分别填值)' % fr.label, False
            return results[result_key], fr.label, True
        if fr.group is not None:
            if not fr.rest:
                # 两段引用 `规则预定义变量.<别名>`：别名即叶子（指定'A费用上限'为…），
                # 值 = input.规则预定义变量.<别名> 本身（标量，未填 → MISSING）
                pd_root = self.input.get("规则预定义变量")
                if isinstance(pd_root, dict):
                    v, found = dig(pd_root, [fr.group])
                else:
                    v, found = None, False
                path_desc = PD_PREFIX + fr.group
                if not found:
                    return None, "MISSING(%s)" % path_desc, False
                return v, path_desc, True
            loc = self.located.get(fr.group)
            if not loc or not loc[0]:
                return None, "NO_GROUP(%s)" % fr.group, False
            elems, desc = loc
            elem = elems[binding.get(fr.group, 0)]
            path_desc = "%s.%s" % (desc, ".".join(fr.rest))
            v, found = dig(elem, fr.rest)
            if not found:
                return None, "MISSING(%s)" % path_desc, False
            return v, path_desc, True
        if not fr.rest:
            return None, "MISSING(无字段路径)", False
        v, found = dig(self.input, fr.rest)
        if not found and re.fullmatch(r'dynamicsAttributeList\.[^.]+\.[^.]+_[^.]+', fr.path):
            # 与 SelectorCatalog 的 BOM 字典编码口径一致，保留前导零。
            return fr.path.rsplit('_', 1)[-1], fr.label, True
        if not found:
            return None, "MISSING(%s)" % fr.path, False
        return v, fr.path, True

    # ---- EXPRESSION 求值 ----

    def _param_list(self, opd):
        """param2 类集合参数 → 候选值列表 (values, ok)。
        字面量串按逗号拆分（`060086,060024 ` 整体一个串，带尾空格）；
        list 里的 FieldRef 取值，',' 占位跳过。"""
        if opd is None:
            return [], False
        if isinstance(opd, list):
            out = []
            for x in opd:
                if isinstance(x, str):
                    if x.strip() == ",":
                        continue
                    out.extend(t for t in _SEP_RE.split(x) if t.strip())
                elif isinstance(x, Literal):
                    out.extend(self._split_scalar(x.value))
                elif isinstance(x, FieldRef):
                    v, _d, ok = self._operand_value(x, {})
                    if not ok:
                        return [], False
                    out.append(v)
                elif isinstance(x, (Expr, Arith)):
                    v, _d, ok = self._operand_value(x, {})
                    if not ok:
                        return [], False
                    out.append(v)
            return out, True
        v, _d, ok = self._operand_value(opd, {})
        if not ok:
            return [], False
        return self._split_scalar(v), True

    @staticmethod
    def _split_scalar(v):
        if isinstance(v, str):
            return [t.strip() for t in _SEP_RE.split(v) if t.strip()]
        return [v]

    def _expr_eval(self, e, binding):
        """EXPRESSION 节点求值 → (ok, detail, failed)。

        failed=True 表示取值缺失等求值失败（区别于合法求出的 False）；
        ok 为求值布尔结果（失败时恒 False）。
        """
        eid = e.eid

        def pv(k):
            return e.params.get(k)

        def val(k):
            return self._operand_value(pv(k), binding)

        if eid in ("STR_BELONG_TO", "STR_NOT_BELONG_TO",
                   "STR_BELONG_TO_MUL", "STR_NOT_BELONG_TO_MUL"):
            lv, ld, lok = val("param1")
            cands, cok = self._param_list(pv("param2"))
            detail = "%s %s %s" % (ld, e.describe(),
                                   cands if cok else "（候选取值缺失）")
            if not lok or not cok:
                return False, detail + " → 取值缺失", True
            hit = any(_eq_loose(lv, c) for c in cands)
            ok = hit if eid.startswith("STR_BELONG_TO") else not hit
            return ok, "%s → 实际 %r" % (detail, lv), False

        if eid in ("STR_INCLUDE", "STR_NOT_INCLUDE"):
            lv, ld, lok = val("param1")
            p2 = pv("param2")
            detail = "%s %s %s" % (ld, e.describe(),
                                   p2.describe() if not isinstance(p2, list)
                                   else [x.describe() if not isinstance(x, str)
                                         else x for x in p2])
            if not lok:
                return False, detail + " → 取值缺失", True
            if isinstance(p2, list):
                # 字段值列表 → 宽容判定：任一项相等或为子串（',' 占位项已跳过）
                cands, cok = self._param_list(p2)
                if not cok:
                    return False, detail + " → 取值缺失", True
                ok = any(_eq_loose(lv, c) or _s(c) in _s(lv) for c in cands)
            else:
                v2, _d2, ok2 = self._operand_value(p2, binding)
                if not ok2:
                    return False, detail + " → 取值缺失", True
                ok = _s(v2) in _s(lv)      # 字符串子串包含
            return (ok if eid == "STR_INCLUDE" else not ok), detail, False

        if eid in ("STR_EMPTY", "STR_NOT_EMPTY",
                   "COLLECTION_IS_EMPTY", "COLLECTION_NOT_EMPTY"):
            lv, ld, lok = val("param1")
            detail = "%s %s" % (ld, e.describe())
            neg = eid in ("STR_NOT_EMPTY", "COLLECTION_NOT_EMPTY")
            if not lok:
                # 路径不存在按"空"语义处理（为空 → 命中；不为空 → 未命中），
                # 属合法语义结果而非求值失败 → failed=False
                return (not neg), detail + " → 取值缺失(按空)", False
            empty = lv is None or lv == [] or lv == {} or _s(lv) == ""
            ok = (not empty) if neg else empty
            return ok, "%s → 实际 %r" % (detail, lv), False

        if eid == "BOOLEAN_BELONG_TO":
            lv, ld, lok = val("param1")
            bv, _bd, bok = self._operand_value(pv("param2"), binding)
            want = _bool(bv)
            detail = "%s 为%s" % (ld, "是" if want else "否")
            if not lok or want is None:
                return False, detail + " → 取值缺失", True
            got = _bool(lv)
            if got is None:
                return False, "%s → 实际 %r(非布尔)" % (detail, lv), True
            return got == want, "%s → 实际 %r" % (detail, lv), False

        if eid in ("DATE_BETWEEN", "TIME_BETWEEN"):
            lv, ld, lok = val("param1")
            sv, _sd, sok = self._operand_value(pv("param2"), binding)
            ev, _ed, eok = self._operand_value(pv("param3"), binding)
            detail = "%s 在 %s 和 %s 之间" % (ld, sv, ev)
            if not (lok and sok and eok):
                return False, detail + " → 取值缺失", True
            s = _s(lv)
            ok = _s(sv) <= s <= _s(ev)
            return ok, "%s → 实际 %r" % (detail, lv), False

        if eid == "NUM_BETWEEN":
            lv, ld, lok = val("param1")
            sv, _sd, sok = self._operand_value(pv("param2"), binding)
            ev, _ed, eok = self._operand_value(pv("param3"), binding)
            detail = "%s 在 %s 和 %s 之间" % (ld, sv, ev)
            if not (lok and sok and eok):
                return False, detail + " → 取值缺失", True
            n, lo, hi = _num(lv), _num(sv), _num(ev)
            if n is None or lo is None or hi is None:
                return False, detail + " → 非数值", True
            return (lo <= n <= hi), "%s → 实际 %r" % (detail, lv), False

        if eid in ("STR_START_WITH", "STR_END_WITH",
                   "STR_NOT_START_WITH", "STR_NOT_END_WITH"):
            lv, ld, lok = val("param1")
            v2, _d2, ok2 = self._operand_value(pv("param2"), binding)
            detail = "%s %s %s" % (ld, e.describe(), v2)
            if not (lok and ok2):
                return False, detail + " → 取值缺失", True
            s, p = _s(lv), _s(v2)
            ok = s.startswith(p) if "START" in eid else s.endswith(p)
            if eid.startswith("STR_NOT_"):
                ok = not ok
            return ok, "%s → 实际 %r" % (detail, lv), False

        # ---- 运算符924 档1：集合(list)系（两侧扁平化为集合后比较）----
        if eid in ("COLLECTION_ANY_STR", "COLLECTION_NOT_IN_STR",
                   "COLLECTION_ANY_BELONG_TO", "COLLECTION_ALL_BELONG_TO",
                   "COLLECTION_ANY_CONTAINS", "COLLECTION_ALL_CONTAINS",
                   "COLLECTION_NOT_IN_CONTAINS"):
            lv, ld, lok = val("param1")
            detail = "%s %s" % (ld, e.describe())
            if not lok:
                return False, detail + " → 取值缺失", True
            raw2 = pv("param2")

            def _side2(x):
                # 集合参数=字段引用（dict/list 混排）→ 先按操作数取实际值
                if isinstance(x, dict) and x.get("nodeType"):
                    return _flat_set(self._operand_value(x, binding)[0])
                if isinstance(x, list):
                    out = set()
                    for it in x:
                        out |= _side2(it)
                    return out
                return _flat_set(x)

            p2 = _side2(raw2)
            a = _flat_set(lv)
            if eid == "COLLECTION_ANY_STR":
                ok = bool(a & p2)
            elif eid == "COLLECTION_NOT_IN_STR":
                ok = not (a & p2)
            elif eid in ("COLLECTION_ANY_BELONG_TO", "COLLECTION_ANY_CONTAINS"):
                ok = bool(a & p2)
            elif eid == "COLLECTION_ALL_BELONG_TO":
                ok = bool(a) and a <= p2
            elif eid == "COLLECTION_ALL_CONTAINS":
                ok = bool(p2) and p2 <= a
            else:  # COLLECTION_NOT_IN_CONTAINS
                ok = not (a & p2)
            return ok, "%s → 实际 %r vs %r" % (detail, lv, p2), False

        raise ValueError("不支持的 expressionId: %s" % (eid or "空"))

    # ---- 算术树求值 ----

    _ARITH_FN = {"ADD": lambda a, b: a + b, "SUB": lambda a, b: a - b,
                 "SUBTRACT": lambda a, b: a - b, "MULTIPLY": lambda a, b: a * b,
                 "DIVIDE": lambda a, b: a / b if b else None}

    def _arith_value(self, ar, binding):
        """算术节点求值 → (value, 描述, ok)。"""
        try:
            if ar.kind == "NESTING":
                return self._calc_tree(ar.tree.get("nestingTree"), binding)
            if ar.kind == "TREE_EX":
                return self._calc_arr(ar.tree.get("arr") or [], binding)
            return self._calc_tree(ar.tree, binding)
        except (TypeError, ZeroDivisionError) as ex:
            return None, "算术错误(%s)" % ex, False

    def _calc_arr(self, arr, binding):
        """TREE_EX：arr 为 [操作数, CALCULATE, 操作数, CALCULATE, …] 左结合；
        CALCULATE 节点无 left/right（平铺），右操作数是数组下一项。"""
        val = None
        desc = ""
        i = 0
        while i < len(arr):
            n = arr[i]
            if isinstance(n, dict) and n.get("nodeType") == "CALCULATE":
                # 右操作数优先取数组下一项（生成器平铺口径），缺省回退 n.right
                if i + 1 < len(arr):
                    rv, rd, rok = self._leaf_value(arr[i + 1], binding)
                elif n.get("right") is not None:
                    rv, rd, rok = self._calc_tree(n.get("right"), binding)
                else:
                    return None, "算术错误(算式缺右操作数)", False
                fn = self._ARITH_FN.get(
                    (n.get("operator") or n.get("code") or "").upper())
                if fn is None or val is None or not rok or rv is None:
                    return None, "算术错误(%s)" % n.get("operator"), False
                out = fn(val, rv)
                if out is None:
                    return None, "算术错误(除零)", False
                val, desc = out, "%s %s %s" % (desc, n.get("label") or "", rd)
                i += 2
            else:
                v, d, ok = self._leaf_value(n, binding)
                if not ok:
                    return None, d, False
                if val is None:
                    val = v
                desc = ("%s %s" % (desc, d)).strip()
                i += 1
        if val is None:
            return None, "MISSING(空算式)", False
        return val, "(%s)" % desc, True

    def _calc_tree(self, t, binding):
        """NESTING nestingTree 二叉 CALCULATE 递归求值。"""
        if not isinstance(t, dict):
            v, d, ok = self._leaf_value(t, binding)
            return (v, d, ok) if ok else (None, d, False)
        if t.get("nodeType") == "CALCULATE":
            lv, ld, lok = self._calc_tree(t.get("left"), binding)
            rv, rd, rok = self._calc_tree(t.get("right"), binding)
            fn = self._ARITH_FN.get((t.get("operator") or t.get("code") or "").upper())
            detail = "(%s %s %s)" % (ld, t.get("label") or t.get("operator"), rd)
            if not (lok and rok) or fn is None:
                return None, detail, False
            out = fn(lv, rv)
            if out is None:
                return None, detail + " 除零", False
            return out, detail, True
        return self._leaf_value(t, binding)

    def _leaf_value(self, n, binding):
        """算术树叶节点（FIELD/CUSTOM/嵌套节点）取值并数值化。"""
        v, d, ok = self._operand_value(_operand(n) if isinstance(n, dict) else n,
                                       binding)
        if not ok:
            return None, d, False
        num = _num(v)
        if num is None:
            return None, "%s 类型错误(%r)" % (d, v), False
        return num, d, True

    # ---- 求值 ----

    def run(self):
        """返回 (pass, 报告树, warnings)。"""
        self.locate_groups()
        binding_names = sorted(a for a in self.located if self.located[a])
        ranges = [range(len(self.located[a][0])) for a in binding_names]
        first_rep = None
        if not ranges:  # 无列表别名（或全部定位失败）→ 单次求值
            rep = {}
            ok = self._eval(self.expr, {}, rep)
            return ok, rep, self.warnings
        for combo in itertools.product(*ranges):
            binding = dict(zip(binding_names, combo))
            rep = {}
            ok = self._eval(self.expr, binding, rep)
            if first_rep is None:
                first_rep = (ok, rep)
            if ok:
                return True, rep, self.warnings
        return first_rep[0], first_rep[1], self.warnings

    def _eval(self, node, binding, rep):
        """递归求值并同步构建报告节点。"""
        if isinstance(node, Logic):
            entry = {"label": node.label, "ok": None, "detail": "", "children": []}
            rep.setdefault("children", []).append(entry)
            if node.op == "AND":
                l = self._eval(node.left, binding, entry)
                r = self._eval(node.right, binding, entry)
                entry["ok"] = l and r
            else:
                l = self._eval(node.left, binding, entry)
                r = self._eval(node.right, binding, entry)
                entry["ok"] = l or r
            return entry["ok"]
        if isinstance(node, Cmp):
            lv, ld, lok = self._operand_value(node.left, binding)
            rv, rd, rok = self._operand_value(node.right, binding)
            detail = "%s[%s] %s %s[%s]" % (
                node.left.describe(), ld, node.label, node.right.describe(), rd)
            if not lok or not rok:
                ok = False
                detail += " → 取值缺失"
            else:
                ok, err = _apply(node.code, lv, rv)
                if err:
                    detail += " → " + err
                else:
                    detail += " → 实际 %r vs %r" % (lv, rv)
            rep.setdefault("children", []).append({"label": detail, "ok": ok,
                                                   "detail": "", "children": []})
            return ok
        if isinstance(node, Expr):
            ok, detail, _failed = self._expr_eval(node, binding)
            rep.setdefault("children", []).append({"label": detail, "ok": ok,
                                                   "detail": "", "children": []})
            return ok
        raise ValueError("未知表达式节点: %r" % (node,))


# ---------------------------------------------------------------- 报告

def render(rep, indent=0, lines=None):
    if lines is None:
        lines = []
    mark = "✓" if rep.get("ok") else ("·" if rep.get("ok") is None else "✗")
    lines.append("  " * indent + mark + " " + str(rep.get("label", ""))
                 + (("  " + rep["detail"]) if rep.get("detail") else ""))
    for c in rep.get("children", []):
        render(c, indent + 1, lines)
    return lines


# ---------------------------------------------------------------- CLI

def _maybe_json(s):
    s = (s or "").strip()
    if s.startswith("{") or s.startswith("["):
        return json.loads(s)
    with open(s, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="本地规则求值器")
    ap.add_argument("--rule-content", required=True, help="ruleContent JSON 文件/串")
    ap.add_argument("--input", required=True, help="填值后的 input 报文 文件/串")
    ap.add_argument("--predefine", default="", help="预定义行文本（可选，用于 BOM 展开）")
    args = ap.parse_args()

    rc = _maybe_json(args.rule_content)
    inp = _maybe_json(args.input)
    bom = None
    if args.predefine:
        try:
            from rule_test.input_generator import BomIndex
            bom = BomIndex()
        except Exception as e:  # 无 sqlite 时降级为结构搜索
            print("[warn] BomIndex 不可用（%s），走结构搜索 fallback" % e, file=sys.stderr)

    ok, rep, warns = Evaluator(parse(rc), inp, args.predefine, bom).run()
    print("=" * 60)
    for w in warns:
        print("[诊断] " + w)
    print("\n".join(render(rep)))
    print("=" * 60)
    print("pass =", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
