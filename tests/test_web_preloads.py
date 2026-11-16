"""Real web-artifact behavior; shipped build wiring is checked by the image qualification gate."""
import errno
from pathlib import Path
import re
import subprocess
import sys

import pytest
from apps.api.version import build
from scripts import preload_web
from scripts.preload_web import add_preloads, module_names, stamp_commit
from packages.web_assets import bundle_directory, bundle_version

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = '<!doctype html><title>Fixture</title><script type="module" src="/assets/app.js"></script>'


def artifact(folder):
    assets = folder / 'assets'
    assets.mkdir(parents=True)
    (folder / 'index.html').write_text(PUBLIC, encoding='utf-8')
    modules = {'app.js': "import {a} from './alpha.js'; import {s} from './shared.js';",
               'alpha.js': "import {d} from './deep.js'; import {s} from './shared.js';",
               'deep.js': "import {a} from './alpha.js';", 'shared.js': '', 'unrelated.js': '',
               'build.js': "export const WEB_COMMIT = '';\n"}
    modules['sw.js'] = (ROOT / 'apps/web/public/assets/sw.js').read_text()
    for name, source in modules.items():
        (assets / name).write_text(source, encoding='utf-8')
    return folder


def test_recursive_preloads_and_cli_produce_the_same_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv('ROTA_BUILD_COMMIT', '0123456789abcdef')
    local = artifact(tmp_path / 'local')
    add_preloads(local)
    html = (local / 'index.html').read_text(encoding='utf-8')
    assert re.findall(r'href="(/assets/[^"]+)"', html) == [
        f'/assets/{version(local)}/alpha.js', f'/assets/{version(local)}/deep.js', f'/assets/{version(local)}/shared.js']
    assert html.endswith(f'<script type="module" src="/assets/{version(local)}/app.js"></script>')
    assert '<title>Fixture</title>' in html
    assert "WEB_COMMIT = '0123456'" in (bundle_directory(local) / 'build.js').read_text(encoding='utf-8')

    packaged = artifact(tmp_path / 'cli')
    subprocess.run([sys.executable, str(ROOT / 'scripts/preload_web.py'), str(packaged)], check=True)
    assert (packaged / 'index.html').read_bytes() == (local / 'index.html').read_bytes()
    assert (bundle_directory(packaged) / 'build.js').read_bytes() == (bundle_directory(local) / 'build.js').read_bytes()


def test_missing_recursive_dependency_fails_without_rewriting_index(tmp_path):
    dist = artifact(tmp_path / 'broken')
    (dist / 'assets/deep.js').unlink()
    with pytest.raises(FileNotFoundError):
        add_preloads(dist)
    assert (dist / 'index.html').read_text(encoding='utf-8') == PUBLIC


def test_current_local_artifact_preloads_the_complete_reachable_graph():
    dist = ROOT / 'apps/web/dist'
    html = (dist / 'index.html').read_text(encoding='utf-8')
    actual = re.findall(r'<link rel="modulepreload" href="/assets/([^"]+)">', html)
    assert actual and [Path(name).name for name in actual] == module_names(dist)


def version(dist):
    return re.search(r'/assets/([0-9a-f]{64})/', (dist / 'index.html').read_text()).group(1)


def test_transitive_change_versions_the_entire_graph_and_precompression_is_exact(tmp_path):
    import gzip
    a, b = artifact(tmp_path / 'before'), artifact(tmp_path / 'after')
    (a / 'assets' / 'shared.js').write_text('export const value = "' + 'x' * 4096 + '";')
    (b / 'assets' / 'shared.js').write_text('export const value = "' + 'y' * 4096 + '";')
    add_preloads(a)
    add_preloads(b)
    assert version(a) != version(b)
    for dist in (a, b):
        path = dist / 'assets' / version(dist)
        assert (path / 'app.js').read_text() == "import {a} from './alpha.js'; import {s} from './shared.js';"
        assert list((dist / 'assets').iterdir()) == [path]
        assert gzip.decompress((path / 'shared.js.gz').read_bytes()) == (path / 'shared.js').read_bytes()
        assert (path / 'alpha.js').is_file() and (path / 'deep.js').is_file()
        assert f"rota-shell-{version(dist)}" in (dist / 'sw.js').read_text()
        assert f"/assets/{version(dist)}/app.js" in (dist / 'sw.js').read_text()


def test_exdev_versioning_matches_the_normal_tree(tmp_path, monkeypatch):
    normal = artifact(tmp_path / 'normal')
    exdev = artifact(tmp_path / 'exdev')
    for dist in (normal, exdev):
        (dist / 'assets' / 'shared.js').write_text('export const value = "' + 'x' * 4096 + '";')

    add_preloads(normal)
    expected = bundle_version(exdev / 'assets')
    real_rename = Path.rename

    def fail_lower_layer_rename(source, target):
        if source == exdev / 'assets' and target == exdev / expected:
            raise OSError(errno.EXDEV, 'cross-device link')
        return real_rename(source, target)

    monkeypatch.setattr(preload_web.Path, 'rename', fail_lower_layer_rename)
    add_preloads(exdev)

    def tree(root):
        return {path.relative_to(root).as_posix():
                (path.is_dir(), path.read_bytes() if path.is_file() else None)
                for path in root.rglob('*')}

    for dist in (normal, exdev):
        html = (dist / 'index.html').read_text(encoding='utf-8')
        bundle = dist / 'assets' / expected
        assert f'/assets/{expected}/app.js' in html
        assert list((dist / 'assets').iterdir()) == [bundle]
        assert not (dist / 'assets' / 'app.js').exists()
        assert list(bundle.rglob('*.gz'))
        worker = (dist / 'sw.js').read_text(encoding='utf-8')
        assert f'rota-shell-{expected}' in worker
        assert f'/assets/{expected}/app.js' in worker
    assert tree(normal) == tree(exdev)


def test_the_bundle_is_stamped_with_the_build_commit_the_server_reports(tmp_path, monkeypatch):
    # Control the compiled input: the real dist may already name a release.
    target = artifact(tmp_path) / 'assets' / 'build.js'
    monkeypatch.delenv('ROTA_BUILD_COMMIT', raising=False)
    stamp_commit(tmp_path)
    assert "WEB_COMMIT = ''" in target.read_text(encoding='utf-8')  # unset: empty, as the server then reports none
    assert build()['commit'] is None
    monkeypatch.setenv('ROTA_BUILD_COMMIT', '0123456789abcdef')
    stamp_commit(tmp_path)
    assert "WEB_COMMIT = '0123456'" in target.read_text(encoding='utf-8')  # the same 7 characters as build()["commit"]
    assert build()['commit'] == '0123456'
    monkeypatch.setenv('ROTA_BUILD_COMMIT', "x';alert(1)//")
    with pytest.raises(SystemExit):
        stamp_commit(tmp_path)


def test_shipped_assets_have_one_copy_and_all_static_links_resolve():
    import json
    from packages.web_assets import asset_url
    web = ROOT / 'apps/web/dist'
    bundle = bundle_directory(web)
    assert list((web / 'assets').iterdir()) == [bundle]
    html = (web / 'index.html').read_text()
    for path in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html):
        assert (web / path.lstrip('/')).is_file(), path
    assert not (bundle / 'manifest.webmanifest').exists(), 'the API owns the manifest (generated from branding)'
    assert asset_url('brand/emblem.png') in html
    assert (web / 'sw.js').is_file() and not (bundle / 'sw.js').exists()
    assert '__BUNDLE_VERSION__' not in (web / 'sw.js').read_text()
    assert '__SHELL_FILES__' not in (web / 'sw.js').read_text()


def test_browser_probes_never_import_an_unversioned_module():
    sources = [*(ROOT / 'tests').glob('test_browser*.py'), ROOT / 'tests/collection_fixture.py']
    assert not [p.name for p in sources if re.search(r"\bimport\(['\"]/assets/", p.read_text())]
