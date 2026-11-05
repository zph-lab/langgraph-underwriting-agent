"""Manual real-model conversion review; uses configured read-only scope unchanged."""
import argparse
import asyncio
import html
import json
import re
import time
from pathlib import Path

from langgraph_app.config import model_config
from langgraph_app.graph import create_graph
from langgraph_app.model import ChatModel
from tools.bridge import NodeTools
from langgraph_app.editor import RuleEditor


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--final-only', action='store_true', help='replay final stage with fresh configured read-only evidence')
    args = parser.parse_args()
    document = Path(args.sample).read_text(encoding='utf-8')
    match = re.search(r'<pre class="content">(如果.*?)</pre>', document, re.S)
    if not match:
        raise ValueError('sample has no source rule')
    source = html.unescape(match[1])
    cfg = model_config()
    tools = NodeTools()
    events = []
    def emit(event):
        events.append(event)
        if event['type'] == 'task_metric' and event.get('phase') in ('model', 'tool'):
            print(json.dumps({k: event[k] for k in ('phase', 'modelRequests', 'durationMs', 'success', 'toolName') if k in event}), flush=True)
    graph = create_graph(ChatModel(cfg), tools, None, emit, cfg['context_window'], cfg['max_tokens'], cfg['model_request_limit'],
                         editor=RuleEditor(cfg['rule_editor']) if cfg.get('rule_editor') else None)
    started = time.perf_counter()
    report = {'scope': 'existing configured model and tool scope; unchanged'}
    try:
        async with asyncio.timeout(cfg['task_timeout_seconds']):
            messages = [{'role': 'user', 'content': source}]
            if args.final_only:
                queries = [('bom_search', word) for word in ('二级机构', '投保日期', '保单类型', '车辆种类', '保额/限额', '特种车型', '危化品')]
                queries += [('rule_search', word) for word in ('销售费用浮动率 指定值', '交强险 商业险 浮动率')]
                calls, replies = [], []
                for index, (name, keyword) in enumerate(queries):
                    cid = 'evidence-' + str(index)
                    params = {'keyword': keyword}
                    result = await tools.call(name, params)
                    if result.get('isError'):
                        raise ValueError('read-only evidence request failed')
                    calls.append({'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(params, ensure_ascii=False)}})
                    replies.append({'role': 'tool', 'tool_call_id': cid, 'content': json.dumps(result, ensure_ascii=False)})
                messages += [{'role': 'assistant', 'content': '已完成基础字段与参考规则检索。', 'tool_calls': calls}, *replies]
                report['mode'] = 'final-stage-only; fresh read-only evidence, not a full-task replay'
            state = await graph.ainvoke({'messages': messages, 'rounds': 4 if args.final_only else 0})
        report['answer'] = state['messages'][-1]['content']
        report['ruleDelivered'] = bool(re.search(r'^\s*如果\s*[:：]', report['answer'], re.M) and re.search(r'^\s*那么\s*[:：]', report['answer'], re.M))
        report['sourceFallback'] = any(e.get('phase') == 'source_fallback' for e in events)
        report['editorStatus'] = next((e.get('status') for e in reversed(events) if e.get('phase') == 'editor_validation'), None)
        report['unboundMethods'] = any(e.get('phase') == 'binding_validation' for e in events)
        report['success'] = (report['ruleDelivered'] and not report['sourceFallback'] and not report['unboundMethods']
                             and (not cfg.get('rule_editor') or report['editorStatus'] == 'passed'))
    except Exception as exc:
        report['success'] = False
        report['errorType'] = type(exc).__name__
        if type(exc).__name__ == 'ModelError':
            report['errorMessage'] = str(exc)
    finally:
        await tools.close()
        report['durationSeconds'] = round(time.perf_counter() - started, 2)
        report['metrics'] = [e for e in events if e['type'] == 'task_metric']
        report['progress'] = ''.join(e.get('assistantMessageEvent', {}).get('delta', '') for e in events
                                     if e.get('assistantMessageEvent', {}).get('type') == 'process_delta')
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'success': report['success'], 'durationSeconds': report['durationSeconds'],
                          'output': str(target), 'errorType': report.get('errorType'),
                          'ruleDelivered': report.get('ruleDelivered'), 'sourceFallback': report.get('sourceFallback')}), flush=True)
    if not report['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
