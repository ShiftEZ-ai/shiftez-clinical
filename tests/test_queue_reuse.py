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

    def git(argv):
        command, *args = argv[3:]
        if command == "rev-parse":
            return {HEAD + "^{tree}": TREE, "HEAD": HEAD}[args[0]].encode()
        if command == "ls-tree":
            assert tuple(args) == ("-r", "-z", HEAD, "--", "tests")
            return b"".join(("100644 blob " + TREE + "\t" + name).encode() + b"\0" for name in FILES)
        if command == "merge-base":
            assert tuple(args) == ("--is-ancestor", HEAD, HEAD)
            if data.get("ancestry_error"):
                raise subprocess.CalledProcessError(128, argv, stderr=b"fatal: bad object")
            return b""
        assert command == "diff" and tuple(args) == ("--name-only", "-z", "--no-ext-diff", "--no-textconv",
                                                     "--no-renames", BASE, args[6], "--", *reuse.CONTROLS)
        if args[6] == data.get("missing_object"):
            raise subprocess.CalledProcessError(128, argv, stderr=b"fatal: bad object")
        if args[6] == data.get("changed_controls"):
            return b".github/workflows/ci.yml\0.github/scripts/queue_reuse.py\0"
        return b""

    def check_output(argv, *, stderr, timeout=None):
        assert stderr == subprocess.PIPE
        if argv[0] == "git":
            assert argv[:3] == ["git", "-C", str(reuse.TRUSTED)]
            return git(argv)
        assert argv[:6] == ["gh", "api", "--hostname", "github.com", "--method", "GET"]
        assert timeout == 60
        prefix = f"repos/{REPO}/actions/"
        assert argv[-1].startswith(prefix)
        resource = argv[-1].removeprefix(prefix)
        raw = argv[6:-1]
        body = api(REPO, resource)
        if resource == "jobs/32/logs":
            if "log_error" in data:
                raise subprocess.CalledProcessError(1, argv, stderr=data["log_error"])
            if b"\x1b" in body and raw != ["--allow-escape-sequences"]:
                raise subprocess.CalledProcessError(1, argv, stderr=b"the response contains terminal escape sequences")
        else:
            assert raw == []
        return body

    monkeypatch.setattr(reuse.subprocess, "check_output", check_output)
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


def test_gate_log_with_terminal_escapes_can_still_bind_the_artifact(monkeypatch, tmp_path, packet, capsys):
    packet["log"] += b"\x1b[36mrunner log\x1b[0m\n"
    invoke(monkeypatch, packet)
    assert (tmp_path / "output").read_text().strip() == "run=30:2"
    assert "runner log" not in capsys.readouterr().out


@pytest.mark.parametrize(("member", "key", "value", "reason"), [
    ("run", "conclusion", "failure", "No successful merge-group run"),
    ("run", "head_sha", OTHER, "No successful merge-group run"),
    ("run", "head_branch", "gh-readonly-queue/dev/pr-1", "No successful merge-group run"),
    ("run", "path", ".github/workflows/other.yml", "Invalid dev CI run identity"),
    ("gate", "conclusion", "cancelled", "The required queue gate did not complete successfully"),
    ("gate", "conclusion", "skipped", "The required queue gate did not complete successfully"),
    ("gate", "run_attempt", 1, "The required queue gate did not complete successfully"),
    ("gate", "started_at", "2020-01-01T00:00:00Z", "Queue verdict predates today's allowance evaluation"),
    ("verdict", "passed", False, "Queue verdict evidence differs"),
    ("proof", "tree", OTHER, "Queue verdict evidence differs"),
    ("proof", "plan_sha256", "b" * 64, "Queue plan seal or planner inputs differ"),
    ("queue_plan", "tree", OTHER, "Expected the full single-replica plan"),
    ("queue_plan", "base_sha", OTHER, "Expected the full single-replica plan"),
    ("queue_plan", "selection", {}, "Expected the full single-replica plan"),
    ("queue_plan", "inventory_sha256", "b" * 64, "Queue plan seal or planner inputs differ"),
    ("queue_plan", "suites", {"unit": {"shards": [{"files": [FILES[0]]}]},
                              "browser": {"shards": [{"files": FILES[2:]}]}}, "Candidate plan omits"),
], ids=[
    "failed-merge-group-run",
    "wrong-run-head",
    "wrong-queue-branch",
    "wrong-workflow",
    "cancelled-gate",
    "skipped-gate",
    "wrong-gate-attempt",
    "stale-gate",
    "nonpassing-verdict",
    "wrong-proof-tree",
    "wrong-plan-seal",
    "wrong-plan-tree",
    "wrong-plan-base",
    "partial-plan-selection",
    "wrong-inventory-seal",
    "missing-suite-files",
])
def test_unbound_evidence_runs_full_suites(monkeypatch, tmp_path, packet, capsys, member, key, value, reason):
    packet[member][key] = value
    invoke(monkeypatch, packet)
    assert not (tmp_path / "output").exists()
    log, = capsys.readouterr().out.splitlines()
    assert reason in log and log.endswith("running the full suites")


@pytest.mark.parametrize(("defect", "reason"), [
    ("missing_run", "Expected one merge-group run"),
    ("missing_proof", "KeyError: 'queue-proof.json'"),
    ("unreadable", "gh (timed out after 60s, stderr: empty)"),
    ("untrusted_controls", "control files changed: .github/workflows/ci.yml, .github/scripts/queue_reuse.py"),
    ("replaced_artifact", "Qualification artifact differs from the trusted gate upload"),
], ids=[
    "missing-run",
    "missing-proof",
    "merge-group-lookup-timeout",
    "changed-control-files",
    "replaced-artifact",
])
def test_absent_or_unavailable_proof_enables_full_suites(monkeypatch, tmp_path, packet, capsys, defect, reason):
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
    log, = capsys.readouterr().out.splitlines()
    assert reason in log and log.endswith("running the full suites")


def test_log_read_failure_names_the_command_and_exit(monkeypatch, tmp_path, packet, capsys):
    packet["log_error"] = b"HTTP 403\n" + b"x" * 300
    invoke(monkeypatch, packet)
    assert not (tmp_path / "output").exists()
    log, = capsys.readouterr().out.splitlines()
    assert "gh api" in log and "actions/jobs/32/logs (exit 1, stderr: HTTP 403" in log
    assert "x" * 240 not in log and log.endswith("running the full suites")


@pytest.mark.parametrize(("defect", "value", "command"), [
    ("missing_object", BASE, "diff"),
    ("ancestry_error", True, "merge-base"),
], ids=["missing-object-in-diff", "missing-object-in-ancestry-check"])
def test_missing_object_names_git_exit(monkeypatch, tmp_path, packet, capsys, defect, value, command):
    packet[defect] = value
    invoke(monkeypatch, packet)
    assert not (tmp_path / "output").exists()
    log, = capsys.readouterr().out.splitlines()
    assert f"git -C '<trusted checkout>' {command}" in log and "exit 128, stderr: fatal: bad object" in log
    assert "control files changed" not in log


def test_refusal_redacts_tokens_and_signed_redirects(monkeypatch, tmp_path, packet, capsys):
    env_token = "synthetic-credential"
    prefixed_token = "ghs_" + "t" * 36
    header_token = "synthetic-bearer"
    redirect = "https://example.test/log?signature=synthetic-signature"
    monkeypatch.setenv("GH_TOKEN", env_token)
    packet["log_error"] = f"{env_token}\n{prefixed_token}\nAuthorization: Bearer {header_token}\n{redirect}".encode()
    invoke(monkeypatch, packet)
    assert not (tmp_path / "output").exists()
    log, = capsys.readouterr().out.splitlines()
    assert log.count("[redacted]") == 4
    assert all(secret not in log for secret in (env_token, prefixed_token, header_token, redirect))


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


def test_queue_records_proof_only_after_a_passing_trusted_verdict(monkeypatch, tmp_path, packet, capsys):
    event_file = tmp_path / "event.json"
    event = json.loads(event_file.read_bytes())
    event["merge_group"] = {"base_ref": reuse.MAIN, "base_sha": BASE, "head_sha": HEAD}
    event_file.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    verdict = tmp_path / "verdict.json"
    verdict.write_text('{"passed":false}', encoding="utf-8")
    invoke(monkeypatch, packet, "--record-attempt", "1", "--reports", str(tmp_path))
    assert not (tmp_path / reuse.PROOF).exists()
    log, = capsys.readouterr().out.splitlines()
    assert "Queue reuse proof could not be recorded (Queue verdict did not pass)" in log
    verdict.write_text('{"passed":true}', encoding="utf-8")
    invoke(monkeypatch, packet, "--record-attempt", "1", "--reports", str(tmp_path))
    proof = json.loads((tmp_path / reuse.PROOF).read_bytes())
    assert (proof["run_id"], proof["run_attempt"], proof["plan_attempt"]) == (30, 2, 1)
    assert proof["plan_sha256"] == reuse.hosted.digest(plan(1))
