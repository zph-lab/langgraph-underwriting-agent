from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .config import PROJECT_DIR, STATE_DIR, model_config
from .graph import create_graph
from .model import ChatModel
from tools.bridge import NodeTools


class RuntimeErrorBase(RuntimeError):
    pass


class Session:
    def __init__(self, sid, manager):
        self.sid, self.manager = sid, manager
        self._subs = set()
        self.last_active = time.time()
        self.task = None
        self.tools = NodeTools()
        self.lock = asyncio.Lock()
        self.closed = False
        self.run_id = None

    def alive(self):
        return True

    def subscribe(self):
        q = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        self.last_active = time.time()
        return q

    def unsubscribe(self, q):
        self._subs.discard(q)
        self.last_active = time.time()

    def subscriber_count(self):
        return len(self._subs)

    def emit(self, event):
        self.last_active = time.time()
        if event.get('type') in ('task_metric', 'model_diagnostic'):
            # These events contain counters only. Never log prompts, deltas or tool results.
            allowed = ('type', 'stage', 'phase', 'requestId', 'responseId', 'httpStatus', 'maxTokens', 'inputChars',
                       'textChars', 'thinkingChars', 'toolArgumentChars', 'finishReason', 'usage',
                       'success', 'durationMs', 'toolName', 'cached', 'requests', 'executions')
            allowed += ('modelRequests', 'rawInputChars', 'modelInputChars', 'requestChars', 'firstOutputMs', 'inputTokenUpperBound', 'httpAttempts')
            logging.getLogger('uvicorn.error').info('agent_diagnostic %s', json.dumps(
                {'timestampUtc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                 'sid': self.sid, 'runId': self.run_id, **{k: event[k] for k in allowed if k in event}}, ensure_ascii=False))
        for q in list(self._subs):
            if q.full():
                q.get_nowait()
            q.put_nowait(event)

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.tools.close()

    async def run(self, message):
        self.run_id = uuid.uuid4().hex
        self.emit({'type': 'task_status', 'text': '任务执行中，正在准备上下文…'})
        try:
            tid = self.manager.thread_id(self.sid)
            graph_config = {'configurable': {'thread_id': tid}, 'recursion_limit': 40}
            config = self.manager.config_factory()
            model = self.manager.model_factory(config)
            from .editor import RuleEditor
            graph = create_graph(model, self.tools, self.manager.saver, self.emit,
                                 config.get('context_window', 32768), config.get('max_tokens', 4096),
                                 config.get('model_request_limit', 16),
                                 editor=RuleEditor(config['rule_editor']) if config.get('rule_editor') else None)
            previous = await graph.aget_state(graph_config)
            history = previous.values.get('messages', []) if previous.values else []
            if not history and tid == self.sid:
                history = self.manager.import_legacy(self.sid)
            # Discard only unfinished turns on cancel/crash; full completed turns remain.
            history = self.manager.complete_history(history)
            async with asyncio.timeout(config.get('task_timeout_seconds', 600)):
                await graph.ainvoke({'messages': [*history, {'role': 'user', 'content': message}],
                                     'rounds': 0, 'validation': []}, graph_config)
        except TimeoutError:
            await self.tools.close()
            self.emit({'type': 'message_end', 'message': {'role': 'assistant', 'content': [],
                       'stopReason': 'error', 'error': '任务达到总耗时上限，已停止生成；已接收过程保留，未完成草稿不能作为最终规则，请缩小条款范围重试。'}})
        except asyncio.CancelledError:
            self.emit({'type': 'message_end', 'message': {'role': 'assistant', 'content': [], 'stopReason': 'aborted'}})
        except Exception as exc:
            # Only explicitly safe user errors; never expose provider/connection diagnostics.
            from .model import ModelError
            detail = str(exc) if isinstance(exc, (ModelError, ValueError)) and type(exc) in (ModelError, ValueError) else '运行失败，请检查模型配置与服务状态后重试'
            self.emit({'type': 'message_end', 'message': {'role': 'assistant', 'content': [],
                                                       'stopReason': 'error', 'error': detail}})
        finally:
            self.emit({'type': 'agent_settled'})


class SessionManager:
    def __init__(self, state_dir=STATE_DIR, model_factory=ChatModel, config_factory=model_config):
        self.state_dir = state_dir
        self.model_factory, self.config_factory = model_factory, config_factory
        self.bridges = {}
        self._lock = asyncio.Lock()
        self._reaper = None
        self._saver_context = None
        self.saver = None
        self.db = None
        self.idle_seconds = 1800

    async def start(self):
        if self.saver:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state_dir / 'sessions.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, thread_id TEXT NOT NULL)')
        self.db.commit()
        self._saver_context = AsyncSqliteSaver.from_conn_string(str(self.state_dir / 'checkpoints.sqlite'))
        self.saver = await self._saver_context.__aenter__()
        await self.saver.setup()

    def thread_id(self, sid):
        row = self.db.execute('SELECT thread_id FROM sessions WHERE sid=?', (sid,)).fetchone()
        if row:
            return row[0]
        self.db.execute('INSERT INTO sessions VALUES (?, ?)', (sid, sid))
        self.db.commit()
        return sid

    @staticmethod
    def complete_history(messages):
        # A completed turn ends with a non-tool-calling assistant response.
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m['role'] == 'assistant' and not m.get('tool_calls') and m.get('content'):
                return messages[:i + 1]
        return []

    @staticmethod
    def import_legacy(sid):
        """Import active branch messages from isolated Pi history, preserving original files."""
        root = PROJECT_DIR / 'config/pi-agent/sessions'
        if not root.exists():
            return []
        for path in sorted(root.rglob('*.jsonl'), reverse=True):
            try:
                entries = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
                if not entries or entries[0].get('id') != sid:
                    continue
                by_id = {entry.get('id'): entry for entry in entries[1:] if entry.get('id')}
                chain, entry = [], entries[-1]
                seen = set()
                while entry and entry.get('id') not in seen:
                    seen.add(entry.get('id'))
                    chain.append(entry)
                    entry = by_id.get(entry.get('parentId'))
                messages = []
                for entry in reversed(chain):
                    msg = entry.get('message', {})
                    role = msg.get('role')
                    if role not in ('user', 'assistant', 'toolResult'):
                        continue
                    content = msg.get('content', [])
                    text = content if isinstance(content, str) else ''.join(p.get('text', '') for p in content if p.get('type') == 'text')
                    if role == 'toolResult':
                        messages.append({'role': 'tool', 'tool_call_id': msg.get('toolCallId', ''),
                                         'content': text})
                    elif role == 'assistant':
                        converted = {'role': 'assistant', 'content': text}
                        calls = [p for p in content if isinstance(p, dict) and p.get('type') == 'toolCall'] if isinstance(content, list) else []
                        if calls:
                            converted['tool_calls'] = [{'id': p['id'], 'type': 'function', 'function': {
                                'name': p['name'], 'arguments': json.dumps(p.get('arguments', {}), ensure_ascii=False)}} for p in calls]
                        if text or calls:
                            messages.append(converted)
                    elif text:
                        messages.append({'role': role, 'content': text})
                return messages
            except (OSError, ValueError, TypeError):
                continue
        return []

    async def ensure(self, sid):
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?', sid):
            raise RuntimeErrorBase('会话 ID 无效')
        async with self._lock:
            await self.start()
            if sid not in self.bridges:
                if len(self.bridges) >= 10:
                    candidates = [b for b in self.bridges.values() if not b._subs and (not b.task or b.task.done())]
                    if not candidates:
                        raise RuntimeErrorBase('会话数已达上限（10），请稍后再试')
                    victim = min(candidates, key=lambda b: b.last_active)
                    victim.closed = True
                    await victim.stop()
                    del self.bridges[victim.sid]
                self.bridges[sid] = Session(sid, self)
            return self.bridges[sid]

    async def prompt(self, sid, message):
        session = await self.ensure(sid)
        async with session.lock:
            if session.closed:
                raise RuntimeErrorBase('会话已关闭，请重试')
            if session.task and not session.task.done():
                raise RuntimeErrorBase('当前会话正在生成，请先停止或等待完成')
            if message.startswith('!'):
                raise RuntimeErrorBase('业务助手不支持执行命令')
            if message.startswith('/skill:'):
                command, _, message = message.partition(' ')
                name = command.removeprefix('/skill:')
                if name not in {p.parent.name for p in (PROJECT_DIR / '.pi/skills').rglob('SKILL.md')}:
                    raise RuntimeErrorBase('未找到指定业务技能')
                message = message or '请按照技能约束帮助我编写规则，缺少信息时请澄清。'
            self.thread_id(sid)
            session.task = asyncio.create_task(session.run(message))
        return {'type': 'response', 'success': True, 'command': 'prompt'}

    async def abort(self, sid):
        session = self.bridges.get(sid)
        if session:
            async with session.lock:
                await session.stop()
        return {'success': True}

    async def new_session(self, sid):
        session = await self.ensure(sid)
        async with session.lock:
            await session.stop()
            self.db.execute('UPDATE sessions SET thread_id=? WHERE sid=?', (uuid.uuid4().hex, sid))
            if not self.db.execute('SELECT 1 FROM sessions WHERE sid=?', (sid,)).fetchone():
                self.db.execute('INSERT INTO sessions VALUES (?,?)', (sid, uuid.uuid4().hex))
            self.db.commit()
        return {'success': True}

    async def close(self, sid):
        async with self._lock:
            session = self.bridges.pop(sid, None)
            if session:
                async with session.lock:
                    session.closed = True
                    await session.stop()

    def start_reaper(self):
        self._reaper = asyncio.create_task(self._reap())

    async def _reap(self):
        while True:
            await asyncio.sleep(60)
            async with self._lock:
                for sid, session in list(self.bridges.items()):
                    if (not session._subs and (not session.task or session.task.done())
                            and time.time() - session.last_active > self.idle_seconds):
                        session.closed = True
                        await session.stop()
                        del self.bridges[sid]

    async def stop_all(self):
        if self._reaper:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        for session in list(self.bridges.values()):
            await session.stop()
        self.bridges.clear()
        if self._saver_context:
            await self._saver_context.__aexit__(None, None, None)
            self.saver = self._saver_context = None
        if self.db:
            self.db.close()
            self.db = None
