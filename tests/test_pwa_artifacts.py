"""Public install assets can be read without a global writer once rendered."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

from fastapi.testclient import TestClient
import pytest

from apps.api import main
from apps.api.services import pwa
from packages.contracts.schemas import BRANDING_DEFAULTS, PWA_ICON_PROFILE

MANIFEST = '/api/pwa/manifest.webmanifest'
ALIAS = '/assets/manifest.webmanifest'
APPLE = '/api/pwa/apple-touch-icon.png'

pytestmark = pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)


def test_warm_manifest_and_icons_do_not_take_the_writer(h, monkeypatch):
    with closing(TestClient(h.app, base_url=h.settings.public_origin)) as client:
        manifest = client.get(MANIFEST)
        assert manifest.status_code == 200
        path = manifest.json()['icons'][0]['src']
        apple, immutable = client.get(APPLE), client.get(path)
        assert apple.status_code == immutable.status_code == 200

        writer_calls = []
        retry_write = main.retry_write

        def observed(*args, **kwargs):
            writer_calls.append(True)
            return retry_write(*args, **kwargs)

        monkeypatch.setattr(main, 'retry_write', observed)
        for url, original in ((MANIFEST, manifest), (ALIAS, manifest), (APPLE, apple), (path, immutable)):
            warm = client.get(url)
            assert warm.status_code == 200 and warm.content == original.content
            for header in ('cache-control', 'etag', 'content-type', 'content-length'):
                assert warm.headers[header] == original.headers[header]
        assert writer_calls == []


@pytest.mark.usefixtures('branding_editor')
def test_branding_update_and_undo_refresh_public_artifacts(h):
    with closing(TestClient(h.app, base_url=h.settings.public_origin)) as client:
        original, apple = client.get(MANIFEST), client.get(APPLE)
        path = original.json()['icons'][0]['src']
        immutable = client.get(path)
        h.command('admin', 'branding.update', {'app_name': BRANDING_DEFAULTS['app_name'],
                  'institution_name': BRANDING_DEFAULTS['institution_name'], 'icon_background': '#112233',
                  'reason': 'Update the install icon background'}, revision=1)
        changed = client.get(MANIFEST, headers={'If-None-Match': original.headers['etag']})
        new_apple = client.get(APPLE, headers={'If-None-Match': apple.headers['etag']})
        assert changed.status_code == new_apple.status_code == 200
        assert changed.json()['icons'] != original.json()['icons']
        assert new_apple.content != apple.content and new_apple.headers['etag'] != apple.headers['etag']
        assert client.get(path).content == immutable.content
        h.command('admin', 'branding.undo', {'reason': 'Restore the install icon background'}, revision=2)
        assert client.get(MANIFEST).content == original.content
        assert client.get(APPLE).content == apple.content


def test_matching_icon_tags_return_empty_304(h):
    with closing(TestClient(h.app, base_url=h.settings.public_origin)) as client:
        manifest = client.get(MANIFEST)
        for path in (APPLE, manifest.json()['icons'][0]['src']):
            response = client.get(path)
            assert response.status_code == 200
            etag = response.headers['etag']
            for known in (etag, f'"old", W/{etag}'):
                cached = client.get(path, headers={'If-None-Match': known})
                assert cached.status_code == 304 and not cached.content
                assert cached.headers['etag'] == etag
                assert cached.headers['cache-control'] == response.headers['cache-control']


def test_concurrent_first_requests_render_each_variant_once(h, monkeypatch):
    gate = Barrier(2)
    retry_write = main.retry_write
    opened = []
    open_image = pwa.Image.open

    def simultaneous(*args, **kwargs):
        gate.wait(timeout=10)
        return retry_write(*args, **kwargs)

    def observed(*args, **kwargs):
        opened.append(True)
        return open_image(*args, **kwargs)

    def fetch(_):
        with closing(TestClient(h.app, base_url=h.settings.public_origin)) as client:
            return client.get(MANIFEST)

    monkeypatch.setattr(main, 'retry_write', simultaneous)
    monkeypatch.setattr(pwa.Image, 'open', observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(fetch, range(2))
    assert first.status_code == second.status_code == 200 and first.content == second.content
    assert len(opened) == len(PWA_ICON_PROFILE['variants'])
