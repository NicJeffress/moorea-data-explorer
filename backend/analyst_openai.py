"""OpenAI Responses API for the analyst (``POST /v1/responses``).

The newer OpenAI models call tools only through the Responses API, while the
browser's agent loop speaks chat-completions. This module translates one
chat-completions request into a Responses request and the reply back, without
changing what any tool does: function tools, calls and outputs map one to one.

Nothing is stored at OpenAI (``store: false``). Reasoning is returned encrypted
(``include: ["reasoning.encrypted_content"]``) and, with the reply's other
output items, rides back on the assistant message as ``OPENAI_ITEMS``; the next
round of the same turn replays them unchanged so the model keeps its reasoning.
"""
import json
import time

OPENAI_ITEMS = 'glen_openai_items'
RESPONSES_URL = 'https://api.openai.com/v1/responses'
_ITEM_KEYS = {'reasoning': {'type', 'id', 'summary', 'encrypted_content', 'status'},
              'function_call': {'type', 'id', 'call_id', 'name', 'arguments', 'status'},
              'message': {'type', 'id', 'role', 'content', 'status'}}


def _text(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    return ''.join(part['text'] for part in content)


def _valid_item(item):
    if not isinstance(item, dict) or item.get('type') not in _ITEM_KEYS or set(item) - _ITEM_KEYS[item['type']]:
        return False
    kind = item['type']
    if kind == 'reasoning':
        return isinstance(item.get('encrypted_content'), str) and isinstance(item.get('summary', []), list)
    if kind == 'function_call':
        return all(isinstance(item.get(k), str) for k in ('call_id', 'name', 'arguments'))
    content = item.get('content')
    return item.get('role') == 'assistant' and isinstance(content, list) and all(
        isinstance(p, dict) and p.get('type') in ('output_text', 'refusal') for p in content)


def _assistant_items(message):
    """OpenAI's own items when they still match the tool calls; else rebuild."""
    calls = message.get('tool_calls') or []
    saved = message.get(OPENAI_ITEMS)
    if isinstance(saved, list) and saved and all(_valid_item(i) for i in saved):
        if [i['call_id'] for i in saved if i['type'] == 'function_call'] == [c.get('id') for c in calls]:
            return [dict(i) for i in saved]
    items = []
    text = _text(message.get('content'))
    if text.strip():
        items.append({'role': 'assistant', 'content': text})
    for call in calls:
        fn = call.get('function') or {}
        args = fn.get('arguments')
        items.append({'type': 'function_call', 'call_id': call.get('id'), 'name': fn.get('name'),
                      'arguments': args if isinstance(args, str) else json.dumps(args or {})})
    return items


def _as_text(message):
    """A tool round as text, for a request that declares no tools (the agent's
    checkpoint summary); its reasoning is not replayed."""
    if message['role'] == 'tool':
        return f"[Result of tool call {message.get('tool_call_id')}]\n{_text(message.get('content'))}"
    lines = [_text(message.get('content'))]
    for call in message.get('tool_calls') or []:
        fn = call.get('function') or {}
        lines.append(f"[Called tool {fn.get('name')} with {fn.get('arguments')}]")
    return '\n'.join(line for line in lines if line.strip())


def to_responses(entry, body):
    """The Responses request body. Raises ValueError for a request the
    translation cannot represent faithfully."""
    tools = body.get('tools') or []
    declared = [{'type': 'function', 'name': t['function']['name'],
                 'description': t['function'].get('description', ''),
                 'parameters': t['function'].get('parameters') or {'type': 'object', 'properties': {}}}
                for t in tools if isinstance(t.get('function'), dict) and isinstance(t['function'].get('name'), str)]
    if len(declared) != len(tools):
        raise ValueError('Every tool needs a function name.')
    choice = body.get('tool_choice')
    if choice not in (None, 'auto', 'none'):
        raise ValueError('Forced tool use is not supported by the analyst.')

    system, items, started = [], [], False
    for message in body['messages']:
        role = message.get('role')
        if role == 'system':
            if started:
                raise ValueError('System messages must come first.')
            system.append(_text(message.get('content')))
            continue
        started = True
        if role == 'user':
            text = _text(message.get('content'))
            if text.strip():
                items.append({'role': 'user', 'content': text})
        elif role == 'assistant':
            if declared:
                items.extend(_assistant_items(message))
            else:
                text = _as_text(message)
                if text:
                    items.append({'role': 'assistant', 'content': text})
        elif role == 'tool':
            if declared:
                items.append({'type': 'function_call_output', 'call_id': message.get('tool_call_id'),
                              'output': _text(message.get('content')) or '(empty result)'})
            else:
                items.append({'role': 'user', 'content': _as_text(message)})
        else:
            raise ValueError('Unsupported message role.')
    if not items:
        raise ValueError('A user message is required.')
    # Every function_call needs its output before the next model turn.
    answered = {i['call_id'] for i in items if i.get('type') == 'function_call_output'}
    complete = []
    for item in items:
        complete.append(item)
        if item.get('type') == 'function_call' and item['call_id'] not in answered:
            complete.append({'type': 'function_call_output', 'call_id': item['call_id'],
                             'output': 'No result was returned for this call.'})
    items = complete

    p = {'model': entry.id, 'input': items, 'max_output_tokens': entry.max_output,
         'reasoning': {'effort': entry.effort}, 'store': False, 'stream': False,
         'include': ['reasoning.encrypted_content'], 'service_tier': 'default'}
    if any(s.strip() for s in system):
        p['instructions'] = '\n\n'.join(s for s in system if s.strip())
    if declared:
        p['tools'] = declared
        p['tool_choice'] = choice or 'auto'
    return p


def attempts(data):
    """The one billed attempt in a Responses reply, or None when unreadable."""
    u = data.get('usage') if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None
    details = u.get('input_tokens_details') or {}
    cached = details.get('cached_tokens', 0) if isinstance(details, dict) else None
    return [{'model': data.get('model'), 'input': u.get('input_tokens'), 'cached': cached,
             'output': u.get('output_tokens')}]


def from_responses(data):
    """(chat-completions response dict, stop reason). Raises ValueError when the
    reply cannot be read."""
    if not isinstance(data, dict) or not isinstance(data.get('output'), list):
        raise ValueError('Unreadable Responses reply.')
    texts, calls, refusals, kept, echo = [], [], [], [], True
    for item in data['output']:
        kind = item.get('type') if isinstance(item, dict) else None
        if kind == 'message':
            for part in item.get('content') or []:
                if part.get('type') == 'output_text':
                    texts.append(part.get('text') or '')
                elif part.get('type') == 'refusal':
                    refusals.append(part.get('refusal') or '')
            kept.append({k: v for k, v in item.items() if k in _ITEM_KEYS['message']})
        elif kind == 'function_call':
            calls.append({'id': item.get('call_id'), 'type': 'function',
                          'function': {'name': item.get('name'), 'arguments': item.get('arguments') or '{}'}})
            kept.append({k: v for k, v in item.items() if k in _ITEM_KEYS['function_call']})
        elif kind == 'reasoning':
            kept.append({k: v for k, v in item.items() if k in _ITEM_KEYS['reasoning']})
        else:
            echo = False  # an item this translation cannot replay faithfully
    status = data.get('status')
    reason = (data.get('incomplete_details') or {}).get('reason') if status == 'incomplete' else None
    if refusals or reason == 'content_filter':
        stop, finish = 'refusal', 'content_filter'
        content = 'The model declined to answer this request. Charts remain available; try asking in a different way.'
        calls = []
    elif reason:
        stop, finish = reason, 'length'   # max_output_tokens: the agent shows the cut-off and runs no call
        content = ''.join(texts)
    else:
        stop = 'tool_calls' if calls else (status or 'completed')
        finish = 'tool_calls' if calls else 'stop'
        content = ''.join(texts)
    reply = {'role': 'assistant', 'content': content or None}
    if calls:
        reply['tool_calls'] = calls
        if echo and finish == 'tool_calls':
            reply[OPENAI_ITEMS] = kept
    u = data.get('usage') or {}
    prompt, completion = u.get('input_tokens'), u.get('output_tokens')
    usage = {'prompt_tokens': prompt, 'completion_tokens': completion,
             'total_tokens': prompt + completion if isinstance(prompt, int) and isinstance(completion, int) else None,
             'prompt_tokens_details': {'cached_tokens': (u.get('input_tokens_details') or {}).get('cached_tokens', 0)}}
    out = {'id': data.get('id'), 'object': 'chat.completion', 'created': int(time.time()), 'model': data.get('model'),
           'choices': [{'index': 0, 'message': reply, 'finish_reason': finish}], 'usage': usage}
    return out, stop
