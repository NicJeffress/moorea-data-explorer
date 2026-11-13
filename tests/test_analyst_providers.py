"""Analyst provider layer: allowlist, prices, Claude translation and the PR 50
safeguards under Claude. Every provider is a fake transport; no API is called."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from starlette.requests import Request

from backend import analyst_claude, analyst_models, analyst_openai, analyst_provider
from backend.analyst_models import MODELS, reservation, select, usage_cost
from backend.preview_analyst import Ledger, Denied, RESERVE_MICRO_USD, TESTER_DAY_MICRO_USD, complete

HAS_SDK = importlib.util.find_spec('anthropic') is not None
PROMPT = 'private prompt text'
TOOLS = [{'type': 'function', 'function': {'name': 'query', 'description': 'Run SQL.',
                                           'parameters': {'type': 'object', 'properties': {'sql_query': {'type': 'string'}}}}}]


def turn(*extra):
    return {'messages': [{'role': 'system', 'content': 'You are the analyst.'},
                         {'role': 'user', 'content': PROMPT}, *extra],
            'tools': TOOLS, 'tool_choice': 'auto', 'temperature': 0, 'max_tokens': 999999,
            'model': 'claude-fable-5-2', 'user': 'session'}


def claude_message(content, stop='end_turn', model='claude-sonnet-5-5', usage=None, **extra):
    u = {'input_tokens': 1000, 'output_tokens': 500, 'cache_creation_input_tokens': 2000,
         'cache_read_input_tokens': 3000}
    u.update(usage or {})
    return {'id': 'msg_fake', 'type': 'message', 'role': 'assistant', 'model': model, 'content': content,
            'stop_reason': stop, 'stop_sequence': None, 'usage': u, **extra}


def fake_claude(responses, sent):
    """A claude_factory whose client answers from `responses` (status, json) or raises."""
    import anthropic
    import httpx2

    def handler(request):
        sent.append({'body': json.loads(request.content), 'headers': dict(request.headers)})
        item = responses[min(len(sent), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx2.Response(status, json=body)

    return lambda key, timeout: anthropic.AsyncAnthropic(
        api_key=key, max_retries=0, timeout=timeout,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def request(body, identity='tester'):
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(body).encode()}
    return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
                    'state': {'preview_identity': identity}}, receive)


def roomy_ledger(tmp):
    # Sonnet and Opus holds exceed the default $1 daily tester allowance (see
    # test_default_allowance_admits_only_the_cheaper_models), so these tests raise it.
    return Ledger(tmp, 100_000_000, tester_day_micro_usd=50_000_000, tester_month_micro_usd=50_000_000)


class Allowlist(unittest.TestCase):
    def test_default_is_unchanged_and_its_hold_is_jeffs(self):
        entry, problem = select({})
        self.assertIsNone(problem)
        self.assertEqual(entry.id, 'gpt-5.4-mini-2026-03-17')
        self.assertEqual(reservation(entry), RESERVE_MICRO_USD)
        self.assertEqual(usage_cost(entry, [{'model': entry.id, 'input': 1000, 'output': 200}]), 1650)

    def test_only_analyst_models_can_be_selected(self):
        for wanted in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5',
                       'gpt-6-astra', 'gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.1-terra'):
            self.assertEqual(select({'GLEN_ANALYST_MODEL': wanted})[0].id, wanted)
        for wanted in ('claude-fable-5-2', 'claude-opus-5', 'claude-sonnet-5', 'gpt-4o', 'gpt-5.4-mini'):
            entry, problem = select({'GLEN_ANALYST_MODEL': wanted})
            self.assertIsNone(entry, wanted)
            self.assertIn(wanted, problem)
        self.assertEqual(MODELS['claude-fable-5-2'].role, 'judge')
        # OPENAI_MODEL still names the default, when it is an allowlisted OpenAI model.
        self.assertEqual(select({'OPENAI_MODEL': 'gpt-6-luna'})[0].contestant, 'gpt-6-luna@medium')
        self.assertEqual(select({'OPENAI_MODEL': 'gpt-5.4-mini'})[0].id, 'gpt-5.4-mini-2026-03-17')
        self.assertEqual(select({'OPENAI_MODEL': 'claude-opus-5-5'})[0].id, 'gpt-5.4-mini-2026-03-17')
        self.assertEqual(select({'OPENAI_MODEL': 'gpt-6-luna', 'GLEN_ANALYST_MODEL': 'gpt-6-astra'})[0].id, 'gpt-6-astra')

    def test_a_contestant_is_a_model_and_an_effort_the_model_documents(self):
        def pick(model, effort=None):
            env = {'GLEN_ANALYST_MODEL': model}
            if effort:
                env['GLEN_ANALYST_EFFORT'] = effort
            return select(env)
        for model, efforts in {'gpt-5.4-mini-2026-03-17': 'none low medium high xhigh',
                               'gpt-6-astra': 'low medium high xhigh max',
                               'gpt-6-luna': 'none low medium high xhigh max',
                               'gpt-6.1-sol': 'low medium high xhigh max',
                               'gpt-6.1-terra': 'none low medium high xhigh max',
                               'claude-opus-5-5': 'low medium high xhigh max',
                               'claude-sonnet-5-5': 'low medium high xhigh max',
                               'claude-haiku-5-5': 'low medium high xhigh max'}.items():
            for effort in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'turbo'):
                entry, problem = pick(model, effort)
                if effort in efforts.split():
                    self.assertEqual(entry.contestant, f'{model}@{effort}')
                    self.assertEqual(pick(f'{model}@{effort}')[0], entry)
                else:
                    self.assertIsNone(entry, (model, effort))
                    self.assertIn(f'"{effort}" is not supported by {model}', problem)
        # Defaults are always explicit; astra documents none, so the allowlist names one.
        self.assertEqual({m: pick(m)[0].effort for m in ('gpt-5.4-mini-2026-03-17', 'gpt-6-astra', 'claude-opus-5-5')},
                         {'gpt-5.4-mini-2026-03-17': 'none', 'gpt-6-astra': 'medium', 'claude-opus-5-5': 'medium'})
        self.assertIn('says effort', select({'GLEN_ANALYST_MODEL': 'gpt-6-luna@low', 'GLEN_ANALYST_EFFORT': 'high'})[1])
        # The hold keys on the pair: reasoning effort needs room for reasoning tokens.
        mini_none, mini_low = pick('gpt-5.4-mini-2026-03-17')[0], pick('gpt-5.4-mini-2026-03-17', 'low')[0]
        self.assertEqual((mini_none.max_output, mini_low.max_output), (2048, 16000))
        self.assertEqual(reservation(mini_none), RESERVE_MICRO_USD)
        self.assertEqual(reservation(mini_low), 400000 * 0.75 + 16000 * 4.5)

    def test_an_unconfirmed_price_is_refused_not_guessed(self):
        unpriced = dict(MODELS, **{'gpt-6-luna': replace(MODELS['gpt-6-luna'], price=None)})
        with patch.object(analyst_models, 'MODELS', unpriced):
            entry, problem = select({'GLEN_ANALYST_MODEL': 'gpt-6-luna'})
            self.assertIsNone(entry)
            self.assertIn('no confirmed price', problem)
            with self.assertRaises(ValueError):
                reservation(unpriced['gpt-6-luna'])

    def test_holds_cover_the_dearest_card_and_every_fallback(self):
        opus = MODELS['claude-opus-5-5']
        # Primary at its dearest input rate (1-hour cache write) plus the Opus 5
        # fallback at its own, each at full input and output.
        self.assertEqual(reservation(opus), 400000 * 8 + 16000 * 20 + 400000 * 10 + 16000 * 25)
        haiku = MODELS['claude-haiku-5-5']
        self.assertEqual(reservation(haiku), 400000 * 1 + math_ceil(16000 * 2.5))
        top = {'model': 'claude-haiku-5-5', 'input': 0, 'cache_write_1h': 400000, 'output': 16000}
        self.assertEqual(usage_cost(haiku, [top]), reservation(haiku))

    def test_default_allowance_admits_only_the_cheaper_models(self):
        admitted = {m: reservation(MODELS[m]) <= TESTER_DAY_MICRO_USD for m in MODELS if MODELS[m].role == 'analyst'}
        self.assertEqual(admitted, {'gpt-5.4-mini-2026-03-17': True, 'gpt-6-luna': True, 'gpt-6.1-terra': True,
                                    'claude-haiku-5-5': True, 'gpt-6.1-sol': False, 'gpt-6-astra': False,
                                    'claude-sonnet-5-5': False, 'claude-opus-5-5': False})
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp, 100_000_000)
            for model in ('claude-sonnet-5-5', 'claude-opus-5-5'):
                with self.assertRaisesRegex(Denied, 'allowance is exhausted'):
                    ledger.reserve('tester', reservation(MODELS[model]))
            ledger.finish(ledger.reserve('tester', reservation(MODELS['claude-haiku-5-5'])), 10)

    def test_prices_follow_the_served_model_and_prompt_length(self):
        haiku, sonnet = MODELS['claude-haiku-5-5'], MODELS['claude-sonnet-5-5']
        short = {'model': haiku.id, 'input': 100000, 'output': 1000}
        long = {'model': haiku.id, 'input': 100001, 'output': 1000}
        self.assertEqual(usage_cost(haiku, [short]), 100000 * 0.10 + 1000 * 0.50)
        self.assertEqual(usage_cost(haiku, [long]), math_ceil(100001 * 0.50 + 1000 * 2.50))
        both = [{'model': 'claude-sonnet-5-5', 'input': 10, 'output': 0},
                {'model': 'claude-sonnet-5', 'input': 10, 'cache_read': 100, 'output': 10}]
        self.assertEqual(usage_cost(sonnet, both), math_ceil(10 * 2 + 10 * 2 + 100 * 0.20 + 10 * 10))
        for bad in ([{'model': 'claude-fable-5-2', 'input': 1, 'output': 1}],  # outside the chain
                    [{'model': haiku.id, 'input': True, 'output': 1}],
                    [{'model': haiku.id, 'input': -1, 'output': 1}],
                    [{'model': haiku.id, 'input': 400001, 'output': 1}],
                    [{'model': haiku.id, 'input': 1, 'output': 16001}],
                    [short, short], []):
            self.assertIsNone(usage_cost(haiku, bad), bad)


def math_ceil(x):
    import math
    return math.ceil(x)


class Translation(unittest.TestCase):
    def test_request_follows_current_api_rules(self):
        body = turn({'role': 'assistant', 'content': 'Looking.', 'tool_calls': [
                        {'id': 'call_1', 'type': 'function', 'function': {'name': 'query', 'arguments': '{"sql_query":"SELECT 1"}'}},
                        {'id': 'call_2', 'type': 'function', 'function': {'name': 'query', 'arguments': '{"sql_query":"SELECT 2"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': '[1]'},
                    {'role': 'tool', 'tool_call_id': 'call_2', 'content': '[2]'},
                    {'role': 'user', 'content': 'That tool call just failed.'})
        k = analyst_claude.to_claude(MODELS['claude-sonnet-5-5'], body)
        self.assertEqual(k['model'], 'claude-sonnet-5-5')
        self.assertEqual(k['max_tokens'], 16000)
        self.assertEqual(k['output_config'], {'effort': 'medium'})
        self.assertEqual(k['system'], 'You are the analyst.')
        self.assertEqual(k['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(k['fallbacks'], [{'model': 'claude-sonnet-5'}])
        self.assertEqual(k['betas'], ['server-side-fallback-2026-07-01'])
        for absent in ('temperature', 'thinking', 'tool_choice', 'top_p', 'user', 'stream'):
            self.assertNotIn(absent, k)
        self.assertEqual(k['tools'], [{'name': 'query', 'description': 'Run SQL.',
                                       'input_schema': TOOLS[0]['function']['parameters']}])
        user, assistant, results = k['messages']
        self.assertEqual(assistant['content'][1:], [
            {'type': 'tool_use', 'id': 'call_1', 'name': 'query', 'input': {'sql_query': 'SELECT 1'}},
            {'type': 'tool_use', 'id': 'call_2', 'name': 'query', 'input': {'sql_query': 'SELECT 2'}}])
        # Parallel results in one user message, before the agent's follow-up text.
        self.assertEqual([b['type'] for b in results['content']], ['tool_result', 'tool_result', 'text'])
        self.assertEqual(results['content'][1], {'type': 'tool_result', 'tool_use_id': 'call_2', 'content': '[2]'})
        self.assertNotIn('fallbacks', analyst_claude.to_claude(MODELS['claude-haiku-5-5'], body))

    def test_tool_choice_and_odd_histories(self):
        haiku = MODELS['claude-haiku-5-5']
        self.assertEqual(analyst_claude.to_claude(haiku, dict(turn(), tool_choice='none'))['tool_choice'], {'type': 'none'})
        for forced in ('required', {'type': 'function', 'function': {'name': 'query'}}):
            with self.assertRaises(ValueError):
                analyst_claude.to_claude(haiku, dict(turn(), tool_choice=forced))
        with self.assertRaises(ValueError):
            analyst_claude.to_claude(haiku, {'messages': [{'role': 'user', 'content': 'a'}, {'role': 'system', 'content': 'b'}]})
        k = analyst_claude.to_claude(haiku, {'messages': [{'role': 'assistant', 'content': 'Earlier answer.'},
                                                          {'role': 'user', 'content': 'Next question'}]})
        self.assertEqual([m['role'] for m in k['messages']], ['user', 'assistant', 'user'])
        # A call left without a result gets an error result rather than a 400.
        k = analyst_claude.to_claude(haiku, turn(
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'query', 'arguments': '{}'}}]},
            {'role': 'user', 'content': 'continue'}))
        self.assertEqual(k['messages'][-1]['content'][0]['is_error'], True)

    def test_checkpoint_without_tools_writes_the_tool_round_as_text(self):
        body = turn({'role': 'assistant', 'content': None, 'glen_claude_content': [{'type': 'thinking', 'thinking': '', 'signature': 's'}],
                     'tool_calls': [{'id': 'c', 'type': 'function', 'function': {'name': 'query', 'arguments': '{"sql_query":"x"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'c', 'content': 'rows'},
                    {'role': 'user', 'content': 'Summarize.'})
        del body['tools']
        body['tool_choice'] = 'none'
        k = analyst_claude.to_claude(MODELS['claude-haiku-5-5'], body)
        self.assertNotIn('tools', k)
        types = {b['type'] for m in k['messages'] for b in m['content']}
        self.assertEqual(types, {'text'})
        self.assertEqual(k['messages'][1]['content'][0]['text'], '[Called tool query with {"sql_query":"x"}]')

    def test_openai_requests_never_carry_claude_blocks(self):
        body = turn({'role': 'assistant', 'content': 'x', 'glen_claude_content': [{'type': 'text', 'text': 'x'}]})
        p = analyst_provider.openai_payload(MODELS['gpt-5.4-mini-2026-03-17'], body)
        self.assertNotIn('glen_claude_content', json.dumps(p))
        self.assertEqual((p['model'], p['max_completion_tokens'], p['reasoning_effort']), ('gpt-5.4-mini-2026-03-17', 2048, 'none'))
        self.assertNotIn('temperature', p)


@unittest.skipUnless(HAS_SDK, 'anthropic SDK not installed: pip install -r requirements.lock.txt')
class ClaudeThroughTheLedger(unittest.IsolatedAsyncioTestCase):
    async def call(self, model, responses, body=None, ledger=None, identity='tester'):
        self.sent = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = ledger or roomy_ledger(tmp.name)
        r = await complete(request(body or turn(), identity), ledger, 'fake-key',
                           entry=MODELS[model], claude_factory=fake_claude(responses, self.sent))
        return r, ledger

    def rows(self, ledger):
        with ledger.connect() as db:
            return db.execute('SELECT charge, active_until, tester_exempt FROM calls ORDER BY started').fetchall()

    def failures(self, ledger):
        with ledger.connect() as db:
            return db.execute('SELECT failures FROM provider_health').fetchone()[0]

    async def test_tool_use_round_trip_replays_thinking_verbatim(self):
        content = [{'type': 'thinking', 'thinking': '', 'signature': 'sig-1'},
                   {'type': 'text', 'text': 'Checking.'},
                   {'type': 'tool_use', 'id': 'toolu_1', 'name': 'query', 'input': {'sql_query': 'SELECT 1'}}]
        r, ledger = await self.call('claude-sonnet-5-5', [(200, claude_message(content, 'tool_use'))])
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.body)
        choice = data['choices'][0]
        self.assertEqual(choice['finish_reason'], 'tool_calls')
        self.assertEqual(choice['message']['content'], 'Checking.')
        self.assertEqual(choice['message']['tool_calls'], [{'id': 'toolu_1', 'type': 'function',
                         'function': {'name': 'query', 'arguments': '{"sql_query": "SELECT 1"}'}}])
        self.assertEqual(data['usage'], {'prompt_tokens': 6000, 'completion_tokens': 500, 'total_tokens': 6500,
                                         'prompt_tokens_details': {'cached_tokens': 3000}})
        cost = 1000 * 2 + 2000 * 2.5 + 3000 * 0.1 + 500 * 10
        record = data['glen_analyst']
        self.assertEqual((record['served_model'], record['stop_reason'], record['cost_micro_usd'], record['priced']),
                         ('claude-sonnet-5-5', 'tool_use', cost, True))
        self.assertEqual(record['usage'], {'input': 1000, 'cache_write_5m': 2000, 'cache_write_1h': 0,
                                           'cache_read': 3000, 'cached': 0, 'output': 500})
        self.assertIsInstance(record['latency_ms'], int)
        self.assertEqual(self.rows(ledger), [(cost, 0, 0)])
        self.assertNotIn(PROMPT, json.dumps(record))
        self.assertNotIn(PROMPT.encode(), ledger.path.read_bytes())
        sent = self.sent[0]
        self.assertEqual(sent['headers']['anthropic-beta'], 'server-side-fallback-2026-07-01')
        self.assertEqual(sent['headers']['authorization'], 'Bearer fake-key')  # the key passed in, nothing else
        self.assertNotIn('temperature', sent['body'])

        # The browser sends the reply back with the tool result: Claude's blocks
        # come back exactly as received, thinking signature included.
        follow = turn(choice['message'], {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': '[1]'})
        r, _ = await self.call('claude-sonnet-5-5', [(200, claude_message([{'type': 'text', 'text': 'One.'}]))], follow)
        self.assertEqual(self.sent[0]['body']['messages'][1]['content'], content)
        self.assertEqual(json.loads(r.body)['choices'][0]['finish_reason'], 'stop')
        self.assertNotIn('glen_claude_content', json.loads(r.body)['choices'][0]['message'])

    async def test_refusal_is_answered_plainly_and_charged(self):
        r, ledger = await self.call('claude-haiku-5-5', [(200, claude_message(
            [{'type': 'tool_use', 'id': 'toolu_x', 'name': 'query', 'input': {}}], 'refusal', 'claude-haiku-5-5',
            {'output_tokens': 3}, stop_details={'type': 'refusal', 'category': 'bio', 'explanation': None}))])
        choice = json.loads(r.body)['choices'][0]
        self.assertEqual(choice['finish_reason'], 'content_filter')
        self.assertNotIn('tool_calls', choice['message'])
        self.assertIn('declined', choice['message']['content'])
        self.assertIn('bio', choice['message']['content'])
        self.assertEqual(json.loads(r.body)['glen_analyst']['stop_reason'], 'refusal')
        self.assertEqual(self.rows(ledger)[0][0], math_ceil(1000 * 0.1 + 2000 * 0.125 + 3000 * 0.01 + 3 * 0.5))
        self.assertEqual(self.failures(ledger), 0)

    async def test_max_tokens_reports_length_so_no_half_written_call_runs(self):
        r, ledger = await self.call('claude-haiku-5-5', [(200, claude_message(
            [{'type': 'text', 'text': 'Partial'}, {'type': 'tool_use', 'id': 't', 'name': 'query', 'input': {}}],
            'max_tokens', 'claude-haiku-5-5', {'output_tokens': 16000}))])
        data = json.loads(r.body)
        self.assertEqual(data['choices'][0]['finish_reason'], 'length')
        self.assertEqual(data['usage']['completion_tokens'], 16000)
        self.assertNotIn('glen_claude_content', data['choices'][0]['message'])
        self.assertEqual(self.rows(ledger)[0][1], 0)

    async def test_fallback_is_priced_per_attempt_at_each_models_rate(self):
        usage = {'input_tokens': 100, 'output_tokens': 50, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
                 'iterations': [{'type': 'message', 'model': 'claude-opus-5-5', 'input_tokens': 100, 'output_tokens': 0,
                                 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0},
                                {'type': 'fallback_message', 'model': 'claude-opus-5', 'input_tokens': 100, 'output_tokens': 50,
                                 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}]}
        content = [{'type': 'fallback', 'from': {'model': 'claude-opus-5-5'}, 'to': {'model': 'claude-opus-5'},
                    'trigger': {'type': 'refusal', 'category': 'cyber'}},
                   {'type': 'text', 'text': 'Answer.'}]
        r, ledger = await self.call('claude-opus-5-5', [(200, claude_message(content, model='claude-opus-5', usage=usage))])
        record = json.loads(r.body)['glen_analyst']
        self.assertEqual((record['served_model'], record['fallback_used']), ('claude-opus-5', True))
        self.assertEqual(self.rows(ledger), [(100 * 4 + 100 * 5 + 50 * 25, 0, 0)])
        self.assertEqual(self.sent[0]['body']['fallbacks'], [{'model': 'claude-opus-5'}])

    async def test_unpriceable_reply_keeps_the_full_hold(self):
        for model, usage in (('claude-fable-5-2', None), ('claude-haiku-5-5', {'output_tokens': 16001}),
                             ('claude-haiku-5-5', {'iterations': [{'type': 'compaction', 'input_tokens': 1, 'output_tokens': 1}]})):
            r, ledger = await self.call('claude-haiku-5-5', [(200, claude_message([{'type': 'text', 'text': 'x'}], model=model, usage=usage))])
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.rows(ledger), [(reservation(MODELS['claude-haiku-5-5']), 0, 0)], model)
            self.assertFalse(json.loads(r.body)['glen_analyst']['priced'])
            self.assertEqual(self.failures(ledger), 1)

    async def test_provider_errors_keep_holds_and_classify_like_openai(self):
        cases = {529: ('overloaded_error', 1, 1), 500: ('api_error', 1, 1), 429: ('rate_limit_error', 1, 1),
                 401: ('authentication_error', 1, 1), 402: ('billing_error', 1, 1), 404: ('not_found_error', 1, 1),
                 400: ('invalid_request_error', 0, 0), 413: ('request_too_large', 0, 0)}
        hold = reservation(MODELS['claude-haiku-5-5'])
        for status, (kind, exempt, failures) in cases.items():
            r, ledger = await self.call('claude-haiku-5-5', [(status, {'type': 'error', 'error': {'type': kind, 'message': PROMPT}})])
            self.assertEqual(r.status_code, 502, status)
            self.assertNotIn(PROMPT.encode(), r.body)
            self.assertEqual(len(self.sent), 1, 'the SDK must not retry')
            self.assertEqual(self.rows(ledger), [(hold, 0, exempt)], status)
            self.assertEqual(self.failures(ledger), failures, status)

    async def test_repeated_outages_pause_dispatch(self):
        import httpx2
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = roomy_ledger(tmp.name)
        for i in range(3):
            r, _ = await self.call('claude-haiku-5-5', [httpx2.ConnectError(PROMPT)], ledger=ledger, identity=str(i))
            self.assertEqual(r.status_code, 502)
            self.assertNotIn(PROMPT.encode(), r.body)
        r, _ = await self.call('claude-haiku-5-5', [(200, claude_message([]))], ledger=ledger, identity='next')
        self.assertEqual((r.status_code, len(self.sent)), (503, 0))
        self.assertEqual(self.rows(ledger), [(reservation(MODELS['claude-haiku-5-5']), 0, 0)] * 3)

    async def test_untranslatable_request_is_rejected_before_any_hold(self):
        r, ledger = await self.call('claude-haiku-5-5', [], dict(turn(), tool_choice='required'))
        self.assertEqual((r.status_code, self.sent), (400, []))
        self.assertEqual(self.rows(ledger), [])


def responses_reply(output, model='gpt-6-luna', status='completed', usage=None, **extra):
    u = {'input_tokens': 5000, 'input_tokens_details': {'cached_tokens': 4000},
         'output_tokens': 300, 'output_tokens_details': {'reasoning_tokens': 200}, 'total_tokens': 5300}
    u.update(usage or {})
    return {'id': 'resp_fake', 'object': 'response', 'model': model, 'status': status, 'output': output,
            'usage': u, **extra}


class OpenAIPricing(unittest.TestCase):
    def test_openai_input_is_charged_at_the_dearer_input_rate_and_listed_exactly(self):
        luna = MODELS['gpt-6-luna']
        a = {'model': 'gpt-6-luna', 'input': 5000, 'cached': 4000, 'output': 300}
        # Usage does not say which input tokens were cache writes: charge all at the write rate.
        self.assertEqual(usage_cost(luna, [a]), math_ceil(5000 * 0.125 + 300 * 0.50))
        self.assertEqual(usage_cost(luna, [a], list_price=True), math_ceil(1000 * 0.10 + 4000 * 0.01 + 300 * 0.50))
        mini = MODELS['gpt-5.4-mini-2026-03-17']
        self.assertEqual(usage_cost(mini, [{'model': mini.id, 'input': 1000, 'cached': 800, 'output': 200}]), 1650)

    def test_astra_long_context_uses_its_higher_card(self):
        astra = MODELS['gpt-6-astra']
        at, above = ({'model': 'gpt-6-astra', 'input': n, 'output': 100} for n in (272000, 272001))
        self.assertEqual(usage_cost(astra, [at]), 272000 * 12.5 + 100 * 50)
        self.assertEqual(usage_cost(astra, [above]), 272001 * 20 + 100 * 75)
        self.assertEqual(reservation(astra), 400000 * 20 + 16000 * 75)

    def test_unpinned_aliases_accept_only_their_own_dated_snapshots(self):
        luna = MODELS['gpt-6-luna']
        for served, ok in (('gpt-6-luna', True), ('gpt-6-luna-2026-11-01', True), ('gpt-6-luna-mini', False),
                           ('gpt-6-luna-2026-11-01-x', False), ('gpt-6.1-terra', False)):
            self.assertEqual(usage_cost(luna, [{'model': served, 'input': 1, 'output': 1}]) is not None, ok, served)
        mini = MODELS['gpt-5.4-mini-2026-03-17']
        self.assertIsNone(usage_cost(mini, [{'model': 'gpt-5.4-mini-2026-09-01', 'input': 1, 'output': 1}]))


class ResponsesTranslation(unittest.TestCase):
    def test_request_uses_the_responses_shapes(self):
        body = turn({'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'call_1', 'type': 'function', 'function': {'name': 'query', 'arguments': '{"sql_query":"SELECT 1"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': '[1]'})
        p = analyst_provider.prepare(MODELS['gpt-6.1-sol'], body)
        self.assertEqual({k: p[k] for k in ('model', 'max_output_tokens', 'reasoning', 'store', 'stream', 'include', 'tool_choice')},
                         {'model': 'gpt-6.1-sol', 'max_output_tokens': 16000, 'reasoning': {'effort': 'medium'},
                          'store': False, 'stream': False, 'include': ['reasoning.encrypted_content'], 'tool_choice': 'auto'})
        self.assertEqual(p['instructions'], 'You are the analyst.')
        self.assertEqual(p['tools'], [{'type': 'function', 'name': 'query', 'description': 'Run SQL.',
                                       'parameters': TOOLS[0]['function']['parameters']}])
        self.assertEqual(p['input'], [{'role': 'user', 'content': PROMPT},
                                      {'type': 'function_call', 'call_id': 'call_1', 'name': 'query', 'arguments': '{"sql_query":"SELECT 1"}'},
                                      {'type': 'function_call_output', 'call_id': 'call_1', 'output': '[1]'}])
        for absent in ('temperature', 'messages', 'max_tokens', 'user', 'reasoning_effort'):
            self.assertNotIn(absent, p)
        with self.assertRaises(ValueError):
            analyst_provider.prepare(MODELS['gpt-6-luna'], dict(turn(), tool_choice='required'))

    def test_chat_completions_default_never_carries_another_providers_items(self):
        body = turn({'role': 'assistant', 'content': 'x', 'glen_openai_items': [], 'glen_claude_content': []})
        p = analyst_provider.prepare(MODELS['gpt-5.4-mini-2026-03-17'], body)
        self.assertNotIn('glen_', json.dumps(p))
        self.assertEqual(p['reasoning_effort'], 'none')


class ResponsesThroughTheLedger(unittest.IsolatedAsyncioTestCase):
    async def call(self, model, replies, body=None, effort=None):
        self.sent = []

        def handler(req):
            self.sent.append({'url': str(req.url), 'body': json.loads(req.content), 'auth': req.headers.get('authorization')})
            status, data = replies[len(self.sent) - 1]
            if isinstance(data, Exception):
                raise data
            return httpx.Response(status, json=data)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = roomy_ledger(tmp.name)
        entry = analyst_models.contestant(model, effort)[0]
        factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)
        r = await complete(request(body or turn()), ledger, 'fake-key', factory, entry=entry)
        with ledger.connect() as db:
            rows = db.execute('SELECT charge, tester_exempt FROM calls').fetchall()
            failures = db.execute('SELECT failures FROM provider_health').fetchone()[0]
        return r, rows, failures

    async def test_tool_round_trip_replays_encrypted_reasoning(self):
        output = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'gAAA-fake'},
                  {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_9', 'name': 'query',
                   'arguments': '{"sql_query":"SELECT 2"}', 'status': 'completed'}]
        r, rows, _ = await self.call('gpt-6-luna', [(200, responses_reply(output, model='gpt-6-luna-2026-11-01'))], effort='high')
        self.assertEqual(self.sent[0]['url'], 'https://api.openai.com/v1/responses')
        self.assertEqual(self.sent[0]['auth'], 'Bearer fake-key')
        self.assertEqual(self.sent[0]['body']['reasoning'], {'effort': 'high'})
        data = json.loads(r.body)
        choice = data['choices'][0]
        self.assertEqual(choice['finish_reason'], 'tool_calls')
        self.assertEqual(choice['message']['tool_calls'], [{'id': 'call_9', 'type': 'function',
                         'function': {'name': 'query', 'arguments': '{"sql_query":"SELECT 2"}'}}])
        record = data['glen_analyst']
        self.assertEqual((record['contestant'], record['api'], record['served_model']),
                         ('gpt-6-luna@high', 'responses', 'gpt-6-luna-2026-11-01'))
        self.assertEqual(rows, [(math_ceil(5000 * 0.125 + 300 * 0.5), 0)])
        self.assertEqual(record['list_cost_micro_usd'], math_ceil(1000 * 0.1 + 4000 * 0.01 + 300 * 0.5))
        self.assertNotIn(PROMPT, json.dumps(record))

        follow = turn(choice['message'], {'role': 'tool', 'tool_call_id': 'call_9', 'content': '[2]'})
        final = [{'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                  'content': [{'type': 'output_text', 'text': 'Two.', 'annotations': []}]}]
        r, _, _ = await self.call('gpt-6-luna', [(200, responses_reply(final))], follow, effort='high')
        self.assertEqual(self.sent[0]['body']['input'][1:], output + [{'type': 'function_call_output', 'call_id': 'call_9', 'output': '[2]'}])
        self.assertEqual(json.loads(r.body)['choices'][0]['message'], {'role': 'assistant', 'content': 'Two.'})

    async def test_cut_off_and_refused_replies_run_no_tool_call(self):
        cut = responses_reply([{'type': 'function_call', 'call_id': 'c', 'name': 'query', 'arguments': '{"sql'}],
                              model='gpt-6.1-terra', status='incomplete', incomplete_details={'reason': 'max_output_tokens'},
                              usage={'output_tokens': 16000})
        r, rows, failures = await self.call('gpt-6.1-terra', [(200, cut)])
        choice = json.loads(r.body)['choices'][0]
        self.assertEqual(choice['finish_reason'], 'length')
        self.assertNotIn('glen_openai_items', choice['message'])
        self.assertEqual(failures, 0)
        refused = responses_reply([{'type': 'message', 'role': 'assistant', 'content': [{'type': 'refusal', 'refusal': 'No.'}]}],
                                  model='gpt-6.1-terra')
        r, rows, failures = await self.call('gpt-6.1-terra', [(200, refused)])
        choice = json.loads(r.body)['choices'][0]
        self.assertEqual((choice['finish_reason'], json.loads(r.body)['glen_analyst']['stop_reason']), ('content_filter', 'refusal'))
        self.assertIn('declined', choice['message']['content'])
        self.assertEqual(failures, 0)

    async def test_unpriceable_or_unreadable_replies_keep_the_full_hold(self):
        hold = reservation(MODELS['gpt-6-luna'])
        for reply in (responses_reply([], model='gpt-6-luna-mini'), responses_reply([], usage={'output_tokens': None}),
                      responses_reply([], usage={'input_tokens_details': {'cached_tokens': 9999}}), {'id': 'x'}):
            r, rows, failures = await self.call('gpt-6-luna', [(200, reply)])
            self.assertIn(r.status_code, (200, 502))
            self.assertEqual((rows, failures), ([(hold, 0)], 1), reply)

    async def test_errors_classify_as_before(self):
        hold = reservation(MODELS['gpt-6.1-terra'])
        for status, code, exempt, failures in ((429, 'rate_limit_exceeded', 1, 1), (500, None, 1, 1),
                                               (404, 'model_not_found', 1, 1), (400, 'invalid_request', 0, 0)):
            r, rows, f = await self.call('gpt-6.1-terra', [(status, {'error': {'code': code, 'message': PROMPT}})])
            self.assertEqual(r.status_code, 502)
            self.assertNotIn(PROMPT.encode(), r.body)
            self.assertEqual((rows, f), ([(hold, exempt)], failures), status)

if __name__ == '__main__':
    unittest.main()
