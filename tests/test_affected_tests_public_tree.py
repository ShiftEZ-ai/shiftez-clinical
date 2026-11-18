import json
from pathlib import Path

from scripts import affected_tests


def test_public_tree_selects_changed_test_without_private_imports(capsys, tmp_path):
    changed = Path(__file__).resolve().relative_to(affected_tests.ROOT).as_posix()
    affected_tests.main(["--fast-ci", changed])
    fast = capsys.readouterr()
    outputs = dict(line.split("=", 1) for line in fast.out.splitlines())
    assert changed in json.loads(outputs["selection_inputs"])["selected"]["unit"]

    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.test_affected_tests_public_tree" '
        'name="test_public_tree_selects_changed_test_without_private_imports">'
        '<failure/></testcase></testsuite>',
        encoding="utf-8",
    )
    assert affected_tests.main(["--files", changed, "--failed-report", str(report)]) == 0
    local = capsys.readouterr()
    assert changed in local.out.split()
    assert "ModuleNotFoundError" not in fast.err + local.err
