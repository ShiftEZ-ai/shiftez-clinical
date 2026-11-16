"""Readiness and web delivery progress even when every sync-request token is borrowed."""
import asyncio
from threading import Event

import anyio.to_thread
import httpx
import pytest
from starlette.staticfiles import StaticFiles

from apps.api import main
from apps.api.read_io import IndependentStaticFiles


@pytest.mark.parametrize('encoding,compressed', [('gzip', True), ('gzip;q=0', False),
    ('*;q=1, gzip;q=0', False), ('br, gzip;q=.5', True), ('gzip;q=invalid', False), ('identity', False)])
def test_versioned_assets_reuse_precompressed_bytes_and_keep_conditional_and_range_semantics(tmp_path, encoding, compressed):
    import gzip
    version = 'a' * 64
    path = tmp_path / version
    path.mkdir()
    body = b'export const probe = true;\n' * 10_000
    (path / 'probe.js').write_bytes(body)
    zipped = gzip.compress(body, mtime=0)
    (path / 'probe.js.gz').write_bytes(zipped)

    async def scenario():
        app = main.FastAPI()
        app.mount('/assets', IndependentStaticFiles(directory=tmp_path, cache_seconds=123))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            headers = {'accept-encoding': encoding}
            response = await client.get(f'/assets/{version}/probe.js', headers=headers)
            assert response.content == body
            assert response.headers.get('content-encoding') == ('gzip' if compressed else None)
            assert response.headers['content-length'] == str(len(zipped) if compressed else len(body))
            assert response.headers['cache-control'] == 'public, max-age=123, immutable'
            assert response.headers['vary'] == 'Accept-Encoding'
            assert response.headers['content-type'].split(';')[0] in {'text/javascript', 'application/javascript'}
            response = await client.get(f'/assets/{version}/probe.js',
                headers={**headers, 'if-none-match': response.headers['etag']})
            assert response.status_code == 304 and 'immutable' in response.headers['cache-control']
            response = await client.get(f'/assets/{version}/probe.js', headers={**headers, 'range': 'bytes=0-5'})
            assert response.status_code == 206 and response.content == body[:6]
            assert 'content-encoding' not in response.headers
    asyncio.run(scenario())


def test_precompressed_symlinks_cannot_escape_asset_root(tmp_path):
    version = tmp_path / ('b' * 64)
    version.mkdir()
    (version / 'probe.js').write_text('public')
    secret = tmp_path.parent / (tmp_path.name + '-secret')
    secret.write_text('private')
    (version / 'probe.js.gz').symlink_to(secret)
    async def scenario():
        app = main.FastAPI()
        app.mount('/', IndependentStaticFiles(directory=tmp_path, cache_seconds=123))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/' + version.name + '/probe.js', headers={'accept-encoding': 'gzip'})
            assert response.text == 'public' and 'content-encoding' not in response.headers
    asyncio.run(scenario())


def test_static_cache_and_precompression_can_be_disabled(h, tmp_path, monkeypatch):
    from dataclasses import replace
    import gzip
    monkeypatch.setenv('ROTA_STATIC_CACHE_SECONDS', '0')
    monkeypatch.setenv('ROTA_STATIC_PRECOMPRESSED', 'false')
    settings = main.Settings.from_env()
    assert settings.static_cache_seconds == 0 and not settings.static_precompressed
    with pytest.raises(RuntimeError, match='STATIC_CACHE_SECONDS'):
        replace(h.settings, static_cache_seconds=-1).check()
    version = tmp_path / ('c' * 64)
    version.mkdir()
    (version / 'probe.js').write_bytes(b'public')
    (version / 'probe.js.gz').write_bytes(gzip.compress(b'public'))

    async def scenario():
        app = main.FastAPI()
        app.mount('/', IndependentStaticFiles(directory=tmp_path, cache_seconds=settings.static_cache_seconds,
            precompressed=settings.static_precompressed))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/' + version.name + '/probe.js', headers={'accept-encoding': 'gzip'})
            assert response.content == b'public'
            assert 'cache-control' not in response.headers and 'content-encoding' not in response.headers
            missing = await client.get('/' + version.name + '/missing')
            assert missing.status_code == 404 and 'cache-control' not in missing.headers
    asyncio.run(scenario())


def test_health_and_complete_web_delivery_bypass_a_saturated_request_pool(h, tmp_path, monkeypatch):
    web = tmp_path / 'web'
    (web / 'assets').mkdir(parents=True)
    (web / 'assets' / 'probe.js').write_bytes(b'export const probe = true;\n' * 10_000)
    (web / 'sw.js').write_text('/* synthetic worker */')
    (web / 'index.html').write_text('<!doctype html><title>Synthetic shell</title>')
    monkeypatch.setattr(main, 'WEB', web)
    app = main.create_app(h.settings, engine=h.app.state.engine)

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 1
        entered, release = asyncio.Event(), Event()
        loop = asyncio.get_running_loop()
        def hold_request():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(60), 'The test did not release the occupied request pool'
        blocked = asyncio.create_task(anyio.to_thread.run_sync(hold_request))
        try:
            await asyncio.wait_for(entered.wait(), 60)
            paths = ['/health/live', '/health/ready', '/health/service', '/assets/probe.js', '/sw.js',
                     '/', '/invite/synthetic', '/reset/synthetic']
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=h.settings.public_origin) as client:
                responses = await asyncio.wait_for(asyncio.gather(*(client.get(path) for path in paths)), 60)
            assert [response.status_code for response in responses] == [200, 200, 503, 200, 200, 200, 200, 200]
            assert responses[3].content == (web / 'assets' / 'probe.js').read_bytes()
            assert not blocked.done() and limiter.borrowed_tokens == 1
        finally:
            release.set()
            await blocked
    asyncio.run(scenario())


@pytest.mark.parametrize('method,headers', [
    ('GET', {}), ('HEAD', {}), ('POST', {}), ('GET', {'range': 'bytes=1-9'}),
    ('GET', {'range': 'bytes=0-2,8-10'}), ('GET', {'range': 'bytes=999999-'}),
    ('GET', {'range': 'bad-range'}), ('GET', {'range': 'bytes=0-2', 'if-range': 'outdated'}),
    ('GET', {'if-none-match': '*'}),
])
def test_static_delivery_retains_starlette_http_semantics(tmp_path, method, headers):
    (tmp_path / 'probe.js').write_bytes(b'export const probe = true;\n' * 10_000)
    async def scenario():
        responses = []
        for cls in (StaticFiles, IndependentStaticFiles):
            app = main.FastAPI()
            app.mount('/', cls(directory=tmp_path))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://test') as client:
                responses.append(await client.request(method, '/probe.js', headers=headers))
        before, after = responses
        assert before.status_code == after.status_code
        for key in ('content-length', 'content-range', 'last-modified', 'etag', 'allow'):
            assert before.headers.get(key) == after.headers.get(key)
        if method == 'GET' and headers.get('range') == 'bytes=0-2,8-10':
            # Each response intentionally generates its own multipart boundary.
            assert b'exp' in after.content and b'ons' in after.content
        else:
            assert before.content == after.content
    asyncio.run(scenario())


@pytest.mark.parametrize('path', ['/missing', '/..%2Foutside-secret', '/%00', '/link'])
def test_static_paths_cannot_escape_the_served_directory(tmp_path, path):
    assets = tmp_path / 'assets'
    assets.mkdir()
    secret = tmp_path / 'outside-secret'
    secret.write_text('Synthetic private data')
    (assets / 'link').symlink_to(secret)
    async def scenario():
        app = main.FastAPI()
        app.mount('/assets', IndependentStaticFiles(directory=assets))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/assets' + path)
            assert response.status_code == 404 and 'Synthetic private data' not in response.text
    asyncio.run(scenario())
