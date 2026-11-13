"""One analyst model call for any allowlisted contestant, priced from the allowlist.

`prepare` validates and translates before any reservation is made; `send`
makes exactly one provider call (no retries) and returns an `Outcome` with the
chat-completions reply, the settled cost (None when uncertain) and a
per-request record for evals. The record never holds prompt or reply text.

Endpoints: OpenAI Chat Completions (`api='chat'`, the default GPT-5.4 mini),
OpenAI Responses (`api='responses'`), Anthropic Messages via the SDK.
"""
from dataclasses import dataclass
import time

import httpx

from . import analyst_claude as claude
from . import analyst_openai as responses
from .analyst_models import usage_cost

OPENAI_URL = 'https://api.openai.com/v1/chat/completions'
USAGE_FIELDS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'cached', 'output')
PRIVATE_FIELDS = (claude.CLAUDE_BLOCKS, responses.OPENAI_ITEMS)


class ProviderUnavailable(Exception):
    """No usable answer: network failure, timeout or unreadable response."""


@dataclass
class Outcome:
    status: int          # 200, or the provider's HTTP error status
    data: dict | None    # chat-completions response when status is 200
    error: dict | None   # the provider's parsed error body, for classification only
    cost: int | None     # microdollars; None means uncertain (keep the full hold)
    record: dict


def openai_payload(entry, body):
    """Chat Completions: allowlist the request rather than forwarding browser options."""
    messages = [{k: v for k, v in m.items() if k not in PRIVATE_FIELDS} for m in body['messages']]
    p = {'messages': messages, **{k: body[k] for k in ('tools', 'tool_choice') if k in body}}
    p.update(model=entry.id, stream=False, store=False, max_completion_tokens=entry.max_output,
             service_tier='default')
    if entry.effort:
        p['reasoning_effort'] = entry.effort
    return p


def prepare(entry, body):
    """The provider request. Raises ValueError when it cannot be built."""
    if entry.provider == 'anthropic':
        return claude.to_claude(entry, body)
    if entry.api == 'responses':
        return responses.to_responses(entry, body)
    return openai_payload(entry, body)


def _record(entry, attempts, cost, stop, served, fallback, started):
    attempts = attempts or []
    usage = {n: sum(a.get(n, 0) for a in attempts if type(a.get(n, 0)) is int) for n in USAGE_FIELDS}
    listed = usage_cost(entry, attempts, list_price=True) if attempts and cost is not None else None
    return {'contestant': entry.contestant, 'provider': entry.provider, 'api': entry.api,
            'requested_model': entry.id, 'served_model': served, 'fallback_used': fallback,
            'effort': entry.effort, 'stop_reason': stop,
            'latency_ms': round((time.monotonic() - started) * 1000), 'usage': usage,
            'attempts': attempts, 'cost_micro_usd': cost, 'list_cost_micro_usd': listed,
            'priced': cost is not None}


def _chat_attempt(entry, data):
    u = data.get('usage') if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return None, None
    # A fixture or proxy that leaves out `model` is priced as the requested model;
    # a named model outside the allowlist is uncertain and keeps the hold.
    served = data.get('model') or entry.id
    details = u.get('prompt_tokens_details')
    cached = details.get('cached_tokens', 0) if isinstance(details, dict) else 0
    return {'model': served, 'input': u.get('prompt_tokens'), 'cached': cached or 0,
            'output': u.get('completion_tokens')}, served


async def _post(url, prepared, api_key, http_factory, timeout):
    async with http_factory(timeout=timeout) as client:
        response = await client.post(url, headers={'Authorization': 'Bearer ' + api_key}, json=prepared)
    if response.is_error:
        # Parse only for classification; never persist or expose the body.
        try:
            error = response.json()
        except ValueError:
            error = None
        return response, error if isinstance(error, dict) else None
    return response, None


async def send(entry, prepared, api_key, *, http_factory=httpx.AsyncClient, claude_factory=None, timeout=75):
    started = time.monotonic()
    if entry.provider == 'anthropic':
        import anthropic
        try:
            message = await claude.create(entry, prepared, api_key, claude_factory, timeout)
        except anthropic.APIStatusError as exc:
            body = exc.body if isinstance(exc.body, dict) else None
            return Outcome(exc.status_code, None, body, None, _record(entry, None, None, None, None, False, started))
        except anthropic.APIError as exc:  # connection, timeout, unreadable response
            raise ProviderUnavailable() from exc
        data, stop, fallback = claude.from_claude(message)
        attempts = claude.attempts(message, entry.id)
        cost = usage_cost(entry, attempts) if attempts else None
        return Outcome(200, data, None, cost, _record(entry, attempts, cost, stop, message.model, fallback, started))

    if entry.api == 'responses':
        response, error = await _post(responses.RESPONSES_URL, prepared, api_key, http_factory, timeout)
        if response.is_error:
            return Outcome(response.status_code, None, error, None, _record(entry, None, None, None, None, False, started))
        raw = response.json()
        data, stop = responses.from_responses(raw)  # ValueError when unreadable: uncertain
        attempts = responses.attempts(raw)
        cost = usage_cost(entry, attempts) if attempts else None
        return Outcome(200, data, None, cost, _record(entry, attempts, cost, stop, raw.get('model'), False, started))

    response, error = await _post(OPENAI_URL, prepared, api_key, http_factory, timeout)
    if response.is_error:
        return Outcome(response.status_code, None, error, None, _record(entry, None, None, None, None, False, started))
    data = response.json()
    attempt, served = _chat_attempt(entry, data)
    cost = usage_cost(entry, [attempt]) if attempt else None
    choices = data.get('choices') if isinstance(data, dict) else None
    stop = choices[0].get('finish_reason') if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    return Outcome(200, data, None, cost, _record(entry, [attempt] if attempt else None, cost, stop, served, False, started))
