"""Summarise hand grades of the sampling eval (results/hand-grades-2026-11-10.json, or the file named).

Hand grades are authoritative; the regex auto scores are a cross-check only.
Each grade list follows the check order in sampling-questions.json.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
grades = json.loads((HERE / 'results' / (sys.argv[1] if len(sys.argv) > 1 else 'hand-grades-2026-11-10.json')).read_text())
bank = {q['id']: q for q in json.loads((HERE / 'sampling-questions.json').read_text())['questions']}

table = defaultdict(lambda: {'runs': 0, 'passed': 0, 'checks': 0, 'checks_passed': 0, 'checkpoints': 0,
                             'cost': 0, 'per_check': defaultdict(lambda: [0, 0])})
for name, entry in grades['files'].items():
    data = json.loads((HERE / 'results' / name).read_text())
    for index, marks in entry['runs'].items():
        run = data['runs'][int(index)]
        key = (entry['label'], run['question'])
        row = table[key]
        row['runs'] += 1
        row['passed'] += all(marks)
        row['checks'] += len(marks)
        row['checks_passed'] += sum(marks)
        row['checkpoints'] += bool(run.get('checkpoint'))
        row['cost'] += run['cost_micro_usd']
        for check, mark in zip(bank[run['question']]['checks'], marks):
            row['per_check'][check['id']][0] += mark
            row['per_check'][check['id']][1] += 1

print('| Model and version | Question | Runs passing every check | Checks passed | Checkpoints | Cost |')
print('|---|---|---|---|---|---|')
for (label, q), r in sorted(table.items()):
    print(f'| {label} | {q} | {r["passed"]}/{r["runs"]} | {r["checks_passed"]}/{r["checks"]} | '
          f'{r["checkpoints"]} | ${r["cost"] / 1e6:.2f} |')
print()
for (label, q), r in sorted(table.items()):
    misses = {k: f'{v[0]}/{v[1]}' for k, v in r['per_check'].items() if v[0] < v[1]}
    if misses:
        print(f'- {label} {q}: ' + ', '.join(f'{k} {v}' for k, v in misses.items()))
