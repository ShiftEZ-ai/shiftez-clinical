import importlib.util
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dependency_snapshot", ROOT / ".github/scripts/dependency_snapshot.py")
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def test_snapshot_submits_every_pinned_lock_package_with_its_scope():
    body = snapshot.snapshot("a" * 40, "refs/heads/dev", "1", "Dependency graph", "2026-11-19T00:00:00+00:00")
    locks = sorted((ROOT / "requirements").glob("*.lock"))
    assert {"requirements/" + path.name for path in locks} <= set(body["manifests"])
    for path in locks:
        pins = re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", path.read_text(encoding="utf-8"), re.M)
        resolved = body["manifests"]["requirements/" + path.name]["resolved"]
        assert set(resolved) == {snapshot.pypi(name, version) for name, version in pins}, path.name
        scope = "runtime" if path.name in snapshot.RUNTIME_LOCKS else "development"
        assert {row["scope"] for row in resolved.values()} == {scope}
    assert "pkg:pypi/psycopg@" in " ".join(body["manifests"]["requirements/postgres.lock"]["resolved"])
    assert body["version"] == 0 and body["ref"] == "refs/heads/dev" and body["job"]["correlator"]


def test_names_are_normalised_and_markers_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "ROOT", tmp_path)
    lock = tmp_path / "requirements" / "runtime.lock"
    lock.parent.mkdir()
    lock.write_text('-r other.lock\n# note\nZope.Interface==7.1 ; python_version >= "3.9" \\\n'
                    '    --hash=sha256:' + "0" * 64 + '\n', encoding="utf-8")
    resolved = snapshot.python_manifest(lock)["resolved"]
    assert list(resolved) == ["pkg:pypi/zope-interface@7.1"] and resolved["pkg:pypi/zope-interface@7.1"]["scope"] == "runtime"


@pytest.mark.parametrize("line", ["requests>=2\n", "requests\n", "requests==\n"])
def test_unpinned_lines_refuse(tmp_path, monkeypatch, line):
    monkeypatch.setattr(snapshot, "ROOT", tmp_path)
    lock = tmp_path / "requirements" / "test.lock"
    lock.parent.mkdir()
    lock.write_text(line, encoding="utf-8")
    with pytest.raises(ValueError, match="Unpinned"):
        snapshot.python_manifest(lock)


def test_main_refuses_anything_but_a_push_to_dev(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/dev")
    with pytest.raises(SystemExit, match="pushes to the default branch only"):
        snapshot.main()


@pytest.mark.parametrize("default", ["dev", "main"])
def test_main_submits_only_pushes_to_the_default_branch(monkeypatch, tmp_path, default):
    event = tmp_path / "event.json"
    event.write_text('{"repository": {"default_branch": "%s"}}' % default, encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    other = "main" if default == "dev" else "dev"
    for ref in ("refs/heads/" + other, "refs/heads/feature"):
        monkeypatch.setenv("GITHUB_REF", ref)
        with pytest.raises(SystemExit, match="pushes to the default branch only"):
            snapshot.main()
    monkeypatch.setenv("GITHUB_REF", "refs/heads/" + default)
    def gate_passed(*args):
        raise LookupError("gate passed")
    monkeypatch.setattr(snapshot, "snapshot", gate_passed)
    for name in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_WORKFLOW"):
        monkeypatch.setenv(name, "1")
    with pytest.raises(LookupError, match="gate passed"):
        snapshot.main()
    monkeypatch.delenv("GITHUB_EVENT_PATH")
    with pytest.raises(SystemExit, match="pushes to the default branch only"):
        snapshot.main()
