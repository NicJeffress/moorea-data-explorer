"""Scores and costs per model and group for results/*-questions.json (and judge grades, if any).

    .venv/bin/python evals/analyst-models/summarize_question_eval.py results/X-questions.json ... [--judge results/Y-judge-run.json]

Exact-graded questions (A, B, E1–E3) count as correct when every exact check passes; the
rescored marks come from checks.py at summary time, so a corrected expected value rescores
old answers without a new paid run. Judge-graded groups count as correct when the judge's
verdict is pass (judge.verdict: no criterion but concise fails). Prints a Markdown table.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checks  # noqa: E402


def rows(paths, judge_paths=()):
    bank = {q['id']: q for q in checks.load_questions()}
    ea, eb = checks.load_expected_a(), checks.load_expected_b()
    verdicts = {}
    for jp in judge_paths:
        for g in json.loads(Path(jp).read_text())['grades']:
            k = g['key']
            # Later judge files replace earlier grades of the same answer. A grade made before the
            # calibration has no 'verdict'; its judge-only 'overall' is used then.
            verdicts[(k['file'], k['run'])] = 'refused' if g.get('refused') else g.get('verdict') or g['grade']['overall']
    table = defaultdict(lambda: dict(runs=0, graded=0, correct=0, cost=0, checkpoints=0, errors=0))
    for path in paths:
        data = json.loads(Path(path).read_text())
        for index, run in enumerate(data['runs']):
            q = bank[run['question']]
            row = table[(data['model'], q['group'])]
            row['runs'] += 1
            row['cost'] += run['cost_micro_usd']
            row['checkpoints'] += bool(run.get('checkpoint'))
            if run.get('error'):
                row['errors'] += 1
                row['graded'] += 1
                continue
            _, exact = checks.score(q, run, ea, eb)
            if exact is not None:
                row['graded'] += 1
                row['correct'] += exact
            elif (Path(path).name, index) in verdicts:
                row['graded'] += 1
                row['correct'] += verdicts[(Path(path).name, index)] == 'pass'
    return table


def per_question(paths):
    """{question: {model: [passes, runs]}} for the exact-graded questions, to show which answers vary."""
    bank = {q['id']: q for q in checks.load_questions()}
    ea, eb = checks.load_expected_a(), checks.load_expected_b()
    out = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for path in paths:
        data = json.loads(Path(path).read_text())
        for run in data['runs']:
            if run.get('error'):
                continue
            _, exact = checks.score(bank[run['question']], run, ea, eb)
            if exact is not None:
                cell = out[run['question']][data['model']]
                cell[0] += exact
                cell[1] += 1
    return out


def per_question_markdown(cells):
    models = sorted({m for row in cells.values() for m in row})
    lines = ['| Question | ' + ' | '.join(models) + ' |', '|---|' + '---|' * len(models)]
    for q in sorted(cells, key=lambda q: (q[0], int(q[1:]))):
        lines.append(f'| {q} | ' + ' | '.join(f'{cells[q][m][0]}/{cells[q][m][1]}' if m in cells[q] else '–' for m in models) + ' |')
    return '\n'.join(lines)


def markdown(table):
    lines = ['| Model | Group | Runs | Correct | Cost | Cost per question | Cost per correct answer | Checkpoints |',
             '|---|---|---|---|---|---|---|---|']
    for (model, group), r in sorted(table.items()):
        correct = f"{r['correct']}/{r['graded']}" if r['graded'] else 'judge not run'
        per_correct = f"${r['cost'] / r['correct'] / 1e6:.4f}" if r['correct'] else '–'
        lines.append(f"| {model} | {group} | {r['runs']} | {correct} | ${r['cost'] / 1e6:.3f} | "
                     f"${r['cost'] / r['runs'] / 1e6:.4f} | {per_correct} | {r['checkpoints']} |")
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--judge', nargs='*', default=[])
    parser.add_argument('--per-question', action='store_true', help='also print exact-check passes per question and model')
    args = parser.parse_args()
    print(markdown(rows(args.paths, args.judge)))
    if args.per_question:
        print()
        print(per_question_markdown(per_question(args.paths)))
