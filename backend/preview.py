"""Explicit private deployment entry point; local development continues to use server:app."""
import mimetypes
from starlette.exceptions import HTTPException
from starlette.applications import Starlette
from starlette.routing import Mount, Route, Router
from .preview_access import PreviewBoundary, Settings
from .static_files import RevalidatedStaticFiles


class PreviewFiles(RevalidatedStaticFiles):
    async def get_response(self, path, scope):
        headers=dict(scope['headers'])
        accepts=headers.get(b'accept-encoding',b'').decode('ascii',errors='ignore')
        gzip_ok=False
        for part in accepts.split(','):
            bits=[x.strip() for x in part.split(';')]
            if bits[0]!='gzip':continue
            try:
                q=next((float(x[2:]) for x in bits[1:] if x.startswith('q=')),1)
                gzip_ok=0<q<=1
            except ValueError:
                gzip_ok=False
        if gzip_ok and b'range' not in headers:
            try:
                response=await super().get_response(path+'.gz',scope)
                response.headers['Content-Encoding']='gzip'
                response.headers['Content-Type']=mimetypes.guess_type(path)[0] or 'application/octet-stream'
                response.headers['Vary']='Accept-Encoding'
                return response
            except HTTPException as exc:
                if exc.status_code!=404:raise
        response=await super().get_response(path,scope)
        response.headers['Vary']='Accept-Encoding'
        return response


class PreviewRoutes:
    """Reuse explicit backend endpoints, never the local checkout's static mounts."""
    def __init__(self, routes, static):
        self.api = Router(routes=[route for route in routes if isinstance(route, Route) and
                          (route.path.startswith('/api/') or route.path in
                           {'/mcp', '/config.json', '/layers-input.json'})], redirect_slashes=False)
        self.static = static

    async def __call__(self, scope, receive, send):
        if scope['path'].startswith('/api/') or scope['path'] in {'/mcp', '/config.json', '/layers-input.json'}:
            return await self.api(scope, receive, send)
        return await self.static(scope, receive, send)


def create_app():
    from . import server
    from scripts.build_preview_release import verify
    settings=Settings.environment()
    verify(settings.release)
    web=settings.release/'web'
    static=PreviewFiles(directory=web,html=True)

    router=Starlette(routes=[Mount('/',server.LocalBoundary(PreviewRoutes(server.routes,static)))],lifespan=server.lifespan)
    return PreviewBoundary(router,settings)
