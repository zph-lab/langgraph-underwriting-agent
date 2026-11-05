import asyncio
import json

import pytest

from langgraph_app.context import model_messages, encoded_size, request_token_bound
from langgraph_app.manager import SessionManager
from tests.test_runtime import CONFIG, FakeTools, finish


def test_projection_preserves_evidence_and_raw_history():
    item = {'qualifiedName': '保单的险别的保额', 'fieldPath': 'policy.cover.amount',
            'type': 'double', 'params': [{'name': '金额', 'type': 'double'}]}
    raw = {'content': [{'type': 'text', 'text': '重复展示' * 500}], 'details': {'items': [item]}}
    messages = [{'role': 'user', 'content': '原文：不小于500万元，否则拒绝'},
                {'role': 'assistant', 'content': '', 'reasoning_content': '历史思考' * 500,
                 'tool_calls': [{'id': 'a', 'function': {'name': 'bom_search'}}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': json.dumps(raw, ensure_ascii=False)},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'b', 'function': {'name': 'bom_search'}}]},
                {'role': 'tool', 'tool_call_id': 'b', 'content': json.dumps(raw, ensure_ascii=False)}]
    snapshot = json.dumps(messages, ensure_ascii=False)
    projected = model_messages(messages)
    assert json.loads(projected[2]['content'])['details']['items'] == [item]
    assert json.loads(projected[4]['content'])['sameResultAs'] == 'a'
    assert projected[0] == messages[0]
    assert encoded_size(projected) < encoded_size(messages) / 2
    assert json.dumps(messages, ensure_ascii=False) == snapshot
    assert model_messages(projected) == projected
    third = {**messages[-1], 'tool_call_id': 'c'}
    triple = model_messages(messages + [
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c', 'function': {'name': 'bom_search'}}]}, third])
    assert model_messages(triple) == triple
    assert request_token_bound(projected) > encoded_size(projected)
    # References may not point into a previous turn that compaction can omit.
    second = model_messages(messages + [{'role': 'user', 'content': '继续'}, *messages[1:3]])
    assert 'details' in json.loads(second[-1]['content'])


def test_synonyms_share_evidence_but_preserve_query_metadata():
    raw = {'details': {'keyword': '甲', 'limit': 10, 'truncated': False,
                       'items': [{'qualifiedName': '保单的金额', 'type': 'double'}]}}
    other = {'details': {**raw['details'], 'keyword': '乙', 'limit': 20, 'truncated': True}}
    messages = [{'role': 'user', 'content': '查询'},
                {'role': 'assistant', 'tool_calls': [{'id': 'a', 'function': {'name': 'bom_search'}},
                                                     {'id': 'b', 'function': {'name': 'bom_search'}}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': json.dumps(raw)},
                {'role': 'tool', 'tool_call_id': 'b', 'content': json.dumps(other)}]
    projected = model_messages(messages)
    ref = json.loads(projected[-1]['content'])
    assert ref['sameResultAs'] == 'a'
    assert ref['query'] == {'keyword': '乙', 'limit': 20, 'truncated': True}
    assert projected == model_messages(projected)


def test_rule_projection_retains_all_branches_and_failed_results():
    text = '预定义: 当前险别\n如果: 不小于500万元\n那么: 动作一；动作二\n否则: 拒绝'
    raw = {'content': [{'type': 'text', 'text': text}], 'details': {'duplicated': text}}
    messages = [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'r', 'function': {'name': 'rule_search'}}]},
                {'role': 'tool', 'tool_call_id': 'r', 'content': json.dumps(raw)}]
    projected = model_messages(messages)
    assert json.loads(projected[-1]['content'])['content'][0]['text'] == text
    raw['isError'] = True
    messages[-1]['content'] = json.dumps(raw)
    twice = model_messages(messages + messages)
    assert all(json.loads(m['content']).get('isError') for m in twice if m['role'] == 'tool')


@pytest.mark.asyncio
async def test_repeated_conversion_search_converges_with_full_source(tmp_path):
    class Model:
        count = 0
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            self.count += 1
            assert any('500万元' in m.get('content', '') for m in messages)
            if not kwargs.get('tools_enabled', True):
                return {'role': 'assistant', 'content': '来源：用户提供的规则\n```\n如果: 保额不小于500万元\n那么: 转人工\n否则: 拒绝\n```\n待确认：字段与动作映射。'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': str(self.count), 'type': 'function', 'function': {
                    'name': 'bom_search', 'arguments': '{"keyword":"保额"}'}}]}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('converge')
        session.tools = FakeTools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '转换规则：保额不小于500万元转人工，否则拒绝')
        events = await finish(session, q)
        assert sum(e['type'] == 'tool_execution_end' for e in events) == 3
        assert not any(e.get('message', {}).get('error') for e in events)
        assert sum(e.get('phase') == 'model' and e['type'] == 'task_metric' for e in events) == 4
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_deadline_cancels_stream_closes_tools_and_settles(tmp_path):
    cancelled = []
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
    class Tools(FakeTools):
        closed = False
        async def close(self): self.closed = True
    mgr = SessionManager(tmp_path, Model, lambda: {**CONFIG, 'task_timeout_seconds': .05})
    try:
        session = await mgr.ensure('deadline')
        session.tools = Tools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '转换规则')
        events = await finish(session, q)
        assert cancelled and session.tools.closed
        assert any('总耗时上限' in e.get('message', {}).get('error', '') for e in events)
        assert events[-1]['type'] == 'agent_settled'
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_one_request_budget_does_not_trigger_delivery_repair(tmp_path):
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            return {'role': 'assistant', 'content': '不完整的摘要'}
    mgr = SessionManager(tmp_path, Model, lambda: {**CONFIG, 'model_request_limit': 1})
    try:
        session = await mgr.ensure('total-budget')
        q = session.subscribe()
        await mgr.prompt(session.sid, '转换规则：保额500万元')
        events = await finish(session, q)
        assert not any(e.get('message', {}).get('error') for e in events)
        assert sum(e['type'] == 'task_metric' and e.get('phase') == 'model' for e in events) == 1
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_long_response_does_not_trigger_another_full_request(tmp_path):
    calls = []
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            calls.append(True)
            return {'role': 'assistant', 'content': '无格式草稿' * 15000}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('repair-overflow')
        q = session.subscribe()
        await mgr.prompt(session.sid, '转换规则：金额500万元')
        events = await finish(session, q)
        assert len(calls) == 1
        assert not any(e.get('message', {}).get('error') for e in events)
        assert events[-1]['type'] == 'agent_settled'
    finally:
        await mgr.stop_all()
