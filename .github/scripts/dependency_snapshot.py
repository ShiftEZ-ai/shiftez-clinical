"""Submit the hash-locked Python and npm dependencies to GitHub's dependency graph, so Dependabot alerts cover them.

The graph cannot parse requirements/*.lock (hash-locked pip files under a .lock name) and sees no dependencies in
pyproject.toml or package-lock.json. This builds one snapshot from the exact locks the builds install and posts it
to the dependency submission API. It runs only on pushes to dev.
"""
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_LOCKS = {"runtime.lock", "postgres.lock"}  # installed in the application image; the rest is tooling and tests
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)(?:\s*;[^\\]*)?\s*\\?\s*")


def pypi(name, version):
    return "pkg:pypi/" + re.sub(r"[-_.]+", "-", name).lower() + "@" + version


def python_manifest(path):
    resolved = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith(("#", "--hash=", "-r ")):
            continue
        match = PIN.fullmatch(text)
        if match is None:
            raise ValueError(f"Unpinned or unreadable lock line: {path.name}:{number}")
        purl = pypi(*match.groups())
        resolved[purl] = {"package_url": purl, "scope": "runtime" if path.name in RUNTIME_LOCKS else "development"}
    if not resolved:
        raise ValueError("Lock file pins nothing: " + path.name)
    return {"name": path.name, "file": {"source_location": path.relative_to(ROOT).as_posix()}, "resolved": resolved}


def npm_manifest(path):
    resolved = {}
    for key, package in json.loads(path.read_text(encoding="utf-8")).get("packages", {}).items():
        if not key.startswith("node_modules/"):
            continue
        name, version = key.rsplit("node_modules/", 1)[1], package.get("version")
        if not version:
            raise ValueError("npm lock entry without a version: " + key)
        purl = "pkg:npm/" + name.replace("@", "%40", 1) + "@" + version
        resolved[purl] = {"package_url": purl, "scope": "development" if package.get("dev") else "runtime"}
    return {"name": path.name, "file": {"source_location": path.relative_to(ROOT).as_posix()}, "resolved": resolved}


def snapshot(sha, ref, run_id, workflow, scanned):
    locks = sorted((ROOT / "requirements").glob("*.lock"))
    if not locks:
        raise ValueError("No requirements/*.lock files")
    manifests = {path.relative_to(ROOT).as_posix(): python_manifest(path) for path in locks}
    npm = ROOT / "package-lock.json"
    if npm.is_file():
        manifests["package-lock.json"] = npm_manifest(npm)
    return {"version": 0, "sha": sha, "ref": ref, "scanned": scanned,
            "job": {"id": run_id, "correlator": workflow + " dependency-snapshot"},
            "detector": {"name": "shiftez-lock-snapshot", "version": "1",
                         "url": os.environ.get("GITHUB_SERVER_URL", "https://github.com") + "/"
                                + os.environ.get("GITHUB_REPOSITORY", "")},
            "manifests": manifests}


def main():
    env = os.environ
    try:
        default = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_bytes())["repository"]["default_branch"]
    except (KeyError, OSError, TypeError, ValueError):
        default = None
    if not isinstance(default, str) or env.get("GITHUB_EVENT_NAME") != "push" or env.get("GITHUB_REF") != "refs/heads/" + default:
        raise SystemExit("Dependency snapshots are submitted for pushes to the default branch only")
    body = snapshot(env["GITHUB_SHA"], env["GITHUB_REF"], env["GITHUB_RUN_ID"], env["GITHUB_WORKFLOW"],
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
    request = urllib.request.Request(
        env["GITHUB_API_URL"] + "/repos/" + env["GITHUB_REPOSITORY"] + "/dependency-graph/snapshots",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + env["SUBMISSION_TOKEN"], "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read())
        if response.status != 201:
            raise SystemExit("Dependency snapshot was not accepted: " + str(response.status))
    count = sum(len(manifest["resolved"]) for manifest in body["manifests"].values())
    print(f"Submitted {count} packages in {len(body['manifests'])} manifests: {result.get('result')} {result.get('id')}")


if __name__ == "__main__":
    sys.exit(main())
