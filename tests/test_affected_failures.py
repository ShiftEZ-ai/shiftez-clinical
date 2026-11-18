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
    import subprocess
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a],
                                    cwd=path, check=True, capture_output=True)
    git("init", "-q", "-b", "work")
    (path / "a.txt").write_text("a", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "a")
    for branch in branches:
        git("branch", branch)
    return path


def test_missing_origin_dev_falls_back_to_local_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(selector, "ROOT", _repo(tmp_path, "dev"))
    assert selector.resolve_base("origin/dev") == "dev"
    assert selector.changed_files("origin/dev") == []


def test_missing_base_stops_with_one_clear_line(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(selector, "ROOT", _repo(tmp_path))
    with pytest.raises(SystemExit, match="Base origin/dev not found in this clone"):
        selector.changed_files("origin/dev")
    with pytest.raises(SystemExit, match="Base other not found"):
        selector.resolve_base("other")
