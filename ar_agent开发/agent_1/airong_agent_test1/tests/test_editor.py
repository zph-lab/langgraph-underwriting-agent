import json

import httpx
import pytest

from langgraph_app.authoring import format_rule_answer, unbound_method_templates, canonicalize_setter_spacing
from langgraph_app.editor import RuleEditor, editor_parts
from langgraph_app.graph import create_graph


def test_rule_box_preserves_business_text_and_keeps_notes_outside():
    body = "如果:\n'当前险别'的代码 是 001\n那么:\n1. 设置金额为0"
    answer = '完整草稿：\n' + body + '\n\n待确认项：\n- 方法绑定'
    result = format_rule_answer(answer)
    assert '```\n' + body + '\n```' in result
    assert result.index('```', result.index('```') + 3) < result.index('待确认项')
    assert format_rule_answer(result) == result
    assert format_rule_answer('仅有摘要') == '仅有摘要'


def test_editor_protocol_folds_conditions_and_keeps_all_actions():
    parts = editor_parts('预定义:\n指定\'当前险别\'为某列表元素\n如果:\n金额 大于 500\n并且 (机构 是 001\n或者 机构 是 002)\n那么:\n1. 设置甲为0\n2. 设置乙为0\n否则:\n设置丙为1')
    assert '并且 (机构 是 001 或者 机构 是 002)' in parts['ruleDesc']
    assert parts['ruleDescRaw'].count('那么 ') == 2
    assert parts['thenText'] == '设置甲为0\n设置乙为0'
    assert parts['elseText'] == '设置丙为1'
    assert parts['predefineContent'] == "指定'当前险别'为某列表元素"


def test_unfilled_parent_method_is_not_confirmed_even_if_editor_accepts_it():
    evidence = [{'details': {'items': [{'ancestorMethods': [{
        'qualifiedName': '保单的险别0, 险别()', 'params': [{'type': 'string', 'ordinal': 0}]}]}]}}]
    assert unbound_method_templates('保单的险别0, 险别()的保额 大于 10000', evidence) == ['保单的险别0, 险别()']
    assert unbound_method_templates('保单的险别(已绑定参数)的保额 大于 10000', evidence) == []
    evidence[0]['details']['items'][0]['qualifiedName'] = '保单的险别0, 险别()的保额'
    bound = '保单的险别0, 险别(动态域集的险别代码的第三者责任险)的保额 大于 10000'
    assert unbound_method_templates(bound, evidence) == []
    assert unbound_method_templates(bound + '\n保单的险别0, 险别()的保额 大于 10', evidence) == ['保单的险别0, 险别()']


def test_setter_spacing_uses_verified_signature_and_preserves_parameter():
    evidence = [{'details': {'items': [{'isMethod': 1, 'type': 'void',
        'label': '设置保单的金额为()', 'params': [{'type': 'double', 'ordinal': 0}]}]}}]
    answer = '```\n如果:\n金额 大于 1\n那么:\n1. 设置 保单的金额 为 001\n设置保单的未知字段为0\n```\n说明：设置 保单的金额 为 1'
    result = canonicalize_setter_spacing(answer, evidence)
    assert '1. 设置保单的金额为001\n' in result
    assert '设置保单的未知字段为0' in result
    assert result.endswith('说明：设置 保单的金额 为 1')
    assert canonicalize_setter_spacing(answer, []) == answer


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['passed', 'invalid', 'auth', 'malformed'])
async def test_editor_never_saves_or_hides_rule_and_checks_explicit_success(monkeypatch, outcome):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/api/normalize_rule':
            return httpx.Response(200, json={'text': request.url.params['text']})
        assert request.url.path == '/api/generate_rule'
        payload = json.loads(request.content)
        assert payload['ruleDescRaw'] == '如果 金额 大于 500\n那么 设置费用为0'
        if outcome == 'auth':
            return httpx.Response(401, json={'detail': '未登录'})
        data = {'success': True, 'generated': {'ruleContent': {}}} if outcome == 'passed' else (
            {'success': False, 'invalidWords': [{'text': '金额'}]} if outcome == 'invalid' else {})
        return httpx.Response(200, json=data)
    client_class = httpx.AsyncClient
    monkeypatch.setattr('langgraph_app.editor.httpx.AsyncClient', lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw))
    answer = '```\n如果:\n金额 大于 500\n那么:\n设置费用为0\n```'
    reviewed, status = await RuleEditor({'url': 'http://editor'}).review(answer)
    assert answer in reviewed
    assert status == {'passed': 'passed', 'invalid': 'invalid'}.get(outcome, 'unavailable')
    assert calls == ['/api/normalize_rule', '/api/generate_rule']


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'expected'), [('same', 3), ('empty', 3), ('new', 4), ('error', 4)])
async def test_retrieval_stops_on_repeated_evidence_not_just_repeated_keywords(kind, expected):
    events = []
    class Model:
        count = 0
        async def generate(self, messages, emit, **kwargs):
            self.count += 1
            if not kwargs.get('tools_enabled', True):
                return {'role': 'assistant', 'content': '如果:\n金额 大于 500\n那么:\n设置费用为0'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': str(self.count), 'function': {'name': 'bom_search',
                    'arguments': json.dumps({'keyword': '词' + str(self.count)})}}]}
    class Tools:
        async def call(self, name, args):
            items = [] if kind == 'empty' else [{'qualifiedName': args['keyword'] if kind == 'new' else '同一个字段'}]
            return {'details': {'items': items}, 'isError': kind == 'error'}
    graph = create_graph(Model(), Tools(), None, events.append, 100000, 2048)
    result = await graph.ainvoke({'messages': [{'role': 'user', 'content': '转换规则：金额大于500，设置费用为0'}], 'rounds': 0})
    assert sum(e['type'] == 'tool_execution_end' for e in events) == expected
    assert result['messages'][-1]['content'].startswith('```')
