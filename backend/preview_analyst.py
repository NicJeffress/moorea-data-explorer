"""Private-preview model gate. No prompts, replies, keys or email addresses are persisted."""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from starlette.responses import JSONResponse

from . import analyst_models, analyst_provider, answer_check
from .analyst_provider import ProviderUnavailable

# The default model's limits. Other models' holds come from their allowlist
# entries (analyst_models.reservation); these names stay for existing callers.
DEFAULT = analyst_models.MODELS[analyst_models.DEFAULT_MODEL]
MODEL = DEFAULT.id
MAX_INPUT = DEFAULT.max_input
MAX_OUTPUT = DEFAULT.max_output


def price_micro_usd(prompt_tokens, completion_tokens):
    """Standard text prices verified 2026-11-06: $0.75/M input, $4.50/M output.

    Cached input is deliberately charged at the uncached rate. Update the prices
    and limits together after checking the official model page, never from a request.
    """
    return (3 * prompt_tokens + 18 * completion_tokens + 3) // 4


# Reserve the entire context plus the output cap. Derived, so a valid settlement
# can never exceed the hold it replaces.
RESERVE_MICRO_USD = price_micro_usd(MAX_INPUT, MAX_OUTPUT)
if RESERVE_MICRO_USD != analyst_models.reservation(DEFAULT):
    raise RuntimeError('The default model hold no longer matches its allowlist prices.')
TESTER_MONTH_MICRO_USD = 2000000
TESTER_DAY_MICRO_USD = 1000000
FAILURES_BEFORE_PAUSE = 3
PAUSE_SECONDS = 60
# Failures further apart than this are not "repeated": the streak starts again.
FAILURE_WINDOW_SECONDS = 600
LEASE_SECONDS = 120


class Denied(Exception):
    def __init__(self, message, status=429, retry_after=None):
        self.message, self.status, self.retry_after = message, status, retry_after


def failure(message, status, retry_after=None):
    headers = {'Cache-Control': 'no-store'}
    if retry_after is not None:
        headers['Retry-After'] = str(max(1, math.ceil(retry_after)))
    return JSONResponse({'error': {'message': message}}, status_code=status, headers=headers)


def validate(body):
    """Text-only chat request, whatever model will serve it. Raises ValueError."""
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list) or not body['messages']:
        raise ValueError('Messages required.')
    for message in body['messages']:
        if not isinstance(message, dict):
            raise ValueError('Invalid message.')
        content = message.get('content')
        if content is not None and not isinstance(content, str):
            if not isinstance(content, list) or any(not isinstance(p, dict) or
                    set(p) - {'type', 'text'} or p.get('type') != 'text' or
                    not isinstance(p.get('text'), str) for p in content):
                raise ValueError('Preview supports text only.')
    tools = body.get('tools', [])
    if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get('type') != 'function' for t in tools):
        raise ValueError('Only function tools are supported.')
    return body


def payload(body):
    """The default model's OpenAI request (kept for existing callers and tests)."""
    return analyst_provider.openai_payload(DEFAULT, validate(body))


def usage_cost(data):
    u = data.get('usage', {}) if isinstance(data, dict) else {}
    if not isinstance(u, dict):
        return None
    p, c = u.get('prompt_tokens'), u.get('completion_tokens')
    if type(p) is not int or type(c) is not int or not (0 <= p <= MAX_INPUT and 0 <= c <= MAX_OUTPUT):
        return None  # uncertain billing retains the full reservation
    return price_micro_usd(p, c)


def provider_side_status(status, data=None):
    """True when an HTTP error status reflects the provider or the operator's
    account (outage, rate limit, key), not the tester's request."""
    error = data.get('error') if isinstance(data, dict) else None
    # The server fixes the model and endpoint; a missing/inaccessible model
    # cannot be corrected by changing the tester's conversation. OpenAI names it
    # with `code`; Anthropic's error envelope ({"type": "error", ...}) with `type`.
    missing_model = (status == 404 and isinstance(error, dict)
                     and (error.get('code') == 'model_not_found'
                          or (data.get('type') == 'error' and error.get('type') == 'not_found_error')))
    # 402 is Anthropic's billing_error: the operator's account, not the request.
    return status >= 500 or status in (401, 402, 403, 429) or missing_model


def _add_column(db, table, column, declaration):
    if column not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')


def _live(mono_start, mono_until, mono_now):
    # A monotonic reading below the start means the host restarted: whatever
    # was running then is gone. Wall-clock steps never move these readings.
    return mono_start <= mono_now < mono_until


class Ledger:
    def __init__(self, directory, budget_micro_usd, clock=time.time,
                 tester_month_micro_usd=TESTER_MONTH_MICRO_USD,
                 tester_day_micro_usd=TESTER_DAY_MICRO_USD, monotonic=None):
        if any(type(v) is not int or v < 0 for v in
               (budget_micro_usd, tester_month_micro_usd, tester_day_micro_usd)):
            raise ValueError('Analyst budgets must be nonnegative integer microdollars.')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'usage.sqlite3'
        self.budget, self.clock = budget_micro_usd, clock
        # Leases and pauses use a host-wide monotonic clock; months and days use
        # wall time. A fake wall clock in tests drives both unless given separately.
        self.monotonic = monotonic or (time.monotonic if clock is time.time else clock)
        self.tester_month, self.tester_day = tester_month_micro_usd, tester_day_micro_usd
        with self.connect() as db:
            # Serialize schema inspection as well as ALTER TABLE. Multiple workers
            # may open the same old (or empty) state directory on first startup.
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, user TEXT NOT NULL, '
                       'month TEXT NOT NULL, started REAL NOT NULL, charge INTEGER NOT NULL, active_until REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS call_time ON calls(started)')
            # Rows written by older code have no monotonic lease, so read as expired.
            _add_column(db, 'calls', 'mono_started', 'REAL NOT NULL DEFAULT 0')
            _add_column(db, 'calls', 'mono_until', 'REAL NOT NULL DEFAULT 0')
            # Holds from a provider error status count toward the operator budget
            # but not toward the tester's own allowance.
            _add_column(db, 'calls', 'tester_exempt', 'INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS provider_health ('
                       'id INTEGER PRIMARY KEY CHECK(id=1), failures INTEGER NOT NULL, '
                       'paused_until REAL NOT NULL)')
            _add_column(db, 'provider_health', 'mono_last_failure', 'REAL NOT NULL DEFAULT 0')
            _add_column(db, 'provider_health', 'mono_paused_until', 'REAL NOT NULL DEFAULT 0')
            db.execute('INSERT OR IGNORE INTO provider_health (id,failures,paused_until) VALUES (1,0,0)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _health(db, mono):
        """The current failure streak and pause end, after restarts and stale streaks."""
        failures, last, until = db.execute('SELECT failures, mono_last_failure, mono_paused_until '
                                           'FROM provider_health WHERE id=1').fetchone()
        if not failures or mono < last or mono - last > FAILURE_WINDOW_SECONDS:
            return 0, 0
        return failures, until

    def _record_failures(self, db, count, mono):
        failures = self._health(db, mono)[0] + count
        paused = failures >= FAILURES_BEFORE_PAUSE
        db.execute('UPDATE provider_health SET failures=?, paused_until=?, mono_last_failure=?, '
                   'mono_paused_until=? WHERE id=1',
                   (failures, self.clock() + PAUSE_SECONDS if paused else 0, mono,
                    mono + PAUSE_SECONDS if paused else 0))

    def reserve(self, identity, hold=RESERVE_MICRO_USD):
        """Admit one call and hold `hold` microdollars: the most the served model
        can bill (analyst_models.reservation), so settlement never exceeds it."""
        if type(hold) is not int or hold <= 0:
            raise ValueError('Invalid analyst hold.')
        user = hashlib.sha256(identity.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now, mono = self.clock(), self.monotonic()
            # An expired lease may have reached the provider, so retain its full
            # hold and count it once as an unaccounted failure.
            expired = db.execute('UPDATE calls SET active_until=0 WHERE active_until>0 '
                                 'AND NOT (mono_started<=? AND ?<mono_until)', (mono, mono)).rowcount
            if expired:
                self._record_failures(db, expired, mono)
            try:
                return self._admit(db, user, now, mono, hold)
            except Denied:
                db.commit()  # Keep the recovery above; a denial writes nothing else.
                raise

    def _admit(self, db, user, now, mono, hold):
        if (self.directory / 'STOP_ANALYST').exists() or self.budget <= 0:
            raise Denied('The analyst is paused. Charts remain available.', 503)
        paused_until = self._health(db, mono)[1]
        if paused_until > mono:
            seconds = math.ceil(paused_until - mono)
            raise Denied(f'The model provider is unavailable. Try again in {seconds} seconds. '
                         'Charts remain available.', 503, seconds)
        # Expired leases were recovered above in this transaction.
        if db.execute('SELECT COUNT(*) FROM calls WHERE active_until>0').fetchone()[0]:
            raise Denied('The analyst is busy. Please retry shortly.', 503)
        recent = db.execute('SELECT COUNT(*), MIN(started) FROM calls WHERE user=? AND started>?',
                            (user, now - 60)).fetchone()
        if recent[0] >= 15:
            raise Denied('Analyst limit: 15 model calls per minute per tester.', 429, recent[1] + 60 - now)
        utc = datetime.fromtimestamp(now, timezone.utc)
        month = utc.strftime('%Y-%m')
        day_start = utc.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = db.execute('SELECT COALESCE(SUM(charge),0) FROM calls WHERE month=?', (month,)).fetchone()[0]
        if spent + hold > self.budget:
            raise Denied('The preview analyst budget is exhausted. Charts remain available.', 503)
        user_month = db.execute('SELECT COALESCE(SUM(charge),0) FROM calls WHERE user=? AND month=? '
                                'AND tester_exempt=0', (user, month)).fetchone()[0]
        user_day = db.execute('SELECT COALESCE(SUM(charge),0) FROM calls WHERE user=? AND started>=? '
                              'AND started<? AND tester_exempt=0',
                              (user, day_start.timestamp(), day_start.timestamp() + 86400)).fetchone()[0]
        if user_month + hold > self.tester_month:
            next_month = (day_start.replace(day=1) + timedelta(days=32)).replace(day=1)
            raise Denied('Your monthly analyst allowance is exhausted. Charts remain available.',
                         429, next_month.timestamp() - now)
        if user_day + hold > self.tester_day:
            raise Denied('Your daily analyst allowance is exhausted. Charts remain available.',
                         429, day_start.timestamp() + 86400 - now)
        ident = uuid.uuid4().hex
        db.execute('INSERT INTO calls (id,user,month,started,charge,active_until,mono_started,mono_until) '
                   'VALUES (?,?,?,?,?,?,?,?)',
                   (ident, user, month, now, hold, now + LEASE_SECONDS, mono, mono + LEASE_SECONDS))
        return ident

    def finish(self, ident, cost=None, *, provider_failed=False, tester_exempt=False):
        """Settle a call once. `provider_failed` counts toward the pause;
        `tester_exempt` (a provider error status) keeps the hold in the operator
        budget but not in the tester's allowance."""
        if cost is not None and (type(cost) is not int or cost < 0):
            raise ValueError('Invalid analyst settlement.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            held = db.execute('SELECT charge FROM calls WHERE id=? AND active_until>0', (ident,)).fetchone()
            if cost is not None and held is not None and cost > held[0]:
                # A settlement above this call's own hold would mean the hold
                # undercounted. Leaving the method rolls back; the hold stays.
                raise ValueError('Invalid analyst settlement.')
            if cost is not None:
                changed = db.execute('UPDATE calls SET charge=?, active_until=0 WHERE id=? AND active_until>0',
                                     (cost, ident)).rowcount
            else:
                changed = db.execute('UPDATE calls SET active_until=0, tester_exempt=? WHERE id=? AND active_until>0',
                                     (int(bool(tester_exempt)), ident)).rowcount
            if not changed:
                return  # Repeated settlement must not change the charge or the breaker.
            if provider_failed:
                self._record_failures(db, 1, self.monotonic())
            elif cost is not None:
                db.execute('UPDATE provider_health SET failures=0, paused_until=0, mono_last_failure=0, '
                           'mono_paused_until=0 WHERE id=1')


async def complete(request, ledger, api_key, client_factory=httpx.AsyncClient, *, entry=None,
                   claude_factory=None):
    """One reserved analyst call. `entry` is the allowlisted model the server
    selected (the default model when omitted); the browser cannot choose it."""
    entry = entry or DEFAULT
    if not api_key:
        return failure('The preview analyst is not configured. Charts remain available.', 503)
    try:
        prepared = analyst_provider.prepare(entry, validate(await request.json()))
    except (ValueError, TypeError, KeyError):
        return failure('Invalid text-only model request.', 400)
    hold = analyst_models.reservation(entry)
    try:
        ident = ledger.reserve(request.state.preview_identity, hold)
    except Denied as exc:
        return failure(exc.message, exc.status, exc.retry_after)
    except sqlite3.Error:
        return failure('The analyst usage ledger is unavailable.', 503)
    cost = None
    # Only provider-side outcomes feed the pause. A request the provider rejects
    # (a 4xx such as an over-long conversation) or a cancelled request does not.
    provider_failed = tester_exempt = False
    try:
        # No waiting queue, no automatic retry. Each tool round is a separate reservation.
        import asyncio
        async with asyncio.timeout(80):
            outcome = await analyst_provider.send(entry, prepared, api_key, http_factory=client_factory,
                                                  claude_factory=claude_factory)
        if outcome.status != 200:
            # Classify only from the known fields; never persist or expose the
            # provider's error body. An unparseable 4xx keeps its tester hold.
            provider_failed = tester_exempt = provider_side_status(outcome.status, outcome.error)
            return failure('The model provider could not complete the request. Please retry later.', 502)
        cost = outcome.cost
        provider_failed = cost is None
        # A final answer with a known-wrong phrasing gets a fixed note appended:
        # no second provider call, so the hold above still covers the request.
        data, checked = answer_check.review(outcome.data)
        if isinstance(data, dict):
            # Served model, usage, cost, latency and stop reason for evals; no text.
            data = dict(data, glen_analyst=dict(outcome.record, hold_micro_usd=hold, answer_check=checked,
                                                charged_micro_usd=hold if cost is None else cost))
        return JSONResponse(data, headers={'Cache-Control': 'no-store'})
    except (httpx.HTTPError, TimeoutError, ValueError, ProviderUnavailable):
        provider_failed = True
        return failure('The analyst is temporarily unavailable. Please retry later.', 502)
    finally:
        # A crash, cancellation or uncertain provider outcome never refunds possible spend.
        # Keep the full hold for every uncertain or unclassified error. A byte
        # estimate or HTTP status alone is not a proof of the provider's charge.
        try:
            ledger.finish(ident, cost, provider_failed=provider_failed, tester_exempt=tester_exempt)
        except (sqlite3.Error, ValueError):
            # Reservation is already durable. Retain it if settlement is unavailable;
            # do not replace a completed response with an unhandled ledger exception.
            pass
