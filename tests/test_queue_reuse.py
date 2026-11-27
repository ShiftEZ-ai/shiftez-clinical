"""Exercise queue reuse with the same inventory reader as trusted_verdict."""
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".github/scripts"), str(ROOT / "ci")]
import queue_reuse as reuse

BASE, HEAD, TREE, OTHER = (letter * 40 for letter in "abcd")
REPO = "ShiftEZ-ai/shiftez-clinical"
FILES = [
    "tests/test_alpha.py", "tests/test_beta.py",
    "tests/test_browser_alpha.py", "tests/test_browser_beta.py",
]


def plan(width):
    return {
        "schema": 1, "sha": HEAD, "tree": TREE, "base_sha": BASE, "replicas": [1],
        "smoke": False, "inventory_sha256": reuse.hosted.digest(FILES), "duration_sha256": "a" * 64,
        "suites": {
            suite: {"shards": [{"files": names[i::width]} for i in range(width)]}
            for suite, names in (("unit", FILES[:2]), ("browser", FILES[2:]))
        },
    }


def archive(files):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as output:
        for name, value in files.items():
            output.writestr(name, json.dumps(value))
    return buffer.getvalue()


@pytest.fixture
def packet(tmp_path, monkeypatch):
    monkeypatch.setattr(reuse, "utc_today", lambda: date(2026, 1, 1))
    repo = {"id": reuse.policy.REPOSITORY, "full_name": REPO, "default_branch": "main"}
    event = {"repository": repo, "ref": reuse.MAIN, "before": BASE, "after": HEAD,
             "created": False, "deleted": False, "forced": False}
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event), encoding="utf-8")
    actor, actor_id = reuse.QUEUE_ACTOR
    for key, value in {
        "EVENT_NAME": "push", "EVENT_PATH": str(event_file), "SHA": HEAD, "WORKFLOW_SHA": HEAD,
        "REF": reuse.MAIN, "ACTOR": actor, "ACTOR_ID": actor_id,
        "OUTPUT": str(tmp_path / "output"), "STEP_SUMMARY": str(tmp_path / "summary"),
        "SERVER_URL": "https://github.com", "REPOSITORY": REPO,
        "RUN_ID": "30", "RUN_ATTEMPT": "2",
    }.items():
        monkeypatch.setenv("GITHUB_" + key, value)

    push_plan = tmp_path / "push-plan.json"
    push_plan.write_text(json.dumps(plan(1)), encoding="utf-8")
    queue_plan = plan(2)
    run = {"event": "merge_group", "conclusion": "success", "status": "completed", "path": reuse.evidence.WORKFLOW,
           "head_sha": HEAD, "head_branch": "gh-readonly-queue/main/pr-1", "repository": repo,
           "head_repository": repo, "id": 30, "run_attempt": 2, "workflow_id": 31}
    proof = {"schema": 1, "run_id": 30, "run_attempt": 2, "plan_attempt": 1, "workflow_sha": HEAD,
             "trusted_sha": BASE, "sha": HEAD, "tree": TREE, "base_sha": BASE}
    gate = {"name": "ci-gate", "status": "completed", "conclusion": "success",
            "id": 32, "run_id": 30, "run_attempt": 2, "head_sha": HEAD,
            "started_at": reuse.utc_today().isoformat() + "T00:00:00Z"}
    verdict = {"passed": True}
    source = {"id": 30, "head_sha": HEAD, "head_branch": run["head_branch"],
              "repository_id": repo["id"], "head_repository_id": repo["id"]}
    artifacts = [{"id": artifact_id, "name": name, "expired": False, "workflow_run": source}
                 for artifact_id, name in [(33, "known-failures-candidate-2"), (34, "ci-plan-1")]]
    data = {"run": run, "queue_plan": queue_plan, "proof": proof, "gate": gate, "verdict": verdict,
            "log": b"2026-01-01T00:00:00.000Z Queue qualification artifact id 33\n",
            "artifacts": artifacts, "queries": [], "path": push_plan}

    def api(repository, resource):
        assert repository == REPO
        data["queries"].append(resource)
        listed = [] if data.get("missing_run") else [run]
        responses = {
            f"workflows/ci.yml/runs?event=merge_group&head_sha={HEAD}&per_page=100":
                {"total_count": len(listed), "workflow_runs": listed},
            "runs/30/attempts/2/jobs?per_page=100": {"total_count": 1, "jobs": [gate]},
            "runs/30/artifacts?per_page=100&page=1": {"artifacts": artifacts},
            "runs/30": run | data.get("readback", {}),
        }
        if resource == "jobs/32/logs":
            return data["log"]
        if resource == "artifacts/33/zip":
            reports = {"verdict.json": verdict, "node-census.json": {"summary": "collect=+0/-0"}}
            if not data.get("missing_proof"):
                reports[reuse.PROOF] = {"plan_sha256": reuse.hosted.digest(queue_plan)} | proof
            return archive(reports)
        if resource == "artifacts/34/zip":
            return archive({"shard-plan.json": queue_plan})
        return json.dumps(responses[resource]).encode()

    def git(command, *args):
        if command == "rev-parse":
            return {HEAD + "^{tree}": TREE, "HEAD": HEAD}[args[0]].encode()
        if command == "ls-tree":
            assert args == ("-r", "-z", HEAD, "--", "tests")
            return b"".join(("100644 blob " + TREE + "\t" + name).encode() + b"\0" for name in FILES)
        if command == "merge-base":
            assert args == ("--is-ancestor", HEAD, HEAD)
            return b""
        assert command == "diff" and args == ("--quiet", "--no-ext-diff", "--no-textconv", BASE,
                                                args[4], "--", *reuse.CONTROLS)
        if args[4] == data.get("changed_controls"):
            raise subprocess.CalledProcessError(1, command)
        return b""

    monkeypatch.setattr(reuse.evidence, "github_get", api)
    monkeypatch.setattr(reuse, "git", git)
    return data


def invoke(monkeypatch, packet, *args):
    monkeypatch.setattr(sys, "argv", ["queue_reuse.py", str(packet["path"]), *args])
    reuse.main()


def test_full_plan_can_reuse_exact_queue_attempt_with_other_shard_width(monkeypatch, tmp_path, packet):
    invoke(monkeypatch, packet)
    assert (tmp_path / "output").read_text().strip() == "run=30:2"
    assert "runs/30" in packet["queries"]
    packet["queries"].clear()

    reports = tmp_path / "reports"
    invoke(monkeypatch, packet, "--require-run", "30:2", "--reports", str(reports))
    assert "jobs/32/logs" in packet["queries"]
    assert "runs/30" in packet["queries"]
    assert json.loads((reports / reuse.PROOF).read_bytes())["plan_attempt"] == 1
    assert json.loads((reports / "node-census.json").read_bytes())["summary"] == "collect=+0/-0"
    assert "/actions/runs/30" in (tmp_path / "summary").read_text()


@pytest.mark.parametrize(("member", "key", "value"), [
    ("run", "conclusion", "failure"),
    ("run", "head_sha", OTHER),
    ("run", "head_branch", "gh-readonly-queue/dev/pr-1"),
    ("run", "path", ".github/workflows/other.yml"),
    ("gate", "conclusion", "cancelled"),
    ("gate", "conclusion", "skipped"),
    ("gate", "run_attempt", 1),
    ("gate", "started_at", "2020-01-01T00:00:00Z"),
    ("verdict", "passed", False),
    ("proof", "tree", OTHER),
    ("proof", "plan_sha256", "b" * 64),
    ("queue_plan", "tree", OTHER),
    ("queue_plan", "base_sha", OTHER),
    ("queue_plan", "selection", {}),
    ("queue_plan", "inventory_sha256", "b" * 64),
    ("queue_plan", "suites", {"unit": {"shards": [{"files": [FILES[0]]}]},
                              "browser": {"shards": [{"files": FILES[2:]}]}}),
])
def test_unbound_evidence_runs_full_suites(monkeypatch, tmp_path, packet, member, key, value):
    packet[member][key] = value
    invoke(monkeypatch, packet)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("defect", ["missing_run", "missing_proof", "unreadable", "untrusted_controls", "replaced_artifact"])
def test_absent_or_unavailable_proof_enables_full_suites(monkeypatch, tmp_path, packet, defect):
    if defect == "missing_run":
        packet["missing_run"] = True
    elif defect == "missing_proof":
        packet["missing_proof"] = True
    elif defect == "unreadable":
        def unavailable(*args):
            raise subprocess.TimeoutExpired("gh", 60)
        monkeypatch.setattr(reuse.evidence, "github_get", unavailable)
    elif defect == "untrusted_controls":
        packet["changed_controls"] = HEAD
    else:
        packet["log"] = b"2026-01-01T00:00:00.000Z Queue qualification artifact id 99\n"
    invoke(monkeypatch, packet)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "schedule", "workflow_dispatch", "push"])
def test_other_events_and_nonqueue_push_do_not_look_up_evidence(monkeypatch, tmp_path, packet, event):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    if event == "push":
        monkeypatch.setenv("GITHUB_ACTOR_ID", "1")
    invoke(monkeypatch, packet)
    assert not packet["queries"]
    assert not (tmp_path / "output").exists()


def test_gate_rejects_attempt_change_and_does_not_write_reports(monkeypatch, tmp_path, packet):
    reports = tmp_path / "reports"
    with pytest.raises(SystemExit, match="Queue reuse verification failed"):
        invoke(monkeypatch, packet, "--require-run", "30:1", "--reports", str(reports))
    packet["readback"] = {"run_attempt": 3}
    with pytest.raises(SystemExit, match="Queue reuse verification failed"):
        invoke(monkeypatch, packet, "--require-run", "30:2", "--reports", str(reports))
    assert not reports.exists()


def test_queue_records_proof_only_after_a_passing_trusted_verdict(monkeypatch, tmp_path, packet):
    event_file = tmp_path / "event.json"
    event = json.loads(event_file.read_bytes())
    event["merge_group"] = {"base_ref": reuse.MAIN, "base_sha": BASE, "head_sha": HEAD}
    event_file.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    verdict = tmp_path / "verdict.json"
    verdict.write_text('{"passed":false}', encoding="utf-8")
    invoke(monkeypatch, packet, "--record-attempt", "1", "--reports", str(tmp_path))
    assert not (tmp_path / reuse.PROOF).exists()
    verdict.write_text('{"passed":true}', encoding="utf-8")
    invoke(monkeypatch, packet, "--record-attempt", "1", "--reports", str(tmp_path))
    proof = json.loads((tmp_path / reuse.PROOF).read_bytes())
    assert (proof["run_id"], proof["run_attempt"], proof["plan_attempt"]) == (30, 2, 1)
    assert proof["plan_sha256"] == reuse.hosted.digest(plan(1))
