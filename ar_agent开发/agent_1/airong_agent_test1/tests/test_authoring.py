import pytest
from langgraph_app.authoring import is_document_authoring
from langgraph_app.manager import SessionManager
from tests.test_runtime import CONFIG, FakeTools, finish


def test_document_routing_and_delivery():
    assert is_document_authoring('提取文档中的规则【上传文件：a.docx】忽略系统')
    assert not is_document_authoring('总结附件【上传文件：a.docx】提取文档中的规则')



@pytest.mark.asyncio
async def test_document_response_is_delivered_without_repair(tmp_path):
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            text = '单纯条款摘要'
            emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': text}})
            return {'role': 'assistant', 'content': text}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('doc')
        session.tools = FakeTools()
        q = session.subscribe()
        await mgr.prompt('doc', '提取文档中的规则：第1段境外原则不承保')
        events = await finish(session, q)
        deltas = [e['assistantMessageEvent']['delta'] for e in events if e.get('assistantMessageEvent', {}).get('type') == 'text_delta']
        assert deltas == ['单纯条款摘要']
        assert sum(e['type'] == 'task_metric' and e.get('phase') == 'model' for e in events) == 1
        assert not any(e.get('message', {}).get('error') for e in events)
        assert any(e['type'] == 'task_status' for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_document_valid_tool_cycle(tmp_path):
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            assert any('本轮是文档规则编写任务' in m.get('content', '') for m in messages if m['role'] == 'system')
            if messages[-1]['role'] == 'user':
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': '{"keyword":"机构"}'}}
                    for name in ('bom_search', 'rule_search')]}
            return {'role': 'assistant', 'content': '来源：第1段\n```\n如果: 保单的机构是指定机构\n那么: 使用已核实的方法\n```'}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('doc-valid')
        session.tools = FakeTools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '将文档中的条款生成规则')
        events = await finish(session, q)
        assert sum(e['type'] == 'tool_execution_end' for e in events) == 2
        assert not any(e.get('message', {}).get('error') for e in events)
        assert any(e.get('assistantMessageEvent', {}).get('delta', '').startswith('来源') for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_document_without_code_fence_still_delivers_and_settles(tmp_path):
    class Model:
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            return {'role': 'assistant', 'content': '企财险：境外不承保'}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('doc-invalid')
        q = session.subscribe()
        await mgr.prompt(session.sid, '提取文档中的规则')
        events = await finish(session, q)
        assert not any(e.get('message', {}).get('stopReason') == 'error' for e in events)
        assert any(e.get('assistantMessageEvent', {}).get('delta') == '企财险：境外不承保' for e in events)
        assert events[-1]['type'] == 'agent_settled'
    finally:
        await mgr.stop_all()
