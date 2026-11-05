# -*- coding: utf-8 -*-
"""本地规则求值器用例矩阵（基准规则 HHZ2026941103，期望值由规则中文文本人工推演）。

规则文本：
    保单的二级机构 是 动态域集的归属机构的新疆
    并且 保单的三级机构 是 0265220000000
    并且 ( ( 当前险别的险别代码 是 060051 且 保险金额 ≤ 3000000
             且 基准费率(‰) ≥ 2 且 打折系数(%) ≥ 75 )
        或 ( 当前险别的险别代码 是 060835 且 保险金额 ≤ 3000000
             且 基准费率(‰) ≥ 2 且 打折系数(%) ≥ 100 ) )

运行: python rule_test/test_rule_evaluator.py -v
"""
import json
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(DIR))

from rule_test.rule_evaluator import Evaluator, parse, render  # noqa: E402

with open(os.path.join(DIR, "_rulecontent_HHZ2026941103.json"), encoding="utf-8") as f:
    RC = json.load(f)
with open(os.path.join(DIR, "_input_HHZ2026941103.json"), encoding="utf-8") as f:
    SKELETON = json.load(f)

DYN_KEY = "Cdptcde_0265010000000"
OK_SUB = "0265010000000"      # 二级机构（与动态域集槽位相等）
OK_THIRD = "0265220000000"    # 三级机构


def elem(ccvrgno="060051", namt=3000000, nrate=2, nfloatrate=75):
    return {"ccvrgno": ccvrgno, "namt": namt, "nrate": nrate, "nfloatrate": nfloatrate}


def bom_input(elems, csubdpt=OK_SUB, cthirddpt=OK_THIRD, dyn=OK_SUB, raw_list=None):
    """挂载点=骨架同款 BOM 路径（coverage062008List）。"""
    return {
        "appbase": {
            "csubdpt": csubdpt,
            "cthirddpt": cthirddpt,
            "productLifeInfo06": {"coverage062008List": raw_list if raw_list is not None else elems},
        },
        "dynamicsAttributeList": {"Csubdpt": {DYN_KEY: dyn}},
    }


def pd_input(d, csubdpt=OK_SUB, cthirddpt=OK_THIRD, dyn=OK_SUB):
    """挂载点=ruleContent 原路径（规则预定义变量.当前险别）。"""
    return {
        "规则预定义变量": {"当前险别": d},
        "appbase": {"csubdpt": csubdpt, "cthirddpt": cthirddpt},
        "dynamicsAttributeList": {"Csubdpt": {DYN_KEY: dyn}},
    }


def evaluate(inp):
    return Evaluator(parse(RC), inp).run()


def report_text(inp):
    ok, rep, warns = evaluate(inp)
    return ok, "\n".join(render(rep)), warns


class Positives(unittest.TestCase):
    """正例：应命中。"""

    def test_p1_branch_060051_boundary(self):
        ok, _, _ = evaluate(bom_input([elem()]))  # nfloatrate=75 含边界
        self.assertTrue(ok)

    def test_p2_branch_060835(self):
        ok, _, _ = evaluate(bom_input([elem(ccvrgno="060835", nfloatrate=100)]))
        self.assertTrue(ok)

    def test_p3_nfloatrate_100_satisfies_both(self):
        ok, _, _ = evaluate(bom_input([elem(nfloatrate=100)]))
        self.assertTrue(ok)

    def test_p4_second_element_satisfies(self):
        ok, _, _ = evaluate(bom_input([elem(ccvrgno="060052", nfloatrate=100), elem()]))
        self.assertTrue(ok)

    def test_p5a_mount_on_predefine_path(self):
        ok, _, _ = evaluate(pd_input(elem()))
        self.assertTrue(ok)

    def test_p5b_mount_as_dict_not_list(self):
        # 列表字段填成单个 dict（结构搜索 dict 模式）
        ok, _, _ = evaluate(bom_input(None, raw_list=elem()))
        self.assertTrue(ok)


class Negatives(unittest.TestCase):
    """反例：应不命中，且报告恰好目标条件 ✗。"""

    def test_n1_wrong_third_dpt(self):
        ok, text, _ = report_text(bom_input([elem()], cthirddpt="0265999999999"))
        self.assertFalse(ok)
        self.assertIn("cthirddpt", text)

    def test_n2_namt_over_boundary(self):
        ok, _, _ = evaluate(bom_input([elem(namt=3000001)]))
        self.assertFalse(ok)

    def test_n3_nrate_below(self):
        ok, _, _ = evaluate(bom_input([elem(nrate=1.99)]))
        self.assertFalse(ok)

    def test_n4a_nfloatrate_74_branch1(self):
        ok, _, _ = evaluate(bom_input([elem(nfloatrate=74)]))
        self.assertFalse(ok)

    def test_n4b_nfloatrate_99_99_branch2(self):
        ok, _, _ = evaluate(bom_input([elem(ccvrgno="060835", nfloatrate=99.99)]))
        self.assertFalse(ok)

    def test_n5_unknown_ccvrgno(self):
        ok, _, _ = evaluate(bom_input([elem(ccvrgno="060052", nfloatrate=100)]))
        self.assertFalse(ok)

    def test_n6_dyn_field_mismatch(self):
        ok, _, _ = evaluate(bom_input([elem()], csubdpt="0265999999999"))
        self.assertFalse(ok)

    def test_n7_cross_element_trap(self):
        # 元素A 险别对但打折不足，元素B 打折足但险别不对 → 严禁跨元素拼条件
        ok, _, _ = evaluate(bom_input([
            elem(ccvrgno="060051", nfloatrate=74),
            elem(ccvrgno="060052", nfloatrate=100),
        ]))
        self.assertFalse(ok)

    def test_n8_unfilled_skeleton(self):
        # 真实 getInputByRuleId 骨架全 null → 不命中 + "未定位"诊断
        ok, rep, warns = Evaluator(parse(RC), json.loads(json.dumps(SKELETON))).run()
        self.assertFalse(ok)
        self.assertTrue(any("未在报文中定位" in w for w in warns), warns)


class BelongChineseSep(unittest.TestCase):
    """「属于…中的一个」候选串分隔符：中英文逗号/顿号/分号均可拆分。
    回归：候选 "060086，12749 ， 14680"（中文逗号）曾拆不开成单个整体串，
    导致填单值不命中、填整串反而命中（语义颠倒）。"""

    @classmethod
    def setUpClass(cls):
        cls.rc = {
            "nodeType": "EXPRESSION",
            "expressionId": "STR_BELONG_TO",
            "paramValue": {
                "param1": {"nodeType": "FIELD",
                           "fieldPath": "规则预定义变量.当前险别.ccvrgno",
                           "label": "当前险别的险别代码"},
                "param2": "060086，12749 ， 14680",
            },
        }

    def _eval(self, ccvrgno):
        inp = {"规则预定义变量": {"当前险别": {"ccvrgno": ccvrgno}}}
        return Evaluator(parse(self.rc), inp).run()[0]

    def test_single_first_hit(self):
        self.assertTrue(self._eval("060086"))

    def test_single_middle_hit(self):
        self.assertTrue(self._eval("12749"))

    def test_whole_string_not_hit(self):
        # 填整个候选串绝不应命中（修复前语义颠倒）
        self.assertFalse(self._eval("060086，12749 ， 14680"))

    def test_outside_not_hit(self):
        self.assertFalse(self._eval("999999"))

    def test_dunhao_and_semicolon_sep(self):
        for sep_str in ("060086、12749", "060086;12749", "060086；12749",
                        "060086,12749 "):
            rc = json.loads(json.dumps(self.rc))
            rc["paramValue"]["param2"] = sep_str
            inp = {"规则预定义变量": {"当前险别": {"ccvrgno": "12749"}}}
            ok, _, _ = Evaluator(parse(rc), inp).run()
            self.assertTrue(ok, sep_str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
