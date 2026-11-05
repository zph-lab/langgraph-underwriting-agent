import json

import pytest

from langgraph_app.manager import SessionManager
from tests.test_runtime import CONFIG, FakeTools, finish


@pytest.mark.asyncio
async def test_cache_is_per_turn_and_errors_are_retryable(tmp_path):
    class Tools(FakeTools):
        count = 0

        async def call(self, name, args):
            self.count += 1
            return {'content': [], 'isError': self.count == 1}

    class Model:
        def __init__(self, cfg):
            self.round = 0

        async def generate(self, messages, emit, **kwargs):
            self.round += 1
            if self.round > 3:
                return {'role': 'assistant', 'content': '完成'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': str(self.round), 'type': 'function', 'function': {
                    'name': 'bom_search', 'arguments': json.dumps({'keyword': '机构'})}}]}

    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('cache')
        session.tools = Tools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '查询')
        events = await finish(session, q)
        ends = [e for e in events if e['type'] == 'tool_execution_end']
        assert [e['cached'] for e in ends] == [False, False, True]
        assert session.tools.count == 2
        assert all(e['durationMs'] >= 0 for e in ends)
        assert len([e for e in events if e['type'] == 'task_metric' and e['phase'] == 'model']) == 4
        await mgr.prompt(session.sid, '下一轮')
        events = await finish(session, q)
        assert session.tools.count == 3
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_large_tool_batch_is_bounded_and_settles(tmp_path):
    class Model:
        def __init__(self, cfg): pass

        async def generate(self, messages, emit, **kwargs):
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': str(i), 'type': 'function', 'function': {
                    'name': 'bom_search', 'arguments': '{"keyword":"机构"}'}}
                for i in range(60)]}

    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('budget')
        session.tools = FakeTools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '查询')
        events = await finish(session, q)
        assert sum(e['type'] == 'tool_execution_start' for e in events) == 48
        assert any('预算' in e.get('message', {}).get('error', '') for e in events)
        assert events[-1]['type'] == 'agent_settled'
    finally:
        await mgr.stop_all()
