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
