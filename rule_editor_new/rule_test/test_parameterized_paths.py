"""当前 BOM 带参字段回归：仅离线读取，不写业务库，不访问 ADP。"""
import unittest
from pathlib import Path

from desc_to_rulecontent.rule_desc_to_content import RuleGenerator
from rule_editor.service import EditorService


class ParameterizedPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = EditorService()
        cls.bom = str(Path(__file__).resolve().parents[1] / '新车险915' / 'bom.json')

    def test_screenshot_fields(self):
        for coverage, amount in [('第三者责任险', '1000000'), ('车上人员责任险（驾驶员）', '10000')]:
            for comma in [', ', ',']:
                text = f'保单的险别0{comma}险别(动态域集的险别代码的{coverage})的保额/限额(元) 大于等于 {amount}'
                with self.subTest(text=text):
                    words = self.service.annotate(text)
                    self.assertTrue(all(w['valid'] for w in words), words)
                    self.assertEqual(words[0]['type'], 'field')
                    generated = RuleGenerator(self.bom).generate(text)
                    self.assertEqual(generated['left']['fieldPath'], 'appbase.getCoverage().namt')
                    self.assertEqual(generated['left']['exprNode']['paramValue']['param1']['nodeType'], 'FIELD')
                    self.assertTrue(generated['left']['exprNode']['paramValue']['param1']['label'].endswith(coverage))

    def test_invalid_parameter_and_field_stay_invalid(self):
        for path in [
            '保单的险别0, 险别(动态域集的险别代码的不存在险种)的保额/限额(元)',
            '保单的险别0, 险别()的不存在字段',
            '保单的险别0, 险别(100)的保额/限额(元)',
            '保单的险别0, 险别(动态域集的险别代码的第三者责任险的保额/限额(元)',
        ]:
            with self.subTest(path=path):
                self.assertTrue(any(not w['valid'] for w in self.service.annotate(path + ' 大于等于 100')))
                with self.assertRaises(ValueError):
                    RuleGenerator(self.bom).generate(path + ' 大于等于 100')

    def test_preview_compacted_parameterized_fields(self):
        # 前端 renderWords 将字段和比较符直接拼接；即使输入含空格也如此。
        for coverage, amount in [('第三者责任险', '1000000'),
                                 ('车上人员责任险（驾驶员）', '10000')]:
            path = f'保单的险别0, 险别(动态域集的险别代码的{coverage})的保额/限额(元)'
            for op in ['大于等于', '小于', '等于']:
                with self.subTest(coverage=coverage, op=op):
                    expected = RuleGenerator(self.bom).generate(f'{path} {op} {amount}')
                    actual = RuleGenerator(self.bom).generate(f'{path}{op}{amount}')
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual['left']['fieldPath'], 'appbase.getCoverage().namt')
                    param = actual['left']['exprNode']['paramValue']['param1']
                    self.assertEqual(param['nodeType'], 'FIELD')
                    self.assertTrue(param['label'].endswith(coverage))
            with self.assertRaises(ValueError):
                RuleGenerator(self.bom).generate(path + '不存在字段大于100')

    def test_paste_grouping_and_numeric_type(self):
        path = '保单的险别0, 险别(动态域集的险别代码的第三者责任险)的保额/限额(元)'
        pasted = self.service.normalize_pasted_rule('如果：' + path + '大于等于1000000')
        self.assertTrue(all(w['valid'] for w in self.service.annotate(pasted.splitlines()[-1])), pasted)
        for text in [f'( {path} 大于等于 1000000 ) 或者 {path} 小于 100',
                     f'{path} 加 100 大于 1000000',
                     path.replace('(', '（').replace(')', '）') + ' 大于等于 1000000']:
            self.assertTrue(all(w['valid'] for w in self.service.annotate(text)), text)
            RuleGenerator(self.bom).generate(text)
        self.assertTrue(any(not w['valid'] for w in self.service.annotate(path + ' 大于 abc')))


if __name__ == '__main__':
    unittest.main()
