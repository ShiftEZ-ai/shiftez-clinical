"""The dev PR verdict only reports stale entries backed by bound dev evidence."""
from functools import partial
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".github/scripts"), str(ROOT / "ci")]
import make_known_failures as maker
import stale_evidence
import trusted_verdict as verdict
import working_policy as policy

BASE, DEV, PR, OTHER, OLDER = (letter * 40 for letter in "abcde")
NODE = "tests/test_product.py::test_sample"
REFUSAL = "Stale passing failure entries: delete their lines from ci/known-failures.json: " + NODE
REPOSITORY = "ShiftEZ-ai/shiftez-clinical"


@pytest.fixture(autouse=True)
def explicit_event(monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    packet = {
        "run": {"id": 123, "run_attempt": 1, "event": "merge_group", "head_branch": "gh-readonly-queue/dev/pr-1",
                "head_sha": DEV, "status": "completed", "conclusion": "success", "path": stale_evidence.WORKFLOW,
                "repository": {"id": policy.REPOSITORY}, "head_repository": {"id": policy.REPOSITORY},
                "created_at": "2026-01-01T01:00:00Z"},
        "report": {"run_id": "123", "sha": DEV, "nodes": [NODE]},
        "diff": b"packages/domain/score.py\0",
        "downloads": [],
        "run_queries": [],
        "other_runs": [],
    }

    def api(command, *, stderr, timeout):
        assert command[:-1] == ["gh", "api", "--hostname", "github.com", "--method", "GET"]
        assert stderr == subprocess.PIPE
        assert timeout == 60
        api_error = packet.get("api_error")
        if isinstance(api_error, Exception):
            raise api_error
        if api_error is not None:
            return api_error
        prefix = f"repos/{REPOSITORY}/actions/"
        assert command[-1].startswith(prefix)
        resource = command[-1].removeprefix(prefix)
        runs = [packet["run"], *packet["other_runs"]]
        if resource.startswith("workflows/ci.yml/runs?event="):
            packet["run_queries"].append(resource)
            rows = [run for run in runs if f"event={run['event']}&" in resource]
            return json.dumps({"workflow_runs": rows}).encode()
        for run in runs:
            artifact_id = run["id"] + 333
            if resource == f"runs/{run['id']}/artifacts?per_page=100&page=1":
                source = {"id": run["id"], "head_branch": run["head_branch"], "head_sha": run["head_sha"],
                          "repository_id": policy.REPOSITORY, "head_repository_id": policy.REPOSITORY}
                source.update(packet.get("source_overrides", {}))
                artifact = {"id": artifact_id, "name": "known-failures-candidate-1", "expired": False,
                            "workflow_run": source}
                artifact.update(packet.get("artifact_overrides", {}))
                artifacts = [artifact]
                if packet.get("duplicate_artifact"):
                    artifacts.append(artifact | {"id": artifact_id + 1})
                return json.dumps({"artifacts": artifacts}).encode()
            if resource == f"artifacts/{artifact_id}/zip":
                packet["downloads"].append(resource)
                report = (packet["report"] if run is packet["run"] else
                          {"run_id": str(run["id"]), "sha": run["head_sha"], "nodes": [NODE]})
                buffer = BytesIO()
                with ZipFile(buffer, "w") as archive:
                    if report is not None:
                        archive.writestr("stale-report.json", json.dumps(report))
                return buffer.getvalue()
        pytest.fail(f"Unexpected GitHub request: {resource}")

    def git(*args):
        if args[0] == "diff":
            assert args[-3:] == (BASE, PR, "--")
            if packet["diff"] is None:
                raise subprocess.CalledProcessError(1, args)
            return packet["diff"]
        assert args[:2] == ("merge-base", "--is-ancestor")
        older, newer = args[2:]
        if older == newer or (older, newer) in {(BASE, DEV), (OLDER, BASE), (OLDER, DEV)}:
            return b""
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(subprocess, "check_output", api)
    monkeypatch.setattr(verdict, "git", git)
    monkeypatch.setattr(policy, "fetch", lambda ref: DEV if ref == "refs/heads/dev" else pytest.fail(ref))
    monkeypatch.setenv("GH_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_RUN_ID", "789")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return packet


def run_pr_verdict(tmp_path, monkeypatch, current, runs, options):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"sha": PR, "tree": PR, "base_sha": BASE, "smoke": False, "replicas": [1]}))
    monkeypatch.setattr(sys, "argv", ["make_known_failures.py", "--plan", str(plan_path),
                                     "--reports", str(tmp_path), "--output", str(tmp_path), "--base-sha", BASE])
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    git_results = {("rev-parse", "HEAD"): PR, ("rev-parse", "HEAD^{tree}"): PR,
                   ("diff", "--name-status", "--no-renames", BASE, "HEAD"): ""}
    monkeypatch.setattr(maker, "git", lambda *args: git_results[args])
    monkeypatch.setattr(maker, "context", lambda base: {
        "current": current, "base": current, "base_sha": base, "replicas": [1], "bootstrap": False,
    })
    monkeypatch.setattr(maker, "preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(maker, "collect_reports", lambda *args, **kwargs: runs)
    monkeypatch.setattr(maker, "load_frozen_baseline", lambda path: (set(), "fixture"))
    monkeypatch.setattr(maker, "load_env_exceptions", lambda path: (set(), "fixture"))
    monkeypatch.setattr(maker, "load_root_evidence", lambda path, baseline_sha: (
        {"runs": current["root_runs"], "counts": {"cleanup": 0}}, current["root_evidence_sha256"],
    ))
    monkeypatch.setattr(maker, "report_outside", lambda *args, **kwargs: None)
    maker.main(**options)
    return json.loads((tmp_path / "verdict.json").read_bytes())


def evaluate(tmp_path, monkeypatch, outcome="passed", event="pull_request", branch="dev", scope=None, other_stale=None,
             default="dev"):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    if default != "dev":
        monkeypatch.setattr(policy, "fetch", lambda ref: DEV if ref == "refs/heads/" + default else pytest.fail(ref))
    data = {"repository": {"full_name": REPOSITORY, "default_branch": default}}
    options = verdict.stale_options(event, branch, data, BASE, PR, scope)
    outcomes = {NODE: outcome}
    if other_stale is not None:
        outcomes[other_stale] = "passed"
    current = {"schema": 3, "tree": BASE, "root_evidence_sha256": "a" * 64, "root_runs": [],
               "fails": [{"class": "baseline", "node": node} for node in outcomes], "unstable": [], "cleanup_cap": 0}
    if "stale_lookup" in options:
        result = run_pr_verdict(tmp_path, monkeypatch, current, [outcomes], options)
        if not result["passed"]:
            raise ValueError(result["reason"])
        return True
    observe = partial(maker.write_stale_report, tmp_path, PR) if options.get("nightly") else None
    return maker.evaluate(current, current, [outcomes], expected_replicas=1, observe_stale=observe,
                          stale_output=maker.stale_report_dir(tmp_path), stale_sha=PR)


def refused(tmp_path, monkeypatch, **kwargs):
    with pytest.raises(ValueError) as error:
        evaluate(tmp_path, monkeypatch, **kwargs)
    assert str(error.value) == REFUSAL
    if kwargs.get("event", "pull_request") == "pull_request":
        assert not (tmp_path / "stale-report.json").exists()
        assert not (tmp_path / "summary.md").exists()


def test_reports_fresh_stale_base_evidence(tmp_path, monkeypatch, evidence):
    assert evaluate(tmp_path, monkeypatch)
    assert (tmp_path / "summary.md").read_text() == f"- {NODE}: stale on dev (run 123); removal candidate\n"
    assert json.loads((tmp_path / "stale-report.json").read_bytes()) == {
        "run_id": "789", "sha": PR, "nodes": [NODE], "reported": {NODE: "123"},
    }


def test_unreported_stale_node_still_refuses_when_another_node_is_reported(tmp_path, monkeypatch, evidence):
    unreported = "tests/test_product.py::test_unreported"
    with pytest.raises(ValueError) as error:
        evaluate(tmp_path, monkeypatch, other_stale=unreported)
    assert str(error.value) == REFUSAL.replace(NODE, unreported)
    assert json.loads((tmp_path / "stale-report.json").read_bytes()) == {
        "run_id": "789", "sha": PR, "nodes": [NODE, unreported], "reported": {NODE: "123"},
    }


def test_reports_bound_nightly_evidence(tmp_path, monkeypatch, evidence):
    group = evidence["run"] | {"id": 124, "conclusion": "failure"}
    evidence["other_runs"].append(group)
    evidence["run"].update(event="schedule", head_branch="dev", conclusion="failure")
    assert evaluate(tmp_path, monkeypatch, scope=set())
    assert evidence["downloads"] == ["artifacts/456/zip"]


def test_default_main_reads_main_nightly_evidence(tmp_path, monkeypatch, evidence):
    group = evidence["run"] | {"id": 124, "conclusion": "failure"}
    evidence["other_runs"].append(group)
    evidence["run"].update(event="schedule", head_branch="main", conclusion="failure")
    assert evaluate(tmp_path, monkeypatch, scope=set(), branch="main", default="main")
    assert evidence["downloads"] == ["artifacts/456/zip"]
    assert [query for query in evidence["run_queries"] if "event=schedule" in query] == [
        "workflows/ci.yml/runs?event=schedule&status=completed&branch=main&per_page=100&page=1"]


@pytest.mark.parametrize("nightly_time", ["2026-01-01T00:00:00Z", "2026-01-01T02:00:00Z"],
                         ids=["older-nightly", "newer-nightly"])
def test_reports_newest_successful_group_before_nightly(tmp_path, monkeypatch, evidence, nightly_time):
    run = evidence["run"]
    evidence["other_runs"] = [
        run | {"id": 124, "event": "schedule", "head_branch": "dev", "conclusion": "failure", "created_at": nightly_time},
        run | {"id": 125, "created_at": "2025-12-31T23:00:00Z"},
        run | {"id": 126, "conclusion": "failure", "created_at": "2026-01-01T03:00:00Z"},
    ]
    assert evaluate(tmp_path, monkeypatch)
    assert evidence["downloads"] == ["artifacts/456/zip"]
    assert json.loads((tmp_path / "stale-report.json").read_bytes())["reported"] == {NODE: "123"}


def test_missing_evidence_is_refused(tmp_path, monkeypatch, evidence):
    evidence["report"] = None
    refused(tmp_path, monkeypatch)


def test_old_unbound_report_is_refused(tmp_path, monkeypatch, evidence):
    evidence["report"] = [NODE]
    refused(tmp_path, monkeypatch)


def test_evidence_older_than_pr_base_is_refused(tmp_path, monkeypatch, evidence):
    evidence["run"]["head_sha"] = evidence["report"]["sha"] = OLDER
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_foreign_run_id_is_refused(tmp_path, monkeypatch, evidence):
    evidence["report"]["run_id"] = "124"
    refused(tmp_path, monkeypatch)


def test_foreign_sha_is_refused(tmp_path, monkeypatch, evidence):
    evidence["report"]["sha"] = OTHER
    refused(tmp_path, monkeypatch)


def test_discarded_merge_group_is_refused(tmp_path, monkeypatch, evidence):
    evidence["run"]["head_sha"] = evidence["report"]["sha"] = OTHER
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_push_cannot_supply_base_evidence(tmp_path, monkeypatch, evidence):
    evidence["run"].update(event="push", head_branch="dev")
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_expired_artifact_is_refused(tmp_path, monkeypatch, evidence):
    evidence["artifact_overrides"] = {"expired": True}
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


@pytest.mark.parametrize(("key", "value"), [
    ("id", 124), ("head_sha", OTHER), ("head_branch", "gh-readonly-queue/dev/pr-2"),
    ("repository_id", policy.REPOSITORY + 1), ("head_repository_id", policy.REPOSITORY + 1),
])
def test_foreign_artifact_is_refused(tmp_path, monkeypatch, evidence, key, value):
    evidence["source_overrides"] = {key: value}
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_duplicate_artifacts_are_refused(tmp_path, monkeypatch, evidence):
    evidence["duplicate_artifact"] = True
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_artifact_for_another_attempt_is_refused(tmp_path, monkeypatch, evidence):
    evidence["run"]["run_attempt"] = 2
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


@pytest.mark.parametrize(("event", "branch"), [
    ("merge_group", "gh-readonly-queue/main/pr-1"), ("schedule", "main"),
])
def test_non_dev_run_is_refused(tmp_path, monkeypatch, evidence, event, branch):
    evidence["run"].update(event=event, head_branch=branch)
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_default_main_reads_main_queue_evidence(tmp_path, monkeypatch, evidence):
    evidence["run"]["head_branch"] = "gh-readonly-queue/main/pr-1"
    assert evaluate(tmp_path, monkeypatch, branch="main", default="main")


@pytest.mark.parametrize(("event", "branch"), [
    ("merge_group", "gh-readonly-queue/dev/pr-1"), ("schedule", "dev"),
])
def test_default_main_refuses_runs_of_the_old_default(tmp_path, monkeypatch, evidence, event, branch):
    evidence["run"].update(event=event, head_branch=branch)
    refused(tmp_path, monkeypatch, branch="main", default="main")
    assert not evidence["downloads"]


def test_stale_lookup_needs_the_default_branch():
    with pytest.raises(ValueError, match="Unexpected repository or default branch"):
        verdict.stale_options("pull_request", "dev", {"repository": {"full_name": REPOSITORY}}, BASE, PR, None)


def test_missing_gh_token_is_refused(tmp_path, monkeypatch, evidence):
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setattr(policy, "fetch", lambda ref: pytest.fail(f"Unexpected fetch: {ref}"))
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


@pytest.mark.parametrize("api_error", [
    subprocess.CalledProcessError(1, ["gh"]), subprocess.TimeoutExpired(["gh"], 60), b"{",
], ids=["failed-command", "timeout", "invalid-json"])
def test_api_error_is_refused(tmp_path, monkeypatch, evidence, api_error):
    evidence["api_error"] = api_error
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_touched_test_file_is_refused(tmp_path, monkeypatch, evidence):
    evidence["diff"] = b"tests/test_product.py\0"
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_touched_known_failures_is_refused(tmp_path, monkeypatch, evidence):
    evidence["diff"] = b"ci/known-failures.json\0"
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_unavailable_changed_files_are_refused(tmp_path, monkeypatch, evidence):
    evidence["diff"] = None
    refused(tmp_path, monkeypatch)
    assert not evidence["downloads"]


def test_listed_pr_failure_keeps_previous_verdict(tmp_path, monkeypatch, evidence):
    assert evaluate(tmp_path, monkeypatch, outcome="failed")
    assert not (tmp_path / "stale-report.json").exists()
    assert not evidence["downloads"]


def test_pr_error_keeps_previous_refusal(tmp_path, monkeypatch, evidence):
    with pytest.raises(ValueError) as error:
        evaluate(tmp_path, monkeypatch, outcome="error")
    assert str(error.value) == "New errors outside the baseline-error class: " + NODE
    assert not evidence["downloads"]


def test_main_pr_keeps_previous_refusal(tmp_path, monkeypatch, evidence):
    refused(tmp_path, monkeypatch, branch="main")
    assert not (tmp_path / "stale-report.json").exists()
    assert not evidence["downloads"]


def test_release_keeps_previous_refusal(tmp_path, monkeypatch, evidence):
    refused(tmp_path, monkeypatch, event="workflow_dispatch", branch="main")
    assert not (tmp_path / "stale-report.json").exists()
    assert not evidence["downloads"]


def test_nightly_writes_evidence_and_keeps_previous_refusal(tmp_path, monkeypatch, evidence):
    monkeypatch.setenv("GITHUB_SHA", PR)
    refused(tmp_path, monkeypatch, event="schedule")
    assert json.loads((tmp_path / "stale-report.json").read_bytes()) == {
        "run_id": "789", "sha": PR, "nodes": [NODE],
    }
    assert not (tmp_path / "summary.md").exists()
    assert not evidence["downloads"]


@pytest.mark.parametrize("event", ["merge_group", "push"])
def test_producer_keeps_previous_verdict_and_summary(tmp_path, monkeypatch, evidence, event, capsys):
    assert evaluate(tmp_path, monkeypatch, event=event)
    old_summary = f"Stale passing failure entries: 1\n- {NODE}\n"
    assert capsys.readouterr().out == old_summary
    assert (tmp_path / "summary.md").read_bytes() == old_summary.replace("\n", os.linesep).encode()
    assert json.loads((tmp_path / "stale-report.json").read_bytes()) == {
        "run_id": "789", "sha": PR, "nodes": [NODE],
    }
    assert not evidence["downloads"]
