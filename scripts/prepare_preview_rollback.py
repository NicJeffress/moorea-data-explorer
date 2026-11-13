"""Make a paused, six-column ledger copy for pre-PR50 code after all workers stop.

Never overwrite the current state or refund a charge. Run from the NEW checkout,
before switching code, and retain both state directories. See preview-operations.md.
"""
import argparse
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3


LEGACY_COLUMNS = ('id', 'user', 'month', 'started', 'charge', 'active_until')
PR50_COLUMNS = LEGACY_COLUMNS + ('mono_started', 'mono_until', 'tester_exempt')


def prepare(source_state, output_state, *, workers_stopped=False):
    source_state, output_state = Path(source_state).resolve(), Path(output_state).resolve()
    if not workers_stopped or not (source_state / 'STOP_ANALYST').is_file():
        raise ValueError('Create STOP_ANALYST, stop every worker, then confirm --workers-stopped.')
    source = source_state / 'usage.sqlite3'
    if not source.is_file():
        raise ValueError('Source usage.sqlite3 does not exist.')
    # A new directory prevents accidental replacement of either the live ledger
    # or an earlier rollback. The original is opened read-only, including WAL.
    output_state.mkdir(mode=0o700)
    try:
        (output_state / 'STOP_ANALYST').touch(mode=0o600)
        target = output_state / 'usage.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(target)) as copy:
                original.backup(copy)
                columns = tuple(row[1] for row in copy.execute('PRAGMA table_info(calls)'))
                if columns not in (LEGACY_COLUMNS, PR50_COLUMNS):
                    raise ValueError('Unknown calls schema; no supported rollback conversion.')
                before = copy.execute('SELECT COUNT(*), COALESCE(SUM(charge),0) FROM calls').fetchone()
                with copy:
                    copy.execute('CREATE TABLE calls_legacy (id TEXT PRIMARY KEY, user TEXT NOT NULL, '
                                 'month TEXT NOT NULL, started REAL NOT NULL, charge INTEGER NOT NULL, '
                                 'active_until REAL NOT NULL)')
                    # All workers are stopped. Unsettled calls keep their full
                    # reservation; only the dead concurrency lease is released.
                    copy.execute('INSERT INTO calls_legacy SELECT id,user,month,started,charge,0 FROM calls')
                    copy.execute('DROP TABLE calls')
                    copy.execute('ALTER TABLE calls_legacy RENAME TO calls')
                    copy.execute('CREATE INDEX call_time ON calls(started)')
                after = copy.execute('SELECT COUNT(*), COALESCE(SUM(charge),0) FROM calls').fetchone()
                if before != after or copy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Rollback verification failed.')
        target.chmod(0o600)
        return {'calls': after[0], 'charge_micro_usd': after[1], 'analyst_paused': True}
    except BaseException:
        # This directory was created by this invocation and did not pre-exist.
        shutil.rmtree(output_state)
        raise


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state', type=Path, required=True)
    parser.add_argument('--output-state', type=Path, required=True)
    parser.add_argument('--workers-stopped', action='store_true',
                        help='Confirm every app/analyst worker is stopped; this tool cannot stop them.')
    args = parser.parse_args()
    print(json.dumps(prepare(args.source_state, args.output_state, workers_stopped=args.workers_stopped)))
