from pathlib import Path
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
import trusted_ci


@pytest.mark.parametrize("workers", ["0", "2"])
def test_timeout_records_failure_and_keeps_running(tmp_path, monkeypatch, workers):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "test_probe.py").write_text(
        "from threading import Event\n"
        "def test_hang():\n"
        "    Event().wait(8)\n"
        "def test_next():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(trusted_ci.hardening, "load", lambda: {"canaries": False, "node_census": False})
    report = tmp_path / "junit.xml"
    limits = ["--timeout=600", "--timeout-method=signal", "-o", "faulthandler_timeout=540"]
    if not hasattr(signal, "SIGALRM"):
        # Windows verifies the trusted argv. The hosted Linux runner exercises SIGALRM.
        class InvocationCaptured(Exception):
            pass

        def capture(command, **kwargs):
            start = command.index(limits[0])
            assert command[start:start + len(limits)] == limits
            assert command[command.index("-n") + 1] == workers
            raise InvocationCaptured

        monkeypatch.setattr(subprocess, "call", capture)
        with pytest.raises(InvocationCaptured):
            trusted_ci.invocation(["test_probe.py"], "unit", report, [], workers)
        return

    completed = trusted_ci.invocation(["test_probe.py"], "unit", report, ["--timeout=1"], workers)
    assert completed == 1
    cases = {row.get("name"): row for row in ET.parse(report).iter("testcase")}
    assert set(cases) == {"test_hang", "test_next"}
    failure = cases["test_hang"].find("failure")
    assert failure is not None and "Timeout" in failure.get("message", "")
    assert trusted_ci.canaries.outcome(cases["test_next"]) == "passed"
