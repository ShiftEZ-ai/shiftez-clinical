from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_browser_system_dependencies_are_bounded_and_retried():
    jobs = yaml.load((ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    steps = [step for job in jobs.values() for step in job["steps"] if "install-deps" in step.get("run", "")]
    assert len(steps) == 1
    script = steps[0]["run"]
    assert "timeout --kill-after=30 600 python -m playwright install-deps chromium && break" in script
    assert "for attempt in 1 2 3; do" in script
    assert 'if [ "$attempt" = 3 ]; then' in script and "exit 1" in script
    assert "Acquire::Retries" in script and "Acquire::http::Timeout" in script
    assert "timeout-minutes" not in steps[0]
