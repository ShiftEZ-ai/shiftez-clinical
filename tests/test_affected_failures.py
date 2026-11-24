import json
import subprocess
from unittest.mock import Mock

from scripts import affected_tests as selector


def test_selection_without_failure_reports_is_unchanged():
    picked = {"unit": {"tests/test_affected_failures.py"}, "browser": set()}
    assert selector.include_failures(picked, []) == picked


def test_failed_node_from_a_report_joins_the_selection(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_affected_failures" name="test_selection_without_failure_reports_is_unchanged">'
        '<failure message="x"/></testcase>'
        '<testcase classname="tests.test_affected_failures" name="test_failed_node_from_a_report_joins_the_selection"/>'
        '</testsuite></testsuites>', encoding="utf-8")
    picked = selector.include_failures({"unit": set(), "browser": set()}, [report])
    assert picked == {"unit": {"tests/test_affected_failures.py::test_selection_without_failure_reports_is_unchanged"},
                      "browser": set()}


def _repo(path, *branches):
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                                    cwd=path, check=True, capture_output=True)
    git("init", "-q", "-b", "work")
    (path / "a.txt").write_text("a", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "a")
    for branch in branches:
        git("branch", branch)
    return path


def test_local_run_selects_the_test_in_a_small_diff(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    tests = root / "tests"
    tests.mkdir()
    changed = "tests/test_changed.py"
    (root / changed).write_text("def test_changed():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", changed], cwd=root, check=True, capture_output=True)

    monkeypatch.setattr(selector, "ROOT", root)
    monkeypatch.setattr(selector, "TESTS", tests)
    monkeypatch.setattr(selector, "SMOKE", {"unit": [], "browser": []})
    run_pytest = Mock(return_value=0)
    monkeypatch.setattr(selector.subprocess, "call", run_pytest)

    assert selector.main(["--base", "HEAD", "--run", "-n", "0"]) == 0
    run_pytest.assert_called_once()
    assert changed in run_pytest.call_args.args[0]


def test_diagnostic_selection_accepts_a_public_test_node():
    node = "tests/test_affected_failures.py::test_selection_without_failure_reports_is_unchanged"
    plan = selector.diagnostic_ci(json.dumps({"unit": [node]}), "0" * 40, env={"GITHUB_WORKFLOW_REF": "workflow"})
    assert plan["unit_tests"] == node
    assert json.loads(plan["browser_matrix"]) == []


def test_missing_origin_dev_falls_back_to_local_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(selector, "ROOT", _repo(tmp_path, "dev"))
    assert selector.resolve_base("origin/dev") == "dev"
    assert selector.changed_files("origin/dev") == []


def test_missing_origin_dev_falls_back_to_main_after_the_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(selector, "ROOT", _repo(tmp_path, "main"))
    assert selector.resolve_base("origin/dev") == "main"
    assert selector.changed_files("origin/dev") == []


def test_missing_base_stops_with_one_clear_line(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(selector, "ROOT", _repo(tmp_path))
    with pytest.raises(SystemExit, match="Base origin/dev not found in this clone"):
        selector.changed_files("origin/dev")
    with pytest.raises(SystemExit, match="Base other not found"):
        selector.resolve_base("other")
