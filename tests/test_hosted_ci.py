"""Offline regression checks for the public CI planner and required aggregate."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


spec = importlib.util.spec_from_file_location(
    "hosted_ci", Path(__file__).resolve().parents[1] / ".github/scripts/hosted_ci.py")
hosted_ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted_ci)


def test_balancing_is_duration_based_complete_and_deterministic():
    seconds = {"a": 100, "b": 50, "c": 40, "d": 10}
    result = hosted_ci.balance(list(seconds), seconds, 2)
    assert result == [{"files": ["a"], "expected_seconds": 100},
                      {"files": ["b", "c", "d"], "expected_seconds": 100}]
    assert result == hosted_ci.balance(list(reversed(seconds)), seconds, 2)
    assert hosted_ci.balance(["new"], seconds, 1)[0]["expected_seconds"] == 45


@pytest.mark.parametrize("files,count", [([], 1), (["a", "a"], 1), (["a"], 0), (["a"], 2)])
def test_invalid_plan_is_refused(files, count):
    with pytest.raises(ValueError):
        hosted_ci.balance(files, {"a": 1}, count)


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", "", "unknown"])
def test_aggregate_refuses_every_non_success(state):
    needs = {name: {"result": "success"} for name in ("checks", "unit", "browser")}
    hosted_ci.require_results(needs)
    needs["unit"]["result"] = state
    with pytest.raises(ValueError):
        hosted_ci.require_results(needs)


def test_missing_producer_and_duplicate_durations_are_refused():
    with pytest.raises(ValueError):
        hosted_ci.require_results({"checks": {"result": "success"}})
    with pytest.raises(ValueError):
        json.loads('{"a":1,"a":2}', object_pairs_hook=hosted_ci.unique_object)


def test_success_requires_complete_original_execution(tmp_path, monkeypatch):
    report = tmp_path / "junit.xml"
    node = "tests/test_x.py::test_a"
    report.with_suffix(".selection.json").write_text(json.dumps([node]))
    report.with_suffix(".deselected.json").write_text("[]")
    rows = lambda paths: [(node, {"outcome": "passed"})]
    hosted_ci.require_execution(report, ["tests/test_x.py"], rows)
    with pytest.raises(ValueError):
        hosted_ci.require_execution(report, ["tests/test_missing.py"], rows)
    with pytest.raises(ValueError):
        hosted_ci.require_execution(report, ["tests/test_x.py"], lambda paths: [])

    monkeypatch.syspath_prepend(str(Path(hosted_ci.__file__).parent))
    monkeypatch.syspath_prepend(str(Path(hosted_ci.__file__).parents[2] / "ci"))
    import make_known_failures as maker
    import trusted_ci

    sources = tmp_path / "tests"
    sources.mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (sources / "test_x.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "def test_a():\n"
        "    marker = Path('first-attempt')\n"
        "    recovered = marker.exists()\n"
        "    marker.touch()\n"
        "    assert recovered\n"
        "def test_always_red():\n"
        "    assert False\n"
        "@pytest.mark.parametrize('value', [0, 1])\n"
        "def test_listed_red(value):\n"
        "    assert value == 1\n"
        "def test_always_green():\n"
        "    pass\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(trusted_ci.hosted, "git", lambda *args: "a" * 40)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    run = trusted_ci.invocation_receipt
    listed_node = "tests/test_x.py::test_listed_red[0]"
    listed = {maker.canonical_node(listed_node)}
    evidence = run(["tests/test_x.py"], "unit", report, [], "0", None, listed=listed)
    original = trusted_ci.canaries.report_cases(report, evidence)
    assert evidence["exit_code"] == evidence["retry"]["exit_code"] == 1
    assert trusted_ci.canaries.failed_nodes(original) == [node, "tests/test_x.py::test_always_red", listed_node]
    failures = maker.retry_nodes(original, listed)
    assert failures == [node, "tests/test_x.py::test_always_red"]
    assert trusted_ci.canaries.recovered_nodes(report, evidence, failures) == {node}
    assert sorted(path.name for path in tmp_path.glob("*.raw.xml")) == ["junit-retry.raw.xml", "junit.raw.xml"]
    listed_only = run([listed_node], "unit", tmp_path / "listed.xml", [], "0", None, listed=listed)
    assert listed_only["exit_code"] == 1 and "retry" not in listed_only
    assert not (tmp_path / "listed-retry.xml").exists()
    (tmp_path / "shard-receipt.json").write_text(json.dumps({
        "sha": "a" * 40, "tree": "a" * 40, "run_id": "fixture", "run_attempt": "1", "reports": [evidence],
    }))
    trusted_ci.canaries.verify_directory(tmp_path)
    for event in ("pull_request", "push"):
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
        single = run(["tests/test_x.py"], "unit", tmp_path / (event + ".xml"), [], "0", None)
        assert single["exit_code"] == 1 and "retry" not in single
        assert not (tmp_path / (event + "-retry.xml")).exists()


def test_scoped_shard_accepts_only_verified_deselection(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(hosted_ci.__file__).parent))
    import trusted_ci

    sources = tmp_path / "tests"
    sources.mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nmarkers = ["browser"]\n')
    path = "tests/test_scoped.py"
    (tmp_path / path).write_text("import pytest\n@pytest.mark.browser\ndef test_a():\n    pass\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setattr(trusted_ci.hosted, "git", lambda *args: "a" * 40)
    report = tmp_path / "junit.xml"
    evidence = trusted_ci.invocation_receipt([path], "unit", report, [], "0", None, partial=True)
    assert trusted_ci.canaries.report_cases(report, evidence, selected_scope={path}) == []
    for scope in ((), {"tests/test_other.py"}):
        with pytest.raises(ValueError, match="Incomplete execution"):
            trusted_ci.canaries.report_cases(report, evidence, selected_scope=scope)
    (tmp_path / "shard-receipt.json").write_text(json.dumps({
        "sha": "a" * 40, "tree": "a" * 40, "run_id": "fixture", "run_attempt": "1", "reports": [evidence],
    }))
    trusted_ci.canaries.verify_directory(tmp_path, partial=True)
    with pytest.raises(ValueError, match="Reported node set differs"):
        trusted_ci.canaries.verify_directory(tmp_path)


@pytest.mark.parametrize("private,counts", [
    (False, (2, 2)),
    (None, (2, 2)),
    ("true", (2, 2)),
    (True, (25, 18)),
])
def test_smoke_requires_private_visibility_and_two_shards(private, counts):
    hosted_ci.require_smoke(True, 2, 2)
    with pytest.raises(ValueError):
        hosted_ci.require_smoke(private, *counts)


@pytest.mark.parametrize("fault", [None, "archive", "binary"])
def test_runtime_hashes_are_checked_before_exporting_a_path(tmp_path, monkeypatch, fault):
    content = b"synthetic executable"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as package:
        entry = tarfile.TarInfo("tool/bin/node")
        entry.size = len(content)
        package.addfile(entry, io.BytesIO(content))
    raw = archive.getvalue()
    pin = {"url": "https://example.org/locked.tar", "root": "tool",
           "sha256": hashlib.sha256(raw).hexdigest(),
           "files": {"bin/node": hashlib.sha256(content).hexdigest()}}
    if fault == "archive":
        pin["sha256"] = "0" * 64
    elif fault == "binary":
        pin["files"]["bin/node"] = "0" * 64
    monkeypatch.setattr(hosted_ci.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(raw))
    if fault:
        with pytest.raises(ValueError):
            hosted_ci.install_runtime("node", pin, tmp_path)
    else:
        folder = hosted_ci.install_runtime("node", pin, tmp_path)
        assert (folder / "node").read_bytes() == content


@pytest.mark.parametrize("workers", ["0", "2"])
def test_collection_receipts_keep_selected_and_deselected_nodes(tmp_path, workers):
    source = tmp_path / "test_sample.py"
    source.write_text("import pytest\ndef test_selected(): pass\n@pytest.mark.omit\ndef test_omitted(): pass\n")
    report = tmp_path / "junit.xml"
    env = {**os.environ, "PYTHONPATH": str(Path(hosted_ci.__file__).parent)}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "hosted_ci", "-p", "no:cacheprovider",
               "--confcutdir=" + str(tmp_path), str(source), "-n", workers, "-m", "not omit",
               "-o", "markers=omit: synthetic deselection", "--junitxml=" + str(report),
               "--hosted-selection-output=" + str(report.with_suffix(".selection.json")),
               "--hosted-deselected-output=" + str(report.with_suffix(".deselected.json"))]
    done = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(report.with_suffix(".selection.json").read_text()) == ["test_sample.py::test_selected"]
    assert json.loads(report.with_suffix(".deselected.json").read_text()) == ["test_sample.py::test_omitted"]
