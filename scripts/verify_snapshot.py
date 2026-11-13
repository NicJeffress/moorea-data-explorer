"""Rebuild portable operator views without changing IDs; verify all serving row counts."""
import json
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'glen-explorer'
LIB = APP / 'library'


def main():
    records = json.loads((LIB / 'query-index.json').read_text())
    build = LIB / 'index.restore.duckdb'
    if build.exists():
        build.unlink()
    with duckdb.connect(str(build)) as db:
        db.execute('CREATE TABLE datasets(view_name VARCHAR, package VARCHAR, source VARCHAR, member VARCHAR, row_count BIGINT, readiness VARCHAR, parquet_path VARCHAR, published BOOLEAN)')
        for r in records:
            path = Path(r['parquet_path']).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():
                raise RuntimeError(f'Missing or nonportable data path: {path}')
            name = r['view_name'].replace('"', '""')
            value = str(path).replace("'", "''")
            db.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet(\'{value}\')')
            db.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?)', [r[k] for k in ['view_name', 'package', 'source', 'member', 'row_count', 'readiness', 'parquet_path', 'published']])
    build.replace(LIB / 'index.duckdb')
    catalog = json.loads((APP / 'data/serving-catalog.json').read_text())
    with duckdb.connect(str(APP / 'data/explorer.duckdb'), read_only=True) as db:
        for r in catalog['records']:
            name = r['table'].replace('"', '""')
            count = db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            if count != r['rows']:
                raise RuntimeError(f'Row count mismatch for {name}: {count} != {r["rows"]}')
    print(f'Verified {len(records)} portable library views and {len(catalog["records"])} serving tables.')


if __name__ == '__main__':
    main()
