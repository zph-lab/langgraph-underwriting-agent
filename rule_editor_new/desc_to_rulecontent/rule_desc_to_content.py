#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
根据 ruleDesc(HTML 或纯文本) + BOM 表生成 ruleContent。

用法:
  python rule_desc_to_content.py --bom <bom.json> --desc "规则描述文本"
  python rule_desc_to_content.py --bom <bom.json> --samples <samples.jsonl> --test
  python rule_desc_to_content.py --bom <bom.json> --samples <samples.jsonl> --out out.jsonl

说明:
  * fieldId 直接取 BOM 节点值(若 BOM 与目标平台不是同一快照, fieldId 可能不一致,
    其余元数据以 BOM 为准)。
  * desc 中日期比较的值若显示为 "null", 真实日期值无法从 desc 复原, 生成结果会保留 "null"。
"""

import argparse
import json
import re
import sys


ROOT_PREFIX = '业务模型对象的'

# 算术运算符映射：描述词形 → CALCULATE operator 编码。
# 主词形为中文 加/减/乘/除（编辑器候选、ruleContent label/desc 均用中文）；
# 符号 + - * / 与全角（＋－＊／×÷）是手输兼容别名——用户直接敲符号也照常
# 解析，产物运算符词形经 ARITH_CN 统一归回中文（见 _build_calc 等处）。
# ARITH_CN: 任一词形 → 中文主词形（ruleContent label/desc 输出用）
ARITH_CN = {
    '加': '加', '+': '加', '＋': '加',
    '减': '减', '-': '减', '－': '减',
    '乘': '乘', '*': '乘', '＊': '乘', '×': '乘',
    '除': '除', '/': '除', '／': '除', '÷': '除',
}
ARITH_OPS = {
    '加': 'ADD', '+': 'ADD', '＋': 'ADD',
    # SUB：车险/非车险 gt 均用 SUB(减)，SUBTRACT 仅为历史遗留
    '减': 'SUB', '-': 'SUB', '－': 'SUB',
    '乘': 'MULTIPLY', '*': 'MULTIPLY', '＊': 'MULTIPLY', '×': 'MULTIPLY',
    '除': 'DIVIDE', '/': 'DIVIDE', '／': 'DIVIDE', '÷': 'DIVIDE',
}
ARITH_PARAM_TYPE = {
    'ADD': 'int,integer,double,float',
    'SUB': 'int,integer,double,float',
    'MULTIPLY': 'int,integer,double,float,long,bigdecimal',
    'DIVIDE': 'int,integer,double,float',
}
FIELD_KEYS = ('label', 'type', 'field', 'fieldId', 'path', 'fieldPath', 'classPath',
              'dataType', 'requiredField', 'afield', 'collectionType', 'actualTypes',
              'actualTypeNames', 'detailsList', 'key', 'codeService', 'annotationName')
# ---------------------------------------------------------------------------
# 运算符表: 规则平台预定义的封闭操作符集合(与 BOM 无关, 一次性枚举)。
# 顺序即匹配优先级: 长运算符(大于等于)必须先于短运算符(大于), 否则会误切。
# 每个运算符对应一个处理器方法, 返回 (语义节点, 新位置)。
# 新增运算符 = 在表里加一行 + 写一个处理器, 不需要改解析主流程。
# ---------------------------------------------------------------------------
OPERATORS = [
    ('为是', '_op_boolean_belong'),
    ('为否', '_op_boolean_belong'),
    # 运算符924 档1（长语式须排在共享前缀的短语式之前: 不包含集合>不包含）
    ('不包含集合', '_op_coll_not_in_contains'),
    ('不包含', '_op_str_not_include'),
    ('包含', '_op_str_include'),
    ('部分属于', '_op_coll_any_belong'),
    ('全部属于', '_op_coll_all_belong'),
    ('部分包含', '_op_coll_any_contains'),
    ('全部包含', '_op_coll_all_contains'),
    ('属于（多选）', '_op_belong_mul'),
    ('不属于（多选）', '_op_not_belong_mul'),
    ('不属于（单选）', '_op_str_not_belong'),
    ('不属于', '_op_str_not_belong'),  # 裸词形「不属于 X 中的一个」（长形式须先匹配）
    ('属于（单选）', '_op_str_belong'),
    ('属于', '_op_str_belong'),
    ('不为空', '_op_not_empty'),
    ('为空', '_op_empty'),
    ('大于等于', '_op_cmp'),
    ('小于等于', '_op_cmp'),
    ('大于', '_op_cmp'),
    ('小于', '_op_cmp'),
    ('不等于', '_op_cmp'),
    ('介于', '_op_between'),
    ('不是', '_op_str_eq'),
    ('等于', '_op_cmp'),
    ('不以', '_op_end_with'),
    ('以', '_op_end_with'),
    ('是', '_op_str_eq'),
]


class RuleGenerator:
    def __init__(self, bom_path, predefines=None):
        with open(bom_path, encoding='utf-8') as f:
            bom = json.load(f)
        try:
            from .selector_values import SelectorCatalog
        except ImportError:
            from selector_values import SelectorCatalog
        self.selectors = SelectorCatalog(bom)
        self.lexicon = {}       # 组合标签(去根前缀) -> 字段节点
        self.mlabel_lex = {}    # 方法标签(祖先用'的'拼接) -> 方法节点
        self._bom_composed_norm = {}   # 去空格组合标签 -> BOM 节点(用于预定义变量匹配)
        self.dyn_label_index = {}      # 动态域值 普通label -> [节点] (多选用)
        self.method_by_fieldpath = {}  # 方法 fieldPath -> 方法 rec (exprNode 用)
        self._walk(bom, [])
        for rec in self.mlabel_lex.values():
            if rec.get('fieldPath'):
                self.method_by_fieldpath.setdefault(rec['fieldPath'], rec)
        # fieldPath 兜底: 为每个字段加 "父组合标签的英文字段名" 别名(如 结果的nprmsum),
        # 使 desc 用英文字段名而 BOM 用中文标签时也能解析(标签与 BOM 不一致场景)。
        alias_add = []
        for lab, rec in list(self.lexicon.items()):
            pc = rec.get('parent_composed') or ''
            f = rec.get('field')
            if f and pc and not rec.get('methodField'):
                alias = pc + '的' + f
                if alias not in self.lexicon:
                    acopy = dict(rec)
                    acopy['composed'] = alias   # 命中别名时按 desc 文本渲染 label
                    alias_add.append((alias, acopy))
        for alias, acopy in alias_add:
            self.lexicon[alias] = acopy
        # 前缀 Trie: 把词典/方法标签建成索引, _longest_label 从 O(词表) 降到 O(标签长度)
        self._trie = {}
        for lab in self.lexicon:
            self._trie_insert(lab)
        for lab in self.mlabel_lex:
            self._trie_insert(lab)
        self._lex_extra = {}   # 构造期/按条叠加的预定义别名(不在 trie 中)
        try:
            from .parameterized_paths import ParameterizedPaths
        except ImportError:
            from parameterized_paths import ParameterizedPaths
        self._parameterized_paths = ParameterizedPaths(self.lexicon, self.method_by_fieldpath)
        if predefines:
            for k, v in self._predefine_entries(predefines).items():
                self.lexicon[k] = v
                self._lex_extra[k] = v

    # ---------- BOM 词典 ----------
    def _walk(self, node, anc):
        label = node.get('label') or ''
        composed = ('的'.join(anc) + '的' if anc else '') + label
        if composed.startswith(ROOT_PREFIX):
            composed = composed[len(ROOT_PREFIX):]
        rec = {k: node.get(k) for k in (
            'label', 'type', 'field', 'fieldId', 'path', 'fieldPath', 'classPath',
            'dataType', 'requiredField', 'afield', 'collectionType', 'actualTypes',
            'actualTypeNames', 'detailsList', 'key', 'codeService', 'annotationName')}
        rec['composed'] = composed
        raw_parent = '的'.join(anc) if anc else ''
        if raw_parent.startswith(ROOT_PREFIX):
            raw_parent = raw_parent[len(ROOT_PREFIX):]
        rec['parent_composed'] = raw_parent
        is_method = (node.get('dataType') == 'method') or (
            isinstance(node.get('path'), str) and node.get('path', '').endswith('()'))
        if is_method:
            mlabel = '的'.join(anc[1:]) + label
            rec['mlabel'] = mlabel
            self.mlabel_lex.setdefault(mlabel, rec)
        else:
            self.lexicon.setdefault(composed, rec)
            norm = composed.replace(' ', '')
            self._bom_composed_norm.setdefault(norm, node)
            if rec.get('type') == 'string' and composed.startswith('动态域集的'):
                self.dyn_label_index.setdefault(label, []).append(rec)
        for c in node.get('children') or []:
            if isinstance(c, dict):
                self._walk(c, anc + [label])

    # ---------- 预定义变量表 ----------
    def _add_predefine(self, entries, alias, root, path_txt):
        path_txt = re.sub(r'\s+', '', path_txt)
        node = self._bom_composed_norm.get(root + '的' + path_txt)
        if node is None:
            return
        # 1) 别名本体(集合)
        l0 = '规则预定义变量的' + alias
        if l0 not in self.lexicon and l0 not in entries:
            rec0 = {k: node.get(k) for k in FIELD_KEYS}
            rec0['composed'] = l0
            rec0['fieldPath'] = '规则预定义变量.' + alias
            rec0['methodField'] = 'METHOD_FIELD'
            rec0['text'] = l0
            entries[l0] = rec0
        # 2) 别名下的子字段
        for c in node.get('children') or []:
            if not isinstance(c, dict):
                continue
            clabel = '规则预定义变量的' + alias + '的' + (c.get('label') or '')
            if clabel in self.lexicon or clabel in entries:
                continue
            crec = {k: c.get(k) for k in FIELD_KEYS}
            crec['composed'] = clabel
            crec['fieldPath'] = '规则预定义变量.' + alias + '.' + c.get('field')
            entries[clabel] = crec

    def _predefine_entries(self, contents):
        """从 predefine content 解析 指定'别名'为一个X来自( in ) '保单'的<路径>
        或 指定'别名'为'保单'的<路径>;
        返回 {label: rec} 条目(不修改 self.lexicon, 供按条规则叠加)。"""
        entries = {}
        for content in contents:
            if not content:
                continue
            for m in re.finditer(
                    r"指定'([^']+)'为一个[^来]*来自\s*\(\s*in\s*\)\s*'?([^'的;]+)'?\s*的"
                    r"([^;]+?)\s*(?:符合条件|;|$)",
                    content):
                self._add_predefine(entries, m.group(1), m.group(2), m.group(3))
            for m in re.finditer(
                    r"指定'([^']+)'为\s*'([^']+)'的([^;]+?)\s*(?:符合条件|;|$)",
                    content):
                self._add_predefine(entries, m.group(1), m.group(2), m.group(3))
        return entries

    def generate_with_predefines(self, desc, predefine_contents, extra_entries=None):
        """用本条规则的 predefine 临时叠加词典后生成, 用完还原。
        extra_entries: 预解析好的全局 predefine 条目(评估时只解析一次, 避免 O(n^2))。"""
        saved = self.lexicon
        saved_extra = self._lex_extra
        entries = self._predefine_entries(predefine_contents or [])
        if extra_entries:
            entries = {**extra_entries, **entries}   # 本条规则优先
        self.lexicon = {**saved, **entries}
        self._lex_extra = {**saved_extra, **entries}
        try:
            return self.generate(desc)
        finally:
            self.lexicon = saved
            self._lex_extra = saved_extra

    def _trie_insert(self, label):
        node = self._trie
        for ch in label:
            node = node.setdefault(ch, {})
        node['__end__'] = label

    def _longest_label(self, text, pos):
        best, bestlen = None, -1
        node = self._trie
        i, n = pos, len(text)
        while i < n:
            node = node.get(text[i])
            if node is None:
                break
            lab = node.get('__end__')
            if lab is not None and len(lab) > bestlen:
                best, bestlen = lab, len(lab)
            i += 1
        # 预定义别名按条叠加(数量很少), 仍用线性 startswith
        for lab in self._lex_extra:
            if len(lab) > bestlen and text.startswith(lab, pos):
                best, bestlen = lab, len(lab)
        if '(' in text[pos:] or '（' in text[pos:]:
            hit = self._parameterized_paths.match(text, pos, require_boundary=False)
            if hit:
                # 预览规范文本会拼接字段与比较符，不能只依赖空白边界。
                # 只接受解析器已支持的后缀，避免把更长的未知字段截成合法前缀。
                suffix = text[hit[0]:]
                boundary = (not suffix or suffix[0].isspace()
                            or suffix[0] in '+-*/＋－＊／×÷)）'
                            or suffix.startswith(tuple(op for op, _ in OPERATORS))
                            or suffix.startswith(tuple(ARITH_OPS))
                            or suffix.startswith(('内容长度', '的内容长度', '在')))
                if not boundary:
                    hit = None
            if hit and hit[0] - pos > bestlen:
                end, rec = hit
                best = text[pos:end]
                rec['composed'] = best
                self.lexicon[best] = rec
        return best

    # ---------- 解析 ----------
    def parse(self, desc):
        text = self._plain(desc)
        tree, pos = self._parse_or(text, 0)
        if pos < len(text):
            raise ValueError('解析后仍有剩余文本 @%d: %r' % (pos, text[pos:pos + 40]))
        return tree

    @staticmethod
    def _plain(desc):
        try:
            from .selector_values import normalize_selectors
        except ImportError:
            from selector_values import normalize_selectors
        return normalize_selectors(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', desc)).strip())

    def _scan_value(self, text, pos):
        """扫描值文本: (半角/全角)括号配对视为值的一部分, 顶层遇到 并且/或者/) 才终止;
        仅当 并且/或者 两侧带空格时视为逻辑运算符(避免散文里的"或者"被误切)。"""
        depth = 0
        i = pos
        while i < len(text):
            ch = text[i]
            if ch in ('(', '（'):
                depth += 1
            elif ch in (')', '）'):
                if depth == 0:
                    return text[pos:i].strip(), i
                depth -= 1
            elif depth == 0 and (text.startswith('并且', i)
                                 or text.startswith('或者', i)):
                prev_ok = i == pos or text[i - 1] == ' '
                nxt = i + 2
                nxt_ok = nxt >= len(text) or text[nxt] == ' '
                if prev_ok and nxt_ok:
                    return text[pos:i].strip(), i
            i += 1
        return text[pos:].strip(), len(text)

    @staticmethod
    def _find_next_op(text, pos, op):
        """在当前括号层级内找下一个 op(遇到本级 ')' 即停止)。"""
        depth = 0
        i = pos
        while i < len(text):
            ch = text[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                if depth == 0:
                    return -1
                depth -= 1
            elif depth == 0 and text.startswith(op, i):
                return i
            i += 1
        return -1

    def _parse_or(self, text, pos):
        """同级 并且/或者 按文本顺序左结合解析(与平台 desc 解析一致, 括号优先)。"""
        left, pos = self._parse_atom(text, pos)
        while True:
            i = self._find_next_op(text, pos, '并且')
            j = self._find_next_op(text, pos, '或者')
            if i == -1 and j == -1:
                return left, pos
            if i != -1 and (j == -1 or i < j):
                right, pos = self._parse_atom(text, i + 2)
                left = {'op': 'and', 'left': left, 'right': right}
            else:
                right, pos = self._parse_atom(text, j + 2)
                left = {'op': 'or', 'left': left, 'right': right}

    def _parse_atom(self, text, pos):
        while pos < len(text) and text[pos] == ' ':
            pos += 1
        if pos >= len(text):
            raise ValueError('空表达式 @%d' % pos)
        if text[pos] == '(':
            inner, pos = self._parse_or(text, pos + 1)
            while pos < len(text) and text[pos] == ' ':
                pos += 1
            if pos >= len(text) or text[pos] != ')':
                raise ValueError('缺右括号 @%d' % pos)
            pos += 1
            node = inner
            node['subtree'] = True          # 括号组根节点标记
            while pos < len(text) and text[pos] == ' ':
                pos += 1
            if text.startswith('等于', pos):
                pos2 = pos + 2
                while pos2 < len(text) and text[pos2] == ' ':
                    pos2 += 1
                if text.startswith('不成立', pos2):
                    node = {'op': 'not', 'expr': node}
                    pos = pos2 + 3
        else:
            node, pos = self._parse_clause(text, pos)
        return node, pos

    def _parse_clause(self, text, pos):
        if text.startswith('""', pos):
            # 空串反写: `"" 是 <字段>` -> STR_EQ(左 CUSTOM "", 右 FIELD)
            m = re.match(r'^""\s+是\s+', text[pos:])
            if m:
                after = pos + m.end()
                label2 = self._longest_label(text, after)
                if label2 is not None:
                    rec2 = self.lexicon[label2]
                    return {'kind': 'str_eq_rev', 'field': rec2,
                            'value': '""'}, after + len(label2)
        label = self._longest_label(text, pos)
        if label is None:
            raise ValueError('未匹配到字段 @%d: %r' % (pos, text[pos:pos + 40]))
        raw = text[pos + len(label):]
        leading = len(raw) - len(raw.lstrip())
        rem = raw.lstrip()
        base = pos + len(label) + leading

        if label in self.mlabel_lex:
            mrec = self.mlabel_lex[label]
            args = []
            # 兼容半角 ( 与全角 （ 作为方法参数左括号
            if rem.startswith('（') or rem.startswith('('):
                args, npos = self._parse_method_args(text, base)
                if npos == -1:
                    raise ValueError('方法参数缺右括号 @%d' % pos)
                rest = text[npos:]
                leading2 = len(rest) - len(rest.lstrip())
                base = npos + leading2
                rem = rest.lstrip()
            subject = {'method': mrec, 'args': args}
            if rem.startswith('内容长度') or rem.startswith('的内容长度'):
                prefix = '内容长度' if rem.startswith('内容长度') else '的内容长度'
                rest = rem[len(prefix):]
                leading2 = len(rest) - len(rest.lstrip())
                rest = rest.lstrip()
                base2 = base + len(prefix) + leading2
                for op_text in ('大于等于', '小于等于', '不等于', '大于', '小于',
                                '等于', '是'):
                    if rest.startswith(op_text):
                        val_text, np = self._scan_value(text, base2 + len(op_text))
                        value = self._resolve_operand(val_text)
                        return {'kind': 'size_of_list', 'op': op_text,
                                'field': subject, 'value': value}, np
                raise ValueError('内容长度后缺比较符: %r' % rest[:20])
            if rem[:1] in ARITH_OPS:
                return self._parse_left_arith(text, base, subject, rem)
            for op_text, handler in OPERATORS:
                if rem.startswith(op_text):
                    return getattr(self, handler)(text, base, subject, rem, op_text)
            raise ValueError('方法后未知操作符: %r' % rem[:20])

        rec = self.lexicon[label]
        if rem.startswith('内容长度') or rem.startswith('的内容长度'):
            prefix = '内容长度' if rem.startswith('内容长度') else '的内容长度'
            rest = rem[len(prefix):]
            leading2 = len(rest) - len(rest.lstrip())
            rest = rest.lstrip()
            base2 = base + len(prefix) + leading2
            for op_text in ('大于等于', '小于等于', '不等于', '大于', '小于', '等于', '是'):
                if rest.startswith(op_text):
                    val_text, np = self._scan_value(text, base2 + len(op_text))
                    value = self._resolve_operand(val_text)
                    return {'kind': 'size_of_list', 'op': op_text,
                            'field': rec, 'value': value}, np
            raise ValueError('内容长度后缺比较符: %r' % rest[:20])
        if rem.startswith('在'):
            # 运算符924 区间词形「在 起 和 止 之间」：kind=between 由 _render
            # 按字段类型选 NUM/DATE/TIME_BETWEEN。终止于子句边界（后接 并且/
            # 或者 允许；desc 规范形态 token 间无空格，同口径兼容）
            m = re.match(r'^在\s*(.+?)\s*和\s*(.+?)\s*之间(?=\s|并且|或者|$)', rem)
            if not m:
                raise ValueError('在…之间格式错误: %r' % rem[:24])
            return {'kind': 'between', 'field': rec,
                    'start': m.group(1).strip(), 'end': m.group(2).strip()}, \
                base + m.end()
        if rem[:1] in ARITH_OPS:
            return self._parse_left_arith(text, base, rec, rem)
        for op_text, handler in OPERATORS:
            if rem.startswith(op_text):
                return getattr(self, handler)(text, base, rec, rem, op_text)
        raise ValueError('未知操作符: %r' % rem[:24])

    # ---- 运算符处理器: 每个返回 (语义节点, 新位置) ----
    def _parse_left_arith(self, text, base, rec, rem):
        """左侧算术: 'A 乘 150 等于 B' / '方法() 加 方法() 大于 X' -> NESTING/TREE_EX。"""
        if isinstance(rec, dict) and 'method' in rec:
            terms = [('method', rec['method'], rec['args'])]
        else:
            terms = [('field', rec)]
        ops = []
        i, n = 0, len(rem)
        while i < n:
            while i < n and rem[i] == ' ':
                i += 1
            if i >= n or self._find_arith_op(rem, i) != i:
                break
            ops.append(rem[i])
            i += 1
            while i < n and rem[i] == ' ':
                i += 1
            hit = self._longest_label(rem, i)
            if hit is not None:
                if hit in self.mlabel_lex:
                    m2 = self.mlabel_lex[hit]
                    i += len(hit)
                    while i < n and rem[i] == ' ':
                        i += 1
                    args = []
                    if rem.startswith('（', i) or rem.startswith('(', i):
                        args, npos = self._parse_method_args(rem, i)
                        if npos == -1:
                            raise ValueError('方法参数缺右括号 @%d' % (base + i))
                        i = npos
                    terms.append(('method', m2, args))
                else:
                    terms.append(('field', self.lexicon[hit]))
                    i += len(hit)
            else:
                end = n
                m = re.search(r'大于等于|小于等于|不等于|大于|小于|等于|是|介于|在',
                              rem[i:])
                if m:
                    end = i + m.start()
                a = self._find_arith_op(rem, i)
                if a != -1 and a < end:
                    end = a
                lit = rem[i:end].strip()
                if lit:
                    terms.append(('lit', lit))
                i = end
        while i < n and rem[i] == ' ':
            i += 1
        # 算术链 + 区间（2026-09-24，运算符924 词形）：`A 加 B 在 起 和 止 之间`
        # / 旧 `A 加 B 介于 起,止`；field 为算术树，_render 出 NESTING param1
        m = re.match(r'^在\s*(.+?)\s*和\s*(.+?)\s*之间(?=\s|并且|或者|$)', rem[i:])
        if m:
            return {'kind': 'between', 'field': {'nest': terms, 'ops': ops},
                    'start': m.group(1).strip(),
                    'end': m.group(2).strip()}, base + i + m.end()
        for cmp_op in ('大于等于', '小于等于', '不等于', '等于', '大于', '小于', '是'):
            if rem.startswith(cmp_op, i):
                val_text, np = self._scan_value(text, base + i + len(cmp_op))
                value = self._resolve_operand(val_text)
                return {'kind': 'cmp', 'op': cmp_op,
                        'field': {'nest': terms, 'ops': ops}, 'value': value}, np
        if rem.startswith('介于', i):
            return self._op_between(text, base + i,
                                    {'nest': terms, 'ops': ops}, rem[i:], '介于')
        raise ValueError('左侧算术后缺比较符: %r' % rem[i:i + 20])

    def _parse_operand(self, val_text):
        """把比较右侧的值文本解析为 字段/字面量 或 算术嵌套 {'nest','ops'}"""
        terms, ops = [], []
        i, n = 0, len(val_text)
        while i < n:
            while i < n and val_text[i] == ' ':
                i += 1
            if i >= n:
                break
            hit = self._longest_label(val_text, i)
            if hit is not None:
                if hit in self.mlabel_lex:
                    m2 = self.mlabel_lex[hit]
                    i += len(hit)
                    while i < n and val_text[i] == ' ':
                        i += 1
                    args = []
                    if val_text.startswith('（', i) or val_text.startswith('(', i):
                        args, npos = self._parse_method_args(val_text, i)
                        if npos == -1:
                            raise ValueError('方法参数缺右括号')
                        i = npos
                    terms.append(('method', m2, args))
                else:
                    terms.append(('field', self.lexicon[hit]))
                    i += len(hit)
            elif val_text[i] in '（(':
                # 括号组整体视作一个操作数，递归解析内部表达式（2026-09-30）。
                # 未配对 → 直接停止，交给预览硬校验标红，生成端不吞尾产生错误规则。
                close = self._match_paren(val_text, i)
                if close < 0:
                    break
                inner = val_text[i + 1:close]
                sub = self._parse_operand(inner)
                if sub is None:
                    t = self._single_term(inner)
                    if t is None:
                        break
                    sub = {'terms': [t], 'ops': []}
                terms.append(('group', sub))
                i = close + 1
            else:
                a = self._find_arith_op(val_text, i)
                end = a if a != -1 else n
                lit = val_text[i:end].strip()
                if lit:
                    terms.append(('lit', lit))
                i = end
            while i < n and val_text[i] == ' ':
                i += 1
            if i < n and self._find_arith_op(val_text, i) == i:
                ops.append(val_text[i])
                i += 1
            else:
                break
        if ops and len(terms) == len(ops) + 1:
            return {'terms': terms, 'ops': ops}
        return None

    def _match_paren(self, text, i):
        """自 text[i]（须为左括号）向后找配对右括号下标；不配对返回 -1。
        半角/全角括号混用按深度计数（（( 入栈、)）出栈）。"""
        depth, j, n = 0, i, len(text)
        while j < n:
            c = text[j]
            if self._is_paren_open(c):
                depth += 1
            elif self._is_paren_close(c):
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        return -1

    def _single_term(self, text):
        """把括号内的单个操作数文本解析为 term 元组（字段/方法调用/字面量）。"""
        text = (text or '').strip()
        if not text:
            return None
        hit = self._longest_label(text, 0)
        if hit is not None:
            if hit in self.mlabel_lex:
                m2 = self.mlabel_lex[hit]
                k = len(hit)
                while k < len(text) and text[k] == ' ':
                    k += 1
                args = []
                if text.startswith('（', k) or text.startswith('(', k):
                    args, npos = self._parse_method_args(text, k)
                    if npos == -1:
                        raise ValueError('方法参数缺右括号')
                return ('method', m2, args)
            return ('field', self.lexicon[hit])
        return ('lit', text)

    @staticmethod
    def _is_paren_open(ch):
        """左括号（半角/全角）。"""
        return ch in ('（', '(')

    @staticmethod
    def _is_paren_close(ch):
        """右括号（半角/全角）。"""
        return ch in ('）', ')')

    @staticmethod
    def _is_sep(ch):
        """参数分隔符（半角/全角逗号）。"""
        return ch in ('，', ',')

    def _parse_method_args(self, text, pos):
        """解析 (…) 内的方法参数: 最长标签优先(参数值本身可含 '，'/'（…）'),
        否则按 '，' 分隔字面量; 保留首尾/连续分隔符产生的空参数占位。
        左/右括号与分隔符均兼容半角/全角。返回 (args, 参数列表结束位置), 缺右括号返回 -1。"""
        i = pos + 1
        args = []
        n = len(text)
        while i < n and self._is_sep(text[i]):
            args.append('')
            i += 1
        while i < n:
            while i < n and text[i] == ' ':
                i += 1
            if i >= n or self._is_paren_close(text[i]):
                break
            hit = self._longest_label(text, i)
            if hit is not None:
                args.append(text[i:i + len(hit)].strip())
                i += len(hit)
            else:
                j = i
                depth = 0
                while j < n:
                    ch = text[j]
                    if self._is_paren_open(ch):
                        depth += 1
                    elif self._is_paren_close(ch):
                        if depth == 0:
                            break
                        depth -= 1
                    elif self._is_sep(ch) and depth == 0:
                        break
                    j += 1
                args.append(text[i:j].strip())
                i = j
            while i < n and self._is_sep(text[i]):
                i += 1
                if i < n and (self._is_sep(text[i]) or self._is_paren_close(text[i])):
                    args.append('')
        if not args:
            args = ['']
        return args, (i + 1 if i < n and self._is_paren_close(text[i]) else -1)

    @staticmethod
    def _find_arith_op(text, start=0):
        """找下一个真正的算术符（中文 加减乘除 或符号 + - * / 及全角变体）。

        中文词：前侧须为空格/数字/右括号、后侧须为空格/数字（防 '驾乘' 误判）。
        符号（2026-09-24）：前侧须为空白/行首/右括号（编辑器规范文本恒带空格），
        后侧须为空白/数字/左括号——防日期 '2026-09-22'、比值串 '2/3'、
        label 内 '保额/限额(元)' 等无空格粘连形态被误切。"""
        for m in re.finditer(r'[加减乘除+\-*/＋－＊／×÷]', text[start:]):
            i = start + m.start()
            ch = text[i]
            if ch in '+-*/＋－＊／×÷':
                prev_ok = i == 0 or text[i - 1] in ' 　)）'
                nxt_ok = (i + 1 >= len(text)
                          or text[i + 1] in ' 　0123456789（(')
            else:
                prev_ok = i == 0 or text[i - 1] in ' 0123456789）)'
                nxt_ok = i + 1 >= len(text) or text[i + 1] in ' 0123456789'
            if prev_ok and nxt_ok:
                return i
        return -1

    def _resolve_operand(self, val_text):
        nest = self._parse_operand(val_text)
        if nest:
            return {'nest': nest['terms'], 'ops': nest['ops']}
        hit = self._longest_label(val_text, 0)
        if hit is not None and hit in self.mlabel_lex:
            # 方法调用作为比较值: 标签 + (参数)
            rest = val_text[len(hit):]
            npos = len(hit) + len(rest) - len(rest.lstrip())
            args = []
            rest = rest.lstrip()
            if rest.startswith('（') or rest.startswith('('):
                args, npos = self._parse_method_args(val_text, npos)
            return {'method': self.mlabel_lex[hit], 'args': args}
        if hit == val_text:
            return self.lexicon[hit]
        rec = self._match_label(val_text)
        return rec if rec is not None else val_text

    def _match_label(self, value):
        """标签匹配: 先精确, 再去空格匹配(desc 值里 动态域集…的 X 可能带空格)。"""
        rec = self.lexicon.get(value)
        if rec is not None:
            return rec
        nv = value.replace(' ', '')
        if nv != value:
            for lab, r in self.lexicon.items():
                if lab.replace(' ', '') == nv:
                    return r
        return None

    def _op_boolean_belong(self, text, base, rec, rem, op):
        p2 = {'label': '是', 'value': None, 'key': 'true'} if op == '为是' \
            else {'label': '否', 'value': None, 'key': 'false'}
        return {'kind': 'boolean_belong', 'field': rec, 'p2': p2}, base + len(op)

    def _op_str_include(self, text, base, rec, rem, op):
        val, np = self._scan_value(text, base + len(op))
        values = self._belong_values(val)
        if isinstance(rec, dict) and (rec.get('type') or '').lower() == 'list':
            # list 字段的"包含" → COLLECTION_ANY_STR（运算符924 档1）
            if isinstance(values, list):
                return {'kind': 'coll_op', 'eid': 'COLLECTION_ANY_STR',
                        'field': rec, 'values': values}, np
            return {'kind': 'coll_op', 'eid': 'COLLECTION_ANY_STR',
                    'field': rec, 'value': val}, np
        if isinstance(values, list):
            return {'kind': 'str_include_list', 'field': rec, 'values': values}, np
        return {'kind': 'str_include', 'field': rec, 'value': val}, np

    def _op_str_not_include(self, text, base, rec, rem, op):
        val, np = self._scan_value(text, base + len(op))
        values = self._belong_values(val)
        if isinstance(rec, dict) and (rec.get('type') or '').lower() == 'list':
            # list 字段的"不包含" → COLLECTION_NOT_IN_STR（运算符924 档1）
            if isinstance(values, list):
                return {'kind': 'coll_op', 'eid': 'COLLECTION_NOT_IN_STR',
                        'field': rec, 'values': values}, np
            return {'kind': 'coll_op', 'eid': 'COLLECTION_NOT_IN_STR',
                    'field': rec, 'value': val}, np
        if isinstance(values, list):
            return {'kind': 'str_not_include_list', 'field': rec,
                    'values': values}, np
        return {'kind': 'str_not_include', 'field': rec, 'value': val}, np

    # ---- 运算符924 档1：COLLECTION_* 集合系 与 介于 区间 ----
    _COLL_SETOPS = {
        '部分属于': 'COLLECTION_ANY_BELONG_TO',
        '全部属于': 'COLLECTION_ALL_BELONG_TO',
        '部分包含': 'COLLECTION_ANY_CONTAINS',
        '全部包含': 'COLLECTION_ALL_CONTAINS',
        '不包含集合': 'COLLECTION_NOT_IN_CONTAINS',
    }

    def _op_coll_setop(self, text, base, rec, op):
        val, np = self._scan_value(text, base + len(op))
        eid = self._COLL_SETOPS[op]
        values = self._belong_values(val) if isinstance(val, str) else val
        if isinstance(values, list):
            return {'kind': 'coll_op', 'eid': eid, 'field': rec,
                    'values': values}, np
        v = (val.get('value') if isinstance(val, dict) else val) or ''
        if not v:
            raise ValueError('%s 后缺集合参数: %r' % (op, val))
        return {'kind': 'coll_op', 'eid': eid, 'field': rec, 'value': v}, np

    def _op_coll_any_belong(self, text, base, rec, rem, op):
        return self._op_coll_setop(text, base, rec, '部分属于')

    def _op_coll_all_belong(self, text, base, rec, rem, op):
        return self._op_coll_setop(text, base, rec, '全部属于')

    def _op_coll_any_contains(self, text, base, rec, rem, op):
        return self._op_coll_setop(text, base, rec, '部分包含')

    def _op_coll_all_contains(self, text, base, rec, rem, op):
        return self._op_coll_setop(text, base, rec, '全部包含')

    def _op_coll_not_in_contains(self, text, base, rec, rem, op):
        return self._op_coll_setop(text, base, rec, '不包含集合')

    def _op_between(self, text, base, rec, rem, op):
        """介于 起,止 / 介于 起 和 止 → kind=between（_render 按字段型选
        NUM_BETWEEN / DATE_BETWEEN / TIME_BETWEEN）。"""
        val, np = self._scan_value(text, base + len(op))
        v = (val.get('value') if isinstance(val, dict) else val) or ''
        m = re.match(r'^\s*(.+?)\s*(?:[，,;、]|和)\s*(.+?)\s*(?:之间)?\s*$', str(v))
        if not m:
            raise ValueError('介于 后缺起止值(形如 起,止): %r' % v[:20])
        return {'kind': 'between', 'field': rec,
                'start': m.group(1), 'end': m.group(2)}, np

    def _op_str_belong(self, text, base, rec, rem, op):
        val_text, np = self._scan_value(text, base + len(op))
        if '单选' in op:
            option = self.selectors.values(rec, op, val_text)[0]
            return {'kind': 'belong_single', 'field': rec, 'value': option['label'],
                    'selector_key': option['key'], 'op': op}, np
        end = val_text.find('中的一个')
        if end == -1:
            return {'kind': 'belong_single', 'field': rec, 'value': val_text,
                    'op': op}, np
        raw_start = base + len(op)
        while raw_start < len(text) and text[raw_start] == ' ':
            raw_start += 1
        values = self._belong_values(val_text[:end])
        return {'kind': 'str_belong', 'field': rec, 'values': values}, \
            raw_start + end + 4

    def _op_str_not_belong(self, text, base, rec, rem, op):
        val_text, np = self._scan_value(text, base + len(op))
        if '单选' in op:
            option = self.selectors.values(rec, op, val_text)[0]
            return {'kind': 'belong_single', 'field': rec, 'value': option['label'],
                    'selector_key': option['key'], 'op': op}, np
        end = val_text.find('中的一个')
        if end == -1:
            return {'kind': 'belong_single', 'field': rec, 'value': val_text,
                    'op': op}, np
        raw_start = base + len(op)
        while raw_start < len(text) and text[raw_start] == ' ':
            raw_start += 1
        values = self._belong_values(val_text[:end])
        return {'kind': 'str_not_belong', 'field': rec, 'values': values}, \
            raw_start + end + 4

    def _belong_values(self, raw):
        """属于列表值解析: 每个逗号一个 ',' 元素; 非空且命中词典的项 -> FIELD;
        存在非空字面量时整体退回原始字符串(与平台一致)。"""
        tokens = raw.split(',')
        if all((self._match_label(t.strip()) is not None or t.strip() == '')
               for t in tokens):
            arr = []
            for i, t in enumerate(tokens):
                if i:
                    arr.append(',')
                s = t.strip()
                if s:
                    arr.append(self._match_label(s))
            return arr
        return raw

    def _op_belong_mul(self, text, base, rec, rem, op):
        return self._parse_belong_mul(text, base, rec, rem, op, neg=False)

    def _op_not_belong_mul(self, text, base, rec, rem, op):
        return self._parse_belong_mul(text, base, rec, rem, op, neg=True)

    def _parse_belong_mul(self, text, base, rec, rem, op, neg):
        rest = rem[len(op):].lstrip()
        lead = len(rem[len(op):]) - len(rest)
        if not rest.startswith('['):
            raise ValueError('多选缺 [')
        end = rest.find(']')
        if end == -1:
            raise ValueError('多选缺 ]')
        options = self.selectors.values(rec, op, rest[:end + 1])
        items = [o['label'] for o in options]
        pos_after = base + len(op) + lead + end + 1
        return {'kind': 'not_belong_mul' if neg else 'belong_mul',
                'field': rec, 'op': op, 'items': items,
                'selector_keys': [o['key'] for o in options]}, pos_after

    def _resolve_dyn_value(self, value, field_rec):
        cands = self.dyn_label_index.get(value) or []
        parent = '动态域集的' + (field_rec.get('label') or '')
        for c in cands:
            if c.get('parent_composed') == parent:
                return c
        return cands[0] if cands else None

    def _resolve_dyn_value_code(self, value, field_rec):
        """按 codeService 反推动态域类, 优先取该类下同标签的值(单选选择器用)。"""
        cands = self.dyn_label_index.get(value) or []
        cs = (field_rec.get('codeService') or '').replace('Service', '')
        cs_low = cs.lower()
        for c in cands:
            cp = c.get('classPath') or ''
            if cs and ('domain.' + cs_low) in cp.lower():
                return c
        return cands[0] if cands else None

    def _op_not_empty(self, text, base, rec, rem, op):
        return {'kind': 'not_empty', 'field': rec}, base + len(op)

    def _op_empty(self, text, base, rec, rem, op):
        return {'kind': 'empty', 'field': rec}, base + len(op)

    def _op_cmp(self, text, base, rec, rem, op):
        val_text, np = self._scan_value(text, base + len(op))
        value = self._resolve_operand(val_text)
        if isinstance(value, str):
            value = self._truncate_code_value(value, rec)
        return {'kind': 'cmp', 'op': op, 'field': rec, 'value': value}, np

    def _op_end_with(self, text, base, rec, rem, op):
        val_text, np = self._scan_value(text, base + len(op))
        neg = op.startswith('不')  # 不以…开头/结尾（运算符924 档1）
        for suffix, kind in (('结尾', 'str_end_with'), ('开头', 'str_start_with')):
            if val_text.endswith(suffix):
                if neg:
                    kind = 'str_not_' + kind[len('str_'):]
                return {'kind': kind, 'field': rec,
                        'value': val_text[:-len(suffix)]}, np
        raise ValueError('以…结尾/以…开头 缺后缀: %r' % val_text[:20])

    def _op_str_eq(self, text, base, rec, rem, op):
        val_text, np = self._scan_value(text, base + len(op))
        value = self._resolve_operand(val_text)
        if isinstance(value, str):
            value = self._truncate_code_value(value, rec)
        kind = 'str_ne' if op == '不是' else 'str_eq'
        return {'kind': kind, 'field': rec, 'value': value}, np

    def _truncate_code_value(self, value, rec):
        """非日期字段值形如 '047002 诉讼财产保全责任保险' / '063043永安贝呵护…' 时,
        只保留编码部分(平台存储只存 code, 不依赖 BOM 词典命中)。"""
        if isinstance(value, str) and isinstance(rec, dict) and 'method' not in rec \
                and rec.get('type') != 'date':
            m = re.match(r'^(\d{4,})(?:\s+\S.*)?$', value)
            if not m:
                # 无空格编码+中文标签, 如 '063043永安贝呵护少儿医疗保险A款保险'
                m = re.match(r'^(\d{4,})([\u4e00-\u9fff].*)?$', value)
            if m:
                return m.group(1)
        return value

    # ---------- 序列化 ----------
    def render(self, tree):
        return self._render(tree, '')

    def _field_node(self, rec, rel=None):
        n = {
            'dataType': rec.get('dataType') or 'field',
            'actualTypeNames': rec.get('actualTypeNames'),
            'preDefine': None,
            # ADP 按 BOM 模板标签恢复字段，方法实参单独存于 exprNode。
            'label': rec.get('_template_label') or rec['composed'],
            'nodeType': 'FIELD',
            'type': rec['type'],
            'afield': rec.get('afield', True),
            'actualTypes': rec.get('actualTypes'),
            'collectionType': rec.get('collectionType'),
            'path': rel if rel is not None else rec['path'],
            'bomModelDetailId': None,
            'field': rec['field'],
            'requiredField': rec.get('requiredField', False),
            'children': None,
            'detailsList': rec.get('detailsList'),
            'classPath': rec['classPath'],
            'fieldPath': rec['fieldPath'],
            'value': None,
            'key': rec.get('key') or rec['field'],
            'returnType': rec['type'],
            'fieldId': rec['fieldId'],
        }
        if rec.get('methodField'):
            # 预定义变量集合别名: methodField/text 按原数据顺序插到前面
            n = {'methodField': rec['methodField'], 'text': rec.get('text'), **n}
        expr = self._expr_node_for(rec)
        if expr is not None:
            if rec.get('_bound_args') is not None:
                expr['paramValue'] = self._method_node(
                    self.method_by_fieldpath[rec['fieldPath'].split('()', 1)[0] + '()'],
                    rec['_bound_args'])['paramValue']
            n['exprNode'] = expr
        return n

    def _expr_node_for(self, rec):
        """字段若通过 BomFieldMethod 方法访问(如 appbase.getCoverage().namt),
        按 BOM 方法节点补出 exprNode 骨架(参数值为规则私有, 不填)。"""
        fp = rec.get('fieldPath') or ''
        i = fp.find('()')
        if i == -1:
            return None
        method = self.method_by_fieldpath.get(fp[:i + 2])
        if method is None:
            return None
        ann = method.get('annotationName') or ''
        if 'BomFieldMethod' not in ann:
            return None
        dl = method.get('detailsList') or []
        if dl:
            expr_array = [{
                'type': (d.get('type') or 'string').rstrip(';'),
                'id': d.get('id'),
                'paramId': d.get('paramId'),
            } for d in dl if isinstance(d, dict)]
        else:
            snake = re.sub(r'(?<!^)(?=[A-Z])', '_', method['field']).upper()
            expr_array = [{'type': 'string', 'id': snake + '_0', 'paramId': 'param1'}]
        return {
            'methodPath': (method['fieldPath'] or '').rstrip('()'),
            'methodName': method['composed'],
            'nodeType': 'METHOD',
            'path': method['path'],
            'expressionId': method['path'],
            'classPath': method['classPath'],
            'expressionArray': expr_array,
            'returnType': method['type'],
            'paramValue': {},
        }

    def _method_node(self, rec, args, path=None):
        field = rec['field']
        dl = rec.get('detailsList') or []
        if dl:
            expr_array = []
            for d in dl:
                if isinstance(d, dict):
                    expr_array.append({
                        'type': (d.get('type') or 'string').rstrip(';'),
                        'id': d.get('id'),
                        'paramId': d.get('paramId'),
                    })
        else:
            snake = re.sub(r'(?<!^)(?=[A-Z])', '_', field).upper()
            expr_array = [{'type': 'string', 'id': snake + '_0', 'paramId': 'param1'}]
        param_value = {}
        for i, arg in enumerate(args, start=1):
            if isinstance(arg, dict):
                param_value['param%d' % i] = self._field_node(arg)
            else:
                param_value['param%d' % i] = {
                    'type': 'input', 'nodeType': 'CUSTOM',
                    'returnType': 'string', 'paramValue': arg,
                }
        return {
            'methodPath': (rec['fieldPath'] or '').rstrip('()'),
            'methodName': rec['mlabel'],
            'nodeType': 'METHOD',
            'path': path if path is not None else rec['path'],
            'expressionId': rec['path'],
            'classPath': rec['classPath'],
            'expressionArray': expr_array,
            'returnType': rec['type'],
            'paramValue': param_value,
        }

    def _resolve_args(self, args):
        resolved = []
        for a in args:
            hit = self._longest_label(a, 0)
            resolved.append(self.lexicon[hit] if hit == a else a)
        return resolved

    def _subject_node(self, subject, field_rel, method_rel, abs_method=False):
        """把 字段/方法 主体转成节点。
        field: EXPRESSION.param1 用绝对 path(None), COMPARISON 左右用相对路径;
        method: 为是/为否 用绝对 path, 其余用法用相对路径(method_rel)。"""
        if isinstance(subject, dict) and 'method' in subject:
            path = None if abs_method else method_rel
            return self._method_node(subject['method'],
                                     self._resolve_args(subject['args']), path=path)
        return self._field_node(subject, field_rel)

    def _custom_node(self, value, rel=None, label=None, typ='input',
                     return_type='boolean', with_path=True):
        n = {}
        if label is not None:
            n['label'] = label
        n['nodeType'] = 'CUSTOM'
        n['type'] = typ
        if with_path:
            n['path'] = rel
        n['returnType'] = return_type
        n['paramValue'] = value
        return n

    def _expr(self, expression_id, pos, param_value, subtree=False):
        n = {
            'label': '', 'nodeType': 'EXPRESSION', 'path': pos,
            'expressionId': expression_id, 'returnType': 'boolean',
        }
        if subtree:
            n['subTree'] = True
        n['codeService'] = 'trueOrFalseService'
        n['paramValue'] = param_value
        return n

    def _render(self, node, pos):
        op = node.get('op')
        if op in ('and', 'or'):
            n = {
                'label': '并且' if op == 'and' else '或者',
                'right': self._render(node['right'], pos + '.right'),
                'nodeType': 'LOGICAL',
                'path': pos,
                'left': self._render(node['left'], pos + '.left'),
                'returnType': 'boolean',
            }
            if node.get('subtree'):
                n['subTree'] = True
            return n
        if op == 'not':
            inner = self._render(node['expr'], pos + '.left')
            return {
                'code': 'EQ',
                'label': '等于',
                'right': self._custom_node('false', pos + '.right',
                                           label='不成立', typ='input',
                                           return_type='boolean'),
                'nodeType': 'COMPARISON',
                'path': pos,
                'left': inner,
                'returnType': 'boolean',
            }
        kind = node['kind']
        f = node['field']
        if kind == 'boolean_belong':
            pv = {
                'param1': self._subject_node(f, None, pos, abs_method=True),
                'param2': node.get('p2') or {'label': '是', 'value': None, 'key': 'true'},
            }
            return self._expr('BOOLEAN_BELONG_TO', pos, pv, node.get('subtree', False))
        if kind == 'str_include':
            pv = {'param1': self._subject_node(f, None, pos), 'param2': node['value']}
            return self._expr('STR_INCLUDE', pos, pv, node.get('subtree', False))
        if kind == 'str_include_list':
            p2 = [item if item == ',' else self._field_node(item) for item in node['values']]
            pv = {'param1': self._subject_node(f, None, pos), 'param2': p2}
            return self._expr('STR_INCLUDE', pos, pv, node.get('subtree', False))
        if kind == 'str_not_include':
            pv = {'param1': self._subject_node(f, None, pos), 'param2': node['value']}
            return self._expr('STR_NOT_INCLUDE', pos, pv, node.get('subtree', False))
        if kind == 'str_not_include_list':
            p2 = [item if item == ',' else self._field_node(item)
                  for item in node['values']]
            pv = {'param1': self._subject_node(f, None, pos), 'param2': p2}
            return self._expr('STR_NOT_INCLUDE', pos, pv, node.get('subtree', False))
        if kind == 'str_belong':
            v = node['values']
            if isinstance(v, list):
                p2 = [item if item == ',' else self._field_node(item) for item in v]
            else:
                p2 = v
            pv = {'param1': self._subject_node(f, None, pos), 'param2': p2}
            return self._expr('STR_BELONG_TO', pos, pv, node.get('subtree', False))
        if kind == 'str_not_belong':
            v = node['values']
            if isinstance(v, list):
                p2 = [item if item == ',' else self._field_node(item) for item in v]
            else:
                p2 = v
            pv = {'param1': self._subject_node(f, None, pos), 'param2': p2}
            return self._expr('STR_NOT_BELONG_TO', pos, pv, node.get('subtree', False))
        if kind in ('not_empty', 'empty'):
            is_method = isinstance(f, dict) and 'method' in f
            ftype = (f.get('type') or '').lower() if isinstance(f, dict) else ''
            if kind == 'not_empty':
                if ftype == 'list':
                    eid = 'COLLECTION_NOT_EMPTY'
                elif is_method or f.get('type') != 'object':
                    eid = 'STR_NOT_EMPTY'
                else:
                    eid = 'OBJECT_NOT_EMPTY'
            else:
                eid = 'COLLECTION_IS_EMPTY' if ftype == 'list' else 'STR_EMPTY'
            return self._expr(eid, pos, {'param1': self._subject_node(f, None, pos)},
                              node.get('subtree', False))
        if kind == 'coll_op':
            # COLLECTION_* 系（运算符924 档1）：param2 = 字面串或字段引用列表
            v = node.get('values', node.get('value'))
            if isinstance(v, list):
                p2 = [item if isinstance(item, str) else self._field_node(item)
                      for item in v]
            elif isinstance(v, dict):
                p2 = self._field_node(v)
            else:
                p2 = v
            pv = {'param1': self._subject_node(f, None, pos), 'param2': p2}
            return self._expr(node['eid'], pos, pv, node.get('subtree', False))
        if kind == 'belong_single':
            code = 'STR_BELONG_TO' if str(node['op']).startswith('属于') \
                else 'STR_NOT_BELONG_TO'
            field_rec = f if isinstance(f, dict) and 'method' not in f else {}
            full = self._match_label(node['value']) if field_rec and 'selector_key' not in node else None
            if full is not None:
                # 全路径标签值: 数据集直接输出 FIELD 节点
                left = self._subject_node(f, pos + '.left', pos + '.left')
                right = self._field_node(full, pos + '.right')
                return {'code': code, 'label': node['op'], 'left': left,
                        'right': right, 'nodeType': 'COMPARISON', 'path': pos,
                        'returnType': 'boolean'}
            recv = self._resolve_dyn_value_code(node['value'], field_rec) \
                if field_rec and 'selector_key' not in node else None
            if 'selector_key' in node:
                key = node['selector_key']
            elif recv:
                key = recv['field'].rsplit('_', 1)[-1]
            else:
                key = node['value']
                if field_rec:
                    key = self._truncate_code_value(key, field_rec)
            right = {'type': 'selector', 'nodeType': 'CUSTOM', 'returnType': 'string',
                     'paramValue': {'key': key, 'label': node['value'], 'value': None},
                     'path': pos + '.right'}
            left = self._subject_node(f, pos + '.left', pos + '.left')
            return {'code': code, 'label': node['op'], 'left': left, 'right': right,
                    'nodeType': 'COMPARISON', 'path': pos, 'returnType': 'boolean'}
        if kind == 'size_of_list':
            op = node['op']
            code = {'大于': 'GT', '小于': 'LT', '大于等于': 'GE', '小于等于': 'LE',
                    '等于': 'EQ', '不等于': 'NE', '是': 'STR_EQ'}[op]
            left = {'label': '', 'nodeType': 'EXPRESSION', 'path': pos + '.left',
                    'expressionId': 'STR_LENGTH', 'returnType': 'int',
                    'codeService': None,
                    'paramValue': {'param1': self._subject_node(f, None, pos)}}
            if isinstance(node['value'], dict):
                right = self._field_node(node['value'], pos + '.right')
            else:
                right = self._custom_node(node['value'], pos + '.right',
                                          typ='input', return_type='int')
            return {'code': code, 'label': op, 'right': right,
                    'nodeType': 'COMPARISON', 'path': pos, 'left': left,
                    'returnType': 'boolean'}
        if kind == 'date_between':
            pv = {'param3': node['end'],
                  'param1': self._field_node(f, pos + '.left.right.left'),
                  'param2': node['start']}
            return self._expr('DATE_BETWEEN', pos, pv, node.get('subtree', False))
        if kind == 'between':
            # 介于 / 在…和…之间：按字段类型选区间运算符（运算符924 档1）。
            # 左侧算术（2026-09-24）时 field 为 nest 算术树：param1 出
            # NESTING/TREE_EX、区间类型按首项推断（与 cmp 分支同口径）
            if isinstance(f, dict) and 'nest' in f:
                t0 = f['nest'][0]
                ltype = (t0[1].get('type') or 'double') \
                    if t0[0] in ('field', 'method') else 'double'
                t = ltype.lower()
                p1 = self._nesting_node(f, pos + '.left.right.left', ltype)
            else:
                t = (f.get('type') or '').lower() if isinstance(f, dict) else ''
                p1 = self._field_node(f, pos + '.left.right.left')
            if t in ('double', 'int', 'integer', 'long', 'big_decimal',
                     'float', 'number'):
                eid = 'NUM_BETWEEN'
            elif t in ('date', 'datetime'):
                eid = 'DATE_BETWEEN'
            else:
                eid = 'TIME_BETWEEN'
            pv = {'param3': node['end'],
                  'param1': p1,
                  'param2': node['start']}
            return self._expr(eid, pos, pv, node.get('subtree', False))
        if kind in ('str_end_with', 'str_start_with',
                    'str_not_end_with', 'str_not_start_with'):
            pv = {'param1': self._subject_node(f, None, pos), 'param2': node['value']}
            eid = {'str_end_with': 'STR_END_WITH',
                   'str_start_with': 'STR_START_WITH',
                   'str_not_end_with': 'STR_NOT_END_WITH',
                   'str_not_start_with': 'STR_NOT_START_WITH'}[kind]
            return self._expr(eid, pos, pv, node.get('subtree', False))
        if kind in ('belong_mul', 'not_belong_mul'):
            code = 'STR_BELONG_TO_MUL' if kind == 'belong_mul' else 'STR_NOT_BELONG_TO_MUL'
            field_rec = node['field'] if isinstance(node['field'], dict) \
                and 'method' not in node['field'] else {}
            if field_rec:
                left = {
                    'classPath': field_rec['classPath'],
                    'codeService': field_rec.get('codeService'),
                    'field': field_rec['field'],
                    'fieldPath': field_rec['fieldPath'],
                    'label': field_rec['composed'],
                    'nodeType': 'FIELD',
                    'path': pos + '.left',
                    'returnType': field_rec['type'],
                    'type': field_rec['type'],
                }
            else:
                left = self._subject_node(node['field'], pos + '.left', pos + '.left')
            cs = field_rec.get('codeService')
            items = []
            for index, v in enumerate(node['items']):
                key = node['selector_keys'][index]
                items.append({'value': None,
                              'key': key,
                              'label': v})
            right = {'type': 'selector-multiple', 'nodeType': 'CUSTOM',
                     'returnType': 'string', 'paramValue': items, 'path': pos + '.right'}
            if cs:
                right['codeService'] = cs
            return {'code': code, 'label': node['op'], 'left': left, 'right': right,
                    'nodeType': 'COMPARISON', 'path': pos, 'returnType': 'boolean'}
        if kind == 'str_eq_rev':
            left = self._custom_node('""', pos + '.left', typ='input',
                                     return_type='string')
            right = self._field_node(node['field'], pos + '.right')
            return {'code': 'STR_EQ', 'label': '是', 'left': left, 'right': right,
                    'nodeType': 'COMPARISON', 'path': pos, 'returnType': 'boolean'}
        if kind in ('str_eq', 'str_ne', 'cmp'):
            if kind == 'cmp':
                op = node['op']
                code = {
                    '大于': 'GT', '小于': 'LT',
                    '大于等于': 'GE', '小于等于': 'LE', '等于': 'EQ', '不等于': 'NE',
                }[op]
                if isinstance(f, dict) and 'method' in f:
                    ftype = f['method']['type']
                elif isinstance(f, dict) and 'nest' in f:
                    t0 = f['nest'][0]
                    ftype = t0[1]['type'] if t0[0] == 'field' else 'double'
                else:
                    ftype = f['type']
                if ftype == 'date':
                    code = 'DATE_' + code
            else:
                op = '是' if kind == 'str_eq' else '不是'
                code = 'STR_EQ' if kind == 'str_eq' else 'STR_NE'
            left_sem = node['field']
            if isinstance(left_sem, dict) and 'nest' in left_sem:
                t0 = left_sem['nest'][0]
                ltype = t0[1]['type'] if t0[0] in ('field', 'method') else 'double'
                left = self._nesting_node(left_sem, pos + '.left', ltype)
                base_type = ltype
            elif isinstance(left_sem, dict) and 'method' in left_sem:
                left = self._subject_node(left_sem, pos + '.left', pos + '.left')
                base_type = left_sem['method']['type']
            else:
                left = self._field_node(left_sem, pos + '.left')
                base_type = left_sem['type']
            right_sem = node['value']
            if isinstance(right_sem, dict) and 'nest' in right_sem:
                right = self._nesting_node(right_sem, pos + '.right', base_type)
            elif isinstance(right_sem, dict) and 'method' in right_sem:
                right = self._subject_node(right_sem, pos + '.right', pos + '.right')
            elif isinstance(right_sem, dict):
                right = self._field_node(node['value'], pos + '.right')
            else:
                typ = 'date' if base_type == 'date' else 'input'
                right = self._custom_node(node['value'], pos + '.right',
                                          typ=typ, return_type=base_type)
            n = {
                'code': code,
                'label': op,
                'right': right,
                'nodeType': 'COMPARISON',
                'path': pos,
                'left': left,
                'returnType': 'boolean',
            }
            if node.get('subtree'):
                n['subTree'] = True
            return n
        raise ValueError('未知节点: %r' % node)

    # ---------- 算术嵌套 ----------
    def _nesting_node(self, nest, pos, rtype):
        if any(t[0] == 'method' for t in nest['nest']):
            arr = []
            for k, term in enumerate(nest['nest']):
                if k:
                    arr.append(self._tree_ex_calc_node(nest['ops'][k - 1], rtype))
                arr.append(self._operand_node(term, pos + '.nestingTree[%d]' % k,
                                              rtype))
            return {'arr': arr, 'nodeType': 'TREE_EX', 'path': pos,
                    'returnType': rtype}
        tree = self._build_calc(nest['nest'], nest['ops'], pos + '.nestingTree', rtype)
        return {'arr': None, 'nodeType': 'NESTING', 'path': pos,
                'nestingTree': tree, 'returnType': rtype}

    def _tree_ex_calc_node(self, op, rtype):
        code = ARITH_OPS[op]
        op = ARITH_CN.get(op, op)   # 手输符号词形归一为中文（label/desc 口径）
        return {
            'code': code, 'units': None, 'label': op,
            'nodeType': 'CALCULATE',
            'operator': code,
            'paramType': ARITH_PARAM_TYPE[code],
            'returnType': rtype,
            'desc': op,
            'element': 'input',
        }

    def _build_calc(self, terms, ops, path, rtype):
        if not ops:
            return self._operand_node(terms[0], path, rtype)
        left = self._build_calc(terms[:-1], ops[:-1], path + '.left', rtype)
        right = self._operand_node(terms[-1], path + '.right', rtype)
        return self._calc_node(ops[-1], left, right, path, rtype)

    def _calc_node(self, op, left, right, path, rtype):
        code = ARITH_OPS[op]
        op = ARITH_CN.get(op, op)   # 手输符号词形归一为中文（label/desc 口径）
        return {
            'code': code, 'units': None, 'label': op,
            'right': right,
            'nodeType': 'CALCULATE',
            'operator': code,
            'paramType': ARITH_PARAM_TYPE[code],
            'path': path,
            'left': left,
            'returnType': rtype,
            'desc': op,
            'element': 'input',
        }

    def _operand_node(self, term, path, rtype):
        kind = term[0]
        val = term[1]
        if kind == 'field':
            return self._field_node(val, path)
        if kind == 'method':
            return self._method_node(val, self._resolve_args(term[2]), path=path)
        if kind == 'group':
            # 括号组 → 内联子 CALCULATE 树（与左结合平铺模型一致，括号仅强制分组）。
            # 复合括号组（含运算符）须与逻辑括号组同口径发 subTree：平台重序列化嵌套
            # CALCULATE 依据该标记回显 ()，否则 MULTIPLY(ADD(A,B),1000) 会被按优先级
            # 平铺成 "A + B × 1000" 丢括号（2026-09-30）。单操作数括号组无运算符、
            # 括号冗余，不打标记。
            node = self._build_calc(val['terms'], val['ops'], path, rtype)
            if val['ops'] and isinstance(node, dict) and node.get('nodeType') == 'CALCULATE':
                node['subTree'] = True
            return node
        return {'type': 'input', 'nodeType': 'CUSTOM', 'path': path,
                'returnType': rtype, 'paramValue': val}

    # ---------- 对外接口 ----------
    def generate(self, desc):
        """desc(HTML 或纯文本) -> ruleContent dict"""
        return self.render(self.parse(desc))


def deep_diff(a, b, path='', out=None):
    if out is None:
        out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k not in a:
                out.append((path, 'MISSING_IN_GEN', k))
            elif k not in b:
                out.append((path, 'EXTRA_IN_GEN', k))
            else:
                deep_diff(a[k], b[k], path + '/' + str(k), out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path, 'LEN', len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            deep_diff(x, y, path + '[%d]' % i, out)
    else:
        if a != b:
            out.append((path, 'VAL', a, b))
    return out


def strip_fieldid(o):
    if isinstance(o, dict):
        return {k: (strip_fieldid(v) if k != 'fieldId' else '<id>') for k, v in o.items()}
    if isinstance(o, list):
        return [strip_fieldid(x) for x in o]
    return o


def norm_expr_type(o, in_selector=False):
    """平台自身输出不一致时归一化: expressionArray 的 type 带 ';'、多选 paramValue 项带不带 value。"""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k == 'type' and isinstance(v, str):
                out[k] = v.rstrip(';')
            elif k == 'paramValue' and o.get('type') == 'selector-multiple':
                out[k] = norm_expr_type(v, True)
            else:
                out[k] = norm_expr_type(v, in_selector)
        return out
    if isinstance(o, list):
        if in_selector:
            return [{kk: norm_expr_type(vv, True) for kk, vv in item.items() if kk != 'value'}
                    if isinstance(item, dict) else item for item in o]
        return [norm_expr_type(x, in_selector) for x in o]
    return o


def plain_desc(desc):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', desc)).strip()


def semantic_fp(o):
    """语义指纹: 忽略 fieldId、AND 结合形态(并且链拍平)、方法参数顺序、subTree。"""
    if isinstance(o, dict):
        t = o.get('nodeType')
        if t == 'LOGICAL':
            if o.get('label') == '并且':
                chain = []
                stack = [o]
                while stack:
                    n = stack.pop(0)
                    if isinstance(n, dict) and n.get('nodeType') == 'LOGICAL' \
                            and n.get('label') == '并且':
                        stack[0:0] = [n.get('left'), n.get('right')]
                    else:
                        chain.append(semantic_fp(n))
                return ('and', tuple(chain))
            return ('or', semantic_fp(o.get('left')), semantic_fp(o.get('right')))
        if t == 'EXPRESSION':
            return ('expr', o.get('expressionId'), semantic_fp(o.get('paramValue')))
        if t == 'COMPARISON':
            return ('cmp', o.get('code'), semantic_fp(o.get('left')),
                    semantic_fp(o.get('right')))
        if t == 'METHOD':
            pv = o.get('paramValue') or {}
            return ('method', o.get('methodName'),
                    tuple(sorted((k, semantic_fp(v)) for k, v in pv.items())))
        if t in ('FIELD', 'CUSTOM'):
            return (t, o.get('label') if t == 'FIELD' else o.get('paramValue'))
        return (t,)
    if isinstance(o, list):
        return tuple(semantic_fp(x) for x in o)
    return o


def fp_equal(a, b):
    """语义指纹等价比较: 额外容忍 CUSTOM 的 'null' 与任意值相等(日期值缺失)。"""
    if isinstance(a, tuple) and isinstance(b, tuple):
        if len(a) == 2 and len(b) == 2 and a[0] == b[0] == 'CUSTOM':
            return a[1] == 'null' or b[1] == 'null' or a[1] == b[1]
        return len(a) == len(b) and all(fp_equal(x, y) for x, y in zip(a, b))
    return a == b


def main():
    ap = argparse.ArgumentParser(description='ruleDesc -> ruleContent 生成器')
    ap.add_argument('--bom', required=True, help='BOM 表 json 路径(必填)')
    ap.add_argument('--desc', help='单条规则描述(HTML 或纯文本)')
    ap.add_argument('--predefine', help='预定义 content 列表文件(json 数组或 jsonl), 供 --desc 使用')
    ap.add_argument('--samples', help='samples.jsonl 路径')
    ap.add_argument('--test', action='store_true', help='回归测试: 与样例 ruleContent 比对(忽略 fieldId)')
    ap.add_argument('--out', help='批量生成时输出 jsonl 路径')
    ap.add_argument('--line', type=int, help='只跑 samples 中第 N 条(1-based); 与 --test/--out 配合')
    args = ap.parse_args()

    if args.desc:
        predefines = []
        if args.predefine:
            with open(args.predefine, encoding='utf-8') as f:
                data = json.load(f)
            predefines = [x.get('content') if isinstance(x, dict) else x for x in data]
        gen = RuleGenerator(args.bom, predefines=predefines)
        rc = gen.generate(args.desc)
        print(json.dumps(rc, ensure_ascii=False, indent=1))
        return

    if not args.samples:
        ap.error('需要 --desc 或 --samples')

    with open(args.samples, encoding='utf-8') as f:
        samples = [json.loads(l) for l in f if l.strip()]
    if args.line:
        if not 1 <= args.line <= len(samples):
            ap.error('--line 超出范围(共 %d 条)' % len(samples))
        samples = [samples[args.line - 1]]
    gen = RuleGenerator(args.bom)

    if args.test:
        ok = 0
        date_loss = 0
        struct_loss = 0
        errs = {}
        diffs = []
        for i, s in enumerate(samples):
            ir = s.get('if_rule') or {}
            desc = plain_desc(ir.get('ruleDesc') or '')
            gt = json.loads(ir['ruleContent'])
            try:
                gen_rc = gen.generate_with_predefines(
                    desc, [(s.get('predefine') or {}).get('content')])
            except Exception as e:
                key = str(e).split('@')[0].strip()
                errs[key] = errs.get(key, 0) + 1
                diffs.append((i, desc, 'PARSE_ERROR: %s' % e))
                continue
            d = deep_diff(norm_expr_type(strip_fieldid(gen_rc)),
                          norm_expr_type(strip_fieldid(gt)))
            val_diffs = [x for x in d if len(x) == 4]
            # 日期值缺失: desc 显示 null 而真实值是日期, 无法从 desc 复原
            def is_date_null(x):
                p, t, a, b = x
                return t == 'VAL' and p.endswith('/paramValue') and a == 'null' \
                    and isinstance(b, str)

            def is_order_swap(x):
                p, t, a, b = x
                return t == 'VAL' and '/paramValue/param' in p \
                    and p.endswith('/paramValue') and isinstance(a, str) \
                    and isinstance(b, str)

            order_swaps = [x for x in val_diffs if is_order_swap(x)]
            date_nulls = [x for x in val_diffs if is_date_null(x)]
            # 数据自身不一致: 全部差异是 日期值缺失 或 方法参数顺序不同
            data_artifact = (len(d) > 0 and len(val_diffs) == len(d)
                             and len(order_swaps) + len(date_nulls) == len(val_diffs)
                             and sorted(x[2] for x in order_swaps)
                             == sorted(x[3] for x in order_swaps))
            # 结构差异但语义等价(AND 结合形态等编辑过程产物)
            struct_artifact = (len(d) > 0
                               and fp_equal(semantic_fp(norm_expr_type(strip_fieldid(gen_rc))),
                                            semantic_fp(norm_expr_type(strip_fieldid(gt)))))
            if not d:
                ok += 1
            elif data_artifact:
                date_loss += 1
            elif struct_artifact:
                struct_loss += 1
            else:
                diffs.append((i, desc, d))
        print('=' * 80)
        print('回归结果: 完全一致(除 fieldId) %d/%d' % (ok, len(samples)))
        if date_loss:
            print('数据自身不一致忽略(日期值缺失/方法参数顺序): %d' % date_loss)
        if struct_loss:
            print('结构差异但语义等价(AND 结合形态): %d' % struct_loss)
        if errs:
            print('解析错误:')
            for k, v in sorted(errs.items(), key=lambda x: -x[1]):
                print('  [%d] %s' % (v, k))
        print('差异样例数: %d' % len(diffs))
        for i, desc, d in diffs[:12]:
            print('-' * 80)
            print('[%d] %s' % (i, desc[:180]))
            if isinstance(d, str):
                print('   ', d)
            else:
                for x in d[:8]:
                    print('   ', x)
        return

    # 批量生成
    out_lines = []
    for s in samples:
        desc = plain_desc((s.get('if_rule') or {}).get('ruleDesc') or '')
        try:
            rc = gen.generate_with_predefines(
                desc, [(s.get('predefine') or {}).get('content')])
            out_lines.append(json.dumps({'ruleId': s.get('rule_id'),
                                         'ruleDesc': desc,
                                         'ruleContent': json.dumps(rc, ensure_ascii=False)},
                                        ensure_ascii=False))
        except Exception as e:
            out_lines.append(json.dumps({'ruleId': s.get('rule_id'),
                                         'ruleDesc': desc,
                                         'error': str(e)}, ensure_ascii=False))
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out_lines) + '\n')
        print('已写出 %d 条到 %s' % (len(out_lines), args.out))
    else:
        print('\n'.join(out_lines))


if __name__ == '__main__':
    sys.exit(main())
