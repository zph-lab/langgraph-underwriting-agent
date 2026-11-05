"""Opt-in private model / read-only data smoke; never prints config or business contents.

Run manually from project root: python -m tests.live_smoke
"""
import asyncio
import json
import uuid

from langgraph_app.config import STATE_DIR
from langgraph_app.manager import SessionManager


async def main():
    manager = SessionManager(STATE_DIR / 'smoke')
    failed = False
    try:
        session = await manager.ensure('migration-smoke-' + uuid.uuid4().hex)
        q = session.subscribe()
        for message in ['请查询保单的二级机构字段。', '请查找高保额设备业务管控规则，完整展示现有规则。']:
            await manager.prompt(session.sid, message)
            events = []
            while True:
                event = await asyncio.wait_for(q.get(), 240)
                events.append(event)
                if event['type'] == 'agent_settled':
                    break
            await session.task
            tool_events = [e for e in events if e['type'] == 'tool_execution_end']
            failed = failed or not tool_events or any(e.get('isError') for e in tool_events) or any(
                e.get('message', {}).get('stopReason') == 'error' for e in events)
            print(json.dumps({'settled': True,
                'tools': [{'name': e['toolName'], 'error': e.get('isError')} for e in tool_events],
                'modelError': [e['message'].get('error') for e in events if e.get('message', {}).get('stopReason') == 'error'],
                'textDelta': any(e['type'] == 'message_update' and e['assistantMessageEvent']['type'] == 'text_delta' for e in events),
                'finalText': any(e['type'] == 'message_end' and any(p.get('type') == 'text' and p.get('text') for p in e['message'].get('content', [])) for e in events)
            }, ensure_ascii=False), flush=True)
    finally:
        await manager.stop_all()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
