#!/usr/bin/env python3
"""Add the same static module preload graph to local and container web artifacts (stdlib only)."""
from pathlib import Path
import os
import re
import sys
import gzip
import json
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.web_assets import bundle_directory, bundle_version


def module_names(dist: Path) -> list[str]:
    """The existing build's direct and transitive imports; cycles and shared imports appear once."""
    assets = dist / 'assets'
    if not (assets / 'app.js').is_file():
        assets = bundle_directory(dist)
    seen, todo = set(), ["app.js"]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += re.findall(r"from '\./([\w]+\.js)'", (assets / name).read_text(encoding="utf-8"))
    return sorted(seen - {"app.js"})


def add_preloads(dist: Path) -> None:
    """Run after copying public/index.html: retain its contents and add the required module links."""
    links = "".join(f'<link rel="modulepreload" href="/assets/{name}">' for name in module_names(dist))
    index = dist / "index.html"
    index.write_text(index.read_text(encoding="utf-8").replace('<script type="module"', links + "\n" + '<script type="module"', 1), encoding="utf-8")
    stamp_commit(dist)
    version_assets(dist)


def version_assets(dist: Path) -> None:
    """One content-addressed module graph; relative imports and CSS URLs keep working."""
    assets = dist / 'assets'
    version = bundle_version(assets)
    worker = assets / 'sw.js'
    worker_source = worker.read_text(encoding='utf-8') if worker.exists() else None
    # Stage inside the copied layer, then atomically publish the one versioned graph.
    staging = assets / f'.{version}.tmp'
    staging.mkdir()
    try:
        for source in assets.iterdir():
            if source == staging:
                continue
            target = staging / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        bundle = assets / version
        os.replace(staging, bundle)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    for source in assets.iterdir():
        if source.name == version:
            continue
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
    if worker_source is not None:
        (bundle / 'sw.js').unlink()
    files = sorted(path for path in bundle.rglob('*') if path.is_file())
    for source in files:
        data = source.read_bytes()
        compressed = gzip.compress(data, mtime=0)
        if len(compressed) < len(data):
            source.with_name(source.name + '.gz').write_bytes(compressed)
    index = dist / 'index.html'
    index.write_text(index.read_text(encoding='utf-8').replace('/assets/', f'/assets/{version}/'), encoding='utf-8')
    if worker_source is not None:
        shell = ['/'] + ['/' + path.relative_to(dist).as_posix() for path in files]
        (dist / 'sw.js').write_text(worker_source.replace('__BUNDLE_VERSION__', version)
            .replace('__SHELL_FILES__', json.dumps(shell)), encoding='utf-8')


def stamp_commit(dist: Path) -> None:
    """The bundle's own build commit (apps/web/src/build.ts), from the same ROTA_BUILD_COMMIT the server reports
    (apps/api/version.py build()): About Rota tells two builds of one version apart. Unset: left empty."""
    commit = os.getenv("ROTA_BUILD_COMMIT", "").strip()[:7]
    if not commit:
        return
    if not re.fullmatch(r"[0-9a-fA-F]+", commit):
        raise SystemExit("ROTA_BUILD_COMMIT must be a git commit id")
    target = dist / "assets" / "build.js"
    text = target.read_text(encoding="utf-8")
    if text.count("WEB_COMMIT = ''") != 1:
        raise SystemExit("apps/web/src/build.ts no longer compiles to WEB_COMMIT = ''; update stamp_commit")
    target.write_text(text.replace("WEB_COMMIT = ''", f"WEB_COMMIT = '{commit}'"), encoding="utf-8")


if __name__ == "__main__":
    add_preloads(Path(sys.argv[1]))
