"""Fail-closed Cloudflare Access boundary for every private-preview route."""
import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from .preview_analyst import Ledger, TESTER_DAY_MICRO_USD, TESTER_MONTH_MICRO_USD, failure


@dataclass(frozen=True)
class Settings:
    origin: str
    issuer: str
    audience: str
    emails: frozenset
    release: Path
    state: Path
    budget: int = 0
    tester_day: int = TESTER_DAY_MICRO_USD
    tester_month: int = TESTER_MONTH_MICRO_USD

    def __post_init__(self):
        u = urlsplit(self.origin)
        if u.scheme != 'https' or not u.hostname or u.netloc != u.hostname or u.path or u.query or u.fragment:
            raise ValueError('Preview origin must be one exact HTTPS host, without a trailing slash.')
        if not re.fullmatch(r'https://[a-z0-9-]+\.cloudflareaccess\.com', self.issuer):
            raise ValueError('Use the exact Cloudflare Access team issuer.')
        if not self.audience or not self.emails or any('@' not in e for e in self.emails):
            raise ValueError('An audience and explicit tester email allowlist are required.')
        if not self.release.is_absolute() or not self.state.is_absolute():
            raise ValueError('Release and private state paths must be absolute.')
        if self.state.resolve().is_relative_to(self.release.resolve()):
            raise ValueError('Private usage state cannot be inside the release.')
        if any(type(v) is not int or v < 0 for v in (self.budget, self.tester_day, self.tester_month)):
            raise ValueError('Budgets must be nonnegative integer microdollars.')

    @classmethod
    def environment(cls):
        return cls(os.environ['GLEN_PREVIEW_ORIGIN'], os.environ['GLEN_ACCESS_ISSUER'],
                   os.environ['GLEN_ACCESS_AUD'], frozenset(x.strip().lower() for x in
                   os.environ['GLEN_PREVIEW_EMAILS'].split(',') if x.strip()),
                   Path(os.environ['GLEN_PREVIEW_RELEASE']), Path(os.environ['GLEN_PREVIEW_STATE']),
                   int(os.environ.get('GLEN_PREVIEW_BUDGET_MICRO_USD', '0')),
                   int(os.environ.get('GLEN_PREVIEW_TESTER_DAY_MICRO_USD', str(TESTER_DAY_MICRO_USD))),
                   int(os.environ.get('GLEN_PREVIEW_TESTER_MONTH_MICRO_USD', str(TESTER_MONTH_MICRO_USD))))


class AccessVerifier:
    def __init__(self, settings, key_client=None):
        self.settings = settings
        self.keys = key_client or jwt.PyJWKClient(settings.issuer + '/cdn-cgi/access/certs', timeout=5)

    def verify(self, token):
        key = self.keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256'], audience=self.settings.audience,
                            issuer=self.settings.issuer, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'email']})
        email = claims['email']
        if not isinstance(email, str) or email.lower() not in self.settings.emails or not isinstance(claims['sub'], str) or not claims['sub']:
            raise ValueError('Not an invited tester.')
        return self.settings.issuer + '|' + claims['sub']


class PreviewBoundary:
    def __init__(self, app, settings, verifier=None, ledger=None):
        self.app, self.settings = app, settings
        self.verifier = verifier or AccessVerifier(settings)
        self.ledger = ledger or Ledger(settings.state, settings.budget,
                                       tester_month_micro_usd=settings.tester_month,
                                       tester_day_micro_usd=settings.tester_day)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        if scope['type'] != 'http':
            return
        headers = {}
        for k, v in scope['headers']:
            if k in headers and k in (b'host', b'origin', b'cf-access-jwt-assertion'):
                return await failure('Duplicate security header.', 400)(scope, receive, send)
            headers[k] = v
        host = urlsplit(self.settings.origin).netloc.encode()
        if headers.get(b'host') != host or (b'origin' in headers and headers[b'origin'] != self.settings.origin.encode()):
            return await failure('Preview host or origin rejected.', 403)(scope, receive, send)
        token = headers.get(b'cf-access-jwt-assertion', b'')
        if not token or len(token) > 16384:
            return await failure('Private preview: sign in through Cloudflare Access.', 401)(scope, receive, send)
        try:
            identity = await asyncio.to_thread(self.verifier.verify, token.decode('ascii'))
        except Exception:
            return await failure('Private preview access denied.', 403)(scope, receive, send)
        path = scope['path']
        # ASGI paths are already URL-decoded. Never let a downstream filesystem
        # normalize an API-shaped path into a different resource.
        if '\\' in path or '\x00' in path or any(part in {'.', '..'} for part in path.split('/')):
            return await failure('Invalid preview path.', 400)(scope, receive, send)
        # Original files remain inaccessible even if a visitor guesses their route.
        if (path.startswith(('/api/gump-records/source/', '/api/ctd-records/source/', '/api/reef-temperature/download/'))
                or (path.startswith(('/api/materials/', '/api/coral-multi/', '/api/coral-photos/')) and path.endswith(('/download', '/original')))):
            return await failure('Original downloads are disabled for this preview.', 403)(scope, receive, send)
        state = dict(scope.get('state', {}), preview_identity=identity, preview_origin=self.settings.origin,
                     preview_ledger=self.ledger)
        # The validated external origin is fixed configuration. Forward only a local
        # authority to the existing loopback/MCP boundary, never client proxy headers.
        local_headers = [(k, v) for k, v in scope['headers'] if k not in
                         (b'host', b'origin', b'forwarded', b'x-forwarded-host', b'x-forwarded-proto',
                          b'cf-access-authenticated-user-email', b'cf-access-jwt-assertion')]
        local_headers += [(b'host', b'127.0.0.1:8765')]
        if b'origin' in headers:
            local_headers += [(b'origin', b'http://127.0.0.1:8765')]
        local = dict(scope, state=state, headers=local_headers, scheme='http', server=('127.0.0.1',8765))

        async def secured(message):
            if message['type'] == 'http.response.start':
                h = [(k,v) for k,v in message.get('headers',[]) if k.lower() not in
                     (b'cache-control', b'x-content-type-options', b'referrer-policy', b'x-frame-options')]
                cache = b'private, max-age=31536000, immutable' if path.startswith('/static-data/v') and message['status']==200 else b'no-store' if path.startswith(('/api/', '/mcp', '/config.json', '/layers-input.json')) else b'no-cache'
                h += [(b'cache-control',cache),(b'x-content-type-options',b'nosniff'),
                      (b'referrer-policy',b'same-origin'),(b'x-frame-options',b'DENY')]
                message = dict(message, headers=h)
            await send(message)
        await self.app(local, receive, secured)
