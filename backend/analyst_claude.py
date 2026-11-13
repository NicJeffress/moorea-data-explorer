"""Claude for the analyst, through the official anthropic SDK (Messages API).

The browser's agent loop (the unmodified geo-agent library) speaks the
chat-completions format. This module translates one chat-completions request
into a Messages request and the reply back, without changing what any tool
does: tool definitions, tool calls and tool results map one to one.

Claude's own content blocks (thinking with its signature, tool_use, fallback
markers) ride back to the browser on the assistant message under
``CLAUDE_BLOCKS`` and are replayed verbatim on the next round of the same turn,
as the API requires for thinking in a tool loop. The OpenAI path strips them.
"""
import json
import time

CLAUDE_BLOCKS = 'glen_claude_content'
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
FINISH = {'end_turn': 'stop', 'stop_sequence': 'stop', 'tool_use': 'tool_calls', 'pause_turn': 'stop',
          'max_tokens': 'length', 'model_context_window_exceeded': 'length', 'refusal': 'content_filter'}
_ECHO_KEYS = {'text': {'type', 'text'}, 'thinking': {'type', 'thinking', 'signature'},
              'redacted_thinking': {'type', 'data'}, 'tool_use': {'type', 'id', 'name', 'input'},
              'fallback': {'type', 'from', 'to', 'trigger'}}


def _text(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    return ''.join(part['text'] for part in content)


def _arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        value = None
    # The client already reports unparseable arguments to the model as a tool
    # error; keep the text so the replayed call matches what the model wrote.
    return value if isinstance(value, dict) else {'arguments': raw}


def _valid_block(block):
    if not isinstance(block, dict) or block.get('type') not in _ECHO_KEYS:
        return False
    kind = block['type']
    if set(block) - _ECHO_KEYS[kind]:
        return False
    if kind == 'text':
        return isinstance(block.get('text'), str)
    if kind == 'thinking':
        return isinstance(block.get('thinking'), str) and isinstance(block.get('signature'), str)
    if kind == 'redacted_thinking':
        return isinstance(block.get('data'), str)
    if kind == 'tool_use':
        return (isinstance(block.get('id'), str) and isinstance(block.get('name'), str)
                and isinstance(block.get('input'), dict))
    # fallback: `from` is required, `to` optional, each {'model': str}.
    ends = [block.get(k) for k in ('from', 'to') if k in block]
    return 'from' in block and all(isinstance(e, dict) and isinstance(e.get('model'), str) for e in ends)


def _assistant_blocks(message):
    """Claude's own blocks when they still match the tool calls; else rebuild."""
    calls = message.get('tool_calls') or []
    saved = message.get(CLAUDE_BLOCKS)
    if isinstance(saved, list) and saved and all(_valid_block(b) for b in saved):
        if [b['id'] for b in saved if b['type'] == 'tool_use'] == [c.get('id') for c in calls]:
            return [dict(b) for b in saved]
    blocks = []
    text = _text(message.get('content'))
    if text.strip():
        blocks.append({'type': 'text', 'text': text})
    for call in calls:
        fn = call.get('function') or {}
        blocks.append({'type': 'tool_use', 'id': call.get('id'), 'name': fn.get('name'),
                       'input': _arguments(fn.get('arguments'))})
    return blocks


def _as_text(message):
    """A tool round written out as text, for a request that declares no tools
    (the agent's checkpoint summary): the API needs tools declared for tool
    blocks, and thinking is not replayed into a changed prefix."""
    if message['role'] == 'tool':
        return f"[Result of tool call {message.get('tool_call_id')}]\n{_text(message.get('content'))}"
    lines = [_text(message.get('content'))]
    for call in message.get('tool_calls') or []:
        fn = call.get('function') or {}
        lines.append(f"[Called tool {fn.get('name')} with {fn.get('arguments')}]")
    return '\n'.join(line for line in lines if line.strip())


def to_claude(entry, body):
    """Keyword arguments for client.beta.messages.create. Raises ValueError for
    a request the translation cannot represent faithfully."""
    tools = body.get('tools') or []
    declared = [{'name': t['function']['name'], 'description': t['function'].get('description', ''),
                 'input_schema': t['function'].get('parameters') or {'type': 'object', 'properties': {}}}
                for t in tools if isinstance(t.get('function'), dict)
                and isinstance(t['function'].get('name'), str)]
    if len(declared) != len(tools):
        raise ValueError('Every tool needs a function name.')
    choice = body.get('tool_choice')
    if choice not in (None, 'auto', 'none'):
        raise ValueError('Forced tool use is not supported for this model.')

    system, turns, started = [], [], False
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
            out_role, blocks = 'user', [{'type': 'text', 'text': text}] if text.strip() else []
        elif role == 'assistant':
            if declared:
                out_role, blocks = 'assistant', _assistant_blocks(message)
            else:
                text = _as_text(message)
                out_role, blocks = 'assistant', [{'type': 'text', 'text': text}] if text else []
        elif role == 'tool':
            if declared:
                out_role, blocks = 'user', [{'type': 'tool_result', 'tool_use_id': message.get('tool_call_id'),
                                             'content': _text(message.get('content')) or '(empty result)'}]
            else:
                out_role, blocks = 'user', [{'type': 'text', 'text': _as_text(message)}]
        else:
            raise ValueError('Unsupported message role.')
        if not blocks:
            continue
        if turns and turns[-1]['role'] == out_role:
            turns[-1]['content'].extend(blocks)
        else:
            turns.append({'role': out_role, 'content': blocks})

    # Every tool_use needs its tool_result at the start of the next user turn.
    for i, turn in enumerate(turns):
        if turn['role'] != 'assistant':
            continue
        ids = [b['id'] for b in turn['content'] if b['type'] == 'tool_use']
        if not ids:
            continue
        if i + 1 == len(turns):
            raise ValueError('A tool call has no result.')
        reply = turns[i + 1]['content']
        answered = {b.get('tool_use_id') for b in reply if b['type'] == 'tool_result'}
        missing = [{'type': 'tool_result', 'tool_use_id': x, 'is_error': True,
                    'content': 'No result was returned for this call.'} for x in ids if x not in answered]
        results = [b for b in reply if b['type'] == 'tool_result']
        turns[i + 1]['content'] = results + missing + [b for b in reply if b['type'] != 'tool_result']
    if not turns:
        raise ValueError('A user message is required.')
    if turns[0]['role'] != 'user':
        # The browser keeps the last 12 messages, which can start with a reply.
        turns.insert(0, {'role': 'user', 'content': [{'type': 'text', 'text': '(Earlier messages are not shown.)'}]})

    kwargs = {'model': entry.id, 'max_tokens': entry.max_output, 'messages': turns,
              'output_config': {'effort': entry.effort}, 'cache_control': {'type': 'ephemeral'}}
    if any(s.strip() for s in system):
        kwargs['system'] = '\n\n'.join(s for s in system if s.strip())
    if declared:
        kwargs['tools'] = declared
        if choice == 'none':
            kwargs['tool_choice'] = {'type': 'none'}
    if entry.fallbacks:
        kwargs['betas'] = [FALLBACK_BETA]
        kwargs['fallbacks'] = [{'model': m} for m in entry.fallbacks]
    return kwargs


def _counts(usage, model):
    created = usage.cache_creation_input_tokens or 0
    split = getattr(usage, 'cache_creation', None)
    one_hour = (split.ephemeral_1h_input_tokens or 0) if split else 0
    return {'model': model, 'input': usage.input_tokens, 'output': usage.output_tokens,
            'cache_write_5m': created - one_hour, 'cache_write_1h': one_hour,
            'cache_read': usage.cache_read_input_tokens or 0}


def attempts(message, requested):
    """Every billed attempt in the response. `usage.iterations`, when present,
    is the per-attempt source of truth; top-level usage covers only the attempt
    that produced the message. An unknown iteration type returns None."""
    iterations = getattr(message.usage, 'iterations', None)
    if not iterations:
        return [_counts(message.usage, message.model)]
    out = []
    for item in iterations:
        if item.type not in ('message', 'fallback_message'):
            return None
        out.append(_counts(item, getattr(item, 'model', None) or requested))
    return out


def from_claude(message):
    """(chat-completions response dict, stop reason, fallback used)."""
    texts, calls, blocks, echo = [], [], [], True
    for block in message.content:
        kind = block.type
        if kind == 'text':
            texts.append(block.text)
            blocks.append({'type': 'text', 'text': block.text})
        elif kind == 'tool_use':
            calls.append({'id': block.id, 'type': 'function',
                          'function': {'name': block.name, 'arguments': json.dumps(block.input)}})
            blocks.append({'type': 'tool_use', 'id': block.id, 'name': block.name, 'input': block.input})
        elif kind == 'thinking':
            blocks.append({'type': 'thinking', 'thinking': block.thinking, 'signature': block.signature})
        elif kind == 'redacted_thinking':
            blocks.append({'type': 'redacted_thinking', 'data': block.data})
        elif kind == 'fallback':
            blocks.append({k: v for k, v in block.model_dump(by_alias=True, exclude_none=True).items()
                           if k in _ECHO_KEYS['fallback']})
        else:
            echo = False  # a block this translation cannot replay faithfully
    stop = message.stop_reason
    fallback = any(getattr(i, 'type', None) == 'fallback_message' for i in (message.usage.iterations or []))
    content = ''.join(texts)
    if stop == 'refusal':
        details = getattr(message, 'stop_details', None)
        category = getattr(details, 'category', None)
        content = ('The model declined to answer this request'
                   + (f' (safety category: {category})' if category else '')
                   + '. Charts remain available; try asking in a different way.')
        calls = []
    reply = {'role': 'assistant', 'content': content or None}
    if calls:
        reply['tool_calls'] = calls
        if echo and stop != 'max_tokens':
            reply[CLAUDE_BLOCKS] = blocks
    u = message.usage
    prompt = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    # A reply cut off at max_tokens reports 'length' even with tool calls, so the
    # agent shows the cut-off and runs none of them (geo-agent #387).
    finish = FINISH.get(stop, 'stop')
    if calls and finish not in ('length', 'content_filter'):
        finish = 'tool_calls'
    data = {'id': message.id, 'object': 'chat.completion', 'created': int(time.time()), 'model': message.model,
            'choices': [{'index': 0, 'message': reply, 'finish_reason': finish}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': u.output_tokens,
                      'total_tokens': prompt + u.output_tokens,
                      'prompt_tokens_details': {'cached_tokens': u.cache_read_input_tokens or 0}}}
    return data, stop, fallback


def default_client(api_key, timeout):
    import anthropic
    # No SDK retries: each call is one reservation, as on the OpenAI path.
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)


async def create(entry, kwargs, api_key, client_factory=None, timeout=75):
    """One Messages call. Raises the SDK's own errors."""
    client = (client_factory or default_client)(api_key, timeout)
    async with client:
        return await client.beta.messages.create(**kwargs)
