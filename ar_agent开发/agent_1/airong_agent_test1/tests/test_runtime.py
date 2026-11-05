import asyncio
import json
from pathlib import Path

import httpx
import pytest

from langgraph_app.config import system_prompt
from langgraph_app.graph import bounded_messages
from langgraph_app.manager import RuntimeErrorBase, SessionManager
from langgraph_app.model import ChatModel, ModelError
from tools.bridge import NodeTools, RuleArgs

CONFIG = {'context_window': 32000, 'max_tokens': 2048}


class FakeModel:
    def __init__(self, cfg):
        pass

    async def generate(self, messages, emit, **kwargs):
        if messages[-1]['role'] == 'user' and messages[-1]['content'] == '阻塞':
            await asyncio.sleep(30)
        if messages[-1]['role'] == 'user':
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'call1', 'type': 'function', 'function': {'name': 'bom_search',
                 'arguments': '{"keyword":"机构","limit":1}'}}]}
        assert messages[-1]['role'] == 'tool'
        content = f"完成，已有{sum(m['role'] == 'user' for m in messages)}轮"
        emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': content}})
        return {'role': 'assistant', 'content': content}


class FakeTools:
    async def call(self, name, args):
        return {'content': [{'type': 'text', 'text': '保单的机构'}], 'details': {'items': []}}

    async def close(self):
        pass


async def finish(session, q):
    events = []
    while True:
        event = await asyncio.wait_for(q.get(), 10)
        events.append(event)
        if event['type'] == 'agent_settled':
            await session.task
            return events


@pytest.mark.asyncio
async def test_lifecycle_persistence_reset_and_abort(tmp_path):
    mgr = SessionManager(tmp_path, FakeModel, lambda: CONFIG)
    try:
        session = await mgr.ensure('test-session')
        session.tools = FakeTools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '查询机构')
        with pytest.raises(RuntimeErrorBase):
            await mgr.prompt(session.sid, '重复发送')
        events = await finish(session, q)
        assert [e['type'] for e in events].count('tool_execution_end') == 1
        assert not any(e.get('message', {}).get('error') for e in events)
        assert any(e.get('message', {}).get('content', [{}])[0].get('text') == '完成，已有1轮'
                   for e in events if e['type'] == 'message_end')
        await mgr.close(session.sid)
    finally:
        await mgr.stop_all()
    mgr = SessionManager(tmp_path, FakeModel, lambda: CONFIG)
    try:
        session = await mgr.ensure('test-session')
        session.tools = FakeTools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '继续')
        events = await finish(session, q)
        assert any('已有2轮' in json.dumps(e, ensure_ascii=False) for e in events)
        await mgr.prompt(session.sid, '阻塞')
        await asyncio.sleep(0.1)
        await mgr.abort(session.sid)
        events = await finish(session, q)
        assert events[-1]['type'] == 'agent_settled'
        await mgr.prompt(session.sid, '取消后继续')
        events = await finish(session, q)
        assert any('已有3轮' in json.dumps(e, ensure_ascii=False) for e in events)
        await mgr.new_session(session.sid)
        await mgr.prompt(session.sid, '/skill:rule-authoring 查询')
        events = await finish(session, q)
        assert any('已有1轮' in json.dumps(e, ensure_ascii=False) for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_guardrails_capacity_and_failure_settles(tmp_path):
    mgr = SessionManager(tmp_path, FakeModel, lambda: CONFIG)
    try:
        for i in range(10):
            (await mgr.ensure(f's{i}')).subscribe()
        with pytest.raises(RuntimeErrorBase):
            await mgr.ensure('overflow')
        with pytest.raises(RuntimeErrorBase):
            await mgr.ensure('../escape')
        with pytest.raises(RuntimeErrorBase):
            await mgr.prompt('s0', '!whoami')
        with pytest.raises(RuntimeErrorBase):
            await mgr.prompt('s0', '/skill:missing hello')
    finally:
        await mgr.stop_all()
    assert '字段只能来自 BOM' in system_prompt()
    with pytest.raises(ValueError):
        RuleArgs.model_validate({'keyword': '规则', 'bomBaseInfoId': 'other'})
    with pytest.raises(ValueError):
        bounded_messages([{'role': 'user', 'content': '长文档' * 100}], 50)
    tools = NodeTools()
    with pytest.raises(ValueError):
        await tools.call('bash', {'command': 'whoami'})


@pytest.mark.asyncio
async def test_real_bom_adapter_matches_original():
    from langgraph_app.config import PROJECT_DIR
    tools = NodeTools()
    try:
        result = await tools.call('bom_search', {'keyword': '机构', 'limit': 3})
        assert not result.get('isError')
        proc = await asyncio.create_subprocess_exec('node', 'tools/bom-search.mjs', '机构', '--limit', '3',
                                                  cwd=PROJECT_DIR, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        assert proc.returncode == 0
        assert result['content'][0]['text'].strip() == stdout.decode('utf-8').strip()
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_api_upload_and_session(monkeypatch, tmp_path):
    from backend import main
    mgr = SessionManager(tmp_path, FakeModel, lambda: CONFIG)
    monkeypatch.setattr(main, 'mgr', mgr)
    async with main.lifespan(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            assert (await client.get('/api/health')).json()['runtime'] == 'langgraph'
            assert (await client.get('/')).status_code == 200
            for asset in ('/app.js', '/styles.css', '/vendor/vue.global.prod.js'):
                assert (await client.get(asset)).status_code == 200
            assert (await client.post('/api/prompt', json={'sid': '', 'message': 'x'})).status_code == 400
            session = await mgr.ensure('api-session')
            session.tools = FakeTools()
            q = session.subscribe()
            assert (await client.post('/api/prompt', json={'sid': session.sid, 'message': '查询'})).json()['ok']
            await finish(session, q)
            for endpoint in ('abort', 'new_session', 'close'):
                assert (await client.post('/api/' + endpoint, json={'sid': session.sid})).json()['ok']
            upload = await client.post('/api/upload', files={'file': ('sample.txt', '测试条款'.encode())})
            assert upload.json()['text'] == '测试条款'


@pytest.mark.asyncio
async def test_sse_endpoint_delivers_settled(monkeypatch, tmp_path):
    from backend import main
    mgr = SessionManager(tmp_path, FakeModel, lambda: CONFIG)
    monkeypatch.setattr(main, 'mgr', mgr)
    try:
        session = await mgr.ensure('sse-session')
        session.tools = FakeTools()
        response = await main.events(session.sid)
        iterator = response.body_iterator
        assert 'event: hello' in await anext(iterator)
        await mgr.prompt(session.sid, '查询')
        events = []
        while True:
            frame = await asyncio.wait_for(anext(iterator), 10)
            event = json.loads(frame.removeprefix('data: ').strip())
            events.append(event)
            if event['type'] == 'agent_settled':
                break
        assert any(e['type'] == 'message_update' for e in events)
        await iterator.aclose()
        assert session.subscriber_count() == 0
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_streaming_model_tool_fragments(monkeypatch):
    payload = [
        {'choices': [{'delta': {'reasoning_content': '分析', 'tool_calls': [
            {'index': 0, 'id': 'c1', 'function': {'name': 'bom_search', 'arguments': '{"key'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'arguments': 'word":"机构"}'}}]}, 'finish_reason': 'tool_calls'}]},
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=''.join(
        'data: ' + json.dumps(p) + '\n\n' for p in payload) + 'data: [DONE]\n\n'))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    events = []
    model = ChatModel({'base_url': 'http://test/v1', 'model': 'mock', 'max_tokens': 100})
    result = await model.generate([{'role': 'user', 'content': '查询'}], events.append)
    assert json.loads(result['tool_calls'][0]['function']['arguments']) == {'keyword': '机构'}
    assert next(e for e in events if e['type'] == 'message_update')['assistantMessageEvent']['type'] == 'thinking_delta'


@pytest.mark.asyncio
async def test_failure_and_malformed_tool_settle(tmp_path):
    class Broken(FakeModel):
        async def generate(self, messages, emit, **kwargs):
            raise ModelError('模拟模型不可用')
    mgr = SessionManager(tmp_path, Broken, lambda: CONFIG)
    try:
        session = await mgr.ensure('failure')
        q = session.subscribe()
        await mgr.prompt(session.sid, '测试')
        events = await finish(session, q)
        assert events[-1]['type'] == 'agent_settled'
        assert any(e.get('message', {}).get('error') == '模拟模型不可用' for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_mcp_stdio_discovery_and_bom():
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from project_paths import PROJECT_DIR

    params = StdioServerParameters(command=sys.executable,
                                  args=['-m', 'mcp_service.server'], cwd=str(PROJECT_DIR))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            assert {tool.name for tool in listing.tools} == {'bom_search', 'rule_search'}
            result = await session.call_tool('bom_search', {'keyword': '机构', 'limit': 1})
            assert not result.isError
            assert 'qualifiedName' in str(result)


@pytest.mark.asyncio
async def test_context_summary_keeps_full_history(tmp_path):
    class SummaryModel(FakeModel):
        async def generate(self, messages, emit, **kwargs):
            if not kwargs.get('tools_enabled', True):
                return {'role': 'assistant', 'content': '保额阈值500万元，适用于设备险'}
            assert any('500万元' in m.get('content', '') for m in messages if m['role'] == 'system')
            return {'role': 'assistant', 'content': '按前述保额阈值继续'}
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph_app.graph import create_graph
    events = []
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'summary.sqlite')) as saver:
        graph = create_graph(SummaryModel({}), FakeTools(), saver, events.append, 24000, 2048)
        history = []
        for i in range(12):
            history.extend([{'role': 'user', 'content': '业务条件' * 180},
                            {'role': 'assistant', 'content': '已确认' * 150}])
        history.append({'role': 'user', 'content': '继续'})
        state = await graph.ainvoke({'messages': history, 'rounds': 0},
                                   {'configurable': {'thread_id': 'summary'}})
        assert len(state['messages']) == len(history) + 1
        assert state['summary'] and state['summarized_until'] > 0
        assert any(e['type'] == 'compaction_start' for e in events)


def test_legacy_branch_import(monkeypatch, tmp_path):
    from langgraph_app import manager
    monkeypatch.setattr(manager, 'PROJECT_DIR', tmp_path)
    root = tmp_path / 'config/pi-agent/sessions/project'
    root.mkdir(parents=True)
    entries = [
        {'type': 'session', 'id': 'old-session'},
        {'type': 'message', 'id': 'a', 'parentId': None, 'message': {'role': 'user', 'content': [{'type': 'text', 'text': '查询'}]}},
        {'type': 'message', 'id': 'b', 'parentId': 'a', 'message': {'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'call1', 'name': 'bom_search', 'arguments': {'keyword': '机构'}}]}},
        {'type': 'message', 'id': 'c', 'parentId': 'b', 'message': {'role': 'toolResult', 'toolCallId': 'call1', 'content': [{'type': 'text', 'text': '保单的机构'}]}},
        {'type': 'message', 'id': 'd', 'parentId': 'c', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '结果'}]}},
    ]
    path = root / 'old.jsonl'
    original = '\n'.join(json.dumps(e) for e in entries)
    path.write_text(original, encoding='utf-8')
    history = SessionManager.import_legacy('old-session')
    assert history[1]['tool_calls'][0]['function']['name'] == 'bom_search'
    assert history[2]['tool_call_id'] == 'call1'
    assert path.read_text(encoding='utf-8') == original
