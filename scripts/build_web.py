#!/usr/bin/env python3
from pathlib import Path
import json
import shutil
import subprocess
import sys
from preload_web import add_preloads

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web"
subprocess.run([sys.executable, str(ROOT / "scripts/generate_contracts.py"), "--check"], cwd=ROOT, check=True)
compiler = ROOT / "node_modules/typescript/bin/tsc"
node = shutil.which("node")
if not node or not compiler.exists():
    raise SystemExit("TypeScript compiler not found. Run npm ci in a connected environment.")
executable = [node, str(compiler)]
version = subprocess.check_output([*executable, "--version"], text=True).strip()
pinned = json.loads((ROOT / 'package-lock.json').read_text(encoding='utf-8'))['packages']['node_modules/typescript']['version']
if version != "Version " + pinned:
    raise SystemExit("Pinned TypeScript " + pinned + " is required, found: " + version)
if (WEB / "dist").exists(): shutil.rmtree(WEB / "dist")
subprocess.run([*executable, "-p", str(WEB / "tsconfig.json")], cwd=ROOT, check=True)
shutil.copytree(WEB / "public", WEB / "dist", dirs_exist_ok=True)
add_preloads(WEB / "dist")
print("Frontend built to apps/web/dist; no runtime CDN or npm dependency")
