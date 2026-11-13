"""Check the live private preview from outside, before any tester is invited.

Run it from your own computer against the deployed preview, after a sign-in through Cloudflare Access:

    cd glen-explorer
    export GLEN_PREVIEW_ACCESS_COOKIE="$(pbpaste)"   # or: --cookie-file ~/private/cf-cookie.txt
    .venv/bin/python scripts/check_live_preview.py --url https://<preview hostname> --origin-ip <droplet IP>

The cookie is the value of the ``CF_Authorization`` cookie for the preview hostname, copied from the browser after you
sign in (developer tools, Application or Storage, Cookies). The file or variable may hold the bare value or
``CF_Authorization=<value>``. The cookie is never a command-line argument, is never printed and is never written to
the report. Unset the variable when you are done.

Checks (one pass/fail line each; the JSON report holds every request):

1. Without sign-in, every route family is refused or sent to the Access login and serves no content.
2. Forged, expired and unsigned tokens, as header and cookie, are refused.
3. With ``--origin-ip``: ports 80, 443, 8080 and 8765 on the droplet do not answer from here, and the preview hostname
   does not resolve to the droplet.
4. With a cookie: ``/api/health`` says ``public_release: false``; cache headers match ``backend/static_files.py`` and
   ``backend/preview_access.py``; versioned tiles come precompressed; raw-file and original-download URLs are not
   served; every ``?view=`` in the site navigation loads; the notice and MCR LTER attribution are on the page; no
   shared Cloudflare cache hits.

Exit code: 0 when every check ran and passed, 1 when any check failed, 2 when none failed but some were skipped
(no cookie or no ``--origin-ip``), so a partial run is never mistaken for acceptance.

What it cannot see: it speaks HTTP and does not run the page's scripts, so it does not prove a view draws; that is the
browser and real-phone check. A refusal by Cloudflare at the edge also hides whether the origin would refuse a forged
token by itself; the local tests in ``tests/test_preview.py`` cover the application's own check.

``--connect`` and ``--token-header`` exist only to test this script against a local stand-in of the preview, where no
Cloudflare sits in front to turn the cookie into the ``Cf-Access-Jwt-Assertion`` header.
"""
import argparse
import base64
import datetime as dt
import ipaddress
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
COOKIE_ENV = 'GLEN_PREVIEW_ACCESS_COOKIE'
COOKIE_NAME = 'CF_Authorization'
# The exact headers the preview sends, from backend/static_files.py and backend/preview_access.py.
PAGE_CACHE = 'no-cache'
TILE_CACHE = 'private, max-age=31536000, immutable'
API_CACHE = 'no-store'
ORIGIN_PORTS = (80, 443, 8080, 8765)
# Cloudflare cache states that mean a response came from, or was stored in, a shared cache.
SHARED_CACHE_STATES = {'HIT', 'STALE', 'UPDATING', 'REVALIDATED', 'EXPIRED'}
# Text that appears only in content the preview serves after sign-in.
# (Not "Private preview": the application's own refusal says "Private preview: sign in through Cloudflare Access.")
CONTENT_MARKERS = ('GLEN_PREVIEW', '"public_release"', '"jsonrpc"', 'navigationGroups', 'data-i18n',
                   '"tile_release"', '"llm_models"')
LOGIN_REDIRECT = re.compile(r'(^|\.)cloudflareaccess\.com$')
TOKEN_LIKE = re.compile(r'^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*$')
# Used only when the live navigation cannot be read; the check then fails and says so.
FALLBACK_VIEWS = [('moorings', None), ('temperature', None), ('profiles', None), ('sst', None), ('light', None), ('sediment_traps', None),
                  ('rivers', None), ('time', 'coral'), ('fish', None), ('metabolism', None), ('zooplankton', None),
                  ('experiments', None), ('planner', None), ('map', None), ('photos', None), ('topography', None)]
RAW_PROBES = [
    '/explorer.duckdb', '/data/explorer.duckdb', '/data/published.duckdb', '/glen-explorer/data/explorer.duckdb',
    '/data/m2/1037_6_0.parquet', '/data/waves.parquet', '/mcr_data/derived/btm_20min.parquet',
    '/derived/btm_20min.parquet', '/.env', '/glen-explorer/.env', '/web/.env', '/data/.env',
    '/release-manifest.json', '/web/release-manifest.json', '/static-data/release-manifest.json',
    '/mcr_data/inventory.csv', '/mcr_data/knb-lter-mcr.1035.20/', '/knb-lter-mcr.1035.20/',
    '/data/source/waves.csv', '/data/source/metadata.xml', '/data/serving-catalog.json', '/data/published.json',
    '/.git/HEAD', '/checks.html', '/model-eval.html',
    # The droplet's private folders (deploy kit layout), in case a proxy is ever pointed at them.
    '/private/glen-explorer/data/explorer.duckdb', '/runtime/glen-explorer/data/explorer.duckdb',
    '/runtime/glen-explorer/.env', '/usage.sqlite3', '/state/usage.sqlite3', '/glen-preview.env',
]
DOWNLOAD_PROBES = ['/api/materials/x/download', '/api/coral-multi/x/original', '/api/coral-photos/image/v/id/original',
                   '/api/gump-records/source/day', '/api/ctd-records/source/transects',
                   '/api/reef-temperature/download/cache']
MCP_INITIALIZE = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                  'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                             'clientInfo': {'name': 'glen-live-preview-check', 'version': '1'}}}


class Parser(argparse.ArgumentParser):
    """Never echo argument values: a mistyped option could carry the cookie."""
    def error(self, message):
        if any(word in message for word in ('unrecognized', 'invalid', 'ambiguous')):
            message = 'invalid arguments (their values are not shown)'
        self.print_usage(sys.stderr)
        self.exit(2, f'{self.prog}: error: {message}. The Access cookie is never a command-line argument: '
                     f'set {COOKIE_ENV} or pass --cookie-file.\n')


def parse_args(argv):
    p = Parser(description='Check the live private preview from outside.', allow_abbrev=False)
    p.add_argument('--url', required=True, help='https://<preview hostname>, without a path')
    p.add_argument('--origin-ip', help='the droplet public IP; its ports must not answer')
    p.add_argument('--cookie-file', type=Path, help=f'file holding the {COOKIE_NAME} value (else ${COOKIE_ENV})')
    p.add_argument('--report', type=Path, help='JSON report path (default reports/live-preview-check-<UTC>.json)')
    p.add_argument('--timeout', type=float, default=20.0, help='seconds per request (default 20)')
    p.add_argument('--port-timeout', type=float, default=3.0, help='seconds per origin port probe (default 3)')
    p.add_argument('--origin-ports', default=','.join(map(str, ORIGIN_PORTS)), help=argparse.SUPPRESS)
    p.add_argument('--connect', help='TESTING ONLY: send requests to http://HOST:PORT, keeping the preview Host')
    p.add_argument('--token-header', action='store_true',
                   help='TESTING ONLY: send the token as Cf-Access-Jwt-Assertion, as Cloudflare does after Access')
    a = p.parse_args(argv)
    u = urlsplit(a.url)
    if u.scheme != 'https' or not u.hostname or u.path not in ('', '/') or u.query or u.fragment:
        p.error('--url must be https://<hostname> with no path')
    a.url = f'https://{u.netloc}'
    try:
        a.origin_ports = tuple(int(x) for x in a.origin_ports.split(',') if x.strip())
    except ValueError:
        p.error('origin ports must be integers')
    if a.origin_ip:
        try:
            ipaddress.ip_address(a.origin_ip)
        except ValueError:
            p.error('--origin-ip must be an IP address')
    return a


def read_cookie(path, environ):
    """Return (token or None, source note). The note never contains the token."""
    if path:
        mode = path.stat().st_mode
        raw = path.read_text().strip()
        note = f'cookie file {path}'
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            note += ' (warning: readable by other users; chmod 600 it)'
    elif environ.get(COOKIE_ENV):
        raw, note = environ[COOKIE_ENV].strip(), f'environment variable {COOKIE_ENV}'
    else:
        return None, 'no cookie given'
    if raw.startswith(COOKIE_NAME + '='):
        raw = raw[len(COOKIE_NAME) + 1:].split(';', 1)[0].strip()
    return (raw or None), note


def token_expiry(token):
    """The token's exp claim as a UTC datetime, read without verifying it, or None."""
    try:
        exp = jwt.decode(token, options={'verify_signature': False}).get('exp')
        return dt.datetime.fromtimestamp(int(exp), dt.timezone.utc)
    except Exception:
        return None


def forged_tokens(real=None):
    """Tokens that must be refused: wrong key, expired, unsigned. With a real token, copy its claims and key id."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    claims, kid = {'iss': 'https://glen-preview.cloudflareaccess.com', 'aud': 'forged-audience',
                   'sub': 'forged-subject', 'email': 'forged@example.org', 'type': 'app'}, 'forged-key'
    if real:
        try:
            claims.update({k: v for k, v in jwt.decode(real, options={'verify_signature': False}).items()
                           if k in ('iss', 'aud', 'sub', 'email', 'type', 'identity_nonce', 'country')})
            kid = jwt.get_unverified_header(real).get('kid', kid)
        except Exception:
            pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_key = jwt.encode(claims | {'iat': now - 60, 'nbf': now - 60, 'exp': now + 3600}, key, algorithm='RS256',
                           headers={'kid': kid})
    expired = jwt.encode(claims | {'iat': now - 7200, 'nbf': now - 7200, 'exp': now - 3600}, key, algorithm='RS256',
                         headers={'kid': kid})
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b'=').decode()
    unsigned = b64({'alg': 'none', 'typ': 'JWT'}) + '.' + b64(claims | {'iat': now, 'exp': now + 3600}) + '.'
    return {'wrong signing key': wrong_key, 'expired': expired, 'unsigned (alg none)': unsigned}


class Checker:
    def __init__(self, url, client, token=None, token_header=False):
        self.url, self.client, self.token, self.token_header = url, client, token, token_header
        self.host = urlsplit(url).hostname
        self.cache_states = []

    # -- requests -------------------------------------------------------------------------------------------------
    def auth_headers(self):
        if self.token_header:
            return {'Cf-Access-Jwt-Assertion': self.token}
        return {'Cookie': f'{COOKIE_NAME}={self.token}'}

    def request(self, path, method='GET', headers=None, auth=False, **kw):
        h = dict(headers or {})
        if auth:
            h |= self.auth_headers()
        # Never let a cookie the site sets carry into a later request, signed in or not.
        self.client.cookies.clear()
        try:
            r = self.client.request(method, path, headers=h, **kw)
        except httpx.HTTPError as exc:
            return None, {'path': path, 'method': method, 'error': type(exc).__name__}
        finally:
            self.client.cookies.clear()
        rec = {'path': path, 'method': method, 'status': r.status_code,
               'cache_control': r.headers.get('cache-control'), 'content_type': r.headers.get('content-type')}
        for name in ('content-encoding', 'cf-cache-status', 'vary'):
            if name in r.headers:
                rec[name.replace('-', '_')] = r.headers[name]
        if r.is_redirect:
            loc = urlsplit(r.headers.get('location', ''))
            rec['redirect_host'] = loc.hostname or '(same host)'
            rec['redirect_path'] = loc.path
        if auth and 'cf-cache-status' in r.headers:
            self.cache_states.append((path, r.headers['cf-cache-status'].upper()))
        return r, rec

    def refused(self, r):
        """Refused or sent to the Access login, with none of the preview's content in the body."""
        if r is None:
            return False, 'request failed'
        if r.is_redirect:
            loc = urlsplit(r.headers.get('location', ''))
            if (loc.hostname and LOGIN_REDIRECT.search(loc.hostname)) or '/cdn-cgi/access/' in loc.path:
                return True, 'sent to the Access login'
            return False, 'redirected somewhere other than the Access login'
        if r.status_code in (401, 403):
            body = r.text[:200000]
            leaked = [m for m in CONTENT_MARKERS if m in body]
            if leaked:
                return False, f'refused with status {r.status_code} but the body holds preview content'
            return True, f'refused ({r.status_code})'
        if 200 <= r.status_code < 300:
            return False, f'served content ({r.status_code})'
        return False, f'unexpected status {r.status_code}'

    # -- checks ---------------------------------------------------------------------------------------------------
    def unauthenticated_paths(self, release):
        return ['/', '/?view=moorings', '/?view=time&family=coral', '/datasets.html', '/workspace.js',
                '/preview.js', '/workspace.css', '/preview.css', '/static-data/current.json',
                f'/static-data/{release}/manifest.json', '/api/health', '/api/datasets', '/config.json',
                '/layers-input.json', '/mcp']

    def check_unauthenticated(self, release):
        probes, failures = [], []
        for path in self.unauthenticated_paths(release):
            for method in (('GET', 'POST') if path == '/mcp' else ('GET',)):
                kw = {'json': MCP_INITIALIZE, 'headers': {'Accept': 'application/json, text/event-stream'}} \
                    if method == 'POST' else {}
                r, rec = self.request(path, method, **kw)
                ok, why = self.refused(r)
                rec['outcome'] = why
                probes.append(rec)
                if not ok:
                    failures.append(f'{method} {path}: {why}')
        return result('unauthenticated', 'Without sign-in every route is refused or sent to the Access login',
                      failures, probes)

    def check_forged(self):
        probes, failures = [], []
        for label, token in forged_tokens(self.token).items():
            for path in ('/', '/api/health', '/static-data/current.json', '/config.json', '/mcp'):
                r, rec = self.request(path, headers={'Cf-Access-Jwt-Assertion': token,
                                                     'Cookie': f'{COOKIE_NAME}={token}'})
                ok, why = self.refused(r)
                rec |= {'token': label, 'outcome': why}
                probes.append(rec)
                if not ok:
                    failures.append(f'{label} token, {path}: {why}')
        return result('forged-token', 'Forged, expired and unsigned Access tokens are refused', failures, probes)

    def check_session(self):
        r, rec = self.request('/api/health', auth=True)
        probes, failures = [rec], []
        expiry = token_expiry(self.token)
        if expiry:
            rec['cookie_expires_utc'] = expiry.isoformat(timespec='seconds')
            if expiry <= dt.datetime.now(dt.timezone.utc):
                failures.append(f'the cookie expired at {expiry:%Y-%m-%d %H:%M} UTC; sign in again and copy a new one')
        if r is None or r.status_code != 200:
            failures.append('the signed-in request was not accepted: ' + (self.refused(r)[1] if r is not None else 'request failed'))
        return result('session', 'The Access cookie is accepted', failures, probes)

    def check_health(self):
        r, rec = self.request('/api/health', auth=True)
        failures = []
        try:
            body = r.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            failures.append('/api/health did not return a JSON object')
        else:
            rec |= {'public_release': body.get('public_release'), 'access': body.get('access'),
                    'health_status': body.get('status')}
            if body.get('public_release') is not False:
                failures.append(f'public_release is {body.get("public_release")!r}, not false')
            if body.get('access') != 'private-preview':
                failures.append(f'access is {body.get("access")!r}, not "private-preview" (the Access boundary is not in front)')
            if body.get('status') != 'ok':
                failures.append(f'status is {body.get("status")!r}, not "ok"')
        return result('health', '/api/health reports public_release: false behind the Access boundary', failures, [rec])

    def page_assets(self, html):
        """Same-site scripts and stylesheets the page loads; third-party CDN files are not this server's."""
        refs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html) + \
            re.findall(r'<link[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"', html) + \
            re.findall(r'<link[^>]*\bhref="([^"]+)"[^>]*\brel="stylesheet"', html)
        seen = []
        for ref in refs:
            if ref.startswith('/') and not ref.startswith('//') and ref not in seen:
                seen.append(ref)
        return seen

    def check_revalidated(self, html):
        """Pages, scripts, styles and the tile pointer revalidate on every load (backend/static_files.py)."""
        paths = ['/', '/datasets.html', '/?view=moorings', '/static-data/current.json']
        paths += [p for p in self.page_assets(html) if p not in paths]
        for extra in ('/preview.js', '/preview.css', '/workspace-navigation.js'):
            if extra not in paths:
                paths.append(extra)
        probes, failures = [], []
        for path in paths:
            r, rec = self.request(path, auth=True)
            probes.append(rec)
            if r is None or r.status_code != 200:
                failures.append(f'{path}: status {rec.get("status", rec.get("error"))}')
            elif rec['cache_control'] != PAGE_CACHE:
                failures.append(f'{path}: Cache-Control {rec["cache_control"]!r}, expected {PAGE_CACHE!r}')
        return result('cache-revalidated', f'Pages, scripts, styles and current.json send Cache-Control: {PAGE_CACHE}',
                      failures, probes)

    def check_tiles(self, release, tile_paths):
        probes, failures = [], []
        if not release:
            failures.append('no tile release named in /static-data/current.json')
        for path in [f'/static-data/{release}/manifest.json'] + list(tile_paths):
            r, rec = self.request(path, auth=True, headers={'Accept-Encoding': 'gzip'})
            probes.append(rec)
            if r is None or r.status_code != 200:
                failures.append(f'{path}: status {rec.get("status", rec.get("error"))}')
                continue
            if rec['cache_control'] != TILE_CACHE:
                failures.append(f'{path}: Cache-Control {rec["cache_control"]!r}, expected {TILE_CACHE!r}')
            if rec.get('content_encoding') != 'gzip':
                failures.append(f'{path}: not served precompressed (Content-Encoding {rec.get("content_encoding")!r})')
        return result('cache-tiles', f'Versioned tiles send {TILE_CACHE} and come gzip-compressed', failures, probes)

    def check_no_store(self):
        probes, failures = [], []
        for path, method in (('/api/health', 'GET'), ('/api/datasets', 'GET'), ('/config.json', 'GET'),
                             ('/layers-input.json', 'GET'), ('/mcp', 'POST')):
            kw = {'json': MCP_INITIALIZE, 'headers': {'Accept': 'application/json, text/event-stream'}} \
                if method == 'POST' else {}
            r, rec = self.request(path, method, auth=True, **kw)
            probes.append(rec)
            if r is None or r.status_code != 200:
                failures.append(f'{method} {path}: status {rec.get("status", rec.get("error"))}')
            elif rec['cache_control'] != API_CACHE:
                failures.append(f'{method} {path}: Cache-Control {rec["cache_control"]!r}, expected {API_CACHE!r}')
        return result('cache-no-store', f'API, MCP and configuration responses send Cache-Control: {API_CACHE}',
                      failures, probes)

    def check_raw_files(self):
        probes, failures = [], []
        for path in RAW_PROBES:
            r, rec = self.request(path, auth=True)
            probes.append(rec)
            if r is None or r.status_code != 404:
                failures.append(f'{path}: status {rec.get("status", rec.get("error"))}, expected 404')
        return result('raw-files', 'Database, Parquet, .env, release-manifest and raw-package paths all 404',
                      failures, probes)

    def check_downloads(self):
        probes, failures = [], []
        for path in DOWNLOAD_PROBES:
            r, rec = self.request(path, auth=True)
            probes.append(rec)
            if r is None or r.status_code not in (403, 404):
                failures.append(f'{path}: status {rec.get("status", rec.get("error"))}, expected 403 or 404')
        return result('original-downloads', 'Original-file download routes are refused', failures, probes)

    def navigation_views(self):
        r, rec = self.request('/workspace-navigation.js', auth=True)
        if r is None or r.status_code != 200:
            return None, rec
        return parse_navigation(r.text), rec

    def check_views(self):
        views, nav = self.navigation_views()
        probes, failures = [nav], []
        if not views:
            failures.append('could not read the views from /workspace-navigation.js; checked a built-in list instead')
            views = FALLBACK_VIEWS
        for view, family in views:
            query = urlencode({'view': view} | ({'family': family} if family else {}))
            r, rec = self.request('/?' + query, auth=True)
            probes.append(rec)
            if r is None or r.status_code != 200:
                failures.append(f'?{query}: status {rec.get("status", rec.get("error"))}')
            elif 'text/html' not in (rec['content_type'] or '') or 'GLEN_PREVIEW=true' not in r.text:
                failures.append(f'?{query}: did not return the preview page')
        return result('views', f'Every ?view= in the site navigation loads ({len(views)} views)', failures, probes)

    def check_notice(self, pages):
        """The notice is in each page's HTML, or in /preview.js, which every preview page loads and which writes it."""
        probes, failures = [], []
        r, rec = self.request('/preview.js', auth=True)
        probes.append(rec)
        script = r.text if r is not None and r.status_code == 200 else ''
        for path, html in pages.items():
            found = {}
            for label, needle in (('not for redistribution', 'not for redistribution'), ('MCR LTER attribution', 'mcr lter')):
                if needle in html.lower():
                    found[label] = 'page HTML'
                elif needle in script.lower() and re.search(r'<script[^>]*src="/preview\.js"', html):
                    found[label] = '/preview.js, loaded by the page'
                else:
                    failures.append(f'{path}: no "{label}" in the HTML or in a /preview.js the page loads')
            probes.append({'path': path, 'found_in': found})
        return result('notice', 'The "not for redistribution" notice and MCR LTER attribution are on the pages',
                      failures, probes)

    def check_security_headers(self):
        r, rec = self.request('/', auth=True)
        failures = []
        if r is None:
            failures.append('request failed')
        else:
            for name, value in (('x-content-type-options', 'nosniff'), ('x-frame-options', 'DENY'),
                                ('referrer-policy', 'same-origin')):
                rec[name.replace('-', '_')] = r.headers.get(name)
                if r.headers.get(name) != value:
                    failures.append(f'{name} is {r.headers.get(name)!r}, expected {value!r}')
        return result('security-headers', 'Pages send nosniff, DENY framing and a same-origin referrer policy',
                      failures, [rec])

    def check_edge_cache(self):
        hits = list(dict.fromkeys(f'{path}: cf-cache-status {state}' for path, state in self.cache_states
                                  if state in SHARED_CACHE_STATES))
        seen = sorted({state for _, state in self.cache_states})
        return result('edge-cache', 'No signed-in response came from a shared Cloudflare cache', hits,
                      [{'cf_cache_status_seen': seen or ['(header absent)']}])


def parse_navigation(source):
    """(view, family) pairs from the site's menu, page tabs, source-record pages and footer pages."""
    text = re.sub(r'(?m)//.*$', '', source)
    views = []
    def add(view, family=None):
        if (view, family) not in views:
            views.append((view, family))
    for name in ('navigationGroups', 'PAGE_SWITCHES'):
        m = re.search(name + r'\s*=\s*\[(.*?)\n\];', text, re.S)
        if not m:
            return []
        for view, family in re.findall(r"\[\s*'([a-z_]+)'\s*,\s*'[^']*'\s*(?:,\s*'([a-z_]+)'\s*)?\]", m.group(1)):
            add(view, family or None)
    m = re.search(r'SOURCE_PAGES\s*=\s*\{(.*?)\n\};', text, re.S)
    for view in re.findall(r'(?m)^\s*([a-z_]+)\s*:\s*\{', m.group(1) if m else ''):
        add(view)
    m = re.search(r"OFF_MENU\s*=\s*new Set\(\[([^\]]*)\]\)", text)
    for view in re.findall(r"'([a-z_]+)'", m.group(1) if m else ''):
        add(view)
    return views


def result(check_id, title, failures, probes, status=None):
    return {'id': check_id, 'title': title, 'status': status or ('fail' if failures else 'pass'),
            'failures': failures, 'probes': probes}


def skipped(check_id, title, why):
    return result(check_id, title, [], [], status='skipped') | {'reason': why}


def probe_port(ip, port, timeout):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return 'open'
    except ConnectionRefusedError:
        return 'refused'
    except (socket.timeout, TimeoutError):
        return 'timeout'
    except OSError as exc:
        return f'unreachable ({exc.strerror or type(exc).__name__})'


def resolve_host(hostname):
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(hostname, 443)})
    except OSError:
        return []


def check_origin(ip, ports, timeout, hostname, resolver=resolve_host, probe=probe_port):
    probes, failures = [], []
    addr = ipaddress.ip_address(ip)
    for port in ports:
        state = probe(ip, port, timeout)
        probes.append({'port': port, 'state': state})
        if state == 'open':
            failures.append(f'{ip}:{port} accepts connections from this computer')
    note = None
    if addr.is_private or addr.is_loopback:
        note = f'{ip} is not a public address, so this does not test reachability from the Internet'
    if resolver:
        resolved = resolver(hostname)
        probes.append({'hostname_resolves_to': resolved or ['(did not resolve)']})
        if ip in resolved:
            failures.append(f'{hostname} resolves straight to the droplet; it must resolve only to Cloudflare')
    out = result('origin', f'The droplet does not answer on ports {", ".join(map(str, ports))} and is not in DNS',
                 failures, probes)
    if note:
        out['note'] = note
    return out


def run(args, client, token, cookie_note, port_probe=probe_port, resolver=resolve_host):
    checker = Checker(args.url, client, token, args.token_header)
    checks = []
    release = None
    if token:
        r, _ = checker.request('/static-data/current.json', auth=True)
        try:
            release = r.json().get('release') if r is not None and r.status_code == 200 else None
        except Exception:
            release = None
    if not release:
        try:
            release = json.loads((ROOT / 'site/static-data/current.json').read_text())['release']
        except Exception:
            release = 'v1'
    checks.append(checker.check_unauthenticated(release))
    checks.append(checker.check_forged())
    if args.origin_ip:
        checks.append(check_origin(args.origin_ip, args.origin_ports, args.port_timeout, checker.host,
                                   resolver=None if args.connect else resolver, probe=port_probe))
    else:
        checks.append(skipped('origin', 'The droplet does not answer on its ports', 'no --origin-ip given'))
    signed_in = [('health', '/api/health reports public_release: false'),
                 ('cache-revalidated', 'Pages, scripts, styles and current.json revalidate'),
                 ('cache-tiles', 'Versioned tiles are private, immutable and precompressed'),
                 ('cache-no-store', 'API, MCP and configuration are no-store'),
                 ('raw-files', 'Raw-file paths all 404'), ('original-downloads', 'Original downloads are refused'),
                 ('views', 'Every navigation view loads'), ('notice', 'Notice and attribution are on the pages'),
                 ('security-headers', 'Security headers are sent'), ('edge-cache', 'No shared cache hits')]
    if not token:
        checks.append(skipped('session', 'The Access cookie is accepted', cookie_note))
        checks += [skipped(i, t, 'no cookie given') for i, t in signed_in]
    else:
        session = checker.check_session()
        checks.append(session)
        if session['status'] == 'fail':
            checks += [result(i, t, ['not run: the cookie was not accepted'], []) for i, t in signed_in]
        else:
            pages = {}
            for path in ('/', '/datasets.html'):
                r, _ = checker.request(path, auth=True)
                pages[path] = r.text if r is not None and r.status_code == 200 else ''
            checks += [checker.check_health(), checker.check_revalidated(pages['/']),
                       checker.check_tiles(release, []), checker.check_no_store(), checker.check_raw_files(),
                       checker.check_downloads(), checker.check_views(), checker.check_notice(pages),
                       checker.check_security_headers(), checker.check_edge_cache()]
    failed = [c for c in checks if c['status'] == 'fail']
    skips = [c for c in checks if c['status'] == 'skipped']
    report = {'format': 1, 'checked_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
              'url': args.url, 'origin_ip': args.origin_ip, 'cookie': cookie_note,
              'connect': args.connect, 'token_header': args.token_header, 'tile_release': release,
              'result': 'fail' if failed else 'incomplete' if skips else 'pass', 'checks': checks}
    return report, (1 if failed else 2 if skips else 0)


def summary(report):
    lines = [f'Live preview check: {report["url"]} at {report["checked_at_utc"]}']
    for c in report['checks']:
        mark = {'pass': 'PASS', 'fail': 'FAIL', 'skipped': 'SKIP'}[c['status']]
        extra = f' ({c["reason"]})' if c['status'] == 'skipped' else ''
        lines.append(f'{mark}  {c["id"]}: {c["title"]}{extra}')
        for f in c['failures'][:8]:
            lines.append(f'        - {f}')
        if len(c['failures']) > 8:
            lines.append(f'        - and {len(c["failures"]) - 8} more in the report')
        if c.get('note'):
            lines.append(f'        note: {c["note"]}')
    word = {'pass': 'PASS: every check ran and passed',
            'fail': 'FAIL: do not invite testers',
            'incomplete': 'INCOMPLETE: nothing failed, but some checks were skipped'}[report['result']]
    lines.append(f'Result: {word}')
    return '\n'.join(lines)


def scrub(text, token):
    return text.replace(token, '[redacted]') if token else text


def make_client(args):
    if args.connect:
        base, headers = f'http://{args.connect}', {'Host': urlsplit(args.url).netloc}
    else:
        base, headers = args.url, {}
    headers['User-Agent'] = 'glen-live-preview-check/1'
    return httpx.Client(base_url=base, headers=headers, timeout=args.timeout, follow_redirects=False)


def main(argv=None, environ=None, client=None, port_probe=probe_port, resolver=resolve_host, out=None):
    out = out or sys.stdout
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if any(TOKEN_LIKE.match(a) for a in argv):
        print('A token-like value was on the command line. It was not used and is not shown. Clear it from your '
              f'shell history, sign out of the preview to end that session, and pass the cookie through {COOKIE_ENV} '
              'or --cookie-file.', file=sys.stderr)
        return 2
    args = parse_args(argv)
    try:
        token, note = read_cookie(args.cookie_file, environ)
    except OSError as exc:
        print(f'Could not read the cookie file: {exc.strerror}', file=sys.stderr)
        return 2
    own = client is None
    client = client or make_client(args)
    try:
        report, code = run(args, client, token, note, port_probe, resolver)
    finally:
        if own:
            client.close()
    path = args.report or ROOT / 'reports' / f'live-preview-check-{report["checked_at_utc"].replace(":", "")}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(json.dumps(report, indent=2), token) + '\n')
    print(scrub(summary(report), token), file=out)
    print(f'Report: {path}', file=out)
    return code


if __name__ == '__main__':
    sys.exit(main())
