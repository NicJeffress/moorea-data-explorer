"""The time span behind each group of an aggregate SQL result.

When the analyst aggregates one catalogued table that has a native TIMESTAMP or
DATE column, or a text date that scripts/build_table_metadata.py checked parses
(docs/table-metadata.json, merged as the record's private `_time`), `attach` runs one companion query under the same WHERE clause and
returns, per group and per aggregated column: the first and last timestamp with
a value and the number of calendar months with a value. Two groups whose means
cover different months then show it in the same result.

Anything else (joins, CTEs, subqueries in FROM, non-aggregate queries, tables
without a usable time column) gets nothing, and the result is unchanged. A
failed or slow companion query is dropped silently: it never replaces a result.
"""
import json
import re
from functools import lru_cache

import duckdb

from . import catalog
from .engine import execute

MAX_GROUPS = 60
MAX_COLUMNS = 4
NOTE = ('Per group: first and last timestamp with a value of each aggregated column, and the '
                'number of calendar months with a value, under the same WHERE. Groups with different '
                'months are not like-for-like; compare the months they share.')


@lru_cache(maxsize=1)
def _aggregates():
    with duckdb.connect() as db:
        names = {r[0] for r in db.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'aggregate'").fetchall()}
    return names | {'count_star'}


def _parse(sql):
    with duckdb.connect() as db:
        tree = json.loads(db.execute('SELECT json_serialize_sql(?)', [sql]).fetchone()[0])
    if tree.get('error') or len(tree.get('statements', [])) != 1:
        return None
    return tree['statements'][0]['node']


def _render(expressions):
    """AST expressions -> SQL text, via DuckDB's own serializer."""
    with duckdb.connect() as db:
        template = json.loads(db.execute("SELECT json_serialize_sql('SELECT 1')").fetchone()[0])
        out = []
        for e in expressions:
            e = dict(e, alias='')
            template['statements'][0]['node']['select_list'] = [e]
            text = db.execute('SELECT json_deserialize_sql(?)', [json.dumps(template)]).fetchone()[0]
            out.append(text[len('SELECT '):])
        return out


def _walk(e):
    if isinstance(e, dict):
        yield e
        for v in e.values():
            yield from _walk(v)
    elif isinstance(e, list):
        for v in e:
            yield from _walk(v)


def _is_aggregate(e):
    return e.get('class') == 'FUNCTION' and e.get('function_name', '').lower() in _aggregates()


def _contains_aggregate(e):
    return any(_is_aggregate(x) for x in _walk(e))


def plan(sql):
    """Companion SQL and labels, or None when this query does not qualify."""
    node = _parse(sql)
    if not node or node.get('type') != 'SELECT_NODE' or node['cte_map']['map']:
        return None
    source = node.get('from_table') or {}
    if source.get('type') != 'BASE_TABLE' or source.get('schema_name') or source.get('catalog_name'):
        return None
    record = catalog.get_record(source['table_name'])
    if not record or record.get('table') != source['table_name']:
        return None
    columns = {c['name']: c for c in record['columns']}
    time_col = next((c['name'] for c in record['columns']
                     if c.get('type') in ('TIMESTAMP', 'DATE', 'TIMESTAMP WITH TIME ZONE')), None)
    time_cols, t = {time_col}, f'"{time_col}"'
    measured = record.get('_time')
    if not time_col and measured and measured['kind'] == 'timestamp':
        # A text date that scripts/build_table_metadata.py checked parses, with its cheapest format.
        time_col, time_cols, t = measured['label'], set(measured['columns']), f'({measured["sql"]})'
    select = node['select_list']
    if not time_col or not any(_contains_aggregate(e) for e in select):
        return None

    # Group expressions, with GROUP BY 1 / alias / ALL resolved to the select item.
    aliases = {e.get('alias'): e for e in select if e.get('alias')}
    groups = []
    if node.get('aggregate_handling') == 'FORCE_AGGREGATES':
        groups = [e for e in select if not _contains_aggregate(e)]
    for g in node.get('group_expressions', []):
        if g.get('class') == 'CONSTANT' and g['value']['type']['id'] in ('INTEGER', 'BIGINT'):
            g = select[int(g['value']['value']) - 1]
        elif g.get('class') == 'COLUMN_REF' and len(g['column_names']) == 1 and g['column_names'][0] in aliases \
                and g['column_names'][0] not in columns:
            g = aliases[g['column_names'][0]]
        groups.append(g)
    if node.get('group_sets') and len(node['group_sets']) > 1:
        return None  # GROUPING SETS / ROLLUP / CUBE

    # Columns whose values are aggregated; '*' when only rows are counted.
    measured = []
    for e in select:
        for f in _walk(e):
            if _is_aggregate(f):
                for ref in _walk(f.get('children', [])):
                    if ref.get('class') == 'COLUMN_REF':
                        name = ref['column_names'][-1]
                        if name in columns and name not in time_cols and name not in measured:
                            measured.append(name)
    measured = measured[:MAX_COLUMNS] or [None]

    group_sql = _render(groups) if groups else []
    # A keyword column such as location renders quoted; label it by its plain name.
    labels = [(g.get('alias') or re.sub(r'^"([^"]+)"$', r'\1', text)) for g, text in zip(groups, group_sql)]
    where = _render([node['where_clause']])[0] if node.get('where_clause') else None
    parts = []
    for i, c in enumerate(measured):
        cond = f' FILTER (WHERE "{c}" IS NOT NULL)' if c else ''
        parts += [f'min({t}){cond} AS f{i}', f'max({t}){cond} AS l{i}',
                  f"count(DISTINCT date_trunc('month', {t})){cond} AS m{i}"]
    companion = ('SELECT ' + ', '.join([f'{s} AS g{i}' for i, s in enumerate(group_sql)] + parts)
                 + f' FROM "{record["table"]}"' + (f' WHERE {where}' if where else '')
                 + (' GROUP BY ' + ', '.join(f'g{i}' for i in range(len(group_sql))) if group_sql else '')
                 + (' ORDER BY ' + ', '.join(f'g{i}' for i in range(len(group_sql))) if group_sql else '')
                 + f' LIMIT {MAX_GROUPS + 1}')
    return {'sql': companion, 'labels': labels, 'columns': measured, 'time_column': time_col}


def attach(sql, payload):
    """Add payload['time_range'] when the query qualifies. Never raises."""
    try:
        p = plan(sql)
        if not p:
            return payload
        result = execute(p['sql'])
    except Exception:
        return payload
    rows = []
    for row in result['rows'][:MAX_GROUPS]:
        values = list(row.values())
        group = dict(zip(p['labels'], values[:len(p['labels'])]))
        rest = values[len(p['labels']):]
        for i, c in enumerate(p['columns']):
            first, last, months = rest[3 * i:3 * i + 3]
            rows.append({**group, 'column': c or 'rows', 'first': first, 'last': last,
                         'months_with_values': months})
    payload['time_range'] = {'time_column': p['time_column'], 'by_group': rows, 'note': NOTE}
    if len(result['rows']) > MAX_GROUPS:
        payload['time_range']['truncated'] = f'first {MAX_GROUPS} groups'
    return payload
