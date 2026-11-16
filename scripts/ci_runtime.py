"""Install locked runtimes and check pre-baked Chromium; no runner identity or qualification receipts."""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))


def node_pin(dockerfile):
    matches = re.findall(r"^ARG NODE_IMAGE=(node:(\d+\.\d+\.\d+)-[^\s]+@sha256:[a-f0-9]{64})$",
                         dockerfile, re.M)
    if len(matches) != 1:
        raise ValueError("One exact digest-pinned Node image is required")
    return matches[0]


def node_version(dockerfile):
    return node_pin(dockerfile)[1]


def python_pin(workflow):
    matches = re.findall(r"^  PYTHON_VERSION: '(\d+\.\d+\.\d+)'$", workflow, re.M)
    if len(matches) != 1:
        raise ValueError("One exact workflow Python pin is required")
    return matches[0]


def chromium_version(manifest):
    browsers = [row for row in manifest["browsers"] if row["name"] == "chromium"]
    if len(browsers) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", browsers[0].get("browserVersion", "")):
        raise ValueError("The pinned Playwright package must name one Chromium version")
    return browsers[0]["browserVersion"]


def browser_manifest():
    import playwright
    from scripts.ci_pool_runtime import playwright_version

    locked = playwright_version((ROOT / "requirements/test.lock").read_text(encoding="utf-8"))
    if metadata.version("playwright") != locked:
        raise ValueError("Installed Playwright differs from the test lock")
    data = (Path(playwright.__file__).parent / "driver/package/browsers.json").read_bytes()
    manifest = json.loads(data)
    version = chromium_version(manifest)
    revisions = {}
    for name in ("chromium", "chromium-headless-shell"):
        rows = [row for row in manifest["browsers"] if row["name"] == name]
        if (len(rows) != 1 or rows[0].get("browserVersion") != version
                or not isinstance(rows[0].get("revision"), str)
                or not re.fullmatch(r"[1-9][0-9]*", rows[0]["revision"])):
            raise ValueError("The pinned Playwright manifest must name exact Chromium and headless shell revisions")
        revisions[name] = rows[0]["revision"]
    return {"playwright": locked, "chromium_version": version, "chromium_revision": revisions["chromium"],
            "headless_shell_revision": revisions["chromium-headless-shell"],
            "manifest_sha256": hashlib.sha256(data).hexdigest()}


def browser_identity():
    from playwright.sync_api import sync_playwright
    manifest = browser_manifest()
    expected = manifest["chromium_version"]
    executable = os.getenv("CHROMIUM_EXECUTABLE") or None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, channel=None if executable else "chromium",
                                              headless=True, args=["--no-sandbox"])
        try:
            if browser.version != expected:
                raise ValueError("Pre-baked Chromium differs from the hash-locked Playwright browser version")
            return {"actual": browser.version, "expected": expected,
                    "revision": manifest["chromium_revision"],
                    "headless_shell_revision": manifest["headless_shell_revision"],
                    "manifest_sha256": manifest["manifest_sha256"]}
        finally:
            browser.close()


def runtime_pins():
    pins = json.loads((ROOT / "requirements/ci-runtimes.json").read_text(encoding="utf-8"))
    if pins.get("schema") != 1 or pins.get("platform") != "linux-x86_64":
        raise ValueError("Unsupported pinned runtime platform")
    for tool, files in (("python", {"bin/python3.13", "lib/libpython3.13.so.1.0"}), ("node", {"bin/node"}), ("gh", {"bin/gh"})):
        pin = pins[tool]
        if (not re.fullmatch(r"[a-f0-9]{64}", pin["sha256"])
                or set(pin["files"]) != files
                or any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in pin["files"].values())
                or not re.fullmatch(r"[A-Za-z0-9._+-]+", pin["root"])
                or not pin["url"].startswith("https://")):
            raise ValueError("Invalid pinned runtime archive or binary hashes")
    if pins["python"].get("module_origins") != {"_ssl": "built-in", "_sqlite3": "built-in"}:
        raise ValueError("Pinned Python must embed its SSL and SQLite providers")
    return pins


def runtime_platform():
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("Pinned CI runtimes require Linux x86_64")


def install_runtime(tool, env):
    """Every runner consumes the same verified archive; there is no provider fallback."""
    runtime_platform()
    pin = runtime_pins()[tool]
    expected = (python_pin((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")) if tool == "python" else
                node_version((ROOT / "infra/Dockerfile").read_text(encoding="utf-8")) if tool == "node" else pin['version'])
    if pin["version"] != expected:
        raise ValueError("Runtime hash lock differs from the workflow/image version pin")
    # Virtualenvs retain the base interpreter's absolute path across exact-key restores.
    directory = Path(env['RUNNER_TEMP']) / ('pin-' + tool + '-' + pin['sha256'])
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir()
    archives = Path(env['RUNNER_TEMP']) / 'runtime-archives'
    archives.mkdir(exist_ok=True)
    archive = archives / (pin['sha256'] + '.tar')
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != pin['sha256']:
        subprocess.run(["bash", str(ROOT / ".github/scripts/fetch_pinned.sh"), pin["url"], pin["sha256"],
                        str(archive)], check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError("Runtime archive differs from its hash pin")
    with tarfile.open(archive) as package:
        package.extractall(directory, filter="data")
    prefix = directory / pin["root"]
    if {name: hashlib.sha256((prefix / name).read_bytes()).hexdigest() for name in pin["files"]} != pin["files"]:
        raise ValueError("Extracted runtime binary differs from its hash pin")
    with open(env["GITHUB_PATH"], "a", encoding="utf-8") as output:
        output.write(str(prefix / "bin") + "\n")
    return prefix / 'bin' / tool


def main(mode, argv=None):
    if mode == 'install':
        parser = argparse.ArgumentParser(description='Install the committed hash-pinned runtime')
        parser.add_argument('--tools', required=True, choices=('python', 'node', 'gh'))
        install_runtime(parser.parse_args(argv).tools, os.environ)
    elif mode == 'browser':
        print('Pre-baked Chromium ' + browser_identity()['actual'])
    elif mode == 'browser-manifest':
        print(json.dumps(browser_manifest(), sort_keys=True))
    else:
        raise ValueError('Unknown runtime operation')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2:])
