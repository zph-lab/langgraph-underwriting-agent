import json

import httpx
import pytest

from langgraph_app.model import ChatModel, ModelError
from langgraph_app.manager import SessionManager
from tests.test_runtime import CONFIG, finish


@pytest.mark.asyncio
@pytest.mark.parametrize('name,disabled', [('deepseek-v4-pro', True), ('mock', False)])
async def test_v4_explicit_non_thinking_request(monkeypatch, name, disabled):
    requests = []
    def respond(req):
        requests.append(json.loads(req.content))
        packet = {'choices': [{'delta': {'content': '规则正文'}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: ' + json.dumps(packet) + '\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    model = ChatModel({'base_url': 'http://test/v1', 'model': name, 'max_tokens': 8192})
    result = await model.generate([{'role': 'user', 'content': '转换规则'}], lambda e: None)
    assert result['content'] == '规则正文'
    assert bool(requests[0].get('tools'))
    if disabled:
        assert requests[0]['thinking'] == {'type': 'disabled'}
    else:
        assert 'thinking' not in requests[0]


@pytest.mark.asyncio
async def test_length_keeps_last_delta_and_diagnostics(monkeypatch):
    packets = [
        {'id': 'server-request-1', 'choices': [{'delta': {'reasoning_content': '模型返回的思考'}}]},
        {'choices': [{'delta': {'content': '未完成的规则草稿'}, 'finish_reason': 'length'}],
         'usage': {'prompt_tokens': 20, 'completion_tokens': 100, 'total_tokens': 120,
                   'completion_tokens_details': {'reasoning_tokens': 90}, 'private': 'secret'}},
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=''.join(
        'data: ' + json.dumps(p) + '\n\n' for p in packets)))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    events = []
    model = ChatModel({'base_url': 'http://test/v1', 'model': 'mock', 'max_tokens': 100})
    with pytest.raises(ModelError, match='长度截断'):
        await model.generate([{'role': 'user', 'content': '业务正文'}], events.append)
    stats = events[-1]
    assert stats['type'] == 'model_diagnostic' and stats['stage'] == 'end'
    assert stats['finishReason'] == 'length' and not stats['success']
    assert stats['responseId'] == 'server-request-1'
    assert stats['textChars'] == len('未完成的规则草稿')
    assert stats['thinkingChars'] == len('模型返回的思考')
    assert stats['usage']['reasoning_tokens'] == 90
    assert 'secret' not in json.dumps(stats) and '业务正文' not in json.dumps(stats)


@pytest.mark.asyncio
async def test_conversion_failure_keeps_process_and_logs_counters(tmp_path, caplog):
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            emit({'type': 'model_diagnostic', 'stage': 'end', 'finishReason': 'length',
                  'thinkingChars': 10, 'usage': None, 'requestId': 'local-request'})
            emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'thinking_delta', 'delta': '可查看的模型思考'}})
            emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': '未校验的规则正文'}})
            raise ModelError('模型输出截断')
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('diagnostics')
        q = session.subscribe()
        with caplog.at_level('INFO', logger='uvicorn.error'):
            await mgr.prompt(session.sid, '将此规则转换为业务口径')
            events = await finish(session, q)
        process = ''.join(e.get('assistantMessageEvent', {}).get('delta', '') for e in events
                          if e.get('assistantMessageEvent', {}).get('type') == 'process_delta')
        assert '可查看的模型思考' not in process and '未校验的规则正文' in process
        assert '生成中断' in process
        assert not any(e.get('assistantMessageEvent', {}).get('type') == 'text_delta' for e in events)
        assert events[-1]['type'] == 'agent_settled'
        assert 'local-request' in caplog.text and 'runId' in caplog.text
        assert '未校验的规则正文' not in caplog.text and '可查看的模型思考' not in caplog.text
    finally:
        await mgr.stop_all()
