"""Failed-node retry and reviewed quarantine contracts, without databases or browsers."""
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.quarantine import entries


@pytest.fixture
def classified_script(tmp_path):
    root = tmp_path / "wrapper"
    script = root / ".github/scripts/pytest_ci.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / ".github/scripts/pytest_ci.py", script)
    (root / "tests").mkdir()
    today = datetime.now(timezone.utc).date()  # real clock: synthetic reviewed entries must be current
    (root / "tests/quarantine.json").write_text(json.dumps([
        {**entry("tests/test_sample.py::" + name), "added": today.isoformat(),
         "expires": (today + timedelta(days=1)).isoformat()} for name in ("test_retry", "test_flaky")]))
    return script


@pytest.fixture
def wrapper(tmp_path, monkeypatch, classified_script):
    spec = importlib.util.spec_from_file_location("l1_pytest_ci", classified_script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/dev")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"default_branch": "dev"}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    return module


def junit(path, outcome="", properties=""):
    path.write_text('<testsuite><testcase classname="tests.test_sample" name="test_retry">'
                    + properties + outcome + '</testcase></testsuite>', encoding="utf-8")


RETRY_EVENTS = [
    ("pull_request", "refs/pull/1/merge", True), ("push", "refs/heads/dev", True),
    ("push", "refs/heads/main", False), ("push", "refs/heads/feature", False),
    ("merge_group", "refs/heads/gh-readonly-queue/dev/group", False),
    ("schedule", "refs/heads/dev", False), ("workflow_dispatch", "refs/heads/dev", False),
    ("workflow_call", "refs/heads/dev", False)]


@pytest.mark.parametrize("event,ref,allowed", RETRY_EVENTS)
@pytest.mark.parametrize("partitioned", [False, True])
def test_retry_only_eligible_events_preserves_first_evidence(wrapper, monkeypatch, tmp_path, event, ref, allowed, partitioned):
    report = tmp_path / "junit-unit.xml"
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setenv("GITHUB_JOB", "unit")
    monkeypatch.delenv("FMUC_CLOUD_TEST_REPORTS", raising=False)
    monkeypatch.delenv("CI_CASE_MANIFEST", raising=False)
    if partitioned:
        monkeypatch.setenv("CI_CASE_MANIFEST", str(tmp_path / "manifest.json"))
        monkeypatch.setenv("SHARD", "2/4")
        monkeypatch.setenv("CI_CASE_SUITE", "unit")
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        path = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")))
        if partitioned:
            assert command[command.index("--ci-case-manifest") + 1] == str(tmp_path / "manifest.json")
            assert command[command.index("--ci-case-shard") + 1] == "2"
            assert not any(key.startswith("CI_CASE_") for key in kwargs["env"])
            selected = Path(command[command.index("--ci-case-output") + 1])
            selected.write_text(json.dumps(["initial" if len(calls) == 1 else "retry"]))
        if len(calls) == 1:
            junit(path, "<failure/>")
            return 1
        assert "--last-failed" in command and "--cov-append" in command
        assert str(tmp_path / "first-retry") in " ".join(command)
        assert json.loads(Path(next(a.split("=", 1)[1] for a in command if a.startswith("cache_dir="))).joinpath("v/cache/lastfailed").read_text()) == {"tests/test_sample.py::test_retry": True}
        junit(path)
        return 0
    monkeypatch.setattr(wrapper.subprocess, "call", execute)
    retries = allowed and not partitioned
    assert wrapper.run_tests(["tests", "--cov=packages/domain", "--basetemp=" + str(tmp_path / "first"),
                              "--junitxml=" + str(report)]) == (0 if retries else 1)
    assert len(calls) == (2 if retries else 1) and "<failure" in report.read_text()
    assert json.loads(report.with_name("junit-unit-flaky.json").read_text())["tests"] == (
        ["tests/test_sample.py::test_retry"] if retries else [])
    assert report.with_name("junit-unit-retry.xml").exists() == retries
    if partitioned:
        assert json.loads(report.with_suffix(".selection.json").read_text()) == ["initial"]
        assert not report.with_suffix(".selection.retry.json").exists()


@pytest.mark.parametrize("classification", ["unclassified", "expired", "future", "malformed", "missing"])
def test_retry_requires_active_reviewed_classification(wrapper, monkeypatch, tmp_path, classification):
    registry = Path(wrapper.__file__).parents[2] / "tests/quarantine.json"
    rows = json.loads(registry.read_text())
    if classification == "unclassified":
        rows = []
    elif classification == "expired":
        rows[0].update(added="2000-01-01", expires="2000-01-02")
    elif classification == "future":
        rows[0].update(added="9998-01-01", expires="9999-01-01")
    elif classification == "malformed":
        rows[0]["owner"] = ""
    if classification == "missing":
        registry.unlink()
    else:
        registry.write_text(json.dumps(rows))
    report = tmp_path / "junit-unit.xml"
    monkeypatch.delenv("CI_CASE_MANIFEST", raising=False)
    monkeypatch.delenv("FMUC_CLOUD_TEST_REPORTS", raising=False)
    monkeypatch.setenv("GITHUB_JOB", "unit")
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        junit(report, "<failure/>")
        return 1
    monkeypatch.setattr(wrapper.subprocess, "call", execute)
    assert wrapper.run_tests(["tests", "--junitxml=" + str(report)]) == 1
    assert len(calls) == 1 and "<failure" in report.read_text()
    assert not report.with_name("junit-unit-retry.xml").exists()
    assert json.loads(report.with_name("junit-unit-flaky.json").read_text()) == {"tests": [], "quarantined": []}
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    wrapper.summary(tmp_path)
    assert json.loads((tmp_path / "flaky.json").read_text())["tests"] == []


@pytest.mark.parametrize("outcome,code", [("<skipped/>", 0), ("<failure/>", 1), (None, 0), ("", 2), ("", 5), ("", -2)])
def test_retry_missing_skipped_or_bad_exit_fails(wrapper, monkeypatch, tmp_path, outcome, code):
    report = tmp_path / "junit-unit.xml"
    monkeypatch.delenv("CI_CASE_MANIFEST", raising=False)
    monkeypatch.delenv("FMUC_CLOUD_TEST_REPORTS", raising=False)
    monkeypatch.setenv("GITHUB_JOB", "unit")
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            junit(report, "<failure/>")
            return 1
        if outcome is not None:
            junit(report.with_name("junit-unit-retry.xml"), outcome)
        return code
    # A stale success must not substitute for a missing rerun result.
    junit(report.with_name("junit-unit-retry.xml"))
    monkeypatch.setattr(wrapper.subprocess, "call", execute)
    assert wrapper.run_tests(["tests", "--junitxml=" + str(report)]) != 0
    assert len(calls) == 2 and "<failure" in report.read_text()


@pytest.mark.parametrize("job,properties", [("cpu-budget", ""), ("solver-cap", ""), ("unit", '<properties><property name="fmuc_no_retry" value="cpu_budget"/></properties>')])
def test_cpu_budgets_never_retry(wrapper, monkeypatch, tmp_path, job, properties):
    report = tmp_path / "junit-unit.xml"
    monkeypatch.delenv("CI_CASE_MANIFEST", raising=False)
    monkeypatch.delenv("FMUC_CLOUD_TEST_REPORTS", raising=False)
    monkeypatch.setenv("GITHUB_JOB", job)
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        junit(report, "<failure/>", properties)
        return 1
    monkeypatch.setattr(wrapper.subprocess, "call", execute)
    assert wrapper.run_tests(["tests", "--junitxml=" + str(report)]) == 1
    assert len(calls) == 1


def entry(node="tests/test_sample.py::test_fail"):
    return dict(nodeid=node, reason="#525: recovered on retry 3 times", owner="maintainer", added="2026-11-01", expires="2026-11-15",
                issue=525, blocker=False)


@pytest.mark.parametrize("rows,today", [([entry()], date(2026, 11, 15)), ([entry()], date(2026, 10, 31)),
    ([entry(), entry()], date(2026, 11, 1)), ([{**entry(), "owner": ""}], date(2026, 11, 1)),
    ([{**entry(), "expires": "bad-date"}], date(2026, 11, 1)), ({}, date(2026, 11, 1)),
    ([{k: v for k, v in entry().items() if k != "issue"}], date(2026, 11, 1)), ([{**entry(), "issue": True}], date(2026, 11, 1)),
    ([{**entry(), "issue": 0}], date(2026, 11, 1)), ([{**entry(), "recovered_runs": "123"}], date(2026, 11, 1)),
    ([{**entry(), "recovered_runs": []}], date(2026, 11, 1)), ([{**entry(), "blocker": "no"}], date(2026, 11, 1)),
    ([{k: v for k, v in entry().items() if k != "blocker"}], date(2026, 11, 1)), ([{**entry(), "blocker": None}], date(2026, 11, 1)),
    ([{**entry(), "milestone": ""}], date(2026, 11, 1)), ([{**entry(), "unknown": 1}], date(2026, 11, 1))])
def test_quarantine_invalid_duplicate_and_expired_entries_fail(tmp_path, rows, today):
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps(rows))
    if today == date(2026, 11, 15):
        assert entries(path, today) == {rows[0]["nodeid"]: rows[0]}
    else:
        with pytest.raises(ValueError):
            entries(path, today)


def test_quarantine_accepts_the_optional_review_fields(tmp_path):
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps([{**entry(), "recovered_runs": ["123"], "milestone": "v0.2.0", "blocker": True}]))
    assert set(entries(path, date(2026, 11, 1))) == {entry()["nodeid"]}


def test_a_malformed_quarantine_stops_the_session_instead_of_emptying_it(tmp_path, monkeypatch):
    import tests.quarantine as plugin

    bad = tmp_path / "quarantine.json"
    bad.write_text(json.dumps([{**entry(), "issue": "525"}]))
    monkeypatch.setattr(plugin, "QUARANTINE_FILE", bad)
    config = type("Config", (), {})()
    with pytest.raises(pytest.UsageError, match="^Invalid quarantine.json: quarantine issue must be an integer"):
        plugin.pytest_configure(config)
    assert not hasattr(config, "_quarantine_entries")


def test_quarantine_runs_failures_and_passes_without_hiding_other_failures(tmp_path, wrapper):
    shutil.copyfile(Path(__file__).with_name("quarantine.py"), tmp_path / "quarantine.py")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text('''import pytest
@pytest.fixture
def bad_setup():
    assert False
@pytest.fixture
def bad_teardown():
    yield
    assert False
def test_fail():
    assert False
def test_pass():
    pass
def test_setup(bad_setup):
    pass
def test_teardown(bad_teardown):
    pass
def test_blocking():
    assert False
''')
    today = datetime.now(timezone.utc).date()  # real clock: this subprocess fixture is relative to quarantine expiry
    rows = [{**entry("tests/test_sample.py::test_" + name), "added": today.isoformat(), "expires": (today + timedelta(days=1)).isoformat()}
            for name in ("fail", "pass", "setup", "teardown")]
    (tmp_path / "quarantine.json").write_text(json.dumps(rows))
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = cpu_budget\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CI_CASE_", "PYTEST_")) and k != "FMUC_CLOUD_TEST_REPORTS"}
    env["PYTHONPATH"] = str(Path(__file__).parents[1])
    command = [sys.executable, "-m", "pytest", "-p", "quarantine", "-c", "pytest.ini", "-q", "tests", "-n", "2", "--basetemp=" + str(tmp_path / "nested")]
    result = subprocess.run([*command, "-k", "not blocking", "--junitxml=quarantined.xml"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUARANTINED" in result.stdout and "xpassed" in result.stdout
    properties = wrapper.case_properties(tmp_path / "quarantined.xml", "fmuc_quarantine")
    assert set(properties) == {row["nodeid"] for row in rows}
    result = subprocess.run([*command, "-k", "blocking"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout + result.stderr


def test_summary_reports_flaky_and_quarantine_for_upload(wrapper, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    observation = {**entry(), "outcome": "skipped"}
    (tmp_path / "junit-unit-flaky.json").write_text(json.dumps({"tests": ["tests/test_sample.py::test_retry"], "quarantined": [observation]}))
    junit(tmp_path / "junit-unit.xml", '<skipped/>', '<properties><property name="fmuc_quarantine" value="ignored"/></properties>')
    wrapper.summary(tmp_path)
    assert "FLAKY `tests/test_sample.py::test_retry`" in summary.read_text()
    assert "expires 2026-11-15" in summary.read_text()
    report = json.loads((tmp_path / "flaky.json").read_text())
    assert report["tests"] == ["tests/test_sample.py::test_retry"]
    assert report["quarantined"] == [observation]


def test_retry_cannot_clear_a_cpu_failure_in_a_mixed_shard(wrapper, monkeypatch, tmp_path):
    report = tmp_path / "junit-unit.xml"
    monkeypatch.setenv("GITHUB_JOB", "unit")
    monkeypatch.delenv("CI_CASE_MANIFEST", raising=False)
    monkeypatch.delenv("FMUC_CLOUD_TEST_REPORTS", raising=False)
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            report.write_text('<testsuite><testcase classname="tests.test_sample" name="test_retry"><failure/></testcase>'
                '<testcase classname="tests.test_sample" name="test_cpu"><properties><property name="fmuc_no_retry" value="cpu_budget"/></properties><failure/></testcase></testsuite>')
            return 1
        failed = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("cache_dir="))) / "v/cache/lastfailed"
        assert json.loads(failed.read_text()) == {"tests/test_sample.py::test_retry": True}
        junit(report.with_name("junit-unit-retry.xml"))
        return 0
    monkeypatch.setattr(wrapper.subprocess, "call", execute)
    assert wrapper.run_tests(["tests", "--junitxml=" + str(report)]) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("include_cpu,workers", [(False, 0), (True, 2)])
def test_real_pytest_reruns_only_classified_failed_non_budget_nodes(tmp_path, classified_script, include_cpu, workers):
    shutil.copyfile(Path(__file__).with_name("quarantine.py"), tmp_path / "quarantine.py")
    (tmp_path / "quarantine.json").write_text("[]")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = cpu_budget\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text('''from pathlib import Path
import pytest
def record(name):
    path = Path(name)
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count
def test_flaky():
    assert record('flaky') == 2
def test_green():
    assert record('green') == 1
@pytest.mark.cpu_budget
def test_cpu():
    assert record('cpu') == 2
''')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CI_CASE_", "PYTEST_")) and k != "FMUC_CLOUD_TEST_REPORTS"}
    env.update(GITHUB_JOB="unit", GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/1/merge",
               TMP=str(tmp_path), TEMP=str(tmp_path), PYTHONPATH=str(Path(__file__).parents[1]))
    script = classified_script
    result = subprocess.run([sys.executable, str(script), "run", "-c", "pytest.ini", "-p", "quarantine", "tests/test_sample.py",
        "-n", str(workers), "-m", "" if include_cpu else "not cpu_budget", "--basetemp=" + str(tmp_path / "nested"), "--junitxml=junit-unit.xml"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == int(include_cpu), result.stdout + result.stderr
    assert (tmp_path / "flaky").read_text() == "2" and (tmp_path / "green").read_text() == "1"
    if include_cpu:
        assert (tmp_path / "cpu").read_text() == "1"
    assert json.loads((tmp_path / "junit-unit-flaky.json").read_text())["tests"] == ["tests/test_sample.py::test_flaky"]


def test_expired_quarantine_does_not_abort_unrelated_pytest_sessions(tmp_path):
    shutil.copyfile(Path(__file__).with_name('quarantine.py'), tmp_path / 'quarantine.py')
    tests = tmp_path / 'tests'
    tests.mkdir()
    stale = {**entry(), 'added': '2000-01-01', 'expires': '2000-01-02'}
    (tmp_path / 'quarantine.json').write_text(json.dumps([stale]))
    (tmp_path / 'conftest.py').write_text('pytest_plugins = ["quarantine"]')
    (tests / 'test_sample.py').write_text('def test_unrelated(): pass\ndef test_fail(): assert False\n')
    validity = tests / 'test_validity.py'
    validity.write_text("from pathlib import Path\nfrom datetime import date\nfrom quarantine import entries\ndef test_validity():\n    registry = entries(Path(__file__).parents[1] / 'quarantine.json', date(2026, 10, 3))\n    assert 'tests/test_sample.py::test_fail' in registry\n")
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(tmp_path), str(Path(__file__).parents[1])))}
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(tests)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and '2 passed, 1 xfailed' in result.stdout
    assert 'Expired quarantine (still active)' in result.stdout and 'UsageError' not in result.stdout
    assert "tests/test_sample.py::test_fail: expired 2000-01-02, issue 525" in result.stdout


def test_repeated_wrapper_runs_do_not_duplicate_repo_path(wrapper, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delenv("CI_CASE_MANIFEST", raising=False)
    monkeypatch.delenv("FMUC_CLOUD_TEST_REPORTS", raising=False)
    report = tmp_path / "junit-unit.xml"
    def execute(*args, **kwargs):
        junit(report)
        return 0
    monkeypatch.setattr(wrapper.subprocess, "call", execute)
    root = str(Path(wrapper.__file__).resolve().parents[2])
    for _ in range(2):
        assert wrapper.run_tests(["--junitxml=" + str(report)]) == 0
        assert sys.path.count(root) == 1


def test_real_quarantine_plugin_registered_failure_is_xfail_without_retry(classified_script):
    root = classified_script.parents[2]
    registry = root / "tests/quarantine.json"
    reviewed = [row for row in json.loads(registry.read_text()) if row["nodeid"].endswith("::test_flaky")]
    registry.write_text(json.dumps(reviewed))
    shutil.copyfile(Path(__file__).with_name("quarantine.py"), root / "tests/quarantine.py")
    (root / "tests/__init__.py").write_text("")
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "tests/test_sample.py").write_text('''from pathlib import Path
def test_flaky():
    count = Path('attempts.count')
    count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
    assert False, 'registered ordinary failure'
''')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CI_CASE_", "PYTEST_")) and k != "FMUC_CLOUD_TEST_REPORTS"}
    env.update(GITHUB_JOB="unit", GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/1/merge",
               TMP=str(root), TEMP=str(root), PYTHONPATH=str(Path(__file__).parents[1]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run([sys.executable, str(classified_script), "run", "-c", "pytest.ini", "-p", "tests.quarantine",
                             "-q", "tests/test_sample.py", "--junitxml=junit-unit.xml", "--basetemp=nested"],
                            cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 xfailed" in result.stdout and (root / "attempts.count").read_text() == "1"
    assert not (root / "junit-unit-retry.xml").exists()
    assert not (root / "junit-unit.selection.retry.json").exists()
    observed = json.loads((root / "junit-unit-flaky.json").read_text())
    assert observed == {"tests": [], "quarantined": [{**reviewed[0], "outcome": "skipped"}]}


@pytest.mark.parametrize("default", ["main", None])
@pytest.mark.parametrize("event,ref,allowed", RETRY_EVENTS)
def test_retry_push_eligibility_follows_the_default_branch(wrapper, monkeypatch, tmp_path, default, event, ref, allowed):
    if default is None:  # no trusted default branch: only a pull request stays retry-eligible
        monkeypatch.delenv("GITHUB_EVENT_PATH")
        allowed = allowed and event == "pull_request"
    else:  # the same table with the branch names swapped
        ref = {"refs/heads/dev": "refs/heads/main", "refs/heads/main": "refs/heads/dev"}.get(
            ref, ref.replace("queue/dev/", "queue/main/"))
        (tmp_path / "event.json").write_text(json.dumps({"repository": {"default_branch": default}}), encoding="utf-8")
    test_retry_only_eligible_events_preserves_first_evidence(wrapper, monkeypatch, tmp_path, event, ref, allowed, False)
