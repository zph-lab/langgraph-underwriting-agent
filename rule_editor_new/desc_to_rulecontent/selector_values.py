"""字典选择运算符：编辑、校验和生成共享 BOM 值域，不访问 ADP。"""
import re

SELECTOR_RE = re.compile(r'(不?属于)\s*[（(]\s*(单选|多选)\s*[）)]')
SELECTOR_OPS = {
    f'{prefix}（{mode}）': ('STR_NOT_BELONG_TO' if prefix == '不属于' else 'STR_BELONG_TO')
    + ('_MUL' if mode == '多选' else '')
    for prefix in ('属于', '不属于') for mode in ('单选', '多选')
}


def normalize_selectors(text):
    return SELECTOR_RE.sub(lambda m: m[1] + '（' + m[2] + '）', text)


class SelectorCatalog:
    def __init__(self, bom):
        self.fields = {}
        self.by_label = {}
        self.domains = {}

        def walk(node, ancestors=()):
            label = node.get('label') or ''
            labels = ancestors + (label,)
            composed = '的'.join(labels).removeprefix('业务模型对象的')
            fp = node.get('fieldPath') or ''
            if fp:
                self.fields[fp] = node
                self.by_label[composed] = node
            if fp.startswith('dynamicsAttributeList.') and node.get('type') == 'string':
                domain = (node.get('classPath') or '').rsplit('.', 1)[-1].lower()
                if domain and node.get('field') and node.get('dataType') != 'method':
                    self.domains.setdefault(domain, []).append({
                        'label': label, 'key': node['field'].rsplit('_', 1)[-1],
                        'fieldPath': fp,
                    })
            for child in node.get('children') or []:
                walk(child, labels)
        walk(bom)

    def options(self, field):
        cs = (field or {}).get('codeService') or ''
        name = re.sub(r'Service$', '', cs, flags=re.I).lower()
        # 车险平台服务使用 car 前缀，动态域类没有；精确匹配优先。
        options = self.domains.get(name)
        if options is None and name.startswith('car'):
            options = self.domains.get(name[3:])
        return options or []

    def resolve(self, field, value):
        hits = [o for o in self.options(field)
                if value in (o['label'], o['key'], o['fieldPath'])]
        keys = {o['key'] for o in hits}
        if len(keys) != 1:
            raise ValueError(f'值「{value}」不在字段对应字典中或名称不唯一')
        return hits[0]

    def values(self, field, op, raw):
        if not field or not field.get('codeService'):
            raise ValueError('字段没有 codeService，不能使用字典选择运算符')
        if not self.options(field):
            raise ValueError('当前 BOM 未提供该 codeService 的字典选项')
        raw = raw.strip()
        if '多选' in op:
            if not (raw.startswith('[') and raw.endswith(']')):
                raise ValueError('多选值须使用 [选项1,选项2]')
            values = re.split(r'[,，、]', raw[1:-1])
        else:
            if any(c in raw for c in '[],，、'):
                raise ValueError('单选只能选择一个值')
            values = [raw]
        if not all(v.strip() for v in values):
            raise ValueError('请选择字典值，不能包含空选项')
        result = []
        for value in values:
            option = self.resolve(field, value.strip())
            if option['key'] not in {o['key'] for o in result}:
                result.append(option)
        return result
