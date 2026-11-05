"""Bounded local evaluation. Sends only case input; stores private outputs, no secrets."""
import asyncio
import hashlib
import json
import math
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from langgraph_app.authoring import delivery_issues
from langgraph_app.config import system_prompt
from langgraph_app.manager import SessionManager
from project_paths import PROJECT_DIR


def percentile(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * q) - 1)] if values else None


async def main():
    root = PROJECT_DIR / 'evals/private'
    cases = [json.loads(s) for s in (root / 'dataset.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    cases = [c for c in cases if c['task'] in ('rule_authoring', 'document_extraction')]
    output = root / ('authoring-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir()
    records = []
    # Separate checkpoints and fresh sessions, never touch real user histories.
    with tempfile.TemporaryDirectory(prefix='eval-', dir=output) as state:
        manager = SessionManager(Path(state))
        try:
            semaphore = asyncio.Semaphore(2)
            async def evaluate(case, repeat):
                async with semaphore:
                    sid = f"eval-{case['id']}-{repeat}"
                    session = await manager.ensure(sid)
                    queue = session.subscribe()
                    events, tools, evidence = [], [], set()
                    t0 = time.perf_counter()
                    first_status = first_text = None
                    status = 'success'
                    try:
                        await manager.prompt(sid, case['input'])
                        async with asyncio.timeout(300):
                            while True:
                                event = await queue.get()
                                t = time.perf_counter() - t0
                                events.append({'seconds': t, 'event': event})
                                if event['type'] == 'task_status' and first_status is None:
                                    first_status = t
                                if event['type'] == 'message_update' and event['assistantMessageEvent'].get('type') == 'text_delta' and first_text is None:
                                    first_text = t
                                if event['type'] == 'tool_execution_end':
                                    tools.append(event['toolName'])
                                    if not event.get('isError'): evidence.add(event['toolName'])
                                if event.get('message', {}).get('stopReason') == 'error': status = 'error'
                                if event['type'] == 'agent_settled': break
                    except TimeoutError:
                        status = 'timeout'
                        await manager.abort(sid)
                    elapsed = time.perf_counter() - t0
                    finals = [x['event']['message'] for x in events if x['event']['type'] == 'message_end' and not any(c.get('type') == 'toolCall' for c in x['event']['message'].get('content', []))]
                    text = ''.join(c.get('text', '') for c in finals[-1].get('content', []) if c.get('type') == 'text') if finals else ''
                    row = {'case_id': case['id'], 'task': case['task'], 'repeat': repeat + 1, 'status': status,
                           'status_seconds': first_status, 'first_visible_text_seconds': first_text,
                           'total_seconds': elapsed, 'tools': tools, 'output': text,
                           'delivery_issues': delivery_issues(text, evidence, require_source=case['task'] == 'document_extraction'), 'events': events}
                    records.append(row)
                    with (output / 'records.jsonl').open('a', encoding='utf-8') as file:
                        file.write(json.dumps(row, ensure_ascii=False) + '\n')
                    print(json.dumps({k: row[k] for k in ('case_id', 'repeat', 'status', 'total_seconds', 'delivery_issues')}, ensure_ascii=False), flush=True)
                    session.unsubscribe(queue)
                    await manager.close(sid)
            await asyncio.gather(*(evaluate(case, repeat) for case in cases for repeat in range(3)))
        finally:
            await manager.stop_all()
    grouped = defaultdict(list)
    for row in records: grouped[row['task']].append(row)
    summary = {'dataset_sha256': hashlib.sha256((root / 'dataset.jsonl').read_bytes()).hexdigest(),
               'prompt_sha256': hashlib.sha256(system_prompt().encode()).hexdigest(),
               'measurement': 'SessionManager event subscriber, real model/tools; excludes HTTP/SSE transport; status excluded from answer timing',
               'annotation_policy': 'User confirmed existing annotations approved for this evaluation',
               'semantic_scoring': 'Separate review required; delivery checks are not semantic accuracy', 'tasks': {}}
    for task, rows in grouped.items():
        summary['tasks'][task] = {'n': len(rows), 'success': sum(r['status'] == 'success' for r in rows),
            'delivery_pass': sum(r['status'] == 'success' and not r['delivery_issues'] for r in rows),
            'p50_seconds': percentile([r['total_seconds'] for r in rows], .5),
            'p95_observed_stop_seconds': percentile([r['total_seconds'] for r in rows], .95),
            'timeout_count': sum(r['status'] == 'timeout' for r in rows),
            'latency_note': 'Timeout observations are censored at 300 seconds; completed-task P95 cannot be inferred',
            'status_p95_seconds': percentile([r['status_seconds'] for r in rows if r['status_seconds'] is not None], .95)}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(output), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
