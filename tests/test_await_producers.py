import http.client
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import await_producers as collector


STATIC = "Static checks"
UNIT = "Unit (2/3, run 1) / unit"
BROWSER = "Browser (1/2, run 1) / browser"
OUTPUTS = {"unit_count": "3", "unit_matrix": "[2]", "browser_count": "2", "browser_matrix": "[1]",
           "replicas": "[1]", "plan_sha256": "a" * 64}
EXPECTED = {STATIC, UNIT, BROWSER}
API = SimpleNamespace(netloc="api", scheme="https", hostname="api", username=None,
                      query="", fragment="", path="")
RUN = "/repos/sample/project/actions/runs/42"


def job(name, conclusion="success", status="completed", attempt=1):
    return {"name": name, "status": status, "conclusion": conclusion, "run_attempt": attempt}


def green():
    return [job(name) for name in sorted(EXPECTED)]


@pytest.fixture
def api(monkeypatch):
    clock = SimpleNamespace(now=0, sleeps=[])

    def advance(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(collector, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=advance))

    def install(respond):
        class Connection:
            def __init__(self, host, timeout):
                assert host == API.netloc
                assert 0 < timeout <= 10

            def request(self, method, target, *, headers):
                assert method == "GET"
                assert headers["Authorization"] == "Bearer fixture"
                assert urlsplit(target).path == RUN + "/jobs"
                query = parse_qs(urlsplit(target).query)
                assert query == {"filter": ["latest"], "per_page": ["100"], "page": query["page"]}
                status, rows, next_page = respond(int(query["page"][0]))
                self.response = io.BytesIO(json.dumps({"jobs": rows}).encode())
                self.response.status = status
                self.response.headers = {"Link": 'rel="next"'} if next_page else {}
                self.response.getheader = self.response.headers.get

            def getresponse(self):
                return self.response

            def close(self):
                pass

        monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
        return clock

    return install


def wait():
    collector.wait_for_jobs(API, RUN, "fixture", EXPECTED, deadline_minutes=1)


def test_names_follow_selected_shards_and_replicas():
    assert collector.expected_jobs(OUTPUTS) == EXPECTED
    assert collector.expected_jobs({**OUTPUTS, "replicas": "[1,2]"}) == EXPECTED | {
        "Unit (2/3, run 2) / unit", "Browser (1/2, run 2) / browser",
    }


def test_success_uses_all_pages_and_ignores_unexpected_job(api):
    pages = {1: (200, [job(STATIC), job("Extra", "failure")], True),
             2: (200, [job(UNIT), job(BROWSER)], False)}
    clock = api(pages.pop)
    wait()
    assert not pages and not clock.sleeps


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "failure", "timed_out", "action_required"])
def test_completed_non_success_refuses(api, conclusion):
    clock = api(lambda page: (200, [job(STATIC), job(UNIT, conclusion), job(BROWSER)], False))
    with pytest.raises(SystemExit) as refusal:
        wait()
    assert f"{UNIT} ({conclusion})" in str(refusal.value)
    assert not clock.sleeps


@pytest.mark.parametrize("rows, reason", [([], "missing"), ([job(BROWSER, None, "in_progress")], "pending")])
def test_deadline_refuses_missing_or_pending_job(api, rows, reason):
    clock = api(lambda page: (200, [job(STATIC), job(UNIT), *rows], False))
    with pytest.raises(SystemExit) as refusal:
        wait()
    assert f"{BROWSER} ({reason})" in str(refusal.value)
    assert clock.sleeps == [25, 25, 10]


@pytest.mark.parametrize("latest, older", [("success", "failure"), ("failure", "success")])
def test_latest_attempt_conclusion_wins(api, latest, older):
    history = [job(BROWSER, older), job(BROWSER, latest, attempt=2)]
    api(lambda page: (200, [job(STATIC), job(UNIT), max(history, key=lambda row: row["run_attempt"])], False))
    if latest == "success":
        wait()
    else:
        with pytest.raises(SystemExit) as refusal:
            wait()
        assert f"{BROWSER} (failure)" in str(refusal.value)


def test_transient_http_error_backs_off_then_passes(api):
    responses = iter([(503, [], False), (200, green(), False)])
    clock = api(lambda page: next(responses))
    wait()
    assert clock.sleeps == [50]


def test_persistent_http_error_refuses_at_deadline(api):
    clock = api(lambda page: (503, [], False))
    with pytest.raises(SystemExit, match=r"deadline passed: .*jobs API unavailable \(HTTP 503\)"):
        wait()
    assert clock.sleeps == [50, 10]


def test_collector_emits_verdict_inputs_only_after_success(api, monkeypatch, tmp_path):
    output = tmp_path / "output"
    checks = {"result": "success", "outputs": OUTPUTS}
    for key, value in {"NEEDS_JSON": json.dumps({"checks": checks}), "GITHUB_REPOSITORY": "sample/project",
                       "GITHUB_RUN_ID": "42", "GITHUB_API_URL": "fixture", "ACTIONS_TOKEN": "fixture",
                       "GITHUB_OUTPUT": str(output)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(collector, "urlsplit", lambda url: API)
    monkeypatch.setattr(sys, "argv", ["await_producers.py", "--deadline-minutes", "1"])
    api(lambda page: (200, [job(STATIC), job(UNIT), job(BROWSER, "failure")], False))
    with pytest.raises(SystemExit):
        collector.main()
    assert not output.exists()
    api(lambda page: (200, green(), False))
    collector.main()
    key, value = output.read_text(encoding="utf-8").split("=", 1)
    assert key == "verified_needs"
    assert json.loads(value) == {"checks": checks, "static": {"result": "success"},
                                 "unit": {"result": "success"}, "browser": {"result": "success"}}


def test_expected_names_match_the_names_ci_yml_renders():
    """A renamed shard job would otherwise leave every run waiting until the collector deadline."""
    workflows = ROOT / ".github/workflows"
    jobs = yaml.load((workflows / "ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    called = yaml.load((workflows / "ci-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    assert list(called) == ["tests"]
    rendered = {jobs["static"]["name"]}
    for suite in ("unit", "browser"):
        job = jobs[suite]
        assert job["uses"] == "./.github/workflows/ci-tests.yml" and job["with"]["suite"] == suite
        assert job["strategy"]["matrix"]["shard"] == "${{ fromJSON(needs.checks.outputs.%s_matrix) }}" % suite
        assert job["strategy"]["matrix"]["replica"] == "${{ fromJSON(needs.checks.outputs.replicas) }}"
        shards = json.loads(OUTPUTS[suite + "_matrix"])
        for shard in shards:
            for replica in json.loads(OUTPUTS["replicas"]):
                values = {"matrix.shard": str(shard), "matrix.replica": str(replica), "inputs.suite": suite,
                          "needs.checks.outputs.%s_count" % suite: OUTPUTS[suite + "_count"]}
                name = job["name"] + " / " + called["tests"]["name"]
                for key, value in values.items():
                    name = name.replace("${{ " + key + " }}", value)
                assert "${{" not in name, name
                rendered.add(name)
    assert rendered == collector.expected_jobs(OUTPUTS)
