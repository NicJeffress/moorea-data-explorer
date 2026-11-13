"""Observed sampling of a time-stamped table: when each column actually has values.

`describe_sampling` answers "how often, from when to when, and where are the
gaps" for every group and non-null column, measured from the stored timestamps.
It never states counts as a share of a nominal grid: a 20-minute table can hold
an instrument that reports every 3 hours, and that is its schedule, not a loss.
"""
from datetime import date, datetime, timedelta

from . import catalog
from .engine import fetch_params

STATION_COLUMNS = ('mooring', 'site', 'station_code', 'station', 'location')
MAX_COLUMNS = 6
MAX_GROUP_COLUMNS = 3
MAX_SERIES = 40
MAX_GAPS = 25
MONTHLY_LIMIT_MONTHS = 36
NUMERIC = ('FLOAT', 'DOUBLE', 'REAL', 'DECIMAL', 'INTEGER', 'BIGINT', 'SMALLINT', 'TINYINT', 'HUGEINT')


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def interval_text(seconds):
    """3600 -> '1 h'; 1200 -> '20 min'; 86400 -> '1 day'."""
    if seconds is None:
        return None
    seconds = round(seconds)
    for unit, size in (('day', 86400), ('h', 3600), ('min', 60)):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f'{n} {unit}' + ('s' if unit == 'day' and n != 1 else '')
    if seconds >= 3600:
        return f'{seconds / 3600:.2f} h'
    if seconds >= 60:
        return f'{seconds / 60:.1f} min'
    return f'{seconds} s'


def _when(value, name):
    if value in (None, ''):
        return None
    if isinstance(value, (date, datetime)):
        return datetime(value.year, value.month, value.day) if not isinstance(value, datetime) else value
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f'{name} must be an ISO date or date-time such as 2018-01-01.') from None


def _time_column(record, requested):
    columns = {c['name']: c for c in record['columns']}
    if requested:
        if requested not in columns:
            raise ValueError(f'Unknown time column {requested!r}.')
        info = catalog.temporal_column_info(columns[requested])
        if info['kind'] != 'timestamp':
            raise ValueError(f'{requested!r} is not a date/time column.')
        return requested, info['sql_expr']
    measured = record.get('_time')  # validated by scripts/build_table_metadata.py
    if measured and measured['kind'] == 'timestamp':
        return measured['label'], measured['sql']
    for c in record['columns']:
        if c.get('type') in ('TIMESTAMP', 'DATE', 'TIMESTAMP WITH TIME ZONE'):
            return c['name'], f'CAST({_quote(c["name"])} AS TIMESTAMP)'
    for c in record['columns']:
        info = catalog.temporal_column_info(c)
        if info['kind'] == 'timestamp':
            return c['name'], info['sql_expr']
    raise ValueError('This table has no date/time column; sampling intervals do not apply. '
                     'Count rows per year or survey with query instead.')


def _as_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(',') if v.strip()]
    return list(value)


def default_group_by(record, tname):
    """The measured group columns for this time column, else the first station-like column."""
    meta = record.get('glen_metadata') or {}
    if meta.get('group_by') is not None and meta.get('time_column') == tname:
        return list(meta['group_by'])
    names = {c['name'] for c in record['columns']}
    return [c for c in STATION_COLUMNS if c in names][:1]


def describe_sampling(dataset_id, columns=None, start=None, end=None, group_by=None,
                      time_column=None, period='auto', record=None):
    """See the module docstring. `record` lets tests pass a fixture catalog record."""
    record = record or catalog.get_record(dataset_id)
    if not record:
        return {'error': 'Unknown dataset. Use browse_stac_catalog to find an approved local ID.'}
    by_name = {c['name']: c for c in record['columns']}
    tname, texpr = _time_column(record, time_column)
    tcols = set((record.get('_time') or {}).get('columns', [])) if tname == (record.get('_time') or {}).get('label') \
        else {tname}
    meta = record.get('glen_metadata') or {}

    groups = _as_list(group_by)
    group_note = None
    if groups is None:
        groups = default_group_by(record, tname)
        if groups:
            group_note = f'Grouped by {", ".join(groups)} by default; pass group_by=[] to pool all rows.'
    if len(groups) > MAX_GROUP_COLUMNS:
        raise ValueError(f'At most {MAX_GROUP_COLUMNS} group_by columns.')
    for g in groups:
        if g not in by_name or g in tcols:
            raise ValueError(f'Unknown group_by column {g!r}.')

    cols = _as_list(columns)
    if not cols:
        cols = list(meta.get('sampling_columns') or [])
        if not cols:
            cols = [c['name'] for c in record['columns'] if c['name'] not in tcols and c['name'] not in groups
                    and any(str(c.get('type', '')).upper().startswith(t) for t in NUMERIC)]
        cols = [c for c in cols if c not in groups][:MAX_COLUMNS]
    if not cols:
        raise ValueError('Name the measured columns to describe.')
    if len(cols) > MAX_COLUMNS:
        raise ValueError(f'At most {MAX_COLUMNS} columns per call.')
    for c in cols:
        if c not in by_name or c in tcols or c in groups:
            raise ValueError(f'Unknown column {c!r}.')

    lo, hi = _when(start, 'start'), _when(end, 'end')
    if lo and hi and hi <= lo:
        raise ValueError('end must be after start (end is exclusive).')
    where, params = [f'{texpr} IS NOT NULL'], []
    if lo:
        where.append(f'{texpr} >= ?')
        params.append(lo)
    if hi:
        where.append(f'{texpr} < ?')
        params.append(hi)
    table = _quote(record['table'])
    gsel = ''.join(f'CAST({_quote(g)} AS VARCHAR) AS g{i}, ' for i, g in enumerate(groups))
    gref = ''.join(f'g{i}, ' for i in range(len(groups)))
    base = (f'SELECT {gsel}{texpr} AS t, ' + ', '.join(f'{_quote(c)} AS c{i}' for i, c in enumerate(cols))
            + f' FROM {table} WHERE ' + ' AND '.join(where))
    long = ' UNION ALL '.join(f'SELECT {gref}t, {i} AS k FROM base WHERE c{i} IS NOT NULL'
                              for i in range(len(cols)))
    cte = (f'WITH base AS ({base}), long AS ({long}), '
           f'd AS (SELECT {gref}k, t, t - LAG(t) OVER (PARTITION BY {gref}k ORDER BY t) AS dt FROM long)')

    if period == 'auto':
        bounds = fetch_params(f'WITH base AS ({base}) SELECT min(t), max(t) FROM base', params)[0]
        first, last = (lo or bounds[0]), (hi or bounds[1])
        months = ((last.year - first.year) * 12 + last.month - first.month) if first and last else 0
        period = 'month' if months <= MONTHLY_LIMIT_MONTHS else 'year'
    if period not in ('month', 'year'):
        raise ValueError("period must be 'auto', 'month' or 'year'.")
    fmt = '%Y-%m' if period == 'month' else '%Y'

    rows = fetch_params(
        f"{cte} SELECT {gref}k, strftime(t, '{fmt}') AS p, count(*) AS n, "
        "epoch(mode(dt) FILTER (WHERE dt > INTERVAL 0 SECOND AND dt <= INTERVAL 1 DAY)) AS typical, "
        "count(*) FILTER (WHERE dt = INTERVAL 0 SECOND) AS dup, min(t), max(t), "
        "max(epoch(dt)) AS longest "
        f"FROM d GROUP BY GROUPING SETS (({gref}k, p), ({gref}k)) ORDER BY {gref}k, p NULLS FIRST", params)
    gaps = fetch_params(
        f'{cte} SELECT {gref}k, t - dt AS after, t AS resumed, epoch(dt) / 86400.0 AS days FROM d '
        f'WHERE dt > INTERVAL 1 DAY ORDER BY {gref}k, after', params)

    ng = len(groups)
    series, index = [], {}
    for row in rows:
        key = row[:ng + 1]
        p, n, typical, dup, first, last, _longest = row[ng + 1:]
        if p is None:
            if len(series) >= MAX_SERIES:
                continue
            entry = {'group': dict(zip(groups, row[:ng])), 'column': cols[row[ng]],
                     'records': n, 'first': str(first), 'last': str(last),
                     'typical_interval': interval_text(typical), 'typical_interval_seconds': typical,
                     'by_' + period: [], 'gaps_over_1_day': [], 'gap_count': 0}
            if dup:
                entry['repeated_timestamps'] = dup
            index[key] = entry
            series.append(entry)
        elif key in index:
            index[key]['by_' + period].append([p, n, interval_text(typical)])
    for row in gaps:
        key = row[:ng + 1]
        if key not in index:
            continue
        entry = index[key]
        entry['gap_count'] += 1
        entry['gaps_over_1_day'].append({'last_before': str(row[ng + 1]), 'next': str(row[ng + 2]),
                                         'days': round(row[ng + 3], 1)})
    for entry in series:
        if entry['gap_count'] > MAX_GAPS:
            longest = sorted(entry['gaps_over_1_day'], key=lambda g: -g['days'])[:MAX_GAPS]
            entry['gaps_over_1_day'] = sorted(longest, key=lambda g: g['last_before'])
            entry['gaps_listed'] = f'longest {MAX_GAPS} of {entry["gap_count"]}'

    result = {'dataset_id': record['id'], 'table': record['table'], 'time_column': tname,
              'start': str(lo) if lo else None, 'end_exclusive': str(hi) if hi else None,
              'group_by': groups, 'columns': cols, 'period': period,
              'by_period_fields': [period, 'records', 'typical_interval'],
              'series': series,
              'how_to_read': ('typical_interval is the most common spacing between consecutive '
                              'timestamps where that column has a value. Gaps list stretches longer '
                              'than one day with no value. Compare groups only over periods where all '
                              'of them have records, and say which periods differ.')}
    if group_note:
        result['note'] = group_note
    total = sum(1 for row in rows if row[ng + 1] is None)
    if total > len(series):
        result['series_truncated'] = f'first {len(series)} of {total} series; narrow group_by, columns or dates'
    return result


def describe_sampling_safe(*args, **kwargs):
    """Tool wrapper: friendly errors, including scans too large for the sandbox."""
    try:
        return describe_sampling(*args, **kwargs)
    except ValueError as exc:
        return {'error': str(exc)}
    except Exception as exc:  # duckdb out-of-memory, interrupt or temp-space refusal
        text = str(exc)
        if any(k in text for k in ('memory', 'Interrupt', 'temp', 'directory')):
            return {'error': 'Too large to scan at once. Pass start/end for a shorter window '
                             '(a year for 20-minute tables, a few months for 1–2 minute loggers), '
                             'fewer columns, or narrower group_by. Whole-record intervals per '
                             'instrument and period are in get_schema glen_metadata.sampling.'}
        return {'error': 'Sampling check failed: ' + text[:300]}
