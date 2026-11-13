"""Put the sampling questions to one analyst model, through GLEN's own tools.

    .venv/bin/python evals/analyst-models/run_sampling_eval.py \
        --model claude-haiku-5-5@medium --mode after --repeats 1 --cap-usd 2

PAID: every run calls the model provider. Run only with Nic's approval.

The loop mirrors the browser agent (upstream/geo-agent/app/agent.js) with the
preview config (site/layers-input.json): auto-approve, a checkpoint after
max_tool_calls rounds that used a server tool, tool results capped at 16,000 characters.
The model sees GLEN's MCP tools and prompt; the browser-only tools
(get/set/annotate_workspace_view, render_chart) are short stubs that report
success, and the map catalog text is left out. --tools live (with --mode after) instead
sends the browser's captured request (live-request.json, see live_tools.py): the geo-agent
map tools, render_chart and the catalog text too, with map tools as stubs.

--mode before reproduces the analyst as of 386d610: that commit's system prompt,
no derived-table metadata, no describe_sampling, no query time_range and no
answer check. --mode before-table is this checkout without the whole-library
metadata (docs/table-metadata.json: measured sampling, text-date time_range,
archive pointers) and with the prompt from --before-commit (14daa19, main as
PR 81 merged). --mode after is this checkout.

Each answer is scored by the checks in sampling-questions.json (regular
expressions; a human reads the answers too). Results go to results/<stamp>.json.
The spend file adds up the settled cost of every call and stops at --cap-usd.
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env', override=False)

from backend import analyst_models, analyst_provider, answer_check, catalog, time_range  # noqa: E402
from backend.preview_analyst import validate  # noqa: E402

BEFORE_COMMIT = '386d610'  # default --before-commit
# The live checkpoint threshold (6 for the 2026-11-10 results; 10 since).
CHECKPOINT_ROUNDS = json.loads((ROOT / 'site/layers-input.json').read_text())['max_tool_calls']
TOOL_RESULT_CAP = 16000
MAX_CALLS = 12
SPEND = HERE / 'results/spend.json'
STUB_TOOLS = {
    'get_workspace_view': 'Inspect what the user is currently viewing in the scientific workspace.',
    'set_workspace_view': 'Change the user\'s workspace view (family, variable, station, start, end, bookmark, preset, '
                          'dataset_id, view, experiment_study, ...). Use it so the main chart matches your analysis.',
    'annotate_workspace_view': 'Add event markers or shaded intervals to the active time-series chart.',
    'render_chart': 'Render a small bar, line, scatter or histogram chart from data rows or a SQL SELECT.',
}


def old_prompt(commit=BEFORE_COMMIT):
    return subprocess.run(['git', 'show', f'{commit}:glen-explorer/site/system-prompt.md'], cwd=ROOT,
                          check=True, capture_output=True, text=True).stdout


def spent():
    # Read under the same lock add_spend writes under, so two runners never see a half-written file.
    import fcntl
    if not SPEND.exists():
        return 0
    with open(SPEND.with_suffix('.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        return json.loads(SPEND.read_text())['micro_usd']


def add_spend(micro, label):
    # A lock file keeps two eval processes (say a runner and the judge) from losing each other's entries.
    import fcntl
    SPEND.parent.mkdir(exist_ok=True)
    with open(SPEND.with_suffix('.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(SPEND.read_text()) if SPEND.exists() else {'micro_usd': 0, 'runs': []}
        data['micro_usd'] += micro
        data['runs'].append({'label': label, 'micro_usd': micro, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
        SPEND.write_text(json.dumps(data, indent=1) + '\n')


async def tool_specs(server, mode):
    tools = []
    for t in await server.mcp.list_tools():
        if mode == 'before' and t.name in ('describe_sampling', 'compare_periods', 'resample'):
            continue
        description = t.description or ''
        if mode == 'before' and t.name == 'query':
            description = description.split('An aggregate over one table')[0].rstrip()
        if mode == 'before-table' and t.name == 'query':
            description = re.sub(r',\s+or a text date\s+named in glen_metadata\.time_column,', '', description)
        tools.append({'type': 'function', 'function': {'name': t.name, 'description': description,
                                                       'parameters': t.inputSchema}})
    for name, description in STUB_TOOLS.items():
        tools.append({'type': 'function', 'function': {'name': name, 'description': description,
                      'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': True}}})
    return tools


async def call_tool(server, name, args):
    if name in STUB_TOOLS:
        return json.dumps({'success': True, 'note': 'evaluation stub: no workspace is open'})
    try:
        result = await server.mcp.call_tool(name, args)
    except Exception as exc:  # the browser shows tool errors to the model as text
        return f'Error: {exc}'
    blocks = result[0] if isinstance(result, tuple) else result
    if isinstance(blocks, dict):
        return json.dumps(blocks)
    return '\n'.join(getattr(b, 'text', '') for b in blocks)


async def ask(server, entry, key, system, tools, question, mode, follow_up=None, call=None, local=None):
    call = call or call_tool
    local = STUB_TOOLS if local is None else local  # browser-side tools: their rounds do not count toward the checkpoint
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': question}]
    calls, cost, uncertain, trace, records = 0, 0, False, [], []
    checkpoint = False

    async def model(with_tools):
        nonlocal calls, cost, uncertain
        body = {'messages': messages, **({'tools': tools} if with_tools else {})}
        prepared = analyst_provider.prepare(entry, validate(body))
        outcome = await analyst_provider.send(entry, prepared, key, timeout=120)
        calls += 1
        if outcome.status != 200:
            raise RuntimeError(f'provider status {outcome.status}')
        if outcome.cost is None:
            uncertain = True
            cost += analyst_models.reservation(entry)
        else:
            cost += outcome.cost
        records.append({k: outcome.record.get(k) for k in ('served_model', 'stop_reason', 'usage', 'cost_micro_usd', 'latency_ms')})
        return outcome.data

    async def turn():
        nonlocal checkpoint
        rounds = 0  # the browser counts rounds per user turn
        while True:
            if rounds >= CHECKPOINT_ROUNDS or calls >= MAX_CALLS:
                messages.append({'role': 'user', 'content':
                                 f'You have reached a checkpoint after {rounds} data queries. Summarize for the user what '
                                 'you have done so far, the key findings, and what remains to be done. Then offer to continue.'})
                data = await model(False)
                checkpoint = True
                break
            data = await model(True)
            message = data['choices'][0]['message']
            tool_calls = message.get('tool_calls') or []
            if not tool_calls:
                break
            messages.append(message)
            if any(tc['function']['name'] not in local for tc in tool_calls):
                rounds += 1
            for tc in tool_calls:
                try:
                    args = json.loads(tc['function'].get('arguments') or '{}')
                except ValueError:
                    args = {}
                out = await call(server, tc['function']['name'], args)
                if len(out) > TOOL_RESULT_CAP:
                    out = out[:TOOL_RESULT_CAP] + '\n... (truncated)'
                trace.append({'tool': tc['function']['name'], 'args': args, 'result_chars': len(out)})
                messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': out})
        raw = data['choices'][0]['message'].get('content') or ''
        shown = answer_check.review(data)[0]['choices'][0]['message']['content'] if mode != 'before' else raw
        messages.append({'role': 'assistant', 'content': shown})
        return raw, shown, rounds

    raw, shown, rounds = await turn()
    result = {'answer': raw, 'shown': shown}
    if follow_up:
        messages.append({'role': 'user', 'content': follow_up})
        raw2, shown2, rounds2 = await turn()
        result.update(follow_up=raw2, follow_up_shown=shown2)
        rounds += rounds2
        raw = raw + '\n\n' + raw2
    result.update(answer_check=answer_check.findings(raw), checkpoint=checkpoint, model_calls=calls,
                  data_rounds=rounds, tools=trace, cost_micro_usd=cost, cost_uncertain=uncertain, records=records)
    return result


def sentences(text):
    return re.split(r'(?<=[.!?])\s+|\n+', text)


def score(question, result):
    text = result['answer'] + '\n\n' + result.get('follow_up', '')
    out = {}
    for check in question['checks']:
        rule, ok = check['auto'], True
        if 'sentence_all' in rule:
            ok = any(all(re.search(p, s, re.I) for p in rule['sentence_all'])
                     and any(re.search(re.escape(w) if w.isalpha() else w, s, re.I) for w in rule['sentence_any'])
                     for s in sentences(text))
        if 'any' in rule:
            ok = ok and any(re.search(p, text, re.I) for p in rule['any'])
        if 'none' in rule:
            ok = ok and not any(re.search(p, text, re.I) for p in rule['none'])
        if 'answer_check_absent' in rule:
            ok = ok and not set(rule['answer_check_absent']) & set(result['answer_check'])
        out[check['id']] = ok
    return out


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True, help='model@effort, e.g. claude-haiku-5-5@medium')
    parser.add_argument('--mode', choices=('before', 'before-table', 'after'), required=True)
    parser.add_argument('--questions', default='W1,W2,W3')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--before-commit', default=BEFORE_COMMIT, help='prompt version for --mode before')
    parser.add_argument('--cap-usd', type=float, required=True, help='stop when total recorded spend reaches this')
    parser.add_argument('--tools', choices=('eval', 'live'), default='eval',
                        help='live (with --mode after): the browser request in --fixture, see live_tools.py')
    parser.add_argument('--fixture', default=None, help='captured browser request for --tools live')
    args = parser.parse_args()
    if args.tools == 'live' and args.mode != 'after':
        raise SystemExit('--tools live replays this checkout as captured; use it with --mode after.')
    model_id, _, effort = args.model.partition('@')
    entry, problem = analyst_models.contestant(model_id, effort or None)
    if problem:
        raise SystemExit(problem)
    key = analyst_models.api_key(entry)
    if not key:
        raise SystemExit(f'No {analyst_models.KEY_NAMES[entry.provider]} in .env')
    bank = {q['id']: q for q in json.loads((HERE / 'sampling-questions.json').read_text())['questions']}
    wanted = [bank[q] for q in args.questions.split(',')]

    patches = []
    if args.mode == 'before':
        patches = [patch.object(catalog, 'DERIVED_METADATA_PATH', ROOT / 'docs/__absent__.json'),
                   patch.object(time_range, 'attach', lambda sql, payload: payload)]
    if args.mode == 'before-table':
        patches = [patch.object(catalog, 'TABLE_METADATA_PATH', ROOT / 'docs/__absent__.json')]
    for p in patches:
        p.start()
    catalog.load.cache_clear()
    from backend import server
    system = (old_prompt(args.before_commit) if args.mode != 'after' else (ROOT / 'site/system-prompt.md').read_text())
    system += '\n\n' + server.analyst_prompt()
    tools = await tool_specs(server, args.mode)
    call, local, stale = None, None, []
    if args.tools == 'live':
        import live_tools
        fixture = args.fixture or live_tools.FIXTURE
        tools, system, local, stale = await live_tools.setup(server, fixture)
        call = live_tools.stub_call(call_tool)
        for note in stale:
            print('fixture may be stale:', note, flush=True)

    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = {'model': args.model, 'mode': args.mode,
           'before_commit': args.before_commit if args.mode != 'after' else None, 'commit': subprocess.run(
        ['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        'tool_list': args.tools, **({'fixture_stale': stale} if args.tools == 'live' else {}),
        'system_prompt_chars': len(system), 'tools': [t['function']['name'] for t in tools], 'runs': []}
    for q in wanted:
        for i in range(args.repeats):
            if spent() >= args.cap_usd * 1e6:
                print(f'cap reached: ${spent() / 1e6:.2f}')
                break
            started = time.monotonic()
            try:
                result = await ask(server, entry, key, system, tools, q['question'], args.mode, q.get('follow_up'),
                                   call=call, local=local)
            except Exception as exc:
                result = {'error': (repr(exc) + ' ' + repr(exc.__cause__))[:400], 'cost_micro_usd': 0, 'answer': '', 'answer_check': []}
            result['seconds'] = round(time.monotonic() - started, 1)
            result['question'] = q['id']
            result['auto_score'] = score(q, result) if result.get('answer') else {}
            add_spend(result['cost_micro_usd'], f'{args.model} {args.mode}{" live" if args.tools == "live" else ""} '
                      f'{args.before_commit if args.mode != "after" else ""} {q["id"]}#{i + 1}')
            out['runs'].append(result)
            passed = sum(result['auto_score'].values())
            print(f'{q["id"]}#{i + 1} {args.model} {args.mode}: {passed}/{len(q["checks"])} checks, '
                  f'${result["cost_micro_usd"] / 1e6:.4f}, {result.get("model_calls")} calls, '
                  f'checkpoint={result.get("checkpoint")}, total ${spent() / 1e6:.2f}', flush=True)
    label = args.mode if args.mode == 'after' else f'{args.mode}-{args.before_commit}'
    label += '-live' if args.tools == 'live' else ''
    path = HERE / f'results/{stamp}-{model_id}-{label}.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + '\n')
    print('wrote', path.relative_to(ROOT))


if __name__ == '__main__':
    asyncio.run(main())
