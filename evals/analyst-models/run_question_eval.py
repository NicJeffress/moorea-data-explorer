"""Put the questions in questions-draft.md to one analyst model, through GLEN's own tools.

    .venv/bin/python evals/analyst-models/run_question_eval.py \
        --model claude-haiku-5-5@medium --cap-usd 9.5

PAID: every run calls the model provider. Run only with Nic's approval. --cap-usd is the
running total in results/spend.json (all eval runs, not just this one) at which it stops.

Same analyst path as run_sampling_eval.py --mode after: this checkout's system prompt plus
the MCP server's prompt, GLEN's MCP tools, the browser's checkpoint after max_tool_calls data
rounds (site/layers-input.json), tool results capped at 16,000 characters and the answer
check. One difference: the workspace tools (get/set/annotate_workspace_view) carry their
real descriptions and schemas from site/workspace-tools.js, so group B can be graded on the
set_workspace_view arguments. They still run as stubs: set_workspace_view echoes its
arguments back as the new state. render_chart stays a short stub.

Automatic scores come from checks.py (exact checks for A, B and E1–E3; cross-checks only
for C, D and F, which the Fable judge grades). Results go to results/<stamp>-<model>-questions.json.

--tools live sends the tool list and system prompt the browser really sends, from
live-request.json (capture_live_request.py): the MCP tools plus the geo-agent map tools,
render_chart and the map catalog text. Map tools are stubs that report success; their calls
are listed in each run's `tools` trace. Results go to results/<stamp>-<model>-questions-live.json.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import checks  # noqa: E402
import live_tools  # noqa: E402
import run_sampling_eval as rse  # noqa: E402

ROOT = rse.ROOT
from backend import analyst_models  # noqa: E402

RENDER_CHART = rse.STUB_TOOLS['render_chart']


def workspace_specs():
    """The browser's own workspace tool definitions, read from site/workspace-tools.js with node."""
    script = ("import('./site/workspace-tools.js').then(m=>{const out=[];"
              "m.registerWorkspaceTools({registerLocal:t=>out.push({name:t.name,description:t.description,"
              "parameters:t.inputSchema})},{});console.log(JSON.stringify(out))})")
    out = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out)


async def tool_specs(server):
    tools = [{'type': 'function', 'function': {'name': t.name, 'description': t.description or '',
                                               'parameters': t.inputSchema}}
             for t in await server.mcp.list_tools()]
    for spec in workspace_specs():
        tools.append({'type': 'function', 'function': spec})
    tools.append({'type': 'function', 'function': {'name': 'render_chart', 'description': RENDER_CHART,
                  'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': True}}})
    return tools


STATE = {'view': 'time', 'family': 'ocean', 'note': 'evaluation stub: the default workspace (FOR01 waves) is open'}


async def stub_call(server, name, args):
    if name == 'get_workspace_view':
        return json.dumps(STATE)
    if name == 'set_workspace_view':
        return json.dumps({'success': True, 'workspace': {**STATE, **args, 'note': 'evaluation stub: settings applied'}})
    if name in ('annotate_workspace_view', 'render_chart'):
        return json.dumps({'success': True, 'note': 'evaluation stub'})
    return await rse.call_tool(server, name, args)


def select(questions, wanted):
    if not wanted:
        return questions
    keys = set(wanted.split(','))
    return [q for q in questions if q['id'] in keys or q['group'] in keys]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True, help='model@effort, e.g. claude-haiku-5-5@medium')
    parser.add_argument('--questions', default='', help='IDs or groups, e.g. A,B or A1,C3 (default: all)')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--cap-usd', type=float, required=True, help='stop when total recorded spend reaches this')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--tools', choices=('eval', 'live'), default='eval',
                        help='eval: MCP tools plus stubs (the 2026-11-10 runs); live: the browser request in --fixture')
    parser.add_argument('--fixture', default=str(live_tools.FIXTURE), help='captured browser request for --tools live')
    args = parser.parse_args()
    model_id, _, effort = args.model.partition('@')
    entry, problem = analyst_models.contestant(model_id, effort or None)
    if problem:
        raise SystemExit(problem)
    key = analyst_models.api_key(entry)
    if not key:
        raise SystemExit(f'No {analyst_models.KEY_NAMES[entry.provider]} in .env')
    wanted = select(checks.load_questions(), args.questions)
    expected_a, expected_b = checks.load_expected_a(), checks.load_expected_b()

    from backend import server
    call, local, stale = stub_call, None, []
    if args.tools == 'live':
        tools, system, local, stale = await live_tools.setup(server, args.fixture)
        call = live_tools.stub_call(stub_call)
        for note in stale:
            print('fixture may be stale:', note, flush=True)
    else:
        system = (ROOT / 'site/system-prompt.md').read_text() + '\n\n' + server.analyst_prompt()
        tools = await tool_specs(server)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = {'model': args.model, 'mode': 'after', 'tool_list': args.tools, 'question_set': 'questions-draft.md',
           **({'fixture': Path(args.fixture).name, 'fixture_commit': live_tools.load(args.fixture)['commit'],
               'fixture_stale': stale} if args.tools == 'live' else {}),
           'commit': subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True,
                                    text=True).stdout.strip(),
           'checkpoint_rounds': rse.CHECKPOINT_ROUNDS, 'system_prompt_chars': len(system),
           'tools': [t['function']['name'] for t in tools], 'runs': []}
    path = HERE / f'results/{stamp}-{model_id}-questions{"-live" if args.tools == "live" else ""}.json'
    gate = asyncio.Semaphore(args.concurrency)
    stopped = False

    async def one(q, i):
        nonlocal stopped
        async with gate:
            if stopped or rse.spent() >= args.cap_usd * 1e6:
                stopped = True
                return
            started = time.monotonic()
            try:
                result = await rse.ask(server, entry, key, system, tools, q['question'], 'after', call=call, local=local)
            except Exception as exc:
                result = {'error': (repr(exc) + ' ' + repr(exc.__cause__))[:400], 'cost_micro_usd': 0, 'answer': '',
                          'answer_check': []}
            result.update(seconds=round(time.monotonic() - started, 1), question=q['id'], group=q['group'], repeat=i + 1)
            marks, exact = checks.score(q, result, expected_a, expected_b) if result.get('answer') else ({}, None)
            result.update(auto_score=marks, exact_pass=exact)
            rse.add_spend(result['cost_micro_usd'], f'{args.model} questions{" live" if args.tools == "live" else ""} {q["id"]}#{i + 1}')
            out['runs'].append(result)
            path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + '\n')
            print(f'{q["id"]}#{i + 1} {args.model}: exact={exact} {marks} ${result["cost_micro_usd"] / 1e6:.4f}, '
                  f'{result.get("model_calls")} calls, checkpoint={result.get("checkpoint")}, '
                  f'total ${rse.spent() / 1e6:.2f}', flush=True)

    await asyncio.gather(*(one(q, i) for q in wanted for i in range(args.repeats)))
    out['runs'].sort(key=lambda r: (r['question'][0], int(r['question'][1:]), r['repeat']))
    out['stopped_at_cap'] = stopped
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + '\n')
    print('wrote', path.relative_to(ROOT), '(stopped at cap)' if stopped else '')


if __name__ == '__main__':
    asyncio.run(main())
