"""Compact model-facing evidence views; checkpoint messages remain untouched."""
from copy import deepcopy
import json

from tools.bridge import tool_schemas


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False))


def model_messages(messages):
    result = []
    seen = {}
    names = {}
    for original in messages:
        message = deepcopy(original)
        if message.get('role') == 'user':
            seen.clear()
            names.clear()
        for call in message.get('tool_calls', []):
            names[call['id']] = call['function']['name']
        # Service reasoning is display/audit data, not another model input.
        message.pop('reasoning_content', None)
        if message.get('role') == 'tool':
            try:
                raw = json.loads(message['content'])
            except (ValueError, TypeError):
                raw = None
            if isinstance(raw, dict):
                if 'sameResultAs' in raw:
                    result.append(message)
                    continue
                name = names.get(message.get('tool_call_id'))
                details = raw.get('details')
                if name == 'bom_search' and not raw.get('isError') and isinstance(details, dict) and (details.get('items') or details.get('modelInfo')):
                    # Keep ALL returned fields including method metadata and parent chains.
                    view = {'details': details, 'isError': bool(raw.get('isError'))}
                else:
                    # Rule text already contains all branches and filled action parameters.
                    view = {k: raw[k] for k in ('content', 'isError') if k in raw}
                    if not view:
                        view = raw
                key = (name, json.dumps(view, sort_keys=True, ensure_ascii=False))
                if name == 'bom_search' and 'details' in view:
                    # Different keywords/limits can retrieve identical candidates.
                    # These query decorations are not new field evidence. Raw tool
                    # replies remain unchanged in checkpoints and UI events.
                    evidence = {k: v for k, v in details.items() if k not in ('keyword', 'limit', 'scope', 'truncated')}
                    key = (name, json.dumps(evidence, sort_keys=True, ensure_ascii=False))
                if not raw.get('isError') and key in seen:
                    view = {'sameResultAs': seen[key], 'notice': '候选证据与前述工具消息相同，请复用其依据；不是新增证据。'}
                    if name == 'bom_search' and isinstance(details, dict):
                        view['query'] = {k: details[k] for k in ('keyword', 'limit', 'truncated') if k in details}
                else:
                    seen[key] = message.get('tool_call_id')
                message['content'] = json.dumps(view, ensure_ascii=False)
        result.append(message)
    return result


def request_size(messages, tools_enabled=True):
    # Same explicit character estimate used by the graph, not measured token usage.
    return encoded_size(messages) + (encoded_size(tool_schemas()) if tools_enabled else 0)


def request_token_bound(messages, tools_enabled=True):
    """UTF-8 byte upper bound for byte-based tokenizers, plus separate template reserve.

    This is deliberately pessimistic; it is not a token-usage measurement.
    """
    payload = json.dumps(messages, ensure_ascii=False).encode('utf-8')
    schema = json.dumps(tool_schemas(), ensure_ascii=False).encode('utf-8') if tools_enabled else b''
    return len(payload) + len(schema)
