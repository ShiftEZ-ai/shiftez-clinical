"""Compiled modules are immutable, source-bound, and prepared before xdist starts."""
import shutil
import subprocess

import pytest

from scripts import compiled_client as client


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    files = {'.gitignore': '/apps/web/dist/\n', 'apps/web/src/a.ts': 'export const value = 1;',
             'apps/web/tsconfig.json': '{}', 'apps/web/public/index.html': '<main></main>',
             'package-lock.json': '{}', 'requirements/ci-runtimes.json': '{}', 'scripts/compiled_client.py': '# compiler'}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    return root


@pytest.mark.parametrize('name', ['apps/web/src/a.ts', 'apps/web/src/new.ts', 'apps/web/tsconfig.json',
                                 'apps/web/public/index.html', 'package-lock.json',
                                 'requirements/ci-runtimes.json', 'scripts/compiled_client.py'])
def test_key_changes_for_source_tree_toolchain_or_builder_but_not_build_output(checkout, name):
    before = client.source_key(checkout)
    bundle = checkout / 'apps/web/dist/assets'
    bundle.mkdir(parents=True)
    (bundle / 'a.js').write_text('stale build')
    assert client.source_key(checkout) == before
    (checkout / name).write_text('changed')
    assert client.source_key(checkout) != before


def test_exact_stamped_artifact_is_shared_and_worker_mutations_stay_private(checkout, tmp_path, monkeypatch):
    bundle = checkout / 'apps/web/dist'
    assets = bundle / 'assets' / ('a' * 64)
    assets.mkdir(parents=True)
    (bundle / 'index.html').write_text('<script type="module" src="/assets/' + 'a' * 64 + '/app.js"></script>')
    (assets / 'a.js').write_text('export const value = 1;')
    (bundle / client.STAMP).write_text(client.source_key(checkout))
    original_run = subprocess.run
    def no_compile(command, **kwargs):
        assert command[0] == 'git', 'Exact artifact must not recompile'
        return original_run(command, **kwargs)
    monkeypatch.setattr(client.subprocess, 'run', no_compile)
    cache = tmp_path / 'cache'
    first = client.prepare(cache, checkout)
    assert client.prepare(cache, checkout) == first
    monkeypatch.setenv('FMUC_CLIENT_MODULES', str(first))
    assert client.shared_modules(checkout) == first
    worker = tmp_path / 'worker'
    shutil.copytree(first, worker)
    (worker / 'a.js').write_text('private mutation')
    assert (first / 'a.js').read_text() == 'export const value = 1;'
    assert (first / 'package.json').read_text() == '{"type":"module"}'
    (checkout / 'apps/web/src/a.ts').write_text('changed')
    with pytest.raises(ValueError, match='differ'):
        client.shared_modules(checkout)


@pytest.mark.parametrize('stamp', [None, 'wrong-key'])
def test_cold_or_stale_bundle_compiles_once_and_failed_compile_is_never_published(checkout, tmp_path, monkeypatch, stamp):
    compiler = checkout / 'node_modules/typescript/bin/tsc'
    compiler.parent.mkdir(parents=True)
    compiler.touch()
    if stamp:
        bundle = checkout / 'apps/web/dist'
        bundle.mkdir()
        (bundle / client.STAMP).write_text(stamp)
    calls = []
    original_run = subprocess.run
    def compile(command, **kwargs):
        if command[0] == 'git':
            return original_run(command, **kwargs)
        calls.append(command)
        assert kwargs['check'] and str(checkout / 'apps/web/tsconfig.json') in command
        (client.Path(command[-1]) / 'a.js').write_text('compiled')
    monkeypatch.setattr(client.shutil, 'which', lambda name: 'node')
    monkeypatch.setattr(client.subprocess, 'run', compile)
    cache = tmp_path / 'cache'
    folder = client.prepare(cache, checkout)
    assert (folder / 'a.js').read_text() == 'compiled'
    assert client.prepare(cache, checkout) == folder and len(calls) == 1
    (checkout / 'apps/web/src/a.ts').write_text('changed')
    def fail(command, **kwargs):
        if command[0] == 'git':
            return original_run(command, **kwargs)
        raise subprocess.CalledProcessError(1, 'tsc')
    monkeypatch.setattr(client.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        client.prepare(cache, checkout)
    assert not (cache / client.source_key(checkout)).exists()
