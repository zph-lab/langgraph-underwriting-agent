"""Same graph, skills, tools and persistence as Web; /abort via Ctrl+C."""
import asyncio
import sys
import uuid

from .manager import SessionManager


async def main():
    manager = SessionManager()
    sid = sys.argv[1] if len(sys.argv) > 1 else uuid.uuid4().hex
    print(f'LangGraph 核保助手，会话 {sid}；/new 新建，/exit 退出。')
    try:
        session = await manager.ensure(sid)
        q = session.subscribe()
        while True:
            message = await asyncio.to_thread(input, '\n你: ')
            if message == '/exit':
                break
            if message == '/new':
                await manager.new_session(sid)
                continue
            if not message.strip():
                continue
            await manager.prompt(sid, message)
            while True:
                event = await q.get()
                if event['type'] == 'agent_settled':
                    print()
                    break
                if event['type'] == 'message_update':
                    delta = event['assistantMessageEvent']
                    if delta['type'] == 'text_delta':
                        print(delta['delta'], end='', flush=True)
                if event['type'] == 'message_end' and event['message'].get('error'):
                    print(event['message']['error'])
                if event['type'] == 'tool_execution_start':
                    print('\n正在检索业务数据…')
    finally:
        await manager.stop_all()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        pass
