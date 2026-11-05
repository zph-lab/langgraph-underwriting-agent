"""Sequential independent HTTP/SSE evaluation against the running service."""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path
import httpx

OUT = Path(__file__).parent / ('four-rules-' + time.strftime('%Y%m%d-%H%M%S'))

async def main():
    OUT.mkdir(parents=True)
    source = Path('C:/Users/admin/Desktop/车险需求.txt').read_text(encoding='utf-8-sig')
    cases = [x.strip() for x in re.split(r'(?m)(?=^\s*如果\s*$)', source) if x.strip()]
    assert len(cases) == 4, len(cases)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8712', timeout=httpx.Timeout(660, connect=10), trust_env=False) as client:
        health = await client.get('/api/health')
        health.raise_for_status()
        (OUT / 'health.json').write_text(json.dumps(health.json(), ensure_ascii=False, indent=2), encoding='utf-8')
        for i, case in enumerate(cases, 1):
            sid = 'eval-four-' + uuid.uuid4().hex
            records = []
            started = None
            ack = None
            async with client.stream('GET', '/api/events', params={'sid': sid}) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if not line.startswith('data: '):
                        continue
                    event = json.loads(line[6:])
                    if started is None:
                        started = time.perf_counter()
                        response = await client.post('/api/prompt', json={'sid': sid, 'message': '请将以下规则转换为本系统规范规则，保留原有全部条件、逻辑关系和动作含义。\n\n' + case})
                        response.raise_for_status()
                        ack = time.perf_counter() - started
                        continue
                    records.append({'seconds': time.perf_counter() - started, 'event': event})
                    if event.get('type') == 'agent_settled':
                        break
            total = time.perf_counter() - started
            diagnostics = [r['event'] for r in records if r['event'].get('type') == 'model_diagnostic' and r['event'].get('stage') == 'end']
            usage = [e.get('usage') for e in diagnostics]
            texts = [r for r in records if r['event'].get('assistantMessageEvent', {}).get('type') in ('process_delta', 'text_delta') and r['event']['assistantMessageEvent'].get('delta')]
            finals = [r for r in records if r['event'].get('assistantMessageEvent', {}).get('type') == 'text_delta' and r['event']['assistantMessageEvent'].get('delta')]
            messages = [r['event']['message'] for r in records if r['event'].get('type') == 'message_end']
            answer = '\n\n'.join(''.join(p.get('text', '') for p in m.get('content', []) if p.get('type') == 'text') for m in messages if not m.get('tool_calls'))
            editors = [r['event'] for r in records if r['event'].get('phase') == 'editor_validation']
            result = {'case': i, 'sid': sid, 'source': case, 'answer': answer, 'http_ack_seconds': ack,
                      'first_visible_text_seconds': texts[0]['seconds'] if texts else None,
                      'final_response_seconds': finals[0]['seconds'] if finals else None,
                      'total_seconds': total, 'model_diagnostics': diagnostics,
                      'model_first_output_ms': diagnostics[0].get('firstOutputMs') if diagnostics else None,
                      'usage_complete': bool(usage) and all(isinstance(u, dict) for u in usage),
                      'input_tokens': sum(u.get('prompt_tokens', 0) for u in usage if isinstance(u, dict)),
                      'output_tokens': sum(u.get('completion_tokens', 0) for u in usage if isinstance(u, dict)),
                      'editor': editors, 'errors': [m for m in messages if m.get('error')], 'events': records}
            (OUT / f'case-{i}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            (OUT / f'case-{i}-answer.txt').write_text(answer, encoding='utf-8')
            print(json.dumps({k: result[k] for k in ['case','http_ack_seconds','first_visible_text_seconds','final_response_seconds','total_seconds','input_tokens','output_tokens','usage_complete','editor','errors']}, ensure_ascii=False), flush=True)
            await client.post('/api/close', json={'sid': sid})
    print('OUTPUT=' + str(OUT.resolve()), flush=True)

asyncio.run(main())
