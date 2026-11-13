"""Merge two branches' results/spend.json by adding their spend, never picking one side.

    .venv/bin/python evals/analyst-models/merge_spend.py <base-ref> <ours-ref> <theirs-ref> [--write]

Each branch's spend.json is the shared base's entries followed by its own new ones. The
merged ledger is the base's entries, then ours' new entries, then theirs', and its total is
the sum of all of them. The eval runners' --cap-usd reads that total, so a merge that kept
one side would undercount spend (Nic, 2026-11-10: "add the two spend totals together").

Refs are git refs (e.g. origin/main, HEAD, MERGE_HEAD). Prints the merged totals; --write
replaces the working copy's spend.json, for use while resolving a merge conflict on it.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = 'glen-explorer/evals/analyst-models/results/spend.json'


def load(ref):
    out = subprocess.run(['git', 'show', f'{ref}:{LEDGER}'], cwd=HERE, capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f'cannot read {LEDGER} at {ref}: {out.stderr.strip()}')
    return json.loads(out.stdout)


def merge(base, ours, theirs):
    """The merged ledger. Raises ValueError if a side does not start with the base's entries
    or its total is not the sum of its entries."""
    for name, side in (('base', base), ('ours', ours), ('theirs', theirs)):
        if side['micro_usd'] != sum(r['micro_usd'] for r in side['runs']):
            raise ValueError(f'{name}: micro_usd is not the sum of its runs')
    n = len(base['runs'])
    for name, side in (('ours', ours), ('theirs', theirs)):
        if side['runs'][:n] != base['runs']:
            raise ValueError(f'{name} does not start with the base entries; merge by hand')
    runs = base['runs'] + ours['runs'][n:] + theirs['runs'][n:]
    return {'micro_usd': sum(r['micro_usd'] for r in runs), 'runs': runs}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('base')
    parser.add_argument('ours')
    parser.add_argument('theirs')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    base, ours, theirs = load(args.base), load(args.ours), load(args.theirs)
    try:
        merged = merge(base, ours, theirs)
    except ValueError as exc:
        raise SystemExit(str(exc))
    for name, side in (('base', base), ('ours', ours), ('theirs', theirs), ('merged', merged)):
        print(f'{name}: ${side["micro_usd"] / 1e6:.6f} over {len(side["runs"])} runs')
    if args.write:
        path = HERE / 'results/spend.json'
        path.write_text(json.dumps(merged, indent=1) + '\n')
        print('wrote', path)


if __name__ == '__main__':
    sys.exit(main())
