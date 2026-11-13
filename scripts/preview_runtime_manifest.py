"""Trace, list and prove the private files the gated preview server reads at runtime.

The preview release (scripts/build_preview_release.py) holds only web files. Many backend routes still read
git-ignored data next to the code: the serving database, prepared manifests, source CSVs and archives. This
script finds exactly which ones, so only those are copied to the server.

    trace   Start backend.preview:create_app on this checkout, send every request family the site uses
            (requests are enumerated from the backend's own manifests), and record each file opened,
            listed or handed to DuckDB/GDAL. Writes the request list, per-request response hashes and
            docs/preview-runtime-manifest.json (git-ignored files only, with sizes and SHA-256).
    prove   Rebuild the tracked code in a new temporary root, copy in ONLY the manifest's files (APFS clones
            where possible), link them the way the server does, replay every request there and compare the
            responses byte for byte with the trace. Fails on any missing file, error status or difference.

Nothing here contacts a network service or a paid provider; the analyst stays disabled (budget 0).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, urlencode

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
MANIFEST = APP / 'docs/preview-runtime-manifest.json'
FORMAT = 1
# Never part of a runtime manifest, whatever a trace sees: credentials, and the preview's own state.
FORBIDDEN_NAMES = ('.env', 'usage.sqlite3')
FORBIDDEN_SUFFIXES = ('.pem', '.key', '.p12', '.pfx', '.token')
# Not data: the Python environment is rebuilt on the server, git metadata is never copied.
ENVIRONMENT = ('.git/', 'glen-explorer/.venv/', 'glen-explorer/.venv')
# Route families whose bytes differ between two runs of the SAME full checkout: /api/display/stack sums in
# parallel DuckDB threads, so about 20 floats change in the 15th significant digit. Compared as JSON with a
# relative tolerance of 1e-12 instead of byte for byte.
VOLATILE = {'/api/display/stack'}
TOLERANCE = 1e-12


def forbidden(rel):
    name = rel.rsplit('/', 1)[-1]
    return (name in FORBIDDEN_NAMES or name.startswith('.env') or name.endswith(FORBIDDEN_SUFFIXES)
            or 'id_rsa' in name or 'id_ed25519' in name or name.startswith('usage.sqlite3'))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- access recording

class Recorder:
    """Python audit events plus DuckDB and rasterio entry points, which open files outside Python."""

    def __init__(self, roots):
        self.roots = [Path(r) for r in roots]  # checkout roots whose files we classify
        self.label = 'startup'
        self.seen = {}  # (kind, absolute path) -> first label
        self.lock = threading.Lock()

    def note(self, kind, path):
        if isinstance(path, int) or path is None:
            return
        try:
            path = os.fsdecode(path)
        except TypeError:
            return
        if path.startswith('/vsizip/'):
            path = path[len('/vsizip/'):]
            for marker in ('.zip/', '.ZIP/'):
                if marker in path:
                    path = path.split(marker, 1)[0] + marker[:-1]
        absolute = os.path.abspath(path)
        if not any(absolute.startswith(str(r) + os.sep) or absolute == str(r) for r in self.roots):
            return
        label = self.label
        if threading.current_thread().name == 'glen-warm-metabolism':
            label = 'startup: metabolism warm-up'
        with self.lock:
            self.seen.setdefault((kind, absolute), label)

    def audit(self, event, args):
        if event == 'open':
            mode = args[1] if len(args) > 1 else 'r'
            flags = args[2] if len(args) > 2 else 0
            writes = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR))
            self.note('write' if writes else 'read', args[0])
        elif event in ('os.listdir', 'os.scandir'):
            self.note('list', args[0] if args and args[0] is not None else '.')
        elif event == 'glob.glob':
            self.note('glob', args[0])

    def install(self):
        sys.addaudithook(self.audit)
        import duckdb
        import rasterio
        recorder = self
        real_connect, real_open = duckdb.connect, rasterio.open

        def paths(value):
            if isinstance(value, (str, os.PathLike)):
                text = os.fspath(value)
                if os.path.isabs(text) or text.endswith(('.parquet', '.duckdb', '.csv')):
                    recorder.note('read', text)
                # SQL text that names a file inline, e.g. read_parquet('/abs/x.parquet').
                for quote_char in ("'", '"'):
                    for part in text.split(quote_char)[1::2]:
                        if part.startswith('/') and os.path.exists(part):
                            recorder.note('read', part)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    paths(item)
            elif isinstance(value, dict):
                for item in value.values():
                    paths(item)

        class Connection:
            def __init__(self, real):
                object.__setattr__(self, '_real', real)

            def __getattr__(self, name):
                attribute = getattr(self._real, name)
                if name in ('execute', 'sql', 'query', 'read_parquet', 'read_csv', 'from_parquet', 'executemany'):
                    def traced(*a, **k):
                        paths(a), paths(k)
                        return attribute(*a, **k)
                    return traced
                return attribute

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

        def connect(*a, **k):
            if a:
                paths(a[0])
            elif 'database' in k:
                paths(k['database'])
            return Connection(real_connect(*a, **k))

        def raster_open(path, *a, **k):
            recorder.note('read', path)
            return real_open(path, *a, **k)

        duckdb.connect = connect
        rasterio.open = raster_open


# ---------------------------------------------------------------- the preview app with test identities

def preview_client(release, state):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from starlette.testclient import TestClient
    env = {'GLEN_PREVIEW_ORIGIN': 'https://preview.example.org', 'GLEN_ACCESS_ISSUER': 'https://test.cloudflareaccess.com',
           'GLEN_ACCESS_AUD': 'aud', 'GLEN_PREVIEW_EMAILS': 'tester@example.org', 'GLEN_PREVIEW_RELEASE': str(release),
           'GLEN_PREVIEW_STATE': str(state), 'GLEN_PREVIEW_BUDGET_MICRO_USD': '0'}
    with patch.dict(os.environ, env):
        from backend.preview import create_app
        from backend.preview_access import AccessVerifier
        app = create_app()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app.verifier = AccessVerifier(app.settings, SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key())))

    def headers():
        now = int(time.time())
        token = jwt.encode({'iss': env['GLEN_ACCESS_ISSUER'], 'aud': 'aud', 'sub': 'trace', 'email': 'tester@example.org',
                            'iat': now - 5, 'exp': now + 3600}, key, algorithm='RS256')
        return {'Cf-Access-Jwt-Assertion': token, 'Origin': env['GLEN_PREVIEW_ORIGIN']}
    return TestClient(app, base_url=env['GLEN_PREVIEW_ORIGIN']), headers


def minimal_release(directory):
    """A valid release with one page. The private-file trace concerns backend routes, not web/ assets."""
    web = directory / 'web'
    web.mkdir(parents=True)
    page = b'<!doctype html><title>trace</title>'
    (web / 'index.html').write_bytes(page)
    import gzip
    (web / 'index.html.gz').write_bytes(gzip.compress(page, mtime=0))
    (directory / 'release-manifest.json').write_text(json.dumps({
        'format': 1, 'access': 'private-preview', 'public_release': False, 'tile_release': 'vtrace',
        'files': [{'path': 'index.html', 'bytes': len(page), 'sha256': hashlib.sha256(page).hexdigest()}]}))
    return directory


# ---------------------------------------------------------------- the request list

def q(path, **params):
    return path + ('?' + urlencode(params) if params else '')


def enumerate_requests():
    """Every route family the site calls, with identifiers taken from the backend's own manifests.

    Display endpoints all read the one serving database, so one representative request per mode is enough
    for a file trace; identifier-driven families (materials, photos, studies, rasters) are enumerated in full
    because each identifier can name a different file.
    """
    from backend import (bathymetry, coral, coral_multi, coral_photos, ctd_records, experiments_page, fish_reef, gump_records,
                         imagery, landsat, m2, m3, m4, materials, mooring_summaries, ocean, reef_life, reef_temperature,
                         satellite, sensors, sst, studies, topography, visualization)
    out = []
    add = lambda family, path, method='GET', body=None: out.append(dict(family=family, path=path, method=method, body=body))

    for path in ['/config.json', '/layers-input.json', '/api/health', '/api/datasets', '/api/datasets?q=temperature',
                 '/api/display/config', '/api/planner', '/api/experiments', '/api/metabolism-overview', '/api/fish', '/api/recruits', '/api/sediment-traps',
                 '/api/nutrients', '/api/fish-reef', '/api/fish-reef/segments', '/api/topography', '/api/topography/surface?step=4',
                 '/api/imagery/landcover', '/api/imagery/bathymetry', '/api/imagery/landsat', '/api/materials',
                 '/api/coral-photos', '/api/coral-multi', '/api/sst', '/api/display/casts', '/api/display/ctd',
                 '/api/ctd-records', '/api/reef-temperature', '/api/gump-records', '/api/mooring-summaries',
                 '/api/display/coral', '/api/display/coral_grid', '/api/display/storm-response', '/api/display/regimes',
                 '/api/display/coupling', '/api/display/stack', '/api/display/experiments', '/api/display/explore',
                 '/api/display/sections', '/api/display/ocean', '/api/display/sensors', '/api/display/series',
                 '/api/display/reef_temp', '/api/display/satellite']:
        add(path.split('?')[0], path)
    for kind in ['light', 'rivers', 'metabolism', 'zooplankton']:
        add('/api/field-observations/{kind}', f'/api/field-observations/{kind}')
    for kind in ['fdom', 'tissue']:
        add('/api/lagoon-biogeochemistry/{kind}', f'/api/lagoon-biogeochemistry/{kind}')
    # Catalog and schemas: every dataset id the catalog lists, first page only.
    from backend import catalog
    data, _ = catalog.load()
    for record in data['records']:
        add('/api/datasets/{id}', '/api/datasets/' + quote(record['id']))

    # Source-record views.
    for station in mooring_summaries.PACKAGES:
        for resolution in mooring_summaries.RESOLUTIONS:
            add('/api/mooring-summaries', q('/api/mooring-summaries', station=station, resolution=resolution))
    for product in gump_records.FILES:
        add('/api/gump-records', q('/api/gump-records', product=product))
    for product in [p['id'] for p in reef_temperature.PRODUCTS]:
        add('/api/reef-temperature', q('/api/reef-temperature', product=product))
    cache = reef_temperature.dataset('cache')
    for series in cache['series'][:3]:
        add('/api/reef-temperature/records', q('/api/reef-temperature/records', series_id=series['id'], recorded_day=cache['dates'][0]))
    for product in [p['id'] for p in ctd_records.PRODUCTS]:
        add('/api/ctd-records', q('/api/ctd-records', product=product))
        first = ctd_records.dataset(product)
        profiles = first.get('profiles') or []
        if profiles:
            add('/api/ctd-records/profile', q('/api/ctd-records/profile', product=product, profile_id=profiles[0]['id']))
    for cast in visualization.casts()[:2]:
        add('/api/display/profile/{cast_id}', f"/api/display/profile/{cast['id']}")

    # Satellite SST and in-situ comparison.
    for product in sst.available():
        add('/api/sst/map', q('/api/sst/map', product=product))
        for s in sst.sites()[:2]:
            add('/api/sst/series', q('/api/sst/series', product=product, site_id=s['id'], start='2020-01-01', end='2020-12-31'))
    for s in sst.sites():
        add('/api/sst/insitu', q('/api/sst/insitu', site_id=s['id'], start='2020-01-01', end='2020-12-31'))
    for product in satellite.PRODUCTS:
        for resolution in satellite.TABLES:
            add('/api/display/satellite', q('/api/display/satellite', product=product, resolution=resolution))

    # Display families: one representative per mode (they all read data/explorer.duckdb).
    for mode in ['btm_daily', 'btm_20min', 'lagoon_daily', 'lagoon_1min']:
        params = dict(mode=mode)
        if mode == 'lagoon_1min':
            params['station'] = ocean.lagoon_sites()[0]
        add('/api/display/reef_temp', q('/api/display/reef_temp', **params))
    add('/api/display/sensors', q('/api/display/sensors', **{'station': sensors.config()['channels'][0]['station'],
                                                            'variable': sensors.config()['channels'][0]['id']}))
    for variable in list(coral.VARIABLES)[:2]:
        add('/api/display/coral', q('/api/display/coral', variable=variable))

    # Reef life.
    overview = reef_life.metabolism_overview()
    for series in overview['series'][:3]:
        add('/api/metabolism/{key}/{day}', f"/api/metabolism/{quote(series['key'])}/{series['start']}")
    for package in [s['package'] for s in experiments_page.catalog()['studies']]:
        add('/api/experiments?study=', q('/api/experiments', study=package))
    for key in list(fish_reef.index())[:3]:
        add('/api/fish-reef/segment/{key}', '/api/fish-reef/segment/' + quote(key, safe=''))

    # Studies: every product (each can name a different prepared file) and its overview.
    choices = [(s, '') for s in [*studies.SOURCES, *studies.coral_monitoring.SOURCES]]
    choices += [('workbook', p) for p in studies.workbook()['products']]
    choices += [('m2', p) for p in m2.manifest()] + [('m3', p) for p in m3.manifest()]
    choices += [('m4', p) for p in m4.manifest()['products']]
    for study, product in choices:
        params = dict(study=study, limit='2') | ({'product': product} if product else {})
        add('/api/studies', q('/api/studies', **params))
        if study in ('m2', 'm3', 'm4'):
            add('/api/studies/overview', q('/api/studies/overview', study=study, product=product))
    # Raw-inspector M2 products (5_25_2, 2004_4_2, 5015_1_1) read their library parquet; they are in the loop above.

    # Imagery and topography rasters: one tile and one inspection per prepared raster.
    def centre(bounds):
        return (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2

    def tile_at(lon, lat, z):
        import math
        n = 2 ** z
        x = int((lon + 180) / 360 * n)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
        return x, y
    lc = imagery.manifest()
    lon, lat = centre(lc['bounds'])
    x, y = tile_at(lon, lat, 13)
    add('/api/imagery/landcover/tiles', f"/api/imagery/landcover/tiles/{lc['version']}/13/{x}/{y}.png")
    add('/api/imagery/landcover/inspect', q('/api/imagery/landcover/inspect', lon=lon, lat=lat))
    b = bathymetry.manifest()
    lon, lat = centre(b['bounds'])
    x, y = tile_at(lon, lat, 14)
    for scale in bathymetry.SCALES:
        add('/api/imagery/bathymetry/tiles', f"/api/imagery/bathymetry/tiles/{b['version']}/{scale}/14/{x}/{y}.png")
    add('/api/imagery/bathymetry/inspect', q('/api/imagery/bathymetry/inspect', lon=lon, lat=lat))
    add('/api/imagery/bathymetry/reference', b['reference_image'])
    for s in landsat.manifest()['scenes']:
        composites = list(s['composites']) + ['band-' + band for band in s.get('full_bands', {})]
        for composite in composites:
            item = landsat.scene(s['id'], composite)
            lon, lat = centre(item['bounds'])
            x, y = tile_at(lon, lat, 12)
            add('/api/imagery/landsat/tiles', f"/api/imagery/landsat/{s['id']}/{composite}/tiles/{item['version']}/12/{x}/{y}.png")
            add('/api/imagery/landsat/inspect', q(f"/api/imagery/landsat/{s['id']}/{composite}/inspect", lon=lon, lat=lat))
    shape = topography.manifest()['shape']
    add('/api/topography/profiles', q('/api/topography/profiles', x=shape[0] // 2, y=shape[1] // 2))  # x indexes the first axis

    # Source materials: every asset's detail page, image/raster previews and one raster cell.
    for a in materials.manifest()['assets']:
        add('/api/materials/{id}', '/api/materials/' + quote(a['id']))
        if a['kind'] in ('image', 'raster'):
            add('/api/materials/{id}/preview', f"/api/materials/{quote(a['id'])}/preview")
        if a['kind'] == 'raster':
            add('/api/materials/{id}/raster', f"/api/materials/{quote(a['id'])}/raster")
    # Multi-annotator photos: every image's comparison, one preview per source archive.
    multi = coral_multi.data()
    for im in multi['images']:
        add('/api/coral-multi/{id}', '/api/coral-multi/' + quote(im['id']) + ('' if im['split'] != 'reference' else '?a=' + im['raters'][0]))
    add('/api/coral-multi/{id}/preview', f"/api/coral-multi/{quote(multi['images'][0]['id'])}/preview")
    # Coral photo quadrats: every comparison, one preview from each archive.
    for quadrat in coral_photos.catalog()['quadrats']:
        add('/api/coral-photos/quadrat/{key}', '/api/coral-photos/quadrat/' + quote(quadrat['key'], safe=''))
    first = {}
    for r in coral_photos.index()['images']:
        first.setdefault(r['archive'], r)
    for r in first.values():
        add('/api/coral-photos/image/{v}/{id}/preview', f"/api/coral-photos/image/{coral_photos.index()['version']}/{quote(r['id'])}/preview")

    # MCP tools the analyst calls; JSON-RPC over the stateless endpoint.
    mcp = [('tools/list', {})] + [('tools/call', {'name': name, 'arguments': args}) for name, args in [
        ('query', {'sql_query': 'SELECT count(*) AS n FROM duckdb_tables()'}), ('list_datasets', {'query': 'coral'}),
        ('get_schema', {'dataset_id': data['records'][0]['id']}), ('get_storm_response', {}),
        ('browse_stac_catalog', {'query': 'temperature'}), ('get_collection', {'collection_id': 'moorea_waves'})]]
    for index, (method, params) in enumerate(mcp, 2):
        add('/mcp ' + (params.get('name') or method), '/mcp', 'POST', {'jsonrpc': '2.0', 'id': index, 'method': method, 'params': params})
    return out


# ---------------------------------------------------------------- running requests

def run_requests(request_list, recorder=None):
    """Send each request through the gated preview app and return [status, bytes, sha256] per request."""
    import logging
    for name in ('httpx', 'mcp', 'mcp.server', 'mcp.server.lowlevel.server', 'mcp.server.streamable_http'):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The server gets its settings from the service environment, never a checkout .env (and the proof root has
    # none), so neither run loads one; otherwise this Mac's analyst model would change /config.json and /api/health.
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    with tempfile.TemporaryDirectory() as scratch:
        release, state = minimal_release(Path(scratch) / 'release'), Path(scratch) / 'state'
        state.mkdir()
        client, headers = preview_client(release, state)
        results = []
        with client as c:
            from backend import reef_life
            reef_life.warm()  # the lifespan's warm-up, completed before timing requests
            h = headers() | {'Accept': 'application/json, text/event-stream'}
            init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'runtime-trace', 'version': '1'}}}
            c.post('/mcp', headers=h, json=init)
            for i, r in enumerate(request_list):
                if recorder:
                    recorder.label = r['family']
                if i % 200 == 0:
                    h = headers() | {'Accept': 'application/json, text/event-stream'}
                started = time.monotonic()
                response = c.post(r['path'], headers=h, json=r['body']) if r['method'] == 'POST' else c.get(r['path'], headers=h)
                body = response.content
                results.append({'family': r['family'], 'path': r['path'], 'status': response.status_code, 'bytes': len(body),
                                'sha256': hashlib.sha256(body).hexdigest(), 'seconds': round(time.monotonic() - started, 3),
                                'error': body[:300].decode(errors='replace') if response.status_code >= 400 else None}
                               | ({'body': body.decode()} if r['family'] in VOLATILE else {}))
                if (i + 1) % 250 == 0:
                    print(f'  {i + 1}/{len(request_list)} requests', flush=True)
    return results


def expected_status(r):
    """Statuses the preview gives on purpose, not because a file is missing."""
    return {200}


# ---------------------------------------------------------------- manifest

def tracked_files():
    out = subprocess.check_output(['git', '-C', str(REPO), 'ls-files', '-z'])
    return {p.decode() for p in out.split(b'\0') if p}


def relative(absolute, roots):
    for root in roots:
        try:
            return Path(absolute).relative_to(root).as_posix()
        except ValueError:
            continue
    return None


def link_points(paths, tracked):
    """The highest untracked ancestor of each file: what the server links from the code checkout."""
    tracked_dirs = set()
    for t in tracked:
        parts = t.split('/')
        for i in range(1, len(parts)):
            tracked_dirs.add('/'.join(parts[:i]))
    points = set()
    for p in paths:
        parts = p.split('/')
        for i in range(1, len(parts) + 1):
            candidate = '/'.join(parts[:i])
            if candidate not in tracked_dirs:
                points.add(candidate)
                break
    return sorted(points)


ROUTE_GROUPS = [  # longest prefix first; the reason each group of files is needed
    ('glen-explorer/data/explorer.duckdb', 'Serving database: every /api/display/* chart, /api/health, /api/fish, /api/planner, catalog schemas, the analyst SQL tool at /mcp'),
    ('glen-explorer/data/serving-catalog.json', 'Dataset catalog: /api/datasets, schemas, /api/health, MCP list_datasets/get_schema'),
    ('glen-explorer/data/m2/', 'M2 prepared study products: /api/studies?study=m2 and /api/studies/overview'),
    ('glen-explorer/data/m3/', 'M3 prepared experiment tables: /api/studies?study=m3, /api/studies/overview, /api/experiments'),
    ('glen-explorer/data/m4/', 'M4 microbial products: /api/studies?study=m4'),
    ('glen-explorer/data/m5/', 'M5 source materials, multi-annotator photos and full Landsat bands: /api/materials, /api/coral-multi, /api/imagery/landsat'),
    ('glen-explorer/data/imagery/', 'Prepared rasters and indexes: land cover, bathymetry, Landsat, topography, fish GPS surveys, coral photo index'),
    ('glen-explorer/data/sst/', 'Satellite SST grids: /api/sst'),
    ('glen-explorer/data/river-watersheds/', 'Watershed outlines: /api/field-observations/rivers'),
    ('glen-explorer/library/', 'Prepared library tables read by a study route'),
    ('mcr_data/derived/', 'Derived BTM logger cache: /api/reef-temperature?product=cache, /api/sst/insitu'),
    ('mcr_data/', 'Original EDI source files that the source-record views parse, hash or open (archives for photos and materials)'),
]


def reason(rel):
    return next((why for prefix, why in ROUTE_GROUPS if rel == prefix or rel.startswith(prefix)), 'Read at runtime')


def build_manifest(accesses, roots, results, request_file):
    tracked = tracked_files()
    files, lists, writes, outside_code, excluded = {}, {}, [], set(), set()
    for kind, absolute, label in sorted(accesses):
        rel = relative(absolute, roots)
        if rel is None or rel.startswith(ENVIRONMENT):
            continue  # the server builds its own environment from requirements.lock.txt
        if kind == 'write':
            writes.append(rel)
            continue
        if rel in tracked:
            outside_code.add(rel)  # code and docs: shipped by the code checkout, not this manifest
            continue
        if kind in ('list', 'glob'):
            lists.setdefault(rel, label)
            continue
        if os.path.isdir(absolute):
            lists.setdefault(rel, label)
            continue
        if not os.path.isfile(absolute):
            continue  # probed but absent (optional inputs); the proof covers their absence
        if forbidden(rel):
            excluded.add(rel)  # e.g. load_dotenv reads .env at import; set on the server by the operator instead
            continue
        files.setdefault(rel, set()).add(label)
    # A listed untracked directory is copied whole only if a handler lists it; none should.
    listed_private = sorted(d for d in lists if d not in {x.rsplit('/', 1)[0] for x in tracked} and not d.endswith('.git'))
    entries = []
    for rel in sorted(files):
        path = REPO / rel
        entries.append({'path': rel, 'bytes': path.stat().st_size, 'sha256': sha256(path),
                        'first_read_by': sorted(files[rel])[0], 'reason': reason(rel)})
    groups = {}
    for e in entries:
        key = e['path'].split('/')[0] if e['path'].startswith('mcr_data') and e['path'].count('/') < 2 else '/'.join(e['path'].split('/')[:3 if e['path'].startswith('glen-explorer') else 2])
        g = groups.setdefault(key, {'files': 0, 'bytes': 0})
        g['files'] += 1
        g['bytes'] += e['bytes']
    errors = [r for r in results if r['status'] not in expected_status(r)]
    manifest = {
        'format': FORMAT,
        'purpose': 'Private runtime inputs of backend.preview:create_app, read from beside the code checkout. Never under a release or a web root.',
        'generated_by': 'glen-explorer/scripts/preview_runtime_manifest.py trace',
        'git_commit': subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
        'requests': {'file': request_file, 'count': len(results), 'families': len({r['family'] for r in results}),
                     'non_200': len(errors)},
        'excluded_credential_reads': sorted(excluded),
        'code_requirements': ['The server checkout is the tracked repository at git_commit (code, docs and small tracked data such as '
                              'glen-explorer/data/published.json and studies/5018.json, plus root 03_temperature_to_parquet.py, which '
                              'reef temperature provenance hashes).',
                              'glen-explorer/upstream/geo-agent at the commit in upstream.lock.json (scripts/bootstrap.py): '
                              'backend/server.py mounts its app/ directory at import, so the server does not start without it.',
                              'A Python environment from requirements.lock.txt; not copied by this manifest.'],
        'link_points': link_points([e['path'] for e in entries], tracked),
        'listed_private_directories': listed_private,
        'total_bytes': sum(e['bytes'] for e in entries),
        'total_files': len(entries),
        'groups': dict(sorted(groups.items())),
        'files': entries,
    }
    return manifest, errors, sorted(set(writes)), sorted(outside_code)


# ---------------------------------------------------------------- commands

def trace(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    # Linked worktree data resolves into the main checkout; classify those paths against it too.
    main = Path(subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                        text=True).strip()).parent
    roots = list(dict.fromkeys([REPO, main]))
    recorder = Recorder(roots)
    recorder.install()
    sys.path.insert(0, str(APP))
    os.chdir(APP)
    # Enumerate in a separate process: its reads would warm the backend's caches and hide files from the trace.
    subprocess.run([sys.executable, str(Path(__file__).resolve()), 'requests', '--out', str(out_dir / 'requests.json')],
                   cwd=APP, check=True)
    request_list = json.loads((out_dir / 'requests.json').read_text())
    print(f'{len(request_list)} requests in {len({r["family"] for r in request_list})} families', flush=True)
    results = run_requests(request_list, recorder)
    (out_dir / 'trace-results.json').write_text(json.dumps(results, indent=1) + '\n')
    accesses = [[kind, path, label] for (kind, path), label in sorted(recorder.seen.items())]
    (out_dir / 'trace-accesses.json').write_text(json.dumps({'roots': [str(r) for r in roots], 'accesses': accesses}) + '\n')
    write_manifest(out_dir)


def write_manifest(out_dir):
    raw = json.loads((out_dir / 'trace-accesses.json').read_text())
    results = json.loads((out_dir / 'trace-results.json').read_text())
    manifest, errors, writes, code = build_manifest(raw['accesses'], [Path(r) for r in raw['roots']], results, 'requests.json')
    MANIFEST.write_text(json.dumps(manifest, indent=1) + '\n')
    (out_dir / 'trace-other-accesses.json').write_text(json.dumps({'writes': writes, 'tracked_reads': code}, indent=1) + '\n')
    print(json.dumps({'files': manifest['total_files'], 'bytes': manifest['total_bytes'], 'non_200': len(errors),
                      'excluded_credential_reads': manifest['excluded_credential_reads'],
                      'link_points': manifest['link_points'], 'writes': writes}, indent=1))
    for e in errors[:40]:
        print('NON-200', e['status'], e['path'], (e['error'] or '')[:160])


def build_root(root, manifest):
    """A server-shaped tree: app/ (tracked code only) and private/ (manifest files), joined by links."""
    app, private = root / 'app', root / 'private'
    files = subprocess.check_output(['git', '-C', str(REPO), 'ls-files', '-z', '-c', '-o', '--exclude-standard'])
    for name in (p.decode() for p in files.split(b'\0') if p):
        source = REPO / name
        if source.is_file() and not source.is_symlink():
            clone(source, app / name)
    # Code the server checkout must also hold: the pinned geo-agent app (scripts/bootstrap.py restores it).
    # backend/server.py mounts its directory at import, so a checkout without it does not start.
    upstream = APP / 'upstream/geo-agent'
    pinned = json.loads((APP / 'upstream.lock.json').read_text())['geo-agent']['commit']
    if subprocess.check_output(['git', '-C', str(upstream), 'rev-parse', 'HEAD'], text=True).strip() != pinned:
        raise SystemExit('upstream/geo-agent is not at the commit in upstream.lock.json; run scripts/bootstrap.py.')
    for name in subprocess.check_output(['git', '-C', str(upstream), 'ls-files', '-z']).split(b'\0'):
        if name:
            clone(upstream / name.decode(), app / 'glen-explorer/upstream/geo-agent' / name.decode())
    for entry in manifest['files']:
        clone(REPO / entry['path'], private / entry['path'])
    for point in manifest['link_points']:
        link = app / point
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(private / point)
    return app


def clone(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    real = os.path.realpath(source)
    if sys.platform == 'darwin' and subprocess.run(['cp', '-c', real, str(target)], capture_output=True).returncode == 0:
        return
    shutil.copyfile(real, target)


def close(a, b):
    """Same JSON structure and values, floats equal within TOLERANCE (relative)."""
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(close(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return a == b or abs(a - b) <= TOLERANCE * max(abs(a), abs(b))
    return a == b


def prove(out_dir, keep):
    manifest = json.loads(MANIFEST.read_text())
    request_list = json.loads((out_dir / 'requests.json').read_text())
    trace_results = json.loads((out_dir / 'trace-results.json').read_text())
    root = Path(tempfile.mkdtemp(prefix='glen-runtime-proof-'))
    try:
        app = build_root(root, manifest)
        print('Proof root', root, flush=True)
        replay = root / 'replay-results.json'
        python = str(APP / '.venv/bin/python')  # not resolved: a venv is found from the path it is started by
        subprocess.run([python, str(app / 'glen-explorer/scripts/preview_runtime_manifest.py'), 'replay',
                        '--requests', str(out_dir / 'requests.json'), '--out', str(replay), '--forbid', str(REPO),
                        '--forbid', str(Path(os.path.realpath(REPO / 'mcr_data')).parent)],
                       cwd=app / 'glen-explorer', check=True)
        results = json.loads(replay.read_text())
        problems, compared = [], 0
        for want, got in zip(trace_results, results):
            assert want['path'] == got['path']
            if got['status'] != want['status'] or got['status'] not in expected_status(got):
                problems.append(f"{got['status']} (trace {want['status']}) {got['path']} {got['error'] or ''}"[:300])
            elif got['sha256'] != want['sha256'] and not (got['family'] in VOLATILE and close(json.loads(want['body']), json.loads(got['body']))):
                problems.append(f"body differs: {got['path']}")
            else:
                compared += 1
        report = {'requests': len(results), 'identical_or_tolerated': compared, 'tolerated_families': sorted(VOLATILE),
                  'problems': problems,
                  'manifest_files': manifest['total_files'], 'manifest_bytes': manifest['total_bytes']}
        (out_dir / 'proof.json').write_text(json.dumps(report, indent=1) + '\n')
        print(json.dumps({k: v for k, v in report.items() if k != 'problems'}), flush=True)
        for p in problems[:40]:
            print('PROBLEM', p)
        if problems:
            raise SystemExit(1)
        print('PASS: every request returned the same status and identical bytes from the manifest-only root.')
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def replay(requests_path, out, forbid):
    """Run inside a proof root. Any read under the original checkout means the proof is not isolated."""
    forbidden_roots = [os.path.realpath(f) for f in forbid]
    leaks = set()

    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.path.realpath(os.fsdecode(args[0]))
            if any(path.startswith(f + os.sep) for f in forbidden_roots) and '/.venv/' not in path:
                leaks.add(path)
    sys.addaudithook(audit)
    sys.path.insert(0, str(APP))
    results = run_requests(json.loads(Path(requests_path).read_text()))
    Path(out).write_text(json.dumps(results, indent=1) + '\n')
    if leaks:
        raise SystemExit('Proof root read files from the original checkout: ' + ', '.join(sorted(leaks)[:10]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    t = sub.add_parser('trace')
    t.add_argument('--out', type=Path, required=True, help='Working folder for the request list and results (not in git).')
    p = sub.add_parser('prove')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--keep', action='store_true', help='Keep the temporary proof root.')
    m = sub.add_parser('manifest', help='Rebuild the manifest from a finished trace folder.')
    m.add_argument('--out', type=Path, required=True)
    e = sub.add_parser('requests')
    e.add_argument('--out', type=Path, required=True)
    r = sub.add_parser('replay')
    r.add_argument('--requests', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--forbid', action='append', default=[])
    a = parser.parse_args()
    if a.command == 'requests':
        sys.path.insert(0, str(APP))
        listed = enumerate_requests()
        a.out.write_text(json.dumps(listed, indent=1) + '\n')
    elif a.command == 'manifest':
        write_manifest(a.out.resolve())
    elif a.command == 'trace':
        trace(a.out.resolve())
    elif a.command == 'prove':
        prove(a.out.resolve(), a.keep)
    else:
        replay(a.requests, a.out, a.forbid)


if __name__ == '__main__':
    main()
