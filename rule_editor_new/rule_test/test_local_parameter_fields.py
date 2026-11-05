"""截图回归：固定动态域枚举、不同险种独立填值及 ADP 模板结构。"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from desc_to_rulecontent.rule_desc_to_content import RuleGenerator
from desc_to_rulecontent.write_rule_to_adp import upload_payload
from rule_test.rule_evaluator import Evaluator, parse


class LocalParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bom = str(Path(__file__).resolve().parents[1] / '新车险915/bom.json')

    def test_enums_are_fixed_without_input(self):
        rc = RuleGenerator(self.bom).generate(
            '保单的二级机构 是 动态域集的归属机构的重庆 并且 '
            '保单的保单类型 是 动态域集的保单类型的投保单')
        for proposal, expected in [('T', True), ('F', False)]:
            # 使用生成树中的真实字段路径，不在测试中猜测 BOM 命名。
            inputs = {}
            for clause, value in [(rc['left'], '0250010000000'), (rc['right'], proposal)]:
                obj = inputs
                parts = clause['left']['fieldPath'].split('.')
                for part in parts[:-1]:
                    obj = obj.setdefault(part, {})
                obj[parts[-1]] = value
            self.assertEqual(Evaluator(parse(rc), inputs).run()[0], expected)

    def test_parameter_values_and_upload_template(self):
        desc = ' 并且 '.join(
            f'保单的险别0, 险别(动态域集的险别代码的{coverage})的保额/限额(元)大于等于{amount}'
            for coverage, amount in [('第三者责任险', 1000000),
                                     ('车上人员责任险（驾驶员）', 10000)])
        rc = RuleGenerator(self.bom).generate(desc)
        fields = [rc['left']['left'], rc['right']['left']]
        results = {}
        for field, value in zip(fields, [1000000, 10000]):
            self.assertEqual(field['label'], '保单的险别0, 险别()的保额/限额(元)')
            params = field['exprNode']['paramValue']
            args = [[k, v['fieldPath']] for k, v in sorted(params.items())]
            key = field['fieldPath'] + '|' + json.dumps(args, ensure_ascii=False, separators=(',', ':'))
            results[key] = value
        self.assertEqual(len(results), 2)
        self.assertTrue(Evaluator(parse(rc), {'__method_results__': results}).run()[0])
        results[next(reversed(results))] = 9999
        self.assertFalse(Evaluator(parse(rc), {'__method_results__': results}).run()[0])
        self.assertFalse(Evaluator(parse(rc), {'appbase': {'getCoverage()': {'namt': 1000000}}}).run()[0])
        with patch('desc_to_rulecontent.write_rule_to_adp.http_json', return_value=(200, '{"success":true}')) as post:
            upload_payload({'ruleContent': rc}, 'LOCAL_ONLY', 'repo', 'cat',
                           'http://mock.invalid/save', 'mock-token', overwrite=False)
            sent = json.loads(post.call_args.args[3]['ruleContent'])
            self.assertEqual(sent, rc)


if __name__ == '__main__':
    unittest.main()
