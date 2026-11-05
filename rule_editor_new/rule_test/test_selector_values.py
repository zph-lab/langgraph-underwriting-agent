"""字典选择回归：独立临时 BOM/SQLite，不改项目数据或访问 ADP。"""
import json
from pathlib import Path
import tempfile
import unittest

from desc_to_rulecontent.rule_desc_to_content import RuleGenerator
from desc_to_rulecontent.selector_values import SelectorCatalog
from rule_editor.core.textsearch import TextSearchService
from rule_editor.etl.bom_catalog import BomCatalog
from rule_test.rule_evaluator import Evaluator, parse


def node(label, field, fp, typ='string', **extra):
    return dict(label=label, field=field, fieldPath=fp, type=typ,
                fieldId=fp, path='model.' + fp, classPath='test.Input', **extra)


class SelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        policy = node('保单', 'appbase', 'appbase', 'object', children=[
            node('业务类型', 'kind', 'appbase.kind', codeService='carKindService'),
            node('机构', 'org', 'appbase.org', codeService='OrgService'),
            node('保费', 'premium', 'appbase.premium', 'double'),
        ])
        kind = node('业务类型', 'Kind', 'dynamicsAttributeList.Kind', 'object', children=[
            dict(node('转保', 'Value_2', 'dynamicsAttributeList.Kind.Value_2'), classPath='test.domain.Kind'),
            dict(node('新保', 'Value_3', 'dynamicsAttributeList.Kind.Value_3'), classPath='test.domain.Kind'),
        ])
        org = node('机构', 'Org', 'dynamicsAttributeList.Org', 'object', children=[
            dict(node('新保', 'Value_99', 'dynamicsAttributeList.Org.Value_99'), classPath='test.domain.Org'),
            dict(node('山西', 'Value_14', 'dynamicsAttributeList.Org.Value_14'), classPath='test.domain.Org'),
        ])
        cls.bom = dict(label='业务模型对象', children=[policy,
            node('动态域集', 'dynamicsAttributeList', 'dynamicsAttributeList', 'object', children=[kind, org])])
        cls.path = str(Path(cls.tmp.name) / 'bom.json')
        Path(cls.path).write_text(json.dumps(cls.bom, ensure_ascii=False), encoding='utf-8')
        cls.cat = BomCatalog(str(Path(cls.tmp.name) / 'catalog.sqlite'))
        cls.cat.build_from_bom(cls.path)
        cls.cat.build_from_db()
        cls.search = TextSearchService(cls.cat)
        cls.search._selectors = SelectorCatalog(cls.bom)

    @classmethod
    def tearDownClass(cls):
        cls.cat.close()
        cls.tmp.cleanup()

    def gen(self, text):
        return RuleGenerator(self.path).generate(text)

    def test_four_operators_and_codes(self):
        for prefix in ('属于', '不属于'):
            for mode in ('单选', '多选'):
                text = f'保单的业务类型 {prefix}（{mode}） ' + ('转保' if mode == '单选' else '[转保,新保]')
                with self.subTest(text=text):
                    result = self.gen(text)
                    expected = ('STR_BELONG_TO' if prefix == '属于' else 'STR_NOT_BELONG_TO') + ('_MUL' if mode == '多选' else '')
                    self.assertEqual(result['code'], expected)
                    self.assertTrue(all(w['valid'] for w in self.search.annotate(text)))
                    value = result['right']['paramValue']
                    self.assertEqual([x['key'] for x in value] if isinstance(value, list) else value['key'],
                                     ['2', '3'] if mode == '多选' else '2')

    def test_wrong_domain_and_invalid_values(self):
        for raw, mode in [('山西', '单选'), ('转保,新保', '单选'), ('[]', '多选'),
                          ('[转保,]', '多选'), ('[转保', '多选'), ('[山西]', '多选')]:
            text = f'保单的业务类型 属于（{mode}） {raw}'
            with self.subTest(text=text):
                self.assertTrue(any(not w['valid'] for w in self.search.annotate(text)))
                with self.assertRaises(ValueError):
                    self.gen(text)

    def test_same_label_uses_field_domain(self):
        self.assertEqual(self.gen('保单的业务类型 属于（单选） 新保')['right']['paramValue']['key'], '3')
        self.assertEqual(self.gen('保单的机构 属于（单选） 新保')['right']['paramValue']['key'], '99')

    def test_ascii_parentheses_and_chinese_separator(self):
        text = '保单的业务类型 不属于 ( 多选 ) [转保， 新保]'
        self.assertTrue(all(w['valid'] for w in self.search.annotate(text)))
        self.assertEqual(len(self.gen(text)['right']['paramValue']), 2)

    def test_mixed_condition_and_legacy(self):
        for text in ['保单的业务类型 属于（多选） [转保,新保] 并且 保单的保费 大于 100',
                     '保单的业务类型 属于（单选） 转保 或者 保单的保费 大于 100',
                     '保单的业务类型 属于 转保,新保 中的一个',
                     '保单的保费 在 10 和 100 之间']:
            with self.subTest(text=text):
                self.gen(text)
                self.assertTrue(all(w['valid'] for w in self.search.annotate(text)), self.search.annotate(text))

    def test_candidate_scope_and_context(self):
        line = '保单的业务类型 不属于（多选） [转保, 新保] 并且 保单的保费 大于 100'
        ctx = self.search.selector_context(line, line.index('转保'))
        self.assertEqual({o['key'] for o in ctx['options']}, {'2', '3'})
        self.assertEqual(line[ctx['start']:ctx['end']], '[转保, 新保]')
        self.assertFalse(self.search.selector_context(line, len(line))['active'])
        labels = [c['label'] for c in self.search.search('属于', '保单的业务类型')]
        self.assertIn('属于（单选）', labels)
        self.assertIn('属于（多选）', labels)

    def test_paste_and_missing_value(self):
        text = self.search.normalize_pasted_rule('如果：保单的业务类型不属于（多选）[转保,新保]')
        self.assertTrue(all(w['valid'] for w in self.search.annotate(text.splitlines()[-1])))
        self.assertTrue(any(not w['valid'] for w in self.search.annotate('保单的业务类型 属于（单选）')))
        with self.assertRaises(ValueError):
            self.gen('保单的业务类型 属于（多选） [转保]x')

    def test_generated_conditions_evaluate_codes(self):
        for prefix, expected in [('属于', True), ('不属于', False)]:
            for mode, raw in [('单选', '转保'), ('多选', '[转保,新保]')]:
                condition = self.gen(f'保单的业务类型 {prefix}（{mode}） {raw}')
                self.assertEqual(Evaluator(parse(condition), {'appbase': {'kind': '2'}}).run()[0], expected)
                self.assertEqual(Evaluator(parse(condition), {'appbase': {'kind': '9'}}).run()[0], not expected)

    def test_grouping_and_no_codeservice(self):
        text = '( 保单的业务类型 属于（多选） [转保,新保] ) 并且 保单的保费 大于 100'
        self.gen(text)
        self.assertTrue(all(w['valid'] for w in self.search.annotate(text)), self.search.annotate(text))
        self.assertNotIn('属于（单选）', [c['label'] for c in
                         self.search._field_operators(self.search._find_path_item('保单的保费'))])
        with self.assertRaises(ValueError):
            self.gen('保单的保费 属于（单选） 转保')


if __name__ == '__main__':
    unittest.main()
