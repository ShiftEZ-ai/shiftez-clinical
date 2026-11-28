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
import xml.etree.ElementTree as ET

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


def test_shard_reuses_only_the_bound_checks_plan(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(hosted_ci.__file__).parent))
    import trusted_ci

    base, head, tree = "a" * 40, "b" * 40, "c" * 40
    inventory = ["tests/test_example.py"]
    candidate = {"sha": head, "tree": tree, "base_sha": base, "allowance_date": "2026-01-01",
                 "inventory_sha256": hosted_ci.digest(inventory)}
    monkeypatch.setenv("CI_ALLOWANCE_DATE", candidate["allowance_date"])
    artifact = tmp_path / "shard-plan.json"
    artifact.write_text(json.dumps(candidate), encoding="utf-8")
    monkeypatch.setenv("CI_CHECKS_PLAN", str(artifact))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("TRUSTED_BOOTSTRAP", raising=False)
    monkeypatch.setattr(trusted_ci.hosted, "git", lambda *args: f"{head}\n{tree}")
    event = {"repository": {"default_branch": "main"}, "pull_request": {"base": {"ref": "main"}}}
    monkeypatch.setattr(trusted_ci.policy, "event", lambda: event)
    monkeypatch.setattr(trusted_ci, "test_inventory", lambda: inventory)
    verified = []

    def resolve_parent(data, sha):
        verified.append((data, sha))
        return base

    monkeypatch.setattr(trusted_ci.policy, "pr_test_base", resolve_parent)
    rebuilt = object()
    monkeypatch.setattr(trusted_ci, "plan", lambda base, measurement: rebuilt)

    seal = hosted_ci.digest(candidate)
    run = trusted_ci.runner_plan
    assert run(base, False, "browser", 1, seal) == candidate
    assert verified == [(event, head)]
    assert run(base, False, "unit", 1, seal) is rebuilt
    assert run(base, True, "browser", 1, seal) is rebuilt
    with monkeypatch.context() as context:
        context.setattr(trusted_ci.policy, "pr_test_base", lambda data, sha: "d" * 40)
        with pytest.raises(ValueError, match="verified target parent"):
            run(base, False, "browser", 1, seal)

    with pytest.raises(ValueError, match="plan identity"):
        run(base, False, "browser", 1, "0" * 64)
    for field in ("sha", "tree", "base_sha", "inventory_sha256"):
        stale = dict(candidate, **{field: "x"})
        artifact.write_text(json.dumps(stale), encoding="utf-8")
        with pytest.raises(ValueError, match="Checks plan differs"):
            run(base, False, "browser", 1, hosted_ci.digest(stale))
    artifact.write_text(json.dumps(candidate), encoding="utf-8")
    with monkeypatch.context() as context:
        context.setenv("CI_ALLOWANCE_DATE", "2026-01-02")
        with pytest.raises(ValueError, match="allowance date differs from Checks"):
            run(base, False, "browser", 1, seal)
    artifact.unlink()
    with pytest.raises(FileNotFoundError):
        run(base, False, "browser", 1, seal)

    with monkeypatch.context() as context:
        context.setenv("TRUSTED_BOOTSTRAP", "true")
        assert run(base, False, "browser", 1, seal) is rebuilt
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    assert run(base, False, "browser", 1, seal) is rebuilt
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    with monkeypatch.context() as context:
        context.setattr(trusted_ci.policy, "event", lambda: {
            "repository": {"default_branch": "dev"}, "pull_request": {"base": {"ref": "preview"}},
        })
        assert run(base, False, "browser", 1, seal) is rebuilt
    monkeypatch.delenv("CI_CHECKS_PLAN")
    assert run(base, False, "browser", 1, seal) is rebuilt


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
    candidate = {"replicas": [1], "smoke": False}
    evidence = run(["tests/test_x.py"], "unit", report, [], "0", None, candidate=candidate, listed=listed)
    original = trusted_ci.canaries.report_cases(report, evidence)
    assert evidence["exit_code"] == evidence["retry"]["exit_code"] == 1
    assert trusted_ci.canaries.failed_nodes(original) == [node, "tests/test_x.py::test_always_red", listed_node]
    failures = maker.retry_nodes(original, listed)
    assert failures == [node, "tests/test_x.py::test_always_red"]
    assert trusted_ci.canaries.recovered_nodes(report, evidence, failures, allow_retry=True) == {node}
    assert sorted(path.name for path in tmp_path.glob("*.raw.xml")) == ["junit-retry.raw.xml", "junit.raw.xml"]
    listed_only = run([listed_node], "unit", tmp_path / "listed.xml", [], "0", None, candidate=candidate, listed=listed)
    assert listed_only["exit_code"] == 1 and "retry" not in listed_only
    assert not (tmp_path / "listed-retry.xml").exists()
    (tmp_path / "shard-receipt.json").write_text(json.dumps({
        "sha": "a" * 40, "tree": "a" * 40, "run_id": "fixture", "run_attempt": "1", "reports": [evidence],
    }))
    trusted_ci.canaries.verify_directory(tmp_path, allow_retry=True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.delenv("GITHUB_REF", raising=False)
    single = run(["tests/test_x.py"], "unit", tmp_path / "push.xml", [], "0", None, candidate=candidate)
    assert single["exit_code"] == 1 and "retry" not in single
    assert not (tmp_path / "push-retry.xml").exists()


def test_browser_retry_enables_tracing_for_failed_nodes(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(hosted_ci.__file__).parent))
    import trusted_ci

    path = "tests/test_browser_example.py"
    failed_node = path + "::test_red"
    calls = []

    def execution(command, *, env):
        focused = failed_node in command
        calls.append((focused, env["PW_TRACE_MODE"]))
        report = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        case = ET.Element("testcase", classname="tests.test_browser_example", name="test_red")
        if not focused:
            ET.SubElement(case, "failure")
        ET.ElementTree(case).write(report)
        Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--hosted-deselected-output="))).write_text("[]", encoding="utf-8")
        return int(not focused)

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    monkeypatch.setenv("PW_TRACE_MODE", "off")
    monkeypatch.setattr(trusted_ci.subprocess, "call", execution)
    monkeypatch.setattr(trusted_ci.hardening, "load", lambda: {"canaries": False, "node_census": False})
    evidence = trusted_ci.invocation_receipt(
        [path], "browser", tmp_path / "junit.xml", [], "4", None, candidate={"replicas": [1]},
    )

    assert evidence["exit_code"] == 1 and evidence["retry"]["exit_code"] == 0
    assert calls == [(False, "off"), (True, "on")]
    assert os.environ["PW_TRACE_MODE"] == "off"


@pytest.mark.parametrize("replicas,smoke,retry_expected,branch,event,retired,default", [
    pytest.param([1], False, True, "dev", "pull_request", False, "dev", id="replicas0-False-True"),
    pytest.param([1], True, False, "dev", "pull_request", False, "dev", id="replicas1-True-False"),
    pytest.param([1, 2], False, False, "dev", "pull_request", False, "dev", id="replicas2-False-False"),
    pytest.param([1], False, True, "dev", "pull_request", True, "dev", id="pr-retirement"),
    pytest.param([1], False, True, "dev", "merge_group", True, "dev", id="group-retirement"),
    pytest.param([1], False, True, "main", "merge_group", True, "dev", id="release-uses-accepted-dev"),
    # After the rename main is the default, so its merge groups are integration and read the candidate.
    pytest.param([1], False, True, "main", "merge_group", True, "main", id="renamed-main-group-retirement"),
    pytest.param([1], False, True, "main", "push", False, "main", id="default-branch-push"),
    pytest.param([1], False, False, "main", "push", False, "dev", id="non-default-push"),
])
def test_pr_runner_retries_only_unlisted_nodes_on_an_eligible_plan(
        tmp_path, monkeypatch, replicas, smoke, retry_expected, branch, event, retired, default):
    monkeypatch.syspath_prepend(str(Path(hosted_ci.__file__).parent))
    import trusted_ci

    path = "tests/test_x.py"
    red = path + "::test_red[0]"
    listed = path + "::test_listed"
    quarantined = path + "::test_quarantined"
    new_flake = path + "::test_new_flake"
    candidate = {"sha": "a" * 40, "tree": "b" * 40, "base_sha": "c" * 40, "replicas": replicas, "smoke": smoke,
                 "suites": {"unit": {"shards": [{"files": [path]}]}}}
    calls = []

    def invocation(targets, suite, report, extra, workers, base_targets, *, partial, trace=False):
        calls.append((targets, workers, trace))
        nodes = [red, listed, quarantined, new_flake] if targets == [path] else targets
        cases = ET.Element("testsuite")
        for node in nodes:
            case = ET.SubElement(cases, "testcase", classname="tests.test_x", name=node.split("::")[1])
            if targets == [path]:
                ET.SubElement(case, "failure")
        ET.ElementTree(cases).write(report)
        report.with_suffix(".selection.json").write_text(json.dumps(nodes), encoding="utf-8")
        report.with_suffix(".deselected.json").write_text("[]", encoding="utf-8")
        return int(targets == [path])

    known = {"schema": 2, "tree": "c" * 40, "root_evidence_sha256": "d" * 64, "cleanup_cap": 0,
             "root_runs": [{"run_id": "1", "run_attempt": "1", "replica": i} for i in (1, 2)],
             "fails": [{"node": listed, "class": "baseline"}], "unstable": []}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/" + branch)
    payload = {"repository": {"default_branch": default}}
    if event == "pull_request":
        payload[event] = {"base": {"ref": branch}}
    elif event == "merge_group":
        payload[event] = {"base_ref": "refs/heads/" + branch}
    monkeypatch.setattr(trusted_ci.policy, "event", lambda: payload)
    release = branch == "main" and default != "main"
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("CI_UNIT_WORKERS", "4")
    monkeypatch.setattr(sys, "argv", ["trusted_ci.py", "run", "--base-sha", "c" * 40,
                                     "--suite", "unit", "--shard", "1", "--plan-sha256", hosted_ci.digest(candidate)])
    monkeypatch.setattr(trusted_ci, "plan", lambda *_: candidate)
    monkeypatch.setattr(trusted_ci, "invocation", invocation)
    monkeypatch.setattr(trusted_ci.hardening, "load", lambda: {"node_census": False})
    def read_file(sha, name):
        assert sha in {candidate["sha"], candidate["base_sha"]}
        if name == trusted_ci.policy.KNOWN:
            return json.dumps(known).encode()
        if name == trusted_ci.policy.QUARANTINE_RETIRED:
            if release:
                assert sha == candidate["base_sha"]
            return json.dumps([quarantined] if retired and sha == candidate["sha"] else []).encode()
        assert name == "tests/quarantine.json"
        row = {"nodeid": quarantined, "reason": "baseline: reviewed", "owner": "maintainers",
               "added": "2000-01-01", "expires": "2000-01-02", "issue": 123, "blocker": False}
        flake = {**row, "nodeid": new_flake, "reason": "unstable: recovered", "recovered_runs": ["1"]}
        return json.dumps([row, flake] if sha == candidate["sha"] else [row]).encode()

    monkeypatch.setattr(trusted_ci.policy, "read_file", read_file)
    trusted_ci.main()
    receipt = json.loads((tmp_path / ".run/reports/shard-receipt.json").read_text(encoding="utf-8"))
    assert receipt["reports"][0]["exit_code"] == 1
    assert ("retry" in receipt["reports"][0]) == retry_expected
    retried = [new_flake, quarantined, red] if retired and not release else [new_flake, red]
    assert calls == [([path], "4", False)] + ([(retried, "0", False)] if retry_expected else [])
    assert (tmp_path / ".run/reports/junit-retry.xml").exists() == retry_expected


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
    evidence = trusted_ci.invocation_receipt(
        [path], "unit", report, [], "0", None, candidate={"replicas": [1]}, partial=True)
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
