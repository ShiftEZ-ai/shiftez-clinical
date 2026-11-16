"""Exercise the PyPI audit deadline and retry policy without the live service."""
import subprocess
import sys

import pytest

from scripts import ci_pip_audit as audit


@pytest.mark.parametrize("transport", [False, True])
def test_persistent_timeout_fails_closed_after_one_retry(monkeypatch, capsys, transport):
    calls, delays = [], []
    clock = [0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])

    def backoff(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(audit.time, "sleep", backoff)

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        if transport:
            return subprocess.CompletedProcess(command, 1, "", "requests.exceptions.ReadTimeout: synthetic\n")
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit(["pip-audit"]) == 1
    assert calls == ([89, 178] if transport else [89, 89]) and delays == [2]
    output = capsys.readouterr().err
    assert "::error::PyPI audit timeout" in output
    assert "30s" in output and "180s" in output


@pytest.mark.parametrize("duration", [31.4, 33.8, 51])
def test_clean_audit_longer_than_socket_timeout_passes(monkeypatch, capsys, duration):
    clock, limits = [0], []
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(audit.time, "sleep", lambda _: pytest.fail("Clean audits must not retry"))

    def run(command, **kwargs):
        limits.append(kwargs["timeout"])
        clock[0] += duration
        if duration >= kwargs["timeout"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "No known vulnerabilities found\n", "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit(["pip-audit", "--timeout", "30"]) == 0
    assert limits == [89]
    output = capsys.readouterr()
    assert output.out == "No known vulnerabilities found\n" and output.err == ""


@pytest.mark.parametrize("exception", ["ReadTimeout", "ConnectTimeout", "Timeout", None])
def test_timeout_then_complete_clean_audit_passes(monkeypatch, capsys, exception):
    calls = []
    monkeypatch.setattr(audit.time, "sleep", lambda _: None)

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            if exception is None:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 1, "", f"requests.exceptions.{exception}: synthetic\n")
        return subprocess.CompletedProcess(command, 0, "No known vulnerabilities found\n", "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit(["pip-audit", "--strict"]) == 0
    assert calls == [["pip-audit", "--strict"]] * 2
    output = capsys.readouterr()
    assert "No known vulnerabilities found" in output.out and "::error::" not in output.err


@pytest.mark.parametrize("stderr", ["Found 1 known vulnerability in 1 package\n", "invalid requirements\n",
                                   "package named ReadTimeout\n"])
def test_non_timeout_failures_do_not_retry(monkeypatch, capsys, stderr):
    calls = []
    monkeypatch.setattr(audit.time, "sleep", lambda _: pytest.fail("Audit verdicts must not retry"))

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", stderr)

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit(["pip-audit"]) == 1
    assert len(calls) == 1 and capsys.readouterr().err == stderr


@pytest.mark.parametrize("clean", [False, True])
def test_total_deadline_rejects_late_results_and_clips_remaining_attempt(monkeypatch, capsys, clean):
    clock, limits = [0], []
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(audit.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def run(command, **kwargs):
        limits.append(kwargs["timeout"])
        clock[0] = 180 if len(limits) == 2 else 170
        if len(limits) == 2 and clean:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit(["pip-audit"]) == 1
    assert limits == [89, 8]
    assert "::error::PyPI audit timeout" in capsys.readouterr().err


def test_real_child_timeout_is_killed_and_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(audit, "TOTAL_TIMEOUT", 0.2)
    monkeypatch.setattr(audit, "RETRY_DELAY", 0)
    monkeypatch.setattr(audit.time, "sleep", lambda _: None)
    assert audit.audit([sys.executable, "-c", "import threading; threading.Event().wait()"]) == 1
    assert "::error::PyPI audit timeout" in capsys.readouterr().err


@pytest.mark.parametrize("encoded", [False, True])
def test_killed_attempt_prints_sanitized_partial_package_context(monkeypatch, capsys, encoded):
    monkeypatch.setenv("GH_TOKEN", "synthetic-secret-one")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-secret-two")
    monkeypatch.setattr(audit.time, "sleep", lambda _: None)
    partial = (('Chec' + 'king' + ' req' + 'uest' + 's==2' + '.32.' + '5\n::' + 'erro' + 'r::i' + 'njec' + 'ted\x1b' + '\x00\nsy' + 'nthe' + 'tic-' + 'secr' + 'et-o' + 'ne s' + 'ynth' + 'etic' + '-sec' + 'ret-' + 'two ' + 'ghp_' + 'synt' + 'heti' + 'cval' + 'ue g' + 'ithu' + 'b_pa' + 't_sy' + 'nthe' + 'ticv' + 'alue' + '\nhtt' + 'ps:/' + '/use' + 'r:pa' + 'sswo' + 'rd@e' + 'xamp' + 'le.i' + 'nval' + 'id/?' + 'toke' + 'n=hi' + 'dden' + '\n'))
    value = partial.encode() if encoded else partial

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=value, stderr=value)

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit(["pip-audit"]) == 1
    output = capsys.readouterr()
    for stream in (output.out, output.err):
        assert "Checking requests==2.32.5" in stream
        assert all(secret not in stream for secret in ("synthetic-secret-one", "synthetic-secret-two",
                   ('ghp_' + 'synt' + 'heti' + 'cval' + 'ue'), ('gith' + 'ub_p' + 'at_s' + 'ynth' + 'etic' + 'valu' + 'e'), ('user' + ':pas' + 'swor' + 'd'), ('toke' + 'n=hi' + 'dden')))
        assert "\x1b" not in stream and "\x00" not in stream and "\n::error::injected" not in stream
    assert "::error::PyPI audit timeout" in output.err


@pytest.mark.parametrize("code", [0, 1])
def test_real_child_verdict_is_preserved(code):
    assert audit.audit([sys.executable, "-c", f"raise SystemExit({code})"]) == code
