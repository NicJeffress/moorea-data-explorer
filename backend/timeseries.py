"""Aligned period comparison and gap-reporting resampling for the analyst.

`compare_periods` compares one column across groups (stations, sites) and periods
(years, months, date ranges) only over the days, or months, on which every compared
series has values. It returns per-series statistics over that shared window, what was
left out and why ("FOR05 has no data after 2018-07-20"), and every requested group or
period with no data.

`resample` returns hour, day, week or month bins with records, mean, min and max.
Bins with no data are left out and counted, never filled; nothing is interpolated.
Every gap between consecutive records longer than a threshold is listed.

Both run publisher-built SQL with bound parameters inside the engine's sandbox
(read-only, no external access, 15 s, 512 MB) and bound their output. Values equal to
the column's declared missing codes or to a common fill value (9999, -9999, 99999,
-99999, -999), NaN and infinities are excluded and counted, never averaged. Source
values are otherwise used as recorded. Counts are records, never a share of a grid.
"""
import math
import re
from datetime import date, datetime, timedelta

from . import catalog
from .engine import fetch_params
from .sampling import NUMERIC, _as_list, _quote, _time_column, _when, default_group_by, interval_text

COMMON_FILLS = (9999.0, -9999.0, 99999.0, -99999.0, -999.0)
MAX_GROUPS = 12
MAX_PERIODS = 6
MAX_SERIES = 24
MAX_SINGLE_PERIOD_DAYS = 3660
MAX_RUNS = 12
MAX_REASONS = 4
MAX_BINS = 240
MAX_GAPS = 25
INTERVALS = {'hour': 3600, 'day': 86400, 'week': 7 * 86400, 'month': None}
BIN_FORMAT = {'hour': '%Y-%m-%d %H:00', 'day': '%Y-%m-%d', 'week': '%Y-%m-%d', 'month': '%Y-%m'}
DAILY_RATIO = 10  # a series with this many days with values per month with values is compared by day
ALL = 'all records'


def _num(x):
    return None if x is None else float(f'{x:.6g}')


def _day(text):
    return date.fromisoformat(text)


# --------------------------------------------------------------------------- shared set-up

class _Spec:
    """Validated table, time, value, group and filter expressions for one column."""

    def __init__(self, dataset_id, column, group_by, groups, where, time_column, record=None):
        record = record or catalog.get_record(dataset_id)
        if not record:
            raise ValueError('Unknown dataset. Use browse_stac_catalog to find an approved local ID.')
        meta = record.get('glen_metadata') or {}
        if record.get('readiness') == 'archive_table_needs_review':
            use = ((meta.get('use_instead') or {}).get('value') or {}).get('table')
            raise ValueError('This archive table keeps raw text values. Use the typed table '
                             + (f'{use} ' if use else '') + '(get_schema glen_metadata.use_instead) instead.')
        self.record = record
        by_name = {c['name']: c for c in record['columns']}
        self.tname, self.texpr = _time_column(record, time_column)
        measured = record.get('_time') or {}
        tcols = set(measured.get('columns', [])) if self.tname == measured.get('label') else {self.tname}

        if not isinstance(column, str) or column not in by_name or column in tcols:
            raise ValueError(f'Unknown or non-measurement column {column!r}; read get_schema for column names.')
        info = by_name[column]
        ctype = str(info.get('type', '')).upper()
        if ctype.startswith(NUMERIC):
            self.vexpr = f'CAST({_quote(column)} AS DOUBLE)'
        elif ctype == 'VARCHAR':
            self.vexpr = f'TRY_CAST(TRIM({_quote(column)}) AS DOUBLE)'
        else:
            raise ValueError(f'{column!r} is {ctype or "not numeric"}; these tools need a numeric column.')
        self.column, self.unit = column, info.get('unit')
        fills = set(COMMON_FILLS)
        for code in info.get('missing_codes') or []:
            try:
                value = float(code)
            except (TypeError, ValueError):
                continue  # text codes such as NA are not numbers; TRY_CAST already drops them
            if math.isfinite(value):  # NaN and infinities are excluded by isnan/isinf
                fills.add(value)
        self.fills = sorted(fills)
        fill_list = ', '.join(repr(f) for f in self.fills)
        self.is_fill = f'(v IN ({fill_list}) OR isnan(v) OR isinf(v))'
        self.valid = f'(v IS NOT NULL AND NOT {self.is_fill})'

        gcols = _as_list(group_by)
        self.group_note = None
        if gcols is None:
            gcols = default_group_by(record, self.tname)
            if gcols:
                self.group_note = f'Grouped by {", ".join(gcols)} by default; pass group_by=[] to pool all rows.'
        if len(gcols) > 3:
            raise ValueError('At most 3 group_by columns.')
        for g in gcols:
            if g not in by_name or g in tcols or g == column:
                raise ValueError(f'Unknown group_by column {g!r}.')
        self.gcols = gcols
        self.label = ("concat_ws(' / ', " + ', '.join(f'CAST({_quote(g)} AS VARCHAR)' for g in gcols) + ')'
                      if gcols else f"'{ALL}'")
        self.requested = [str(g) for g in _as_list(groups)] if groups not in (None, '', []) else None
        if self.requested and not gcols:
            raise ValueError('groups needs a group_by column (the table has no default station column).')
        if self.requested and len(self.requested) > MAX_GROUPS:
            raise ValueError(f'At most {MAX_GROUPS} groups per call.')

        self.filters, self.params = [], []
        if where:
            if not isinstance(where, dict):
                raise ValueError('where must be an object of column: value or [values].')
            for col, values in where.items():
                if col not in by_name or col in tcols:
                    raise ValueError(f'Unknown where column {col!r}.')
                values = values if isinstance(values, list) else [values]
                if not values or len(values) > 20:
                    raise ValueError('Each where column takes 1 to 20 values.')
                self.filters.append(f'CAST({_quote(col)} AS VARCHAR) IN ({", ".join("?" * len(values))})')
                self.params += [str(v) for v in values]
        self.where = dict(where or {})
        if self.requested:
            marks = ', '.join('?' * len(self.requested))
            first = f'CAST({_quote(gcols[0])} AS VARCHAR)'
            self.filters.append(f'({self.label} IN ({marks}) OR {first} IN ({marks}))')
            self.params += self.requested * 2
        self.table = _quote(record['table'])

    def base(self, time_conditions, time_params, extra_select=''):
        """The filtered rows: g (group label), t, v, plus extra_select. Returns (sql, params)."""
        where = [f'{self.texpr} IS NOT NULL', time_conditions] + self.filters
        sql = (f'SELECT {self.label} AS g, {self.texpr} AS t, {self.vexpr} AS v{extra_select} '
               f'FROM {self.table} WHERE ' + ' AND '.join(where))
        return sql, list(time_params) + self.params

    def matches(self, requested, label):
        return label == requested or (label.split(' / ')[0] == requested if self.gcols else False)

    def header(self):
        out = {'dataset_id': self.record['id'], 'table': self.record['table'], 'column': self.column,
               'unit': self.unit, 'time_column': self.tname, 'group_by': self.gcols}
        if self.where:
            out['where'] = self.where
        if self.group_note:
            out['note'] = self.group_note
        return out


def _excluded(spec, count):
    if not count:
        return None
    return {'records': count, 'values': 'declared missing codes and common fill values '
            + ', '.join(f'{f:g}' for f in spec.fills) + ', NaN and infinities',
            'rule': 'excluded, never averaged; all other values as recorded'}


# --------------------------------------------------------------------------- compare_periods

def _period(text):
    """'2018' | '2018-03' | '2018-03-05' | '2018-01-01/2018-07-01' (end exclusive) -> (label, start, end)."""
    text = str(text).strip()
    if '/' in text:
        a, b = (s.strip() for s in text.split('/', 1))
        lo, hi = _when(a, 'period start'), _when(b, 'period end')
    elif re.fullmatch(r'\d{4}', text):
        lo = datetime(int(text), 1, 1)
        hi = datetime(lo.year + 1, 1, 1)
    elif re.fullmatch(r'\d{4}-\d{2}', text):
        lo = datetime(int(text[:4]), int(text[5:]), 1)
        hi = datetime(lo.year + (lo.month == 12), lo.month % 12 + 1, 1)
    elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        lo = _when(text, 'period')
        hi = lo + timedelta(days=1)
    else:
        raise ValueError(f'Period {text!r}: use 2018, 2018-03, 2018-03-05 or 2018-01-01/2018-07-01 (end exclusive).')
    if not lo or not hi or hi <= lo:
        raise ValueError(f'Period {text!r}: the end must be after the start (end exclusive).')
    return text, lo, hi


def _days(lo, hi):
    """Calendar days touched by [lo, hi)."""
    d, last = lo.date(), (hi - timedelta(microseconds=1)).date()
    while d <= last:
        yield d
        d += timedelta(days=1)


def _month_of(d):
    return d.strftime('%Y-%m')


def _runs(items, step):
    """Sorted unique items -> [(first, last, count)] of consecutive runs, step(a) == next item."""
    runs = []
    for x in items:
        if runs and step(runs[-1][1]) == x:
            runs[-1] = (runs[-1][0], x, runs[-1][2] + 1)
        else:
            runs.append((x, x, 1))
    return runs


def _next_month(m):
    y, mo = int(m[:4]), int(m[5:7])
    return f'{y + (mo == 12):04d}-{mo % 12 + 1:02d}'


def _prev_month(m):
    y, mo = int(m[:4]), int(m[5:7])
    return f'{y - (mo == 1):04d}-{(mo - 2) % 12 + 1:02d}'


def _key_runs_text(keys, align, multi):
    """Shared keys -> readable runs, capped."""
    if align == 'day':
        if multi:  # month-day keys; consecutive on a leap reference year
            ref = sorted(date.fromisoformat('2000-' + k) for k in keys)
            runs = _runs(ref, lambda d: d + timedelta(days=1))
            text = [(a.strftime('%m-%d'), b.strftime('%m-%d'), n) for a, b, n in runs]
        else:
            runs = _runs(sorted(_day(k) for k in keys), lambda d: d + timedelta(days=1))
            text = [(str(a), str(b), n) for a, b, n in runs]
    else:
        if multi:
            runs = _runs(sorted(int(k) for k in keys), lambda m: m + 1)
            text = [(f'{a:02d}', f'{b:02d}', n) for a, b, n in runs]
        else:
            runs = _runs(sorted(keys), _next_month)
            text = list(runs)
    unit = 'days' if align == 'day' else 'months'
    out = [{'from': a, 'to': b, unit: n} for a, b, n in text]
    return out


def compare_periods(dataset_id, column, periods, groups=None, group_by=None, where=None,
                    align='auto', time_column=None, record=None):
    """See the module docstring. `record` lets tests pass a fixture catalog record."""
    spec = _Spec(dataset_id, column, group_by, groups, where, time_column, record)
    plist = [_period(p) for p in (_as_list(periods) or [])]
    if not plist:
        raise ValueError('Name one or more periods, e.g. ["2018"] or ["2017", "2018"].')
    if len(plist) > MAX_PERIODS:
        raise ValueError(f'At most {MAX_PERIODS} periods per call.')
    ordered = sorted(plist, key=lambda p: p[1])
    for (_, _, hi), (label, lo, _) in zip(ordered, ordered[1:]):
        if lo < hi:
            raise ValueError(f'Periods overlap at {label}; give separate, non-overlapping periods.')
    multi = len(plist) > 1
    for label, lo, hi in plist:
        span = (hi - lo).days
        if multi and span > 366:
            raise ValueError(f'Period {label} is longer than a year; when comparing periods, each is at most a '
                             'year so that the same months and days can be matched.')
        if span > MAX_SINGLE_PERIOD_DAYS:
            raise ValueError('At most 10 years per call; compare shorter periods.')
    if align not in ('auto', 'day', 'month'):
        raise ValueError("align must be 'auto', 'day' or 'month'.")

    tconds = ' OR '.join(f'({spec.texpr} >= ? AND {spec.texpr} < ?)' for _ in plist)
    tparams = [x for _, lo, hi in plist for x in (lo, hi)]
    pcase = 'CASE ' + ' '.join(f'WHEN t >= ? AND t < ? THEN {i}' for i in range(len(plist))) + ' END'
    base, params = spec.base(f'({tconds})', tparams)
    rows = fetch_params(
        f'WITH base AS ({base}), b AS (SELECT *, {pcase} AS p FROM base) '
        "SELECT g, p, strftime(t, '%Y-%m-%d') AS d, count(*) AS n_rows, "
        f'count(*) FILTER (WHERE {spec.valid}) AS n, count(*) FILTER (WHERE {spec.is_fill}) AS fills, '
        f'min(t) FILTER (WHERE {spec.valid}), max(t) FILTER (WHERE {spec.valid}) '
        'FROM b GROUP BY ALL ORDER BY g, p, d', params + tparams)

    # Per series (group label, period index): days with values and their record counts.
    series = {}
    for g, p, d, n_rows, n, fills, first, last in rows:
        s = series.setdefault((g, p), {'rows': 0, 'fills': 0, 'days': {}, 'first': None, 'last': None})
        s['rows'] += n_rows
        s['fills'] += fills
        if n:
            s['days'][d] = n
            s['first'] = first if s['first'] is None or first < s['first'] else s['first']
            s['last'] = last if s['last'] is None or last > s['last'] else s['last']
    labels = sorted({g for g, _ in series})
    if spec.requested is None and len(labels) > MAX_GROUPS:
        raise ValueError(f'{len(labels)} groups have rows in these periods; name up to {MAX_GROUPS} with groups, '
                         'or narrow with where.')

    # Every requested group x period with no values is named.
    no_data = []
    wanted = labels if spec.requested is None else spec.requested
    for req in wanted:
        found = [g for g in labels if spec.matches(req, g)] if spec.requested else [req]
        for i, (plabel, _, _) in enumerate(plist):
            hits = [series.get((g, i)) for g in found]
            hits = [h for h in hits if h]
            if not any(h['days'] for h in hits):
                rows_seen = sum(h['rows'] for h in hits)
                reason = (f'{rows_seen} rows but no {spec.column} values' if rows_seen else 'no rows')
                no_data.append({'group': req if spec.gcols else ALL, 'period': plabel,
                                'reason': f'no data in {plabel} ({reason})'})
    compared = [(g, i) for (g, i), s in sorted(series.items()) if s['days']]
    if len(compared) > MAX_SERIES:
        raise ValueError(f'{len(compared)} group-period series; at most {MAX_SERIES}. Name fewer groups or periods.')
    out = spec.header() | {'periods': [p[0] for p in plist]}
    if no_data:
        out['no_data'] = no_data
    if not compared:
        out['error'] = 'No values in any requested group and period.'
        return out

    # Alignment: by day when every series has values on most days of its months, else by month.
    if align == 'auto':
        ratios = [len(series[k]['days']) / len({d[:7] for d in series[k]['days']}) for k in compared]
        align = 'day' if min(ratios) >= DAILY_RATIO else 'month'
        reason = ('every series has values on most days of its months' if align == 'day'
                  else 'at least one series has values on only a few days per month')
    else:
        reason = 'as requested'

    def key(d):
        if align == 'day':
            return d[5:] if multi else d
        return d[5:7] if multi else d[:7]

    keys = {k: {} for k in compared}
    for k in compared:
        for d, n in series[k]['days'].items():
            keys[k][key(d)] = keys[k].get(key(d), 0) + n
    shared = set.intersection(*(set(v) for v in keys.values()))
    union = set().union(*(set(v) for v in keys.values()))

    out |= {'align': align, 'align_reason': reason,
            'aligned_on': {('day', False): 'calendar days', ('day', True): 'month-day in each period',
                           ('month', False): 'calendar months', ('month', True): 'month of year in each period'
                           }[(align, multi)]}

    # Why each series lost records: its own runs with no data while another series has data.
    left_out = []
    for k in compared:
        g, i = k
        plabel, lo, hi = plist[i]
        mine = keys[k]
        dropped = sum(n for kk, n in mine.items() if kk not in shared)
        if align == 'day':
            cal = list(_days(lo, hi))
            missing = [d for d in cal if key(str(d)) not in mine]
            runs = _runs(missing, lambda d: d + timedelta(days=1))
            runs = [(a, b, n) for a, b, n in runs
                    if any(key(str(a + timedelta(days=j))) in union for j in range(n))]
            first_cal, last_cal = cal[0], cal[-1]
        else:
            months = sorted({_month_of(d) for d in _days(lo, hi)})
            missing = [m for m in months if key(m + '-01') not in mine]
            runs = _runs(missing, _next_month)
            runs = [r for r in runs if any(key(m + '-01') in union for m in months if r[0] <= m <= r[1])]
            first_cal, last_cal = months[0], months[-1]
        who = ' '.join(([g] if spec.gcols else []) + ([plabel] if multi or not spec.gcols else []))
        reasons = []
        for a, b, n in sorted(runs, key=lambda r: -r[2])[:MAX_REASONS]:
            if a == first_cal and b == last_cal:
                continue
            if b == last_cal:
                before = a - timedelta(days=1) if align == 'day' else _prev_month(a)
                reasons.append(f'{who} has no data after {before}')
            elif a == first_cal:
                after = b + timedelta(days=1) if align == 'day' else _next_month(b)
                reasons.append(f'{who} has no data before {after}')
            else:
                reasons.append(f'{who} has no data from {a} to {b} ({n} {"days" if align == "day" else "months"})')
        if dropped or reasons:
            left_out.append({'group': g if spec.gcols else ALL, 'period': plabel,
                             'records_left_out': dropped, 'why': reasons or
                             ['other series have no data on some of its ' + ('days' if align == 'day' else 'months')]})

    runs = _key_runs_text(shared, align, multi)
    out['shared_window'] = {'count': len(shared), 'unit': 'days' if align == 'day' else 'months',
                            'runs': runs[:MAX_RUNS]}
    if len(runs) > MAX_RUNS:
        out['shared_window']['runs_listed'] = f'first {MAX_RUNS} of {len(runs)}'

    stats_by = {}
    if shared:
        kexpr = {('day', False): "strftime(t, '%Y-%m-%d')", ('day', True): "strftime(t, '%m-%d')",
                 ('month', False): "strftime(t, '%Y-%m')", ('month', True): "strftime(t, '%m')"}[(align, multi)]
        srows = fetch_params(
            f'WITH base AS ({base}), b AS (SELECT *, {pcase} AS p FROM base WHERE {spec.valid}) '
            'SELECT g, p, count(*), avg(v), median(v), min(v), max(v), min(t), max(t) FROM b '
            f'WHERE list_contains(?, {kexpr}) GROUP BY g, p ORDER BY g, p',
            params + tparams + [sorted(shared)])
        for g, p, n, mean, median, lo_v, hi_v, first, last in srows:
            stats_by[(g, p)] = {'records': n, 'mean': _num(mean), 'median': _num(median),
                                'min': _num(lo_v), 'max': _num(hi_v), 'first': str(first), 'last': str(last)}
    out['stats_over_shared_window'] = [
        {'group': g if spec.gcols else ALL, 'period': plist[i][0], **stats_by.get((g, i), {'records': 0})}
        for g, i in compared]
    out['whole_period'] = [
        {'group': g if spec.gcols else ALL, 'period': plist[i][0],
         'records': sum(series[(g, i)]['days'].values()),
         'first': str(series[(g, i)]['first']), 'last': str(series[(g, i)]['last'])} for g, i in compared]
    if left_out:
        out['left_out'] = left_out
    fills = sum(s['fills'] for s in series.values())
    if fills:
        out['excluded_fill_values'] = _excluded(spec, fills)
    if not shared:
        out['no_shared_window'] = ('These series have no ' + ('day' if align == 'day' else 'month')
                                   + ' with values in common, so they cannot be compared like-for-like. '
                                   + ("Try align='month', or " if align == 'day' else '')
                                   + 'report each series with its own dates.')
    out['how_to_read'] = ('stats_over_shared_window covers only the ' + out['aligned_on']
                          + ' on which every compared series has values; compare those. whole_period gives each '
                          "series' own records and dates, not like-for-like. left_out says what each series lost "
                          'and why; name every group in no_data. Records are as stored; fill values are excluded.')
    return out


# --------------------------------------------------------------------------- resample

def _gap_seconds(text):
    if text in (None, ''):
        return None
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)\s*',
                     str(text).lower())
    if not m:
        raise ValueError("min_gap is a length such as '6 h', '2 days' or '1 week'.")
    size = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}[m.group(2)[0] if not m.group(2).startswith('min')
                                                          else 'm']
    return float(m.group(1)) * size


def _bin_starts(first, last, interval):
    """Every bin start from the bin of `first` through the bin of `last`."""
    if interval == 'month':
        y, m = first.year, first.month
        while (y, m) <= (last.year, last.month):
            yield datetime(y, m, 1)
            y, m = y + (m == 12), m % 12 + 1
        return
    if interval == 'week':
        t = datetime.combine(first.date() - timedelta(days=first.weekday()), datetime.min.time())
    elif interval == 'day':
        t = datetime.combine(first.date(), datetime.min.time())
    else:
        t = first.replace(minute=0, second=0, microsecond=0)
    step = timedelta(seconds=INTERVALS[interval])
    while t <= last:
        yield t
        t += step


def resample(dataset_id, column, interval='day', start=None, end=None, groups=None, group_by=None, where=None,
             min_gap=None, time_column=None, record=None):
    """See the module docstring. `record` lets tests pass a fixture catalog record."""
    if interval not in INTERVALS:
        raise ValueError("interval must be 'hour', 'day', 'week' or 'month'.")
    spec = _Spec(dataset_id, column, group_by, groups, where, time_column, record)
    lo, hi = _when(start, 'start'), _when(end, 'end')
    if lo and hi and hi <= lo:
        raise ValueError('end must be after start (end is exclusive).')
    gap_floor = _gap_seconds(min_gap)
    conds, tparams = [], []
    if lo:
        conds.append(f'{spec.texpr} >= ?')
        tparams.append(lo)
    if hi:
        conds.append(f'{spec.texpr} < ?')
        tparams.append(hi)
    base, params = spec.base(' AND '.join(conds) or 'TRUE', tparams)
    fmt = BIN_FORMAT[interval]
    bins = fetch_params(
        f'WITH base AS ({base}), b AS (SELECT g, date_trunc(\'{interval}\', t) AS bin, t, v FROM base) '
        f"SELECT g, strftime(bin, '{fmt}') AS label, bin, count(*) FILTER (WHERE {spec.valid}) AS n, "
        f'avg(v) FILTER (WHERE {spec.valid}), min(v) FILTER (WHERE {spec.valid}), '
        f'max(v) FILTER (WHERE {spec.valid}), count(*) FILTER (WHERE {spec.is_fill}) AS fills, '
        f'count(*) OVER () AS total '
        f'FROM b GROUP BY g, bin ORDER BY g, bin LIMIT {4 * MAX_BINS + 1}', params)
    with_values = [b for b in bins if b[3]]
    total_groups = len({b[0] for b in bins})
    if spec.requested is None and total_groups > MAX_GROUPS:
        raise ValueError(f'{total_groups} groups have rows; name up to {MAX_GROUPS} with groups, or narrow with where.')
    if len(bins) > 4 * MAX_BINS or len(with_values) > MAX_BINS:
        n = bins[0][-1] if bins else 0
        raise ValueError(f'About {n} {interval} bins; at most {MAX_BINS} per call. Use a coarser interval, a '
                         'shorter start/end window, or fewer groups.')

    # Spacing between consecutive records with values, per group.
    steps = fetch_params(
        f'WITH base AS ({base}), k AS (SELECT g, t FROM base WHERE {spec.valid}), '
        'd AS (SELECT g, t, t - LAG(t) OVER (PARTITION BY g ORDER BY t) AS dt FROM k) '
        'SELECT g, epoch(mode(dt) FILTER (WHERE dt > INTERVAL 0 SECOND)), count(*), min(t), max(t) '
        'FROM d GROUP BY g ORDER BY g', params)
    info = {g: {'typical': typ, 'records': n, 'first': first, 'last': last} for g, typ, n, first, last in steps}
    for g, i in info.items():
        i['threshold'] = gap_floor or max(86400.0, 3 * (i['typical'] or 0))
    floor = min((i['threshold'] for i in info.values()), default=86400.0)
    gap_rows = fetch_params(
        f'WITH base AS ({base}), k AS (SELECT g, t FROM base WHERE {spec.valid}), '
        'd AS (SELECT g, t, t - LAG(t) OVER (PARTITION BY g ORDER BY t) AS dt FROM k) '
        'SELECT g, t - dt, t, epoch(dt) FROM d WHERE epoch(dt) > ? ORDER BY g, t LIMIT 5000',
        params + [floor])

    out = spec.header() | {'interval': interval, 'start': str(lo) if lo else None,
                           'end_exclusive': str(hi) if hi else None,
                           'bin_fields': ['bin_start', 'records', 'mean', 'min', 'max'],
                           'bin_start_rule': {'hour': 'hour start', 'day': 'calendar day',
                                              'week': 'Monday of the week', 'month': 'calendar month'}[interval]
                           + ', times as stored'}
    series, no_data = [], []
    labels = sorted(info)
    wanted = spec.requested if spec.requested is not None else labels
    for req in wanted:
        found = any(spec.matches(req, g) for g in labels) if spec.requested else req in info
        if not found:
            no_data.append({'group': req, 'reason': 'no data' + (' in this window' if lo or hi else '')})
    fills_total = sum(b[7] for b in bins)
    for g in labels:
        i = info[g]
        mine = [b for b in with_values if b[0] == g]
        window_first = lo or i['first']
        window_last = (hi - timedelta(microseconds=1)) if hi else i['last']
        all_bins = sum(1 for _ in _bin_starts(window_first, window_last, interval))
        gaps = []
        if lo and (i['first'] - lo).total_seconds() > i['threshold']:
            gaps.append({'last_before': None, 'next': str(i['first']), 'days': round((i['first'] - lo).total_seconds()
                                                                                    / 86400, 1),
                         'note': 'no data from the window start'})
        for gg, before, after, secs in gap_rows:
            if gg == g and secs > i['threshold']:
                gaps.append({'last_before': str(before), 'next': str(after), 'days': round(secs / 86400, 1)})
        if hi and (hi - i['last']).total_seconds() > i['threshold']:
            gaps.append({'last_before': str(i['last']), 'next': None,
                         'days': round((hi - i['last']).total_seconds() / 86400, 1),
                         'note': 'no data to the window end'})
        entry = {'group': g if spec.gcols else ALL, 'records': i['records'], 'first': str(i['first']),
                 'last': str(i['last']), 'typical_interval': interval_text(i['typical']),
                 'bins_with_data': len(mine), 'bins_without_data': max(all_bins - len(mine), 0),
                 'bins': [[b[1], b[3], _num(b[4]), _num(b[5]), _num(b[6])] for b in mine],
                 'gaps_longer_than': interval_text(i['threshold']), 'gap_count': len(gaps)}
        if len(gaps) > MAX_GAPS:
            gaps = sorted(sorted(gaps, key=lambda x: -x['days'])[:MAX_GAPS],
                          key=lambda x: x['last_before'] or '')
            entry['gaps_listed'] = f'longest {MAX_GAPS} of {entry["gap_count"]}'
        entry['gaps'] = gaps
        series.append(entry)
    out['series'] = series
    if no_data:
        out['no_data'] = no_data
    if fills_total:
        out['excluded_fill_values'] = _excluded(spec, fills_total)
    out['how_to_read'] = ('Bins with no data are left out (bins_without_data counts them); nothing is filled or '
                          'interpolated. gaps lists every stretch between consecutive records longer than '
                          'gaps_longer_than (default: 1 day or 3 typical intervals, whichever is longer), '
                          'including the window edges. Say "no data from … to …"; never a share of bins.')
    return out


# --------------------------------------------------------------------------- tool wrappers

def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        return {'error': str(exc)}
    except Exception as exc:  # duckdb out-of-memory, interrupt or temp-space refusal
        text = str(exc)
        if any(k in text for k in ('memory', 'Interrupt', 'temp', 'directory')):
            return {'error': 'Too large to scan at once. Use shorter periods or a shorter start/end window '
                             '(a year for 20-minute tables, a few months for 1–2 minute loggers), fewer '
                             'groups, or a where filter.'}
        return {'error': 'Time-series tool failed: ' + text[:300]}


def compare_periods_safe(*args, **kwargs):
    return _safe(compare_periods, *args, **kwargs)


def resample_safe(*args, **kwargs):
    return _safe(resample, *args, **kwargs)
