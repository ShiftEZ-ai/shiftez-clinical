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
    }

    def api(repository, resource):
        assert repository == REPOSITORY
        run = packet["run"]
        if resource.startswith("workflows/ci.yml/runs?event="):
            rows = [run] if f"event={run['event']}&" in resource else []
            return json.dumps({"workflow_runs": rows}).encode()
        if resource == "runs/123/artifacts?per_page=100&page=1":
            source = {"id": run["id"], "head_branch": run["head_branch"], "head_sha": run["head_sha"],
                      "repository_id": policy.REPOSITORY, "head_repository_id": policy.REPOSITORY}
            artifact = {"id": 456, "name": "known-failures-candidate-1", "expired": False, "workflow_run": source}
            return json.dumps({"artifacts": [artifact]}).encode()
        assert resource == "artifacts/456/zip"
        packet["downloads"].append(resource)
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            if packet["report"] is not None:
                archive.writestr("stale-report.json", json.dumps(packet["report"]))
        return buffer.getvalue()

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

    monkeypatch.setattr(stale_evidence, "github_get", api)
    monkeypatch.setattr(verdict, "git", git)
    monkeypatch.setattr(policy, "fetch", lambda ref: DEV if ref == "refs/heads/dev" else pytest.fail(ref))
    monkeypatch.setenv("GH_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_RUN_ID", "789")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return packet


def evaluate(tmp_path, monkeypatch, outcome="passed", event="pull_request", branch="dev", scope=None):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    options = verdict.stale_options(event, branch, {"repository": {"full_name": REPOSITORY}}, BASE, PR, scope)
    current = {"schema": 3, "tree": BASE, "root_evidence_sha256": "a" * 64, "root_runs": [],
               "fails": [{"class": "baseline", "node": NODE}], "unstable": [], "cleanup_cap": 0}
    observe = None
    if "stale_lookup" in options:
        observe = partial(maker.report_pr_stale, tmp_path, PR, options["stale_lookup"])
    elif options.get("nightly"):
        observe = partial(maker.write_stale_report, tmp_path, PR)
    return maker.evaluate(current, current, [{NODE: outcome}], expected_replicas=1, observe_stale=observe,
                          stale_output=maker.stale_report_dir(tmp_path), stale_sha=PR)


def refused(tmp_path, monkeypatch, **kwargs):
    with pytest.raises(ValueError) as error:
        evaluate(tmp_path, monkeypatch, **kwargs)
    assert str(error.value) == REFUSAL


def test_reports_fresh_stale_base_evidence(tmp_path, monkeypatch, evidence):
    assert evaluate(tmp_path, monkeypatch)
    assert (tmp_path / "summary.md").read_text() == f"- {NODE}: stale on dev (run 123); removal candidate\n"
    assert json.loads((tmp_path / "stale-report.json").read_bytes()) == {
        "run_id": "789", "sha": PR, "nodes": [NODE], "reported": {NODE: "123"},
    }


def test_reports_bound_nightly_evidence(tmp_path, monkeypatch, evidence):
    evidence["run"].update(event="schedule", head_branch="dev", conclusion="failure")
    assert evaluate(tmp_path, monkeypatch, scope=set())
    assert evidence["downloads"] == ["artifacts/456/zip"]


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
