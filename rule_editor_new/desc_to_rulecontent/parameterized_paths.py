"""BOM 方法返回对象的带参子字段路径；编辑校验与生成共用。"""
import re


def key(text):
    return re.sub(r'\s+', '', text).translate(str.maketrans('（），', '(),'))


class ParameterizedPaths:
    def __init__(self, records, methods):
        self.fields = {key(label): rec for label, rec in records.items()}
        self.templates = []
        for label, rec in records.items():
            fp = rec.get('fieldPath') or rec.get('field_path') or ''
            # 仅对象方法的子字段，普通字段名中的括号不是参数槽。
            if '().' not in fp or label.count('()') != 1:
                continue
            method = methods.get(fp.split('()', 1)[0] + '()')
            if method is None:
                continue
            head, tail = key(label).split('()', 1)
            self.templates.append((head, tail, dict(rec, _template_label=label), method))
    def match(self, text, pos=0, require_boundary=True):
        # 归一化时保留原文偏移；输出 label 与编辑器原文一致。
        chars, ends = [], []
        for i in range(pos, len(text)):
            if not text[i].isspace():
                chars.append(key(text[i]))
                ends.append(i + 1)
        normalized = ''.join(chars)
        best = None
        for head, tail, rec, method in self.templates:
            if not normalized.startswith(head + '('):
                continue
            start = len(head) + 1
            depth, end = 1, start
            while end < len(normalized) and depth:
                depth += (normalized[end] == '(') - (normalized[end] == ')')
                end += 1
            if depth or not normalized.startswith(tail, end):
                continue
            stop = end + len(tail)
            raw_end = ends[stop - 1]
            if require_boundary and raw_end < len(text) and not (text[raw_end].isspace() or text[raw_end] in '+-*/＋－＊／×÷)）'):
                continue
            args = normalized[start:end - 1]
            # 参数自身可能是带括号和逗号的 BOM 标签，先查完整路径再切槽。
            values = [args] if args in self.fields else args.split(',')
            details = method.get('detailsList') or [{'type': 'string', 'paramId': 'param1'}]
            if len(values) != len(details) or any(not v for v in values):
                continue
            resolved = []
            for value, detail in zip(values, details):
                entry = self.fields.get(value)
                actual = (entry or {}).get('type', '')
                expected = (detail.get('type') or '').rstrip(';').lower()
                numeric = {'int', 'integer', 'long', 'double', 'float', 'bigdecimal', 'short'}
                if entry is None:
                    if re.fullmatch(r'[-+]?\d+(?:\.\d+)?', value):
                        actual = 'double'
                    elif len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
                        actual = 'string'
                    else:
                        break
                if expected and actual and expected != actual.lower() and not {expected, actual.lower()} <= numeric:
                    break
                resolved.append(entry if entry is not None else value)
            else:
                if best is None or raw_end > best[0]:
                    best = (raw_end, dict(rec, _bound_args=resolved))
        return best

