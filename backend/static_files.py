"""Static files that a browser checks before reusing.

With no Cache-Control header a browser guesses how long a file stays fresh, and on 2026-11-05 one kept showing an old
branch of the site after the files had changed, on a plain page load. Pages, scripts and stylesheets now say no-cache,
which means "ask first": an unchanged file still costs only a small 304 reply. Other files keep the default, because the
static tiles are versioned by folder and revalidating thousands of them would be slow.
"""
from pathlib import PurePosixPath

from starlette.staticfiles import StaticFiles

REVALIDATED = {'', '.html', '.js', '.css'}  # '' is a directory or the site root, which serves index.html
POINTER_FILES = {'current.json'}  # names the tile release, so it changes while the tiles it points to do not


class RevalidatedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        name = PurePosixPath(path)
        if name.suffix in REVALIDATED or name.name in POINTER_FILES:
            response.headers['Cache-Control'] = 'no-cache'
        return response
