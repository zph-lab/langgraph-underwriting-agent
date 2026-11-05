from langgraph_app.authoring import canonicalize_rule_syntax


def test_evidenced_names_and_membership_preserve_constants_and_logic():
    evidence = [{'details': {'items': [{'qualifiedName': '动态域集的保单类型的投保单'}]}}]
    answer = '''说明 {不改}
```text
如果:
    保单的保单类型 是 动态域集的保单类型的 投保单
    并且 ( 保单的项目名称 是 { "甲 {分部}", "乙 丙" } 中的一个 ) 不成立
那么:
    提示 "甲 {分部} 乙"
```
'''
    result = canonicalize_rule_syntax(answer, evidence)
    assert '是 动态域集的保单类型的投保单' in result
    assert '属于 "甲 {分部}", "乙 丙" 中的一个 ) 不成立' in result
    assert '说明 {不改}' in result and '提示 "甲 {分部} 乙"' in result
    assert canonicalize_rule_syntax(result, evidence) == result


def test_unknown_fields_methods_and_nonrules_unchanged():
    answer = '```\n如果:\n    未知对象的 值 是 3\n那么:\n    方法 {参数}\n```'
    assert canonicalize_rule_syntax(answer, []) == answer
    assert canonicalize_rule_syntax('示例 是 {甲,乙} 中的一个', []) == '示例 是 {甲,乙} 中的一个'


def test_fullwidth_and_negative_membership():
    answer = '```\n如果: 字段 不属于 ｛甲,乙｝ 中的一个\n那么: 动作\n```'
    assert '字段 不属于 甲,乙 中的一个' in canonicalize_rule_syntax(answer, [])
