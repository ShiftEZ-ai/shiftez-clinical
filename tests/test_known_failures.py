"""Known failures never hide missing execution, stale entries or new regressions."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import make_known_failures as ratchet

RED = "tests/test_sample.py::test_red"
GREEN = "tests/test_sample.py::test_green"
TODAY = date(2026, 11, 16)
TREE = "a" * 40


def listing(fails=(), unstable=()):
    return {"schema": 1, "tree": TREE, "fails": sorted(fails),
            "unstable": [{"node": node, "issue": 123, "expires_on": "2026-12-01"} for node in sorted(unstable)]}


def check(current, base, runs, **kwargs):
    return ratchet.evaluate(current, base, runs, today=TODAY, **kwargs)


def test_all_known_passes_and_new_failure_fails():
    known = listing([RED])
    assert check(known, known, [{RED: "failed", GREEN: "passed"}])
    with pytest.raises(ValueError, match="New failures"):
        check(known, known, [{RED: "failed", GREEN: "failed"}])


def test_stale_and_new_failure_wave_is_reported_together():
    known = listing([RED])
    with pytest.raises(ValueError) as error:
        check(known, known, [{RED: "passed", GREEN: "failed"}])
    assert "Stale passing" in str(error.value) and "New failures" in str(error.value)


def test_stale_passing_failure_must_be_deleted():
    known = listing([RED])
    with pytest.raises(ValueError, match="delete their lines"):
        check(known, known, [{RED: "passed"}])
    assert check(listing(), known, [{RED: "passed"}])


def test_vanished_node_must_be_deleted_and_skipped_is_not_passed():
    known = listing([RED])
    with pytest.raises(ValueError, match="Vanished"):
        check(known, known, [{GREEN: "passed"}])
    assert check(listing(), known, [{GREEN: "passed"}])
    with pytest.raises(ValueError, match="did not execute"):
        check(known, known, [{RED: "skipped"}])


def test_after_bootstrap_additions_are_refused():
    with pytest.raises(ValueError, match="Additions"):
        check(listing([RED]), listing(), [{RED: "failed"}])


@pytest.mark.parametrize("issue,expires_on", [
    (0, "2026-12-16"), (-1, "2026-12-16"), (True, "2026-12-16"),
    ("123", "2026-12-16"), (1.5, "2026-12-16"),
    (123, ""), (123, None), (123, "2026-12-1"), (123, "2026-02-30"),
    (123, "2026-11-16"), (123, "2026-11-15"),
    (123, (TODAY + timedelta(days=91)).isoformat()),
])
def test_bootstrap_refuses_missing_or_invalid_issue_and_expiry(issue, expires_on):
    with pytest.raises(ValueError, match="1 to 90 days"):
        ratchet.require_bootstrap_inputs(issue, expires_on, today=TODAY)


@pytest.mark.parametrize("days", [1, 30, 90])
def test_bootstrap_accepts_positive_issue_and_inclusive_expiry_bounds(days):
    ratchet.require_bootstrap_inputs(123, (TODAY + timedelta(days=days)).isoformat(), today=TODAY)


def test_bootstrap_checks_metadata_before_fanout_and_even_without_unstable_nodes():
    ctx = {"bootstrap": True, "base": None, "current": None}
    with pytest.raises(ValueError, match="CI_UNSTABLE_ISSUE"):
        ratchet.preflight(ctx, smoke=False, limit=80, issue=0, expires_on="")
    with pytest.raises(ValueError, match="CI_UNSTABLE_ISSUE"):
        ratchet.candidate_list([{GREEN: "passed"}] * 2, [], TREE, 0, "", 80, today=TODAY)
    ratchet.preflight(ctx, smoke=True, limit=80, issue=0, expires_on="")


@pytest.mark.parametrize("change", ["extra", "missing", "tree", "not-add-only"])
def test_bootstrap_requires_exact_candidate_and_an_add_only_pr(change):
    runs = [{RED: "failed", GREEN: "passed"}] * 2
    candidate = ratchet.candidate_list(runs, [], TREE, 123, "2026-12-01", 80, today=TODAY)
    assert check(candidate, None, runs, bootstrap_candidate=candidate, bootstrap_add_only=True)
    proposed = deepcopy(candidate)
    if change == "extra":
        proposed["fails"].append(GREEN)
    elif change == "missing":
        proposed["fails"].clear()
    elif change == "tree":
        proposed["tree"] = "b" * 40
    with pytest.raises(ValueError, match="Bootstrap"):
        check(proposed, None, runs, bootstrap_candidate=candidate, bootstrap_add_only=change != "not-add-only")


def test_no_baseline_is_shipped_or_invented_before_bootstrap():
    with pytest.raises(ValueError, match="Bootstrap candidate uploaded"):
        check(None, None, [{GREEN: "passed"}] * 2)


def test_unstable_accepts_pass_or_fail_but_enforces_cap_and_expiry():
    known = listing(unstable=[RED])
    assert check(known, known, [{RED: "passed"}])
    assert check(known, known, [{RED: "failed"}])
    expired = deepcopy(known)
    expired["unstable"][0]["expires_on"] = TODAY.isoformat()
    with pytest.raises(ValueError, match="Expired"):
        check(expired, known, [{RED: "failed"}])
    too_many = listing(unstable=[RED, GREEN])
    with pytest.raises(ValueError, match="cap"):
        check(too_many, too_many, [{RED: "passed", GREEN: "passed"}], limit=1)


def test_two_run_difference_and_seed_membership_define_unstable():
    runs = [{RED: "failed", GREEN: "passed"}, {RED: "passed", GREEN: "passed"}]
    candidate = ratchet.candidate_list(runs, [GREEN, "tests/test_removed.py::test_old"], TREE, 123, "2026-12-01", 80, today=TODAY)
    assert candidate == listing(unstable=[RED, GREEN])
    with pytest.raises(ValueError, match="cap"):
        ratchet.candidate_list(runs, [GREEN], TREE, 123, "2026-12-01", 1, today=TODAY)
    with pytest.raises(ValueError, match="two complete"):
        ratchet.candidate_list(runs[:1], [], TREE, 123, "2026-12-01", 80, today=TODAY)


def test_fails_to_unstable_requires_differing_results_on_two_runs():
    base, current = listing([RED]), listing(unstable=[RED])
    with pytest.raises(ValueError, match="two full runs"):
        check(current, base, [{RED: "failed"}])
    with pytest.raises(ValueError, match="different results"):
        check(current, base, [{RED: "failed"}] * 2)
    assert check(current, base, [{RED: "failed"}, {RED: "passed"}])


def test_parameter_redaction_keeps_siblings_distinct_without_display_labels():
    first = ratchet.canonical_node(RED + "[FixtureGiven FixtureFamily]")
    second = ratchet.canonical_node(RED + "[OtherFixture]")
    assert "Fixture" not in first and first != second
    assert first == ratchet.canonical_node(RED + "[FixtureGiven FixtureFamily]")
    assert ratchet.decode_list(ratchet.encoded(listing([first]))) == listing([first])


@pytest.mark.parametrize("fault", ["issue", "expiry", "duplicate", "raw-parameter"])
def test_invalid_known_file_is_refused(fault):
    value = listing(unstable=[RED])
    if fault == "issue":
        value["unstable"][0]["issue"] = 0
    elif fault == "expiry":
        value["unstable"][0]["expires_on"] = "tomorrow"
    elif fault == "duplicate":
        value["fails"] = [RED]
    else:
        value["unstable"][0]["node"] += "[display label]"
    with pytest.raises(ValueError):
        ratchet.decode_list(ratchet.encoded(value))


def reports(tmp_path):
    plan = {"sha": "b" * 40, "tree": TREE, "replicas": [1, 2], "suites": {
        suite: {"shards": [{"files": [file]}]} for suite, file in (
            ("unit", "tests/test_sample.py"), ("browser", "tests/test_browser_sample.py"))}}
    for suite, row in plan["suites"].items():
        node = row["shards"][0]["files"][0] + "::test_red"
        for replica in plan["replicas"]:
            folder = tmp_path / (suite + str(replica))
            folder.mkdir()
            report = folder / "junit.xml"
            xml = ET.Element("testsuite")
            case = ET.SubElement(xml, "testcase", classname=node.split("::")[0][:-3].replace("/", "."), name="test_red")
            ET.SubElement(case, "failure")
            ET.ElementTree(xml).write(report)
            report.with_suffix(".selection.json").write_text(json.dumps([node]))
            report.with_suffix(".deselected.json").write_text("[]")
            files = [report, report.with_suffix(".selection.json"), report.with_suffix(".deselected.json")]
            receipt = {"schema": 1, "sha": plan["sha"], "tree": TREE, "plan_sha256": ratchet.digest(plan),
                       "suite": suite, "shard": 1, "replica": replica, "run_id": "fixture", "run_attempt": "1",
                       "reports": [{"file": "junit.xml", "exit_code": 1,
                                    "sha256": {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}}]}
            (folder / "shard-receipt.json").write_text(json.dumps(receipt))
    return plan


def test_aggregate_requires_every_source_bound_shard(tmp_path):
    plan = reports(tmp_path)
    runs = ratchet.collect_reports(plan, tmp_path, "fixture", "1")
    assert len(runs) == 2 and len(runs[0]) == 2
    (tmp_path / "browser2/shard-receipt.json").unlink()
    with pytest.raises(ValueError, match="Missing shard"):
        ratchet.collect_reports(plan, tmp_path, "fixture", "1")


@pytest.mark.parametrize("fault", ["wrong-tree", "wrong-run", "changed-junit", "duplicate-shard"])
def test_wrong_or_duplicate_execution_evidence_never_becomes_green(tmp_path, fault):
    plan = reports(tmp_path)
    receipt = tmp_path / "unit1/shard-receipt.json"
    value = json.loads(receipt.read_text())
    if fault == "wrong-tree":
        value["tree"] = "c" * 40
    elif fault == "wrong-run":
        value["run_id"] = "other"
    elif fault == "changed-junit":
        (receipt.parent / "junit.xml").write_text("<testsuite/>")
    else:
        duplicate = tmp_path / "duplicate"
        duplicate.mkdir()
        (duplicate / "shard-receipt.json").write_text(receipt.read_text())
    receipt.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        ratchet.collect_reports(plan, tmp_path, "fixture", "1")


def test_bootstrap_cli_produces_then_accepts_the_exact_add_only_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in (".github/scripts/hosted_ci.py", "ci/make_known_failures.py", "ci/unstable-seeds.json"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    (repo / "ci/unstable-seeds.json").write_text(json.dumps({"schema": 1, "nodes": []}))

    def git(*args):
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", "-c",
                                        "core.hooksPath=" + str(repo / ".git/no-hooks"),
                                        "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=CI Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    base = git("rev-parse", "HEAD")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    plan = reports(evidence)
    plan_path = tmp_path / "plan.json"

    def bind():
        plan.update(sha=git("rev-parse", "HEAD"), tree=git("rev-parse", "HEAD^{tree}"),
                    base_sha=base, smoke=False)
        plan_path.write_text(json.dumps(plan))
        for path in evidence.rglob("shard-receipt.json"):
            receipt = json.loads(path.read_text())
            receipt.update(sha=plan["sha"], tree=plan["tree"], plan_sha256=ratchet.digest(plan))
            path.write_text(json.dumps(receipt))

    bind()
    env = {**os.environ, "GITHUB_RUN_ID": "fixture", "GITHUB_RUN_ATTEMPT": "1",
           "GITHUB_EVENT_NAME": "workflow_dispatch"}
    command = [sys.executable, "ci/make_known_failures.py", "--plan", str(plan_path),
               "--reports", str(evidence), "--base-sha", base, "--unstable-issue", "123",
               "--unstable-expires-on", (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()]
    done = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    verdict = repo / ".run/ratchet/verdict.json"
    assert json.loads(verdict.read_text())["passed"] is False
    candidate = repo / ".run/ratchet/known-failures.candidate.json"
    shutil.copyfile(candidate, repo / "ci/known-failures.json")
    git("add", "ci/known-failures.json")
    git("-c", "user.name=CI Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline")
    bind()
    env["GITHUB_EVENT_NAME"] = "pull_request"
    done = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(verdict.read_text())["passed"] is True
    env["GITHUB_EVENT_NAME"] = "merge_group"
    done = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(verdict.read_text())["passed"] is True
