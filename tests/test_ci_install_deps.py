from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALL = "timeout --kill-after=30 600 python -m playwright install-deps chromium"
REPAIR = "sudo dpkg --configure -a || true"


def install_script():
    jobs = yaml.load((ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    steps = [step for job in jobs.values() for step in job["steps"] if "install-deps" in step.get("run", "")]
    assert len(steps) == 1 and "timeout-minutes" not in steps[0]
    return steps[0]["run"]


def test_browser_system_dependencies_are_bounded_and_retried():
    script = install_script()
    assert INSTALL + " && break" in script
    assert "for attempt in 1 2 3; do" in script
    assert 'if [ "$attempt" != 1 ]; then ' + REPAIR + "; fi" in script
    assert 'if [ "$attempt" = 3 ]; then' in script and "exit 1" in script
    assert "Acquire::Retries" in script and "Acquire::http::Timeout" in script


@pytest.mark.parametrize("behaviour, repairs, code", [
    ("exit 0", 0, 0),
    ("[ -f done ] && exit 0; touch done; exit 1", 1, 0),
    ("exit 1", 2, 1),
    ("exec sleep 5", 2, 1),  # a stalled install is killed and still retried after a dpkg repair
])
def test_the_install_loop_retries_and_repairs(tmp_path, behaviour, repairs, code):
    bash, timeout = shutil.which("bash"), shutil.which("timeout")
    assert bash and timeout, "bash and coreutils timeout are on every hosted runner"
    loop = install_script()
    loop = loop[loop.index("for attempt in 1 2 3; do"):]
    stand_in = (loop.replace(INSTALL, f"timeout --kill-after=1 1 bash -c '{behaviour}'")
                .replace(REPAIR, "echo dpkg-repair"))
    run = subprocess.run([bash, "-c", stand_in], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert run.returncode == code, run.stdout + run.stderr
    assert run.stdout.count("dpkg-repair") == repairs, run.stdout
    if code:
        assert "did not install in 3 attempts" in run.stdout


def test_the_shard_evidence_upload_is_retried_once():
    steps = yaml.load((ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]["tests"]["steps"]
    names = [step.get("name") for step in steps]
    first = steps[names.index("Retain first-attempt reports and failure evidence")]
    retry = steps[names.index("Retry the evidence upload once")]
    assert names.index("Retry the evidence upload once") == names.index("Retain first-attempt reports and failure evidence") + 1
    assert first["id"] == "evidence" and first["continue-on-error"] == "true" and first["if"] == "always()"
    assert retry["if"] == "always() && steps.evidence.outcome == 'failure'" and "continue-on-error" not in retry
    assert retry["uses"] == first["uses"]
    assert {**first["with"], "overwrite": "true"} == retry["with"]


def test_pull_request_checks_run_the_pipeline_tests_as_a_merge_group():
    jobs = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    step = next(step for step in jobs["static"]["steps"] if step.get("name") == "Pipeline tests under the merge-group event")
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "GITHUB_EVENT_NAME=merge_group python -m pytest" in step["run"]
    pattern = step["run"].split("grep -lE '", 1)[1].split("'", 1)[0]
    for module in ("make_known_failures", "trusted_ci", "trusted_verdict", "canary_evidence", "latest_reports", "working_policy"):
        assert module in pattern.split("|")
