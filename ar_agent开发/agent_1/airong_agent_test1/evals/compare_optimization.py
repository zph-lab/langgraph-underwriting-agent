"""Private, bounded before/after measurement. Run from project root with -m evals.compare_optimization.

Default: replay historical tool evidence through the deployed tokenizer (no generation).
--live: additionally run UW-025 and UW-029 once per arm, 300s per task, concurrency 1.
The baseline graph is reconstructed from this change's pre-edit behavior; no service restart.
"""
import argparse
import asyncio
import hashlib
import json
import os
import time
import types
from urllib.parse import urlsplit

import httpx

from langgraph_app import manager as runtime
from langgraph_app.config import model_config, system_prompt
from langgraph_app.context import encoded_size, model_messages
from langgraph_app.authoring import delivery_issues
from tools.bridge import tool_schemas
from project_paths import PROJECT_DIR


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def metric_values(text):
    # Deliberately omit labels (including model names) and all non-metric content.
    values = {}
    for line in text.splitlines():
        if not line.startswith('vllm:') or '_created{' in line:
            continue
        name = line.split('{', 1)[0].split(' ', 1)[0]
        if name.endswith('_bucket'):
            continue
        try:
            values[name] = values.get(name, 0) + float(line.rsplit(' ', 1)[1])
        except ValueError:
            pass
    return values


def baseline_factory():
    """Exact pre-edit graph control flow, reconstructed with checked replacements."""
    source = (PROJECT_DIR / 'langgraph_app/graph.py').read_text(encoding='utf-8')
    def replace_once(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError('图代码已变化，必须重新审核基线重建，不能静默继续测量')
        source = source.replace(old, new, 1)
    start = source.index('        raw_size = encoded_size(messages)')
    end = source.index('        started = time.perf_counter()', start)
    source = source[:start] + source[end:]
    start = source.index("            if not kwargs.get('tools_enabled', True)")
    end = source.index('            succeeded = True', start)
    source = source[:start] + source[end:]
    replace_once("                  'modelRequests': model_requests, 'rawInputChars': raw_size,\n                  'modelInputChars': encoded_size(messages), 'requestChars': size,\n", '')
    replace_once("                  'inputTokenUpperBound': token_bound,\n", '')
    replace_once("projected = model_messages(state['messages'])", "projected = state['messages']")
    replace_once('active_budget = budget - len(latest_prompt) - 6500', 'active_budget = budget')
    replace_once("finalize = document_task and (stalled_batches >= 2 or state.get('rounds', 0) >= 8)", 'finalize = False')
    module = types.ModuleType('langgraph_app._optimization_baseline')
    exec(compile(source, '<pre-optimization-graph>', 'exec'), module.__dict__)
    return module.create_graph, hashlib.sha256(source.encode()).hexdigest()


def paired_summary(rows):
    pairs = []
    for case in sorted({r['case_id'] for r in rows}):
        arms = {r['arm']: r for r in rows if r['case_id'] == case}
        if set(arms) != {'before', 'after'}:
            continue
        before, after = arms['before'], arms['after']
        completed = before['status'] == after['status'] == 'success'
        usage_complete = not before['usage_missing_calls'] and not after['usage_missing_calls']
        pairs.append({'case_id': case, 'both_completed': completed,
            'seconds_reduction_percent': (1 - after['seconds'] / before['seconds']) * 100 if completed else None,
            'input_token_reduction_percent': (1 - after['prompt_tokens'] / before['prompt_tokens']) * 100
                if usage_complete and before['prompt_tokens'] else None,
            'note': 'One run per arm. Negative reduction means regression; timeout is not completion latency.'})
    return pairs


async def main(live=False):
    cfg = model_config()
    # Direct internal-network measurements; never change machine proxy settings.
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    root = PROJECT_DIR / 'evals/private'
    output = root / ('optimization-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir()
    url = urlsplit(cfg['base_url'])
    origin = url.scheme + '://' + url.netloc
    headers = dict(cfg.get('headers', {}))
    if cfg.get('api_key'):
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    report = {'measurement': 'Same historical tool payloads, deployed /tokenize, no token estimates',
              'context_window_configured': cfg['context_window'], 'max_tokens_configured': cfg['max_tokens'],
              'live': [], 'replay': []}
    report['graph_sha256'] = hashlib.sha256((PROJECT_DIR / 'langgraph_app/graph.py').read_bytes()).hexdigest()
    async with httpx.AsyncClient(headers=headers, timeout=60, trust_env=False) as client:
        async def metrics():
            r = await client.get(origin + '/metrics')
            r.raise_for_status()
            return metric_values(r.text)
        report['server_before'] = await metrics()
        version = await client.get(origin + '/version')
        report['server_version'] = version.json().get('version')
        historical = [json.loads(s) for s in (root / 'authoring-20261026-095841/records.jsonl').read_text(encoding='utf-8').splitlines()]
        for row in historical:
            # A controlled evidence-only replay, not a reconstruction of original full requests.
            messages = [{'role': 'user', 'content': '对照已返回的字段和规则依据。'}]
            for wrapped in row['events']:
                event = wrapped['event']
                if event['type'] != 'tool_execution_end':
                    continue
                cid = event['toolCallId']
                messages.extend([{'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': cid, 'type': 'function', 'function': {'name': event['toolName'], 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': cid, 'content': json.dumps(event['result'], ensure_ascii=False)}])
            sizes, tokens = [], []
            for payload in (messages, model_messages(messages)):
                sizes.append(encoded_size(payload))
                response = await client.post(origin + '/tokenize', json={'model': cfg['model'],
                    'messages': payload, 'tools': tool_schemas()})
                response.raise_for_status()
                tokens.append(response.json()['count'])
            report['replay'].append({'case_id': row['case_id'], 'repeat': row['repeat'],
                'before_chars': sizes[0], 'after_chars': sizes[1],
                'before_tokens': tokens[0], 'after_tokens': tokens[1]})
        dump(output / 'report.json', report)
        print('replay saved: ' + str(output), flush=True)
        if live:
            original = runtime.create_graph
            baseline, source_hash = baseline_factory()
            report['baseline_graph_sha256'] = source_hash
            report['baseline_note'] = 'Pre-edit graph behavior reconstructed from checked source replacements; same current adapter with usage enabled in both arms.'
            cases = [json.loads(s) for s in (root / 'dataset.jsonl').read_text(encoding='utf-8').splitlines()]
            cases = [c for c in cases if c['id'] in ('UW-025', 'UW-029')]
            report['dataset_sha256'] = hashlib.sha256((root / 'dataset.jsonl').read_bytes()).hexdigest()
            report['prompt_sha256'] = hashlib.sha256(system_prompt().encode()).hexdigest()
            # Alternate order to reduce, but not eliminate, warm-cache/order confounding.
            for index, case in enumerate(cases):
                for arm in (('before', 'after') if index == 0 else ('after', 'before')):
                    runtime.create_graph = baseline if arm == 'before' else original
                    state_dir = output / (case['id'] + '-' + arm)
                    mgr = runtime.SessionManager(state_dir)
                    events = []
                    started = time.perf_counter()
                    before = await metrics()
                    status = 'success'
                    try:
                        session = await mgr.ensure('compare')
                        queue = session.subscribe()
                        await mgr.prompt(session.sid, case['input'])
                        async with asyncio.timeout(300):
                            while True:
                                event = await queue.get()
                                events.append({'seconds': time.perf_counter() - started, 'event': event})
                                if event.get('message', {}).get('stopReason') == 'error':
                                    status = 'error'
                                if event['type'] == 'agent_settled':
                                    break
                    except TimeoutError:
                        status = 'timeout'
                    finally:
                        elapsed = time.perf_counter() - started
                        await mgr.stop_all()
                        runtime.create_graph = original
                    after = await metrics()
                    diagnostics = [x['event'] for x in events if x['event']['type'] == 'model_diagnostic' and x['event'].get('stage') == 'end']
                    model_calls = sum(x['event']['type'] == 'model_diagnostic' and x['event'].get('stage') == 'start' for x in events)
                    finals = [x['event']['message'] for x in events if x['event']['type'] == 'message_end'
                              and not any(c.get('type') == 'toolCall' for c in x['event']['message'].get('content', []))]
                    text = ''.join(c.get('text', '') for c in finals[-1].get('content', []) if c.get('type') == 'text') if finals else ''
                    evidence = {x['event']['toolName'] for x in events if x['event']['type'] == 'tool_execution_end' and not x['event'].get('isError')}
                    result = {'case_id': case['id'], 'arm': arm, 'status': status, 'seconds': elapsed,
                        'model_calls': model_calls, 'model_calls_ended': len(diagnostics),
                        'tool_requests': sum(x['event']['type'] == 'tool_execution_start' for x in events),
                        'input_chars_sum': sum(d.get('inputChars', 0) for d in diagnostics),
                        'prompt_tokens': sum((d.get('usage') or {}).get('prompt_tokens', 0) for d in diagnostics),
                        'peak_prompt_tokens': max(((d.get('usage') or {}).get('prompt_tokens', 0) for d in diagnostics), default=0),
                        'completion_tokens': sum((d.get('usage') or {}).get('completion_tokens', 0) for d in diagnostics),
                        'usage_missing_calls': model_calls - sum(d.get('usage') is not None for d in diagnostics),
                        'token_note': 'Sums cover calls with returned usage; a cancelled/incomplete call makes totals lower bounds.',
                        'length_count': sum(d.get('finishReason') == 'length' for d in diagnostics),
                        'delivery_issues': delivery_issues(text, evidence),
                        'server_metric_delta_shared': {k: after[k] - before[k] for k in after if k in before and k.endswith(('_total', '_sum', '_count'))}}
                    report['live'].append(result)
                    dump(state_dir / 'events.json', events)
                    dump(output / 'report.json', report)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
        report['server_after'] = await metrics()
    before = sum(r['before_tokens'] for r in report['replay'])
    after = sum(r['after_tokens'] for r in report['replay'])
    report['replay_summary'] = {'n': len(report['replay']), 'before_tokens': before,
        'after_tokens': after, 'token_reduction_percent': (1 - after / before) * 100,
        'note': 'Evidence-only replay, not actual end-to-end input-token savings or quality evaluation.'}
    report['paired_summary'] = paired_summary(report['live'])
    dump(output / 'report.json', report)
    print(json.dumps(report['replay_summary'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    asyncio.run(main(parser.parse_args().live))
