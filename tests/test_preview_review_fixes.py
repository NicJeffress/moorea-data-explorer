"""Regression boundaries from the independent PR50 review; no live provider calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import httpx
from starlette.requests import Request
from backend.preview_analyst import Ledger, RESERVE_MICRO_USD, complete, provider_side_status
from scripts.prepare_preview_rollback import prepare, LEGACY_COLUMNS


def old_ledger(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE calls (id TEXT PRIMARY KEY, user TEXT NOT NULL, month TEXT NOT NULL, '
                   'started REAL NOT NULL, charge INTEGER NOT NULL, active_until REAL NOT NULL)')
        db.execute('CREATE INDEX call_time ON calls(started)')
        db.execute("INSERT INTO calls VALUES ('old','user','2026-11',1794225600,300,0)")


class ConcurrentMigration(unittest.TestCase):
    def test_workers_can_open_empty_and_old_databases_together(self):
        for existing in (False, True):
            for attempt in range(3):
                with self.subTest(existing=existing, attempt=attempt), tempfile.TemporaryDirectory() as tmp:
                    if existing:
                        old_ledger(Path(tmp) / 'usage.sqlite3')
                    ready = threading.Barrier(8)
                    def start(_):
                        ready.wait(timeout=10)
                        return Ledger(tmp, 25000000)
                    with ThreadPoolExecutor(max_workers=8) as workers:
                        ledgers = list(workers.map(start, range(8)))
                    with ledgers[0].connect() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*), COALESCE(SUM(charge),0) FROM calls').fetchone(),
                                         (1, 300) if existing else (0, 0))
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM provider_health').fetchone()[0], 1)
                        self.assertEqual(len(db.execute('PRAGMA table_info(calls)').fetchall()), 9)


class FixedModelFailures(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_model_failure_pauses_without_using_tester_allowance(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1794225600.]
            ledger = Ledger(tmp, 25000000, clock=lambda: now[0])
            sent = []
            async def receive():
                return {'type': 'http.request', 'body': b'{"messages":[{"role":"user","content":"hello"}]}'}
            def request():
                return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
                                'state': {'preview_identity': 'tester'}}, receive)
            def handler(req):
                sent.append(req)
                return httpx.Response(404, json={'error': {'code': 'model_not_found', 'message': 'private detail'}})
            factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)
            for i in range(8):
                if i >= 3:
                    denied = await complete(request(), ledger, 'fake', factory)
                    self.assertEqual(denied.status_code, 503)
                    self.assertEqual(denied.headers['retry-after'], '60')
                    now[0] += 61
                response = await complete(request(), ledger, 'fake', factory)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn(b'private detail', response.body)
            self.assertEqual(len(sent), 8)
            with ledger.connect() as db:
                self.assertEqual(db.execute('SELECT SUM(charge), SUM(tester_exempt) FROM calls').fetchone(),
                                 (8 * RESERVE_MICRO_USD, 8))
            now[0] += 61
            ledger.finish(ledger.reserve('tester'), 100)  # same tester still has an allowance
            self.assertNotIn(b'private detail', ledger.path.read_bytes())

    def test_only_known_fixed_model_errors_extend_status_classification(self):
        for data in (None, [], {'error': None}, {'error': {'code': 'other'}}, {'error': 'model_not_found'}):
            self.assertFalse(provider_side_status(404, data))
        self.assertFalse(provider_side_status(400, {'error': {'code': 'model_not_found'}}))
        self.assertTrue(provider_side_status(404, {'error': {'code': 'model_not_found'}}))


class CompatibleRollback(unittest.TestCase):
    def test_rollback_preserves_every_charge_and_legacy_insert_then_reupgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, target = root / 'current', root / 'rollback'
            now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc).timestamp()
            ledger = Ledger(state, 25000000, clock=lambda: now)
            ledger.finish(ledger.reserve('settled'), 300)
            ledger.finish(ledger.reserve('operator'), provider_failed=True, tester_exempt=True)
            ledger.reserve('abandoned')
            with ledger.connect() as db:
                before = db.execute('SELECT id,user,month,started,charge FROM calls ORDER BY id').fetchall()
            (state / 'STOP_ANALYST').touch()
            digest = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
            result = prepare(state, target, workers_stopped=True)
            self.assertEqual(result, {'calls': 3, 'charge_micro_usd': 300 + 2*RESERVE_MICRO_USD,
                                      'analyst_paused': True})
            self.assertEqual(hashlib.sha256(ledger.path.read_bytes()).hexdigest(), digest)
            self.assertTrue((target / 'STOP_ANALYST').is_file())
            with closing(sqlite3.connect(target / 'usage.sqlite3')) as db, db:
                self.assertEqual(tuple(row[1] for row in db.execute('PRAGMA table_info(calls)')), LEGACY_COLUMNS)
                self.assertEqual(db.execute('SELECT id,user,month,started,charge FROM calls ORDER BY id').fetchall(), before)
                self.assertEqual(db.execute('SELECT SUM(active_until) FROM calls').fetchone()[0], 0)
                # Exact insertion contract used by pre-PR50 Ledger.reserve().
                db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?)', ('legacy', 'u', '2026-11', now, 99, 0))
            reopened = Ledger(target, 25000000, clock=lambda: now)
            with reopened.connect() as db:
                self.assertEqual(db.execute('SELECT SUM(charge) FROM calls').fetchone()[0], 399 + 2*RESERVE_MICRO_USD)
            # Reuse the active rollback state for any later upgrade, never the older source state.

    def test_requires_stopped_workers_and_never_replaces_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, target = root / 'current', root / 'rollback'
            Ledger(state, 25000000)
            for stopped in (False, True):
                with self.assertRaises(ValueError):
                    prepare(state, target, workers_stopped=stopped)
            (state / 'STOP_ANALYST').touch()
            with self.assertRaises(ValueError):
                prepare(state, target)
            target.mkdir()
            marker = target / 'keep'
            marker.write_text('pre-existing')
            with self.assertRaises(FileExistsError):
                prepare(state, target, workers_stopped=True)
            self.assertEqual(marker.read_text(), 'pre-existing')

    def test_unknown_schema_leaves_source_untouched_and_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, target = root / 'current', root / 'rollback'
            state.mkdir()
            old_ledger(state / 'usage.sqlite3')
            with closing(sqlite3.connect(state / 'usage.sqlite3')) as db:
                db.execute('ALTER TABLE calls ADD COLUMN future_schema TEXT')
            (state / 'STOP_ANALYST').touch()
            digest = hashlib.sha256((state / 'usage.sqlite3').read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'Unknown'):
                prepare(state, target, workers_stopped=True)
            self.assertFalse(target.exists())
            self.assertEqual(hashlib.sha256((state / 'usage.sqlite3').read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
