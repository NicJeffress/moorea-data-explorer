"""Fault and quota checks use fake time/providers; no API access or research inputs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from starlette.requests import Request
from backend.preview_access import Settings
from backend.preview_analyst import (Ledger, Denied, MAX_INPUT, MAX_OUTPUT, RESERVE_MICRO_USD,
                                     complete, price_micro_usd, usage_cost)


class Fairness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc).timestamp()
        self.ledger = Ledger(self.tmp.name, 25000000, clock=lambda: self.now)

    def spend(self, user, times, amount=100000):
        for _ in range(times):
            self.ledger.finish(self.ledger.reserve(user), amount)

    def test_daily_allowance_keeps_other_testers_available(self):
        self.spend('a', 7)
        with self.assertRaisesRegex(Denied, 'daily'):
            self.ledger.reserve('a')
        self.ledger.finish(self.ledger.reserve('b'), 100)
        self.now += 86400
        self.ledger.finish(self.ledger.reserve('a'), 100)

    def test_monthly_allowance_is_persistent_and_resets_in_utc(self):
        self.spend('a', 7)
        self.now += 86400
        self.spend('a', 7)
        self.now += 86400
        self.spend('a', 3)
        restarted = Ledger(self.tmp.name, 25000000, clock=lambda: self.now)
        with self.assertRaisesRegex(Denied, 'monthly'):
            restarted.reserve('a')
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
        restarted.finish(restarted.reserve('a'), 100)

    def test_uncertain_holds_count_toward_daily_and_monthly_allowances(self):
        for _ in range(3):
            self.ledger.finish(self.ledger.reserve('a'))
        with self.assertRaisesRegex(Denied, 'daily'):
            self.ledger.reserve('a')
        self.now += 86400
        for _ in range(3):
            self.ledger.finish(self.ledger.reserve('a'))
        self.now += 86400
        with self.assertRaisesRegex(Denied, 'monthly'):
            self.ledger.reserve('a')

    def test_breaker_survives_restart_and_only_one_recovery_call_is_admitted(self):
        for i in range(3):
            self.ledger.finish(self.ledger.reserve(str(i)), provider_failed=True)
        restarted = Ledger(self.tmp.name, 25000000, clock=lambda: self.now)
        with self.assertRaisesRegex(Denied, '60 seconds'):
            restarted.reserve('waiting')
        self.now += 60
        def attempt(i):
            try: return restarted.reserve(str(i))
            except Denied: return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        calls = [x for x in results if x]
        self.assertEqual(len(calls), 1)
        restarted.finish(calls[0], provider_failed=True)
        with self.assertRaisesRegex(Denied, '60 seconds'):
            restarted.reserve('waiting')
        self.now += 60
        restarted.finish(restarted.reserve('recover'), 100)
        restarted.finish(restarted.reserve('next'), 100)

    def test_repeated_settlement_is_idempotent_and_cannot_refund_uncertainty(self):
        ident = self.ledger.reserve('a')
        self.ledger.finish(ident, provider_failed=True)
        self.ledger.finish(ident, 0)
        self.ledger.finish(ident, provider_failed=True)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT charge FROM calls').fetchone()[0], RESERVE_MICRO_USD)
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 1)

    def test_invalid_configuration_or_settlement_cannot_undercount(self):
        for value in [-1, True, 1.5]:
            with self.assertRaises(ValueError):
                Ledger(Path(self.tmp.name)/'bad', value)
        ident = self.ledger.reserve('a')
        for value in [-1, True, RESERVE_MICRO_USD+1]:
            with self.assertRaises(ValueError):
                self.ledger.finish(ident, value)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT charge FROM calls').fetchone()[0], RESERVE_MICRO_USD)

    def test_abandoned_calls_pause_recovery_without_refunding_or_double_counting(self):
        calls = []
        for i in range(3):
            calls.append(self.ledger.reserve(str(i)))
            self.now += 121  # Simulate a process dying before settlement.
            self.ledger = Ledger(self.tmp.name, 25000000, clock=lambda: self.now)
        for _ in range(2):
            with self.assertRaisesRegex(Denied, '60 seconds'):
                self.ledger.reserve('recovery')
        # A late successful callback cannot refund a recovered uncertain call
        # or clear a newer failure pause.
        self.ledger.finish(calls[-1], 0)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT SUM(charge) FROM calls').fetchone()[0], 3*RESERVE_MICRO_USD)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls WHERE active_until>0').fetchone()[0], 0)
        self.now += 60
        self.ledger.finish(self.ledger.reserve('recovery'), 100)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 0)

    def test_concurrent_recovery_counts_abandoned_call_once(self):
        for i in range(2):
            self.ledger.finish(self.ledger.reserve(str(i)), provider_failed=True)
        self.ledger.reserve('crashed')
        self.now += 121
        def attempt(i):
            try: return self.ledger.reserve(str(i))
            except Denied: return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(attempt, range(8))), [None]*8)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 3)


class ProviderFaults(unittest.IsolatedAsyncioTestCase):
    async def test_provider_errors_pause_dispatch_and_retain_uncertain_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1794225600.]
            ledger = Ledger(tmp, 25000000, clock=lambda: now[0])
            sent = []
            def handler(request):
                sent.append(1)
                return httpx.Response(503, json={'error': {'message': 'unavailable'}})
            factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)
            def request(identity):
                async def receive():
                    return {'type': 'http.request', 'body': json.dumps({'messages': [{'role': 'user', 'content': 'private fixture'}]}).encode()}
                return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
                                'state': {'preview_identity': identity}}, receive)
            for i in range(3):
                self.assertEqual((await complete(request(str(i)), ledger, 'fake', factory)).status_code, 502)
            self.assertEqual((await complete(request('fourth'), ledger, 'fake', factory)).status_code, 503)
            self.assertEqual(len(sent), 3)
            with ledger.connect() as db:
                self.assertEqual(db.execute('SELECT SUM(charge) FROM calls').fetchone()[0], 3*RESERVE_MICRO_USD)
            self.assertNotIn(b'private fixture', ledger.path.read_bytes())

    async def run_statuses(self, statuses, usage=None):
        """Send one call per status (or one 200 with `usage`); return ledger, calls sent, responses."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Ledger(tmp.name, 25000000, clock=lambda: 1794225600.)
        sent, responses = [], []
        def handler(request):
            status = statuses[len(sent)]
            sent.append(status)
            return httpx.Response(status, json={'choices': [], 'usage': usage} if status == 200 else {'error': {}})
        factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)
        for i in range(len(statuses) + 1):
            async def receive():
                return {'type': 'http.request', 'body': b'{"messages":[{"role":"user","content":"x"}]}'}
            responses.append(await complete(Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
                                                     'state': {'preview_identity': str(i)}}, receive),
                                            ledger, 'fake', factory) if i < len(statuses) else None)
        return ledger, sent, responses

    async def test_requests_the_provider_rejects_do_not_pause_other_testers(self):
        ledger, sent, responses = await self.run_statuses([400, 413, 422, 404, 200],
                                                           {'prompt_tokens': 10, 'completion_tokens': 1})
        self.assertEqual(len(sent), 5)  # no pause after four rejected requests
        self.assertEqual([r.status_code for r in responses[:5]], [502, 502, 502, 502, 200])
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 0)
            # Rejected requests still keep their hold against the tester's allowance.
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls WHERE charge=? AND tester_exempt=0',
                                        (RESERVE_MICRO_USD,)).fetchone()[0], 4)

    async def test_provider_error_statuses_pause_but_spare_the_tester_allowance(self):
        for status in (500, 503, 429, 401):
            ledger, sent, responses = await self.run_statuses([status] * 3)
            with ledger.connect() as db:
                self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 3, status)
                self.assertEqual(db.execute('SELECT SUM(tester_exempt), SUM(charge) FROM calls').fetchone(),
                                 (3, 3 * RESERVE_MICRO_USD), status)

    async def test_pause_and_allowance_denials_say_when_to_retry(self):
        ledger, _, _ = await self.run_statuses([503] * 3)
        async def receive():
            return {'type': 'http.request', 'body': b'{"messages":[{"role":"user","content":"x"}]}'}
        r = await complete(Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [],
                                    'state': {'preview_identity': 'later'}}, receive), ledger, 'fake')
        self.assertEqual((r.status_code, r.headers['retry-after']), (503, '60'))

    async def test_largest_valid_usage_settles_within_the_hold(self):
        self.assertEqual(RESERVE_MICRO_USD, price_micro_usd(MAX_INPUT, MAX_OUTPUT))
        top = {'prompt_tokens': MAX_INPUT, 'completion_tokens': MAX_OUTPUT}
        self.assertEqual(usage_cost({'usage': top}), RESERVE_MICRO_USD)
        ledger, _, responses = await self.run_statuses([200], top)
        self.assertEqual(responses[0].status_code, 200)
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT charge, active_until FROM calls').fetchall(), [(RESERVE_MICRO_USD, 0)])


class ClockAndStreak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wall = datetime(2026, 11, 9, 12, tzinfo=timezone.utc).timestamp()
        self.mono = 5000.

    def ledger(self, **kw):
        return Ledger(self.tmp.name, 25000000, clock=lambda: self.wall, monotonic=lambda: self.mono, **kw)

    def test_wall_clock_steps_do_not_expire_a_live_call(self):
        ledger = self.ledger()
        ident = ledger.reserve('a')
        self.wall += 3600  # NTP step during the call
        with self.assertRaisesRegex(Denied, 'busy'):
            self.ledger().reserve('b')
        self.mono += 30
        ledger.finish(ident, 100)
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT charge FROM calls').fetchone()[0], 100)
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 0)
        self.wall -= 7200  # and a backward step cannot stretch a pause
        for i in range(3):
            ledger.finish(ledger.reserve(f'f{i}'), provider_failed=True)
        self.mono += 60
        ledger.finish(ledger.reserve('after'), 100)

    def test_host_restart_releases_old_leases_and_pauses(self):
        ledger = self.ledger()
        for i in range(3):
            ledger.finish(ledger.reserve(f'f{i}'), provider_failed=True)
        with self.assertRaisesRegex(Denied, '60 seconds'):
            ledger.reserve('waiting')
        self.mono = 10.  # the host restarted; its monotonic clock began again
        restarted = self.ledger()
        restarted.reserve('live')  # the pause ended with the old host; this call is then cut off
        self.mono = 5.  # a second restart, before the lease's own monotonic start
        again = self.ledger()
        again.finish(again.reserve('b'), 100)
        with again.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls WHERE active_until>0').fetchone()[0], 0)

    def test_failures_far_apart_are_not_a_streak(self):
        ledger = self.ledger()
        for i in range(2):
            ledger.finish(ledger.reserve(f'f{i}'), provider_failed=True)
        self.mono += 601
        self.wall += 601
        ledger.finish(ledger.reserve('f2'), provider_failed=True)
        ledger.finish(ledger.reserve('next'), 100)  # not paused
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 0)

    def test_provider_outage_does_not_use_up_the_tester_allowance(self):
        ledger = self.ledger()
        for _ in range(8):
            ledger.finish(ledger.reserve('a'), provider_failed=True, tester_exempt=True)
            self.mono += 61
            self.wall += 61
        ledger.finish(ledger.reserve('a'), 100)
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT SUM(charge) FROM calls').fetchone()[0], 8 * RESERVE_MICRO_USD + 100)

    def test_allowance_denials_carry_the_time_to_reset(self):
        ledger = self.ledger(tester_day_micro_usd=RESERVE_MICRO_USD)
        ledger.finish(ledger.reserve('a'), 1)
        with self.assertRaises(Denied) as daily:
            ledger.reserve('a')
        self.assertEqual(daily.exception.retry_after, 12 * 3600)
        ledger = Ledger(Path(self.tmp.name) / 'month', 25000000, clock=lambda: self.wall,
                        tester_month_micro_usd=RESERVE_MICRO_USD)
        ledger.finish(ledger.reserve('a'), 1)
        with self.assertRaises(Denied) as monthly:
            ledger.reserve('a')
        reset = datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(monthly.exception.retry_after, reset - self.wall)

    def test_denied_admission_keeps_recovered_leases(self):
        ledger = self.ledger()
        ledger.reserve('a')
        (Path(self.tmp.name) / 'STOP_ANALYST').touch()
        self.mono += 121
        with self.assertRaisesRegex(Denied, 'paused'):
            ledger.reserve('b')
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT active_until, charge FROM calls').fetchall(), [(0, RESERVE_MICRO_USD)])
            self.assertEqual(db.execute('SELECT failures FROM provider_health').fetchone()[0], 1)

    def test_older_ledger_files_are_upgraded(self):
        import sqlite3
        from contextlib import closing
        path = Path(self.tmp.name) / 'usage.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE calls (id TEXT PRIMARY KEY, user TEXT NOT NULL, month TEXT NOT NULL, '
                       'started REAL NOT NULL, charge INTEGER NOT NULL, active_until REAL NOT NULL)')
            db.execute('CREATE TABLE provider_health (id INTEGER PRIMARY KEY CHECK(id=1), '
                       'failures INTEGER NOT NULL, paused_until REAL NOT NULL)')
            db.execute('INSERT INTO provider_health VALUES (1,0,0)')
            db.execute("INSERT INTO calls VALUES ('old','u','2026-11',?,?,?)",
                       (self.wall, RESERVE_MICRO_USD, self.wall + 120))
        ledger = self.ledger()
        ledger.finish(ledger.reserve('a'), 100)  # the old in-flight row reads as abandoned
        with ledger.connect() as db:
            self.assertEqual(db.execute('SELECT SUM(charge) FROM calls').fetchone()[0], RESERVE_MICRO_USD + 100)


class TesterCeilingSettings(unittest.TestCase):
    def test_ceilings_come_from_the_environment_and_are_validated(self):
        import os
        from unittest import mock
        env = {'GLEN_PREVIEW_ORIGIN': 'https://preview.example.org',
               'GLEN_ACCESS_ISSUER': 'https://test.cloudflareaccess.com', 'GLEN_ACCESS_AUD': 'aud',
               'GLEN_PREVIEW_EMAILS': 'a@example.org', 'GLEN_PREVIEW_RELEASE': '/srv/release',
               'GLEN_PREVIEW_STATE': '/srv/state', 'GLEN_PREVIEW_TESTER_DAY_MICRO_USD': '500000',
               'GLEN_PREVIEW_TESTER_MONTH_MICRO_USD': '1500000'}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.environment()
        self.assertEqual((settings.tester_day, settings.tester_month), (500000, 1500000))
        with mock.patch.dict(os.environ, env | {'GLEN_PREVIEW_TESTER_DAY_MICRO_USD': '-1'}, clear=True):
            with self.assertRaises(ValueError):
                Settings.environment()


if __name__ == '__main__':
    unittest.main()
