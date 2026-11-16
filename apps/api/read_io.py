"""Health and immutable web files never queue behind synchronous API handlers."""
import asyncio
from contextlib import asynccontextmanager
import errno
import os
import stat
import mimetypes

from anyio import CancelScope
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from packages.web_assets import versioned_asset


async def background_io(function, *args):
    # asyncio's I/O executor is separate from Starlette/AnyIO's request capacity limiter.
    with CancelScope(shield=True):
        return await asyncio.to_thread(function, *args)


class _File:
    def __init__(self, file):
        self.file = file

    async def read(self, size):
        return await background_io(self.file.read, size)

    async def seek(self, offset):
        return await background_io(self.file.seek, offset)


class IndependentFileResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        if self.stat_result is None:
            self.stat_result = await background_io(os.stat, self.path)
            if not stat.S_ISREG(self.stat_result.st_mode):
                raise RuntimeError(f'File at path {self.path} is not a file.')
            self.set_stat_headers(self.stat_result)
        return await super().__call__(scope, receive, send)

    @asynccontextmanager
    async def _open_file(self):
        # Keep Starlette's streaming, range, HEAD and disconnect handling unchanged.
        file = await background_io(open, self.path, 'rb')
        try:
            yield _File(file)
        finally:
            await background_io(file.close)


class IndependentStaticFiles(StaticFiles):
    """Asset-only mount; Starlette still owns path containment and conditional responses."""
    def __init__(self, *args, cache_seconds=0, precompressed=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache_seconds, self.precompressed = cache_seconds, precompressed

    async def check_config(self):
        result = await background_io(os.stat, self.directory)
        if not stat.S_ISDIR(result.st_mode):
            raise RuntimeError(f'StaticFiles path {self.directory!r} is not a directory.')

    async def get_response(self, path, scope):
        if scope['method'] not in ('GET', 'HEAD'):
            raise HTTPException(405, headers={'Allow': 'GET, HEAD'})
        try:
            full_path, result = await background_io(self.lookup_path, path)
        except PermissionError:
            raise HTTPException(401) from None
        except ValueError:
            raise HTTPException(404) from None
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                raise HTTPException(404) from None
            raise
        if result is None or not stat.S_ISREG(result.st_mode):
            raise HTTPException(404)
        headers = Headers(scope=scope)
        versioned = versioned_asset(path)
        compressed = False
        media_type = mimetypes.guess_type(full_path)[0]
        if versioned and self.precompressed and 'range' not in headers and accepts_gzip(headers.get('accept-encoding', '')):
            zipped, zipped_stat = await background_io(self.lookup_path, path + '.gz')
            if zipped_stat is not None and stat.S_ISREG(zipped_stat.st_mode):
                full_path, result, compressed = zipped, zipped_stat, True
        response = IndependentFileResponse(full_path, stat_result=result, media_type=media_type)
        if versioned:
            response.headers['Vary'] = 'Accept-Encoding'
            if self.cache_seconds:
                response.headers['Cache-Control'] = f'public, max-age={self.cache_seconds}, immutable'
        if compressed:
            response.headers['Content-Encoding'] = 'gzip'
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


def accepts_gzip(value):
    qualities = {}
    for item in value.lower().split(','):
        encoding, *parameters = item.strip().split(';')
        quality = 1.0
        for parameter in parameters:
            key, _, raw = parameter.strip().partition('=')
            if key == 'q':
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0
        qualities[encoding] = quality
    return 0 < qualities.get('gzip', qualities.get('*', 0)) <= 1
