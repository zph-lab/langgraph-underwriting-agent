import json

import pytest

from langgraph_app.authoring import concise_progress, source_rule_fallback
from langgraph_app.manager import SessionManager
from tools.bridge import NodeTools
from tests.test_runtime import CONFIG, FakeTools, finish


def test_public_progress_is_short_and_excludes_draft():
    assert concise_progress('已找到保额字段。继续核对险别入参。多余说明。') == '已找到保额字段。继续核对险别入参。'
    assert concise_progress('如果: 保额大于500') == ''
    assert concise_progress('先核对绑定。```\n如果: 保额大于500') == '先核对绑定。'
    assert len(concise_progress('长说明' * 100)) <= 180


@pytest.mark.asyncio
async def test_bom_exposes_actual_parent_method_parameters(monkeypatch):
    from project_paths import PROJECT_DIR
    monkeypatch.setenv('BOM_SQLITE_PATH', str(PROJECT_DIR / 'bom/bom_非车.sqlite'))
    tools = NodeTools()
    try:
        result = await tools.call('bom_search', {'keyword': 'getCoverage090001().namt'})
        assert not result.get('isError')
        details = result['details']
        assert details['modelInfo']['model_name']
        leaf = next(x for x in details['items'] if x['fieldPath'] == 'appbase.getCoverage090001().namt')
        method = leaf['ancestorMethods'][-1]
        assert method['fieldPath'] == 'appbase.getCoverage090001()'
        assert method['type'] == 'object'
        assert method['params'] == [{'paramId': 'param1', 'type': 'string', 'ordinal': 0}]
        access = leaf['objectAccessTemplates'][0]
        assert access['template'] == method['qualifiedName'][:-2] + '(<param1:string>)' + leaf['qualifiedName'][len(method['qualifiedName']):]
        assert access['params'] == method['params']
        empty = await tools.call('bom_search', {'keyword': '肯定不存在的组合字段XYZ'})
        assert not empty['details']['items'] and empty['details']['modelInfo'] == details['modelInfo']
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_conversion_keeps_progress_separate_and_uses_binding_evidence(tmp_path):
    leaf = {'qualifiedName': '保单的险别0, 险别()的保额/限额(元)', 'type': 'double',
            'ancestorMethods': [{'qualifiedName': '保单的险别0, 险别()', 'type': 'object',
                                 'params': [{'paramId': 'param1', 'type': 'string', 'ordinal': 0}]}]}
    reference = '如果: 保单的险别(第三者责任险)的保额/限额(元) 大于等于 1000000\n那么: 设置费用为0'
    class Tools(FakeTools):
        async def call(self, name, args):
            if name == 'bom_search':
                return {'details': {'items': [leaf]}, 'content': []}
            return {'details': {'items': [{'ruleId': 'test'}]}, 'content': [{'type': 'text', 'text': reference}]}
    class Model:
        calls = 0
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            Model.calls += 1
            emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'thinking_delta', 'delta': '内部长篇推演' * 100}})
            if Model.calls <= 2:
                if Model.calls == 2:
                    bom = json.loads(messages[-1]['content'])['details']['items'][0]
                    assert bom['ancestorMethods'][0]['params'][0]['type'] == 'string'
                name, keyword = ('bom_search', '保额') if Model.calls == 1 else ('rule_search', '第三者责任险 保额')
                text = '核对保额字段。' if Model.calls == 1 else '已找到字段，继续核对险别参数绑定。'
                emit({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': text}})
                return {'role': 'assistant', 'content': text, 'tool_calls': [{
                    'id': str(Model.calls), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps({'keyword': keyword})}}]}
            assert reference == json.loads(messages[-1]['content'])['content'][0]['text']
            return {'role': 'assistant', 'content': '```\n' + reference + '\n```'}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('bound-conversion')
        session.tools = Tools()
        q = session.subscribe()
        await mgr.prompt(session.sid, '转换规则：第三者责任险保额大于等于1000000，设置费用为0')
        events = await finish(session, q)
        process = ''.join(e.get('assistantMessageEvent', {}).get('delta', '') for e in events
                          if e.get('assistantMessageEvent', {}).get('type') == 'process_delta')
        assert '已找到字段，继续核对险别参数绑定。' in process
        assert '返回 1 个候选' in process
        assert '内部长篇推演' not in process and '模型转换过程' not in process
        assert reference not in process
        finals = [e['message']['content'][0]['text'] for e in events if e['type'] == 'message_end']
        assert finals[-1] == '```\n' + reference + '\n```'
        assert '待确认' not in finals[-1] and Model.calls == 3
        assert not any(e.get('message', {}).get('error') for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(('user_request', 'batches'), [('请转换规则：保额大于500转人工', 4), ('请转换文档规则：保额大于500转人工', 8)])
async def test_conversion_search_batches_always_enter_final_output(tmp_path, user_request, batches):
    class Model:
        count = 0
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            self.count += 1
            if not kwargs.get('tools_enabled', True):
                assert self.count == batches + 1
                assert messages[-1]['role'] == 'user'
                assert '必须在本条回复直接交付完整规则正文' in messages[-1]['content']
                return {'role': 'assistant', 'content': '```\n如果: 保额大于500\n那么: 转人工\n```'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': str(self.count), 'type': 'function', 'function': {
                    'name': 'bom_search', 'arguments': json.dumps({'keyword': '保额' + str(self.count)})}}]}
    mgr = SessionManager(tmp_path, Model, lambda: {**CONFIG, 'context_window': 100000})
    try:
        session = await mgr.ensure('batch-bound')
        class FreshTools(FakeTools):
            async def call(self, name, args):
                return {'content': [], 'details': {'items': [{'qualifiedName': args['keyword']}]}}
        session.tools = FreshTools()
        q = session.subscribe()
        await mgr.prompt(session.sid, user_request)
        events = await finish(session, q)
        assert sum(e['type'] == 'tool_execution_end' for e in events) == batches
        assert any('那么: 转人工' in e.get('assistantMessageEvent', {}).get('delta', '') for e in events)
        assert not any(e.get('message', {}).get('error') for e in events)
    finally:
        await mgr.stop_all()


def test_source_fallback_preserves_all_conditions_actions_and_attachment_boundary():
    source = "如果\n    '当前保单'的机构是重庆\n    并且 (保额大于1000000 或者 保额等于10000)\n那么\n    设置交强险费用为0;\n    设置商业险费用为0;请把此规则转换成我们的业务口径"
    draft = source_rule_fallback(source, '我最后查一下动作写法。')
    assert "'当前保单'的机构是重庆" in draft
    assert '(保额大于1000000 或者 保额等于10000)' in draft
    assert '设置交强险费用为0;' in draft and '设置商业险费用为0' in draft
    assert '请把此规则转换' not in draft and '规范映射尚未完成' in draft
    assert source_rule_fallback(source, '如果: 规范条件\n那么: 规范动作') is None
    assert source_rule_fallback('转换文档规则【上传文件：a.txt】' + source, '过渡语') is None
    assert source_rule_fallback('请转换规则：保额大于500', '过渡语') is None


@pytest.mark.asyncio
async def test_transition_only_response_delivers_source_draft_without_repair(tmp_path):
    class Model:
        calls = 0
        def __init__(self, cfg): pass
        async def generate(self, messages, emit, **kwargs):
            Model.calls += 1
            return {'role': 'assistant', 'content': '最后再查一下已有规则中的动作。'}
    mgr = SessionManager(tmp_path, Model, lambda: CONFIG)
    try:
        session = await mgr.ensure('source-draft')
        q = session.subscribe()
        await mgr.prompt(session.sid, '如果:\n    金额大于500\n那么:\n    设置费用为0;请把此规则转换成我们的业务口径')
        events = await finish(session, q)
        assert Model.calls == 1
        final = next(e['assistantMessageEvent']['delta'] for e in events
                     if e.get('assistantMessageEvent', {}).get('type') == 'text_delta')
        assert '金额大于500' in final and '设置费用为0' in final
        assert '规范映射尚未完成' in final
        assert not any(e.get('message', {}).get('error') for e in events)
    finally:
        await mgr.stop_all()


@pytest.mark.asyncio
async def test_default_vehicle_bom_resolves_sample_fields_and_binding(monkeypatch):
    monkeypatch.delenv('BOM_SQLITE_PATH', raising=False)
    tools = NodeTools()
    try:
        for keyword, expected in [
            ('保单类型', '保单的保单类型'),
            ('使用性质子类', '保单的车辆信息的使用性质子类'),
            ('保费计算方式', '保单的保费计算方式'),
            ('业务员交强险销售费用浮动率指定值', '保单的业务员交强险销售费用浮动率指定值'),
            ('业务员商业险销售费用浮动率指定值', '保单的业务员商业险销售费用浮动率指定值'),
        ]:
            result = await tools.call('bom_search', {'keyword': keyword})
            assert not result.get('isError')
            assert 'NonVehicle' not in result['details']['modelInfo']['model_name']
            assert expected in [x['qualifiedName'] for x in result['details']['items']]
        result = await tools.call('bom_search', {'keyword': 'getCoverage().namt'})
        leaf = next(x for x in result['details']['items'] if x['fieldPath'] == 'appbase.getCoverage().namt')
        assert leaf['ancestorMethods'][0]['params'] == [{'paramId': 'param1', 'type': 'string', 'ordinal': 0}]
    finally:
        await tools.close()
