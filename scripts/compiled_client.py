"""Share immutable client modules across CI workers, bound to the exact build inputs."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.web_assets import bundle_directory

STAMP = 'client-source.sha256'


def source_key(root=ROOT):
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--',
        'apps/web', 'package.json', 'package-lock.json', 'requirements/ci-runtimes.json',
        'scripts/build_web.py', 'scripts/preload_web.py', 'scripts/compiled_client.py', 'packages/web_assets.py'], cwd=root).decode().split('\0')
    digest = hashlib.sha256()
    for name in sorted(set(names) - {''}):
        digest.update(name.encode() + b'\0' + (root / name).read_bytes() + b'\0')
    return digest.hexdigest()


def prepare(cache, root=ROOT):
    key = source_key(root)
    folder = cache / key
    # The job creates this before pytest starts; no worker ever writes here.
    if not (folder / STAMP).is_file() or (folder / STAMP).read_text() != key:
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            modules = Path(temporary) / 'modules'
            bundle = root / 'apps/web/dist'
            if (bundle / STAMP).is_file() and (bundle / STAMP).read_text() == key:
                shutil.copytree(bundle_directory(bundle), modules, ignore=shutil.ignore_patterns('*.gz'))
            else:
                node = shutil.which('node')
                compiler = root / 'node_modules/typescript/bin/tsc'
                assert node and compiler.is_file(), 'Pinned TypeScript and Node are required for real client-logic tests'
                modules.mkdir()
                subprocess.run([node, str(compiler), '-p', str(root / 'apps/web/tsconfig.json'), '--outDir', str(modules)],
                               check=True, capture_output=True, text=True)
            (modules / 'package.json').write_text('{"type":"module"}', encoding='utf-8')
            (modules / STAMP).write_text(key, encoding='utf-8')
            if folder.exists():
                shutil.rmtree(folder)
            modules.rename(folder)
    return folder


def shared_modules(root=ROOT):
    folder = Path(os.environ['FMUC_CLIENT_MODULES'])
    key = source_key(root)
    if folder.name != key or (folder / STAMP).read_text() != key:
        raise ValueError('Shared client modules differ from the current source/toolchain inputs')
    return folder


if __name__ == '__main__':
    if sys.argv[1:] == ['stamp']:
        (ROOT / 'apps/web/dist' / STAMP).write_text(source_key(), encoding='utf-8')
    elif not sys.argv[1:]:
        folder = prepare(Path(os.environ['RUNNER_TEMP']) / 'fmuc-compiled-client')
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as output:
            output.write('FMUC_CLIENT_MODULES=' + str(folder) + '\n')
    else:
        raise SystemExit('usage: compiled_client.py [stamp]')
