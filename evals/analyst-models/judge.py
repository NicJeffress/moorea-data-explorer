"""Blind Fable 5.2 judge for the analyst answers in results/*-questions.json.

    # free: build the judge requests and count their input tokens
    .venv/bin/python evals/analyst-models/judge.py estimate results/A.json results/B.json
    # paid, a few cents: judge N random answers to measure output and thinking tokens
    .venv/bin/python evals/analyst-models/judge.py sample --n 1 --cap-usd 9.5 results/A.json
    # paid: the whole set. Refuses without --approved-by (Nic approves the projected cost first)
    .venv/bin/python evals/analyst-models/judge.py run --approved-by "Nic, 2026-11-.." --cap-usd .. results/*.json

The judge (claude-fable-5-2, output_config.effort "medium", no thinking parameter: thinking
is always on) gets the question, the rubric, the answer, the analyst's tool calls (SQL text,
not results) and the facts the answer should use (judge-facts.json, expected-a.json). It
never sees which model wrote the answer; answers are shuffled and get opaque IDs, and the
key back to model and run is stored beside the grades. Structured output gives one
pass / fail / not_applicable verdict per criterion with a one-line reason.

No server-side fallback model: a refusal is recorded as a refusal rather than graded by a
different model. Groups A, C, D, E and F are judged; B is graded by its set_workspace_view
check only.
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import checks  # noqa: E402
import run_sampling_eval as rse  # noqa: E402

ROOT = rse.ROOT
JUDGE_MODEL = 'claude-fable-5-2'
EFFORT = 'medium'
PRICE = {'input': 8.0, 'output': 40.0}  # USD per million tokens
JUDGED_GROUPS = {'A', 'C', 'D', 'E', 'F'}
CRITERIA = [
    ('numbers_right', 'Every number is right per the facts, or comes from a query the analyst ran. not_applicable if the answer gives no numbers.'),
    ('names_source', 'Names the source table or EDI package it used. not_applicable if no data were needed.'),
    ('caveats', 'States the caveats that matter for this question (units, time zone, missing codes, sample design, effort), and no more than matter.'),
    ('no_invention', 'Invents no numbers, sites, studies, dates or tools.'),
    ('no_causation', 'Does not claim causation from correlation or co-occurrence.'),
    ('no_coverage_wording', 'Uses no coverage wording: no "coverage", "% of slots", "data completeness" shares; grey is "no data".'),
    ('language', 'Answers in the language the question was asked in.'),
    ('declines_well', 'Declines or redirects politely and briefly when it should. not_applicable if it should answer.'),
    ('concise', 'Is no longer than the question needs.'),
    ('question_specific', 'Does what the "good answer" line for this question asks.'),
]
RUBRIC = '\n'.join(f'- {cid}: {text}' for cid, text in CRITERIA)
SCHEMA = {
    'type': 'object',
    'properties': {
        'criteria': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'enum': [c for c, _ in CRITERIA]},
                    'verdict': {'type': 'string', 'enum': ['pass', 'fail', 'not_applicable']},
                    'reason': {'type': 'string'},
                },
                'required': ['id', 'verdict', 'reason'],
                'additionalProperties': False,
            },
        },
        'overall': {'type': 'string', 'enum': ['pass', 'fail', 'borderline']},
    },
    'required': ['criteria', 'overall'],
    'additionalProperties': False,
}
SYSTEM = ('You grade answers written by an AI data analyst inside GLEN, a private preview of the Moorea Coral Reef '
          'LTER data library, for its developers. For each rubric criterion give pass, fail or not_applicable and '
          'one short sentence of reason. Grade only against the question, the facts given and the analyst\'s tool '
          'calls; you cannot query the data, so treat a number that the facts do not cover as right if it plausibly '
          'came from one of the listed queries, and say so. "overall" is pass when every applicable criterion '
          'passes, fail when an error would mislead a scientist, and borderline otherwise.\n\n'
          'Calibration (2026-11-10): the analyst\'s own instructions require it to give the table, units, time span '
          'and method, show its SQL and cite the DOI or package. Do not count those against "concise"; judge '
          'concision on the rest of the answer. Catalog details such as DOIs, table titles, package IDs, review '
          'status, row counts and date ranges come from tool results you do not see: mark them invented only if '
          'the facts contradict them. The analyst\'s instructions also name Cyclone Oli (February 2010), the 2019 '
          'marine heatwave and bleaching, the Code Red swells (2011, 2022) and the 2006–09 crown-of-thorns band, so '
          'mentioning those is not invention.')
CALIBRATION = '2026-11-10 b: SQL and citations exempt from concise; catalog details and prompt events not invention'
# Concise is reported but does not decide the overall verdict.
NOT_IN_OVERALL = {'concise'}


def verdict(grade):
    """pass when no criterion other than concise fails; otherwise fail. The judge's own
    'overall' is kept beside it; summaries use this one."""
    return 'pass' if all(c['verdict'] != 'fail' for c in grade['criteria'] if c['id'] not in NOT_IN_OVERALL) else 'fail'


def facts_for(question, expected_a, facts):
    qid = question['id']
    out = list(facts.get(qid, []))
    source = 'A1' if qid == 'E1' else qid
    if source in expected_a:
        e = expected_a[source]
        out.insert(0, f"Expected answer, computed from {e['table']} ({e['package']}) by GLEN with this rule: {e['rule']} "
                      f"Result: {json.dumps(e['expected'], default=str)}. {e['note']}")
    return out


def tool_lines(run):
    lines = []
    for t in run.get('tools', [])[:20]:
        args = t['args']
        text = args.get('sql_query') if isinstance(args, dict) and 'sql_query' in args else json.dumps(args, ensure_ascii=False)
        lines.append(f"- {t['tool']}: {text[:600]}")
    return '\n'.join(lines) or '(no tool calls)'


def prompt(question, run, expected_a, facts):
    fact_text = '\n'.join(f'- {f}' for f in facts_for(question, expected_a, facts)) or '- (none beyond the rubric)'
    answer = run.get('shown') or run.get('answer') or ''
    if run.get('checkpoint'):
        answer += '\n\n[The analyst stopped at its checkpoint after its maximum number of data rounds and offered to continue.]'
    return (f'<question>{question["question"]}</question>\n'
            f'<good_answer>{question["expect"]}</good_answer>\n'
            f'<facts>\n{fact_text}\n</facts>\n'
            f'<rubric>\n{RUBRIC}\n</rubric>\n'
            f'<analyst_tool_calls>\n{tool_lines(run)}\n</analyst_tool_calls>\n'
            f'<answer>\n{answer}\n</answer>\n'
            'Grade the answer against every rubric criterion.')


def items(paths, seed=20261110, only=''):
    """Blind, shuffled judge items: [{sample_id, key, question, run}] with key = (file, run index, model)."""
    bank = {q['id']: q for q in checks.load_questions()}
    out = []
    for path in paths:
        data = json.loads(Path(path).read_text())
        for index, run in enumerate(data['runs']):
            q = bank.get(run['question'])
            if q is None or q['group'] not in JUDGED_GROUPS or not run.get('answer'):
                continue
            if only and q['id'] not in only.split(',') and q['group'] not in only.split(','):
                continue
            key = {'file': Path(path).name, 'run': index, 'model': data['model'], 'question': run['question']}
            sample = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:10]
            out.append({'sample_id': sample, 'key': key, 'question': q, 'run': run})
    random.Random(seed).shuffle(out)
    return out


def request(item, expected_a, facts):
    return dict(model=JUDGE_MODEL, max_tokens=16000, system=SYSTEM,
                output_config={'effort': EFFORT, 'format': {'type': 'json_schema', 'schema': SCHEMA}},
                messages=[{'role': 'user', 'content': prompt(item['question'], item['run'], expected_a, facts)}])


def cost_micro(usage):
    return round(usage.input_tokens * PRICE['input'] + usage.output_tokens * PRICE['output'])


def client():
    import anthropic
    from backend import analyst_models
    entry, _ = analyst_models.contestant('claude-haiku-5-5', 'medium')
    return anthropic.Anthropic(api_key=analyst_models.api_key(entry), max_retries=2, timeout=300)


class UsageLimit(RuntimeError):
    """The Claude plan reported a usage or rate limit; the run stops cleanly."""


LIMIT_WORDS = ('usage limit', 'rate limit', 'limit reached', 'limit will reset', 'too many requests', 'overloaded',
               '429', 'quota')
PLAN_USAGE = HERE / 'results/plan-usage.json'


def cli_env(environ=None):
    """The child's environment: no Anthropic key or endpoint, so `claude` can only use the
    logged-in Claude plan and never the GLEN API key (which run_sampling_eval loads from .env)."""
    environ = dict(os.environ if environ is None else environ)
    return {k: v for k, v in environ.items() if not k.startswith('ANTHROPIC_')}


def cli_command(kwargs, system_file, claude='claude'):
    """Headless Claude Code with no tools, no MCP servers, no settings files (so no hooks or
    CLAUDE.md), the same model, effort, system prompt and schema as the API path. Never --bare:
    it reads only ANTHROPIC_API_KEY and ignores the login."""
    return [claude, '-p', '--model', kwargs['model'], '--effort', kwargs['output_config']['effort'],
            '--tools', '', '--strict-mcp-config', '--restricted', '--setting-sources', '',
            '--no-session-persistence', '--output-format', 'json',
            '--system-prompt-file', str(system_file),
            '--json-schema', json.dumps(kwargs['output_config']['format']['schema'])]


def call_cli(kwargs, claude='claude', timeout=900, environ=None):
    """One judge call through `claude -p`. Returns (grade dict, record). Raises UsageLimit when
    the plan is out of usage, RuntimeError on other failures."""
    with tempfile.TemporaryDirectory(prefix='glen-judge-') as scratch:
        system_file = Path(scratch) / 'system.txt'
        system_file.write_text(kwargs['system'])
        done = subprocess.run(cli_command(kwargs, system_file, claude), input=kwargs['messages'][0]['content'],
                              cwd=scratch, env=cli_env(environ), capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        data = None
    message = str((data or {}).get('result') or '')
    problem = (done.stderr or '') + ' ' + message
    if data is None or done.returncode or data.get('is_error'):
        if any(w in problem.lower() for w in LIMIT_WORDS):
            raise UsageLimit(problem.strip()[:300])
        raise RuntimeError(f'claude -p failed (exit {done.returncode}): {problem.strip()[:300]}')
    served = sorted((data.get('modelUsage') or {}).keys())
    if served and kwargs['model'] not in served:
        raise RuntimeError(f'claude -p served {served}, not {kwargs["model"]}; grade not recorded')
    grade = data.get('structured_output')
    if grade is None:
        grade = json.loads(data['result'])
    usage = data.get('usage') or {}
    record = {'served_models': served, 'session_id': data.get('session_id'),
              'usage': {'input_tokens': usage.get('input_tokens', 0)
                        + usage.get('cache_creation_input_tokens', 0) + usage.get('cache_read_input_tokens', 0),
                        'output_tokens': usage.get('output_tokens', 0)},
              'api_equivalent_cost_micro_usd': round((data.get('total_cost_usd') or 0) * 1e6),
              'stop_reason': data.get('stop_reason') or data.get('subtype')}
    return grade, record


def add_plan_usage(record, label):
    """A running count of judge calls made on the Claude plan, apart from spend.json: these
    calls cost no API money and do not count against the eval's --cap-usd."""
    data = json.loads(PLAN_USAGE.read_text()) if PLAN_USAGE.exists() else {'calls': 0, 'api_equivalent_micro_usd': 0, 'runs': []}
    data['calls'] += 1
    data['api_equivalent_micro_usd'] += record['api_equivalent_cost_micro_usd']
    data['runs'].append({'label': label, 'api_equivalent_micro_usd': record['api_equivalent_cost_micro_usd'],
                         'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
    PLAN_USAGE.write_text(json.dumps(data, indent=1) + '\n')


def estimate(paths, output_tokens_per_item=None):
    expected_a, facts = checks.load_expected_a(), json.loads((HERE / 'judge-facts.json').read_text())['questions']
    todo = items(paths)
    c = client()
    counts = []
    for item in todo:
        kwargs = request(item, expected_a, facts)
        counts.append(c.messages.count_tokens(model=kwargs['model'], system=kwargs['system'], messages=kwargs['messages'],
                                              output_config=kwargs['output_config']).input_tokens)
    out_tokens = output_tokens_per_item or 2500
    total = sum(counts) * PRICE['input'] + len(counts) * out_tokens * PRICE['output']
    return {'items': len(counts), 'input_tokens': sum(counts), 'assumed_output_tokens_per_item': out_tokens,
            'projected_usd': round(total / 1e6, 3)}


def judge(paths, n=None, cap_usd=None, label='judge', only='', via='api', min_interval=5.0, claude='claude'):
    """Grade the items. via 'api' calls the Messages API on the GLEN key and adds to spend.json;
    via 'claude-cli' runs headless `claude -p` on the logged-in Claude plan, one call at a time
    at least min_interval seconds apart, counts calls in plan-usage.json and stops at a usage limit."""
    expected_a, facts = checks.load_expected_a(), json.loads((HERE / 'judge-facts.json').read_text())['questions']
    todo = items(paths, only=only)[:n] if n else items(paths, only=only)
    c = client() if via == 'api' else None
    grades, last = [], 0.0
    for item in todo:
        if via == 'api' and cap_usd is not None and rse.spent() >= cap_usd * 1e6:
            print('cap reached')
            break
        started = time.monotonic()
        kwargs = request(item, expected_a, facts)
        grade = {'sample_id': item['sample_id'], 'key': item['key'], 'via': via}
        if via == 'claude-cli':
            time.sleep(max(0.0, min_interval - (started - last)) if last else 0)
            last = time.monotonic()
            try:
                result, record = call_cli(kwargs, claude=claude)
            except UsageLimit as exc:
                print('stopped: Claude plan usage limit:', exc)
                grades.append(dict(grade, stopped='usage_limit', detail=str(exc)))
                break
            add_plan_usage(record, f'{label} {JUDGE_MODEL}@{EFFORT} {item["sample_id"]}')
            grade.update(record, cost_micro_usd=0, grade=result, verdict=verdict(result))
            cost_text = f'API-equivalent ${record["api_equivalent_cost_micro_usd"] / 1e6:.4f} (plan)'
        else:
            response = c.messages.create(**kwargs)
            cost = cost_micro(response.usage)
            rse.add_spend(cost, f'{label} {JUDGE_MODEL}@{EFFORT} {item["sample_id"]}')
            grade.update(stop_reason=response.stop_reason, cost_micro_usd=cost,
                         usage={'input_tokens': response.usage.input_tokens, 'output_tokens': response.usage.output_tokens})
            if response.stop_reason == 'refusal':
                grade['refused'] = True
            else:
                text = next(b.text for b in response.content if b.type == 'text')
                grade['grade'] = json.loads(text)
                grade['verdict'] = verdict(grade['grade'])
            cost_text = f'${cost / 1e6:.4f}'
        grade['seconds'] = round(time.monotonic() - started, 1)
        grades.append(grade)
        print(item['sample_id'], item['key']['question'], grade.get('verdict'), grade.get('grade', {}).get('overall'),
              cost_text, flush=True)
    return grades


def compare(path_a, path_b):
    """How often two judge files agree on the same sample IDs: overall verdict and each criterion."""
    a = {g['sample_id']: g for g in json.loads(Path(path_a).read_text())['grades'] if 'grade' in g}
    b = {g['sample_id']: g for g in json.loads(Path(path_b).read_text())['grades'] if 'grade' in g}
    shared = sorted(set(a) & set(b))
    same = [s for s in shared if a[s].get('verdict') == b[s].get('verdict')]
    crit = {}
    for s in shared:
        va = {c['id']: c['verdict'] for c in a[s]['grade']['criteria']}
        vb = {c['id']: c['verdict'] for c in b[s]['grade']['criteria']}
        for cid in va.keys() & vb.keys():
            hit = crit.setdefault(cid, [0, 0])
            hit[0] += va[cid] == vb[cid]
            hit[1] += 1
    return {'items': len(shared), 'verdict_matches': len(same),
            'differ': [{'sample_id': s, 'question': a[s]['key']['question'], 'a': a[s].get('verdict'), 'b': b[s].get('verdict')}
                       for s in shared if s not in same],
            'criteria': {cid: f'{h[0]}/{h[1]}' for cid, h in sorted(crit.items())}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('estimate', 'sample', 'run', 'compare'))
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--n', type=int, default=1)
    parser.add_argument('--cap-usd', type=float)
    parser.add_argument('--approved-by', default='')
    parser.add_argument('--output-tokens', type=int, help='measured output tokens per item, for estimate')
    parser.add_argument('--questions', default='', help='only these question IDs or groups, e.g. C,D,F')
    parser.add_argument('--via', choices=('api', 'claude-cli'), default='api',
                        help='api: Messages API on the GLEN key (paid); claude-cli: headless claude -p on the logged-in Claude plan')
    parser.add_argument('--min-interval', type=float, default=5.0, help='seconds between claude-cli calls')
    args = parser.parse_args()
    if args.action == 'estimate':
        print(json.dumps(estimate(args.paths, args.output_tokens), indent=1))
        return
    if args.action == 'compare':
        if len(args.paths) != 2:
            raise SystemExit('compare takes two judge files')
        print(json.dumps(compare(*args.paths), indent=1))
        return
    if args.via == 'api' and args.cap_usd is None:
        raise SystemExit('--cap-usd is required for paid actions')
    if args.action == 'run' and not args.approved_by.strip():
        raise SystemExit('The full judge run needs Nic\'s approval: pass --approved-by "Nic, <date>".')
    grades = judge(args.paths, n=args.n if args.action == 'sample' else None, cap_usd=args.cap_usd,
                   label=f'judge-{args.action}', only=args.questions, via=args.via, min_interval=args.min_interval)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    suffix = '-cli' if args.via == 'claude-cli' else ''
    path = HERE / f'results/{stamp}-judge-{args.action}{suffix}.json'
    path.write_text(json.dumps({'judge': f'{JUDGE_MODEL}@{EFFORT}', 'via': args.via, 'calibration': CALIBRATION,
                                'approved_by': args.approved_by or None,
                                'inputs': [Path(p).name for p in args.paths], 'grades': grades},
                               indent=1, ensure_ascii=False) + '\n')
    print('wrote', path.relative_to(ROOT))


if __name__ == '__main__':
    main()
