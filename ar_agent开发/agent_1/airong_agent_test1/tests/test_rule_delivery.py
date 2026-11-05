import pytest

from langgraph_app.authoring import is_rule_authoring, is_rule_retrieval, render_retrieved_rules
from langgraph_app.manager import SessionManager
from tests.test_runtime import CONFIG, FakeTools, finish


RULE = '预定义: 指定当前险别来自险别列表\n如果: 机构是重庆并且日期早于2026-10-22\n那么:\n    1. 设置交强险费用为0\n    2. 设置商业险费用为0\n否则: 保留原费用'
RESULT = {'content': [{'type': 'text', 'text': '命中 150 条\n\nRULE_ID=YA_1\n' + RULE}],
          'details': {'items': [{'ruleId': 'YA_1', 'ruleName': '费用规则'}], 'overflow': True}}


def test_routing_and_complete_draft_contract():
    assert is_rule_retrieval('查一下现有规则里有哪些涉及保单的二级机构的')
    assert is_rule_authoring('如果机构是重庆，那么设置费用为0；请把此规则转换成我们的业务口径')
    assert not is_rule_retrieval('请转换规则【上传文件：x】查找现有规则')
    rendered = render_retrieved_rules([RESULT, RESULT])
    assert RULE in rendered
    assert rendered.count('预定义:') == 1
    assert '费用规则（YA_1）' in rendered
    assert '实际返回 1 条' in rendered
    assert '命中 150 条' not in rendered
    assert '上限限制' in rendered


@pytest.mark.asyncio
async def test_retrieval_bypasses_model_summary_and_persists_full_text(tmp_path):
    class Model:
        calls = 0
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            Model.calls += 1
            assert Model.calls == 1, 'no model summary after tool results'
            return {'role': 'assistant', 'content': '正在检索', 'tool_calls': [{
                'id': 'r', 'type': 'function', 'function': {'name': 'rule_search', 'arguments': '{"keyword":"机构"}'}}]}
    class Tools(FakeTools):
        async def call(self, name, args): return RESULT
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('full-retrieval')
        session.tools = Tools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '查找已有规则中的机构规则')
        events = await finish(session, q)
        finals = [e['message']['content'][0]['text'] for e in events if e['type'] == 'message_end']
        assert RULE in finals[-1]
        assert '来源：公司已有规则库' in finals[-1]
        assert not any(e.get('message', {}).get('error') for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_inline_conversion_delivers_without_format_gate(tmp_path):
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            text = RULE + '\n说明：按已有规则口径保留全部动作。'
            emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': text}})
            return {'role': 'assistant', 'content': text}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('inline-conversion')
        q = session.subscribe()
        await mgr.prompt(session.sid, RULE + '；请把此规则转换成我们的业务口径')
        events = await finish(session, q)
        deltas = [e['assistantMessageEvent']['delta'] for e in events if e.get('assistantMessageEvent', {}).get('type') == 'text_delta']
        assert len(deltas) == 1
        assert RULE in deltas[0] and deltas[0].endswith('按已有规则口径保留全部动作。')
        assert events[-1]['type'] == 'agent_settled'
    finally:
        await mgr.stop_all()
