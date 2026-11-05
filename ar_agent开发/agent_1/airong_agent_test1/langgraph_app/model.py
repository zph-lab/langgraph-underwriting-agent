from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx

from tools.bridge import tool_schemas


class ModelError(RuntimeError):
    pass


class ChatModel:
    """OpenAI-compatible streaming adapter for private vLLM services."""
    def __init__(self, config: dict):
        self.config = config

    async def generate(self, messages: list[dict], emit, *, tools_enabled=True, max_tokens=None):
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        stats = {'requestId': request_id, 'maxTokens': max_tokens or self.config['max_tokens'],
                 'inputChars': len(json.dumps(messages, ensure_ascii=False)),
                 'textChars': 0, 'thinkingChars': 0, 'toolArgumentChars': 0,
                 'finishReason': None, 'usage': None, 'firstOutputMs': None, 'httpAttempts': 0}
        emit({'type': 'model_diagnostic', 'stage': 'start', **stats})

        def track(event):
            if event.get('type') == 'model_response_metadata':
                stats.update(event['metadata'])
                return
            delta = event.get('assistantMessageEvent', {})
            field = {'text_delta': 'textChars', 'thinking_delta': 'thinkingChars'}.get(delta.get('type'))
            if field:
                stats[field] += len(delta.get('delta') or '')
                if delta.get('delta') and stats['firstOutputMs'] is None:
                    stats['firstOutputMs'] = round((time.perf_counter() - started) * 1000, 2)
            emit(event)

        success = False
        try:
            result = await self._generate(messages, track, tools_enabled=tools_enabled, max_tokens=max_tokens)
            success = True
            return result
        finally:
            emit({'type': 'model_diagnostic', 'stage': 'end', **stats, 'success': success,
                  'durationMs': round((time.perf_counter() - started) * 1000, 2)})

    async def _generate(self, messages: list[dict], emit, *, tools_enabled=True, max_tokens=None):
        cfg = self.config
        headers = dict(cfg.get('headers', {}))
        if cfg.get('api_key'):
            headers['Authorization'] = 'Bearer ' + cfg['api_key']
        body = {'model': cfg['model'], 'messages': messages,
                'stream': True, 'max_tokens': max_tokens or cfg['max_tokens']}
        # V4 enables high-effort reasoning by default. This host intentionally
        # excludes reasoning history; use its supported non-thinking tool mode.
        if cfg['model'] in ('deepseek-v4-pro', 'deepseek-flash', 'deepseek-v4-flash'):
            body['thinking'] = {'type': 'disabled'}
        if cfg.get('stream_usage', True):
            body['stream_options'] = {'include_usage': True}
        if tools_enabled:
            body['tools'] = tool_schemas()
        text, thinking, calls = '', '', {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
            for attempt in range(3):
                try:
                    emit({'type': 'model_response_metadata', 'metadata': {'httpAttempts': attempt + 1}})
                    async with client.stream('POST', cfg['base_url'] + '/chat/completions',
                                             headers=headers, json=body) as response:
                        emit({'type': 'model_response_metadata', 'metadata': {'httpStatus': response.status_code}})
                        if response.status_code in (429, 502, 503, 504) and attempt < 2:
                            emit({'type': 'auto_retry_start', 'attempt': attempt + 1, 'maxAttempts': 3})
                            await asyncio.sleep(0.5 * 2 ** attempt)
                            continue
                        if response.is_error:
                            raise ModelError(f'模型服务请求失败（HTTP {response.status_code}），请检查本地模型配置')
                        completed = False
                        async for line in response.aiter_lines():
                            if not line.startswith('data:'):
                                continue
                            data = line[5:].strip()
                            if data == '[DONE]':
                                completed = True
                                break
                            packet = json.loads(data)
                            if isinstance(packet.get('id'), str):
                                emit({'type': 'model_response_metadata', 'metadata': {'responseId': packet['id'][:160]}})
                            usage = packet.get('usage')
                            if isinstance(usage, dict):
                                safe_usage = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                              if isinstance(usage.get(k), int)}
                                details = usage.get('completion_tokens_details') or {}
                                if isinstance(details, dict) and isinstance(details.get('reasoning_tokens'), int):
                                    safe_usage['reasoning_tokens'] = details['reasoning_tokens']
                                emit({'type': 'model_response_metadata', 'metadata': {'usage': safe_usage}})
                            if packet.get('error'):
                                raise ModelError('模型服务返回错误，请检查服务端状态')
                            choices = packet.get('choices', [])
                            if not choices:
                                continue
                            choice = choices[0]
                            if choice.get('finish_reason'):
                                emit({'type': 'model_response_metadata', 'metadata': {'finishReason': choice['finish_reason']}})
                                completed = True
                            delta = choice.get('delta', {})
                            for key, kind in [('content', 'text_delta'), ('reasoning_content', 'thinking_delta')]:
                                value = delta.get(key) or ''
                                if value:
                                    if key == 'content':
                                        text += value
                                    else:
                                        thinking += value
                                    emit({'type': 'message_update', 'assistantMessageEvent': {'type': kind, 'delta': value}})
                            for fragment in delta.get('tool_calls', []):
                                call = calls.setdefault(fragment['index'], {'id': '', 'type': 'function',
                                                       'function': {'name': '', 'arguments': ''}})
                                if fragment.get('id'):
                                    call['id'] = fragment['id']
                                for key in ('name', 'arguments'):
                                    call['function'][key] += fragment.get('function', {}).get(key, '') or ''
                            emit({'type': 'model_response_metadata', 'metadata': {
                                'toolArgumentChars': sum(len(c['function']['arguments']) for c in calls.values())}})
                            if choice.get('finish_reason') == 'length':
                                raise ModelError('模型服务报告输出长度截断；已接收的转换过程可展开查看，请结合请求日志检查输出额度与生成情况')
                        if not completed:
                            raise ModelError('模型流意外中断，请重试')
                        result = {'role': 'assistant', 'content': text}
                        if calls:
                            result['tool_calls'] = [calls[k] for k in sorted(calls)]
                            if any(not c['id'] or not c['function']['name'] for c in result['tool_calls']):
                                raise ModelError('模型返回的工具调用不完整')
                        if thinking:
                            result['reasoning_content'] = thinking
                        return result
                except httpx.HTTPError:
                    # Retrying after emitted output can duplicate text/tool calls.
                    raise ModelError('无法连接模型服务或连接中断，请检查网络与模型地址') from None
        raise ModelError('模型服务暂不可用，请稍后重试')
