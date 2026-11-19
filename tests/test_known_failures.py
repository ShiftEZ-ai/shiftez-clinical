"""Known failures never hide missing execution, stale entries or new regressions."""
from copy import deepcopy
from functools import partial
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


ROOT_SEAL = "c" * 64
ROOT_RUNS = [{"run_id": "fixture-root", "run_attempt": "1", "replica": replica} for replica in (1, 2)]
make_candidate = partial(ratchet.candidate_list, cleanup_cap=10, root_evidence_sha256=ROOT_SEAL,
                         root_runs=ROOT_RUNS, cleanup_issue=123)


@pytest.fixture(autouse=True)
def no_ci_event(monkeypatch):
    """Tests choose their event explicitly; the hosted job's own GITHUB_EVENT_NAME must not leak in."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


def listing(fails=(), unstable=(), cleanup=(), cap=10):
    rows = ([{"node": node, "class": "baseline"} for node in fails]
            + [{"node": node, "class": "cleanup", "issue": 123} for node in cleanup])
    return {"schema": 2, "tree": TREE, "root_evidence_sha256": ROOT_SEAL, "root_runs": deepcopy(ROOT_RUNS), "cleanup_cap": cap,
            "fails": sorted(rows, key=lambda row: row["node"]),
            "unstable": [{"node": node, "class": "unstable", "issue": 123, "expires_on": "2026-12-01"}
                         for node in sorted(unstable)]}


def check(current, base, runs, **kwargs):
    return ratchet.evaluate(current, base, runs, today=TODAY, **kwargs)


def test_merge_group_accepts_a_listed_failure_that_remains_red():
    known = listing([RED])
    assert check(known, known, [{RED: "failed", GREEN: "passed"}], group=True)


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
    assert check(known, known, [{RED: "passed"}] * 2, recovered={RED})


def test_vanished_node_must_be_deleted_and_skipped_is_not_passed():
    known = listing([RED])
    with pytest.raises(ValueError, match="Vanished"):
        check(known, known, [{GREEN: "passed"}])
    assert check(listing(), known, [{GREEN: "passed"}])
    other = "tests/test_other.py::test_other"
    selection = {"tests/test_other.py"}
    assert check(known, known, [{other: "passed"}] * 2, selected_files=selection)
    with pytest.raises(ValueError, match="Vanished"):
        check(known, known, [{GREEN: "passed"}] * 2, selected_files={"tests/test_sample.py"})
    with pytest.raises(ValueError, match="New failures"):
        check(known, known, [{other: "failed"}] * 2, selected_files=selection)
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
        make_candidate([{GREEN: "passed"}] * 2, [], TREE, 0, "", 80, today=TODAY)
    ratchet.preflight(ctx, smoke=True, limit=80, issue=0, expires_on="")


@pytest.mark.parametrize("change", ["extra", "missing", "tree", "class", "not-add-only"])
def test_bootstrap_requires_exact_candidate_and_an_add_only_pr(change):
    runs = [{RED: "failed", GREEN: "passed"}] * 2
    candidate = make_candidate(runs, [RED], TREE, 123, "2026-12-01", 80, today=TODAY)
    assert check(candidate, None, runs, bootstrap_candidate=candidate, bootstrap_add_only=True)
    proposed = deepcopy(candidate)
    if change == "extra":
        proposed["fails"].append({"node": GREEN, "class": "baseline"})
    elif change == "missing":
        proposed["fails"].clear()
    elif change == "tree":
        proposed["tree"] = "b" * 40
    elif change == "class":
        proposed["fails"][0].update({"class": "cleanup", "issue": 123})
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
    candidate = make_candidate(runs, [GREEN, "tests/test_removed.py::test_old"], TREE, 123, "2026-12-01", 80, today=TODAY)
    assert candidate == listing(unstable=[RED])
    two_unstable = [runs[0], {RED: "passed", GREEN: "failed"}]
    with pytest.raises(ValueError, match="cap"):
        make_candidate(two_unstable, [], TREE, 123, "2026-12-01", 1, today=TODAY)
    with pytest.raises(ValueError, match="two complete"):
        make_candidate(runs[:1], [], TREE, 123, "2026-12-01", 80, today=TODAY)


def test_bootstrap_classifies_baseline_and_cleanup_failures_honestly():
    runs = [{RED: "failed", GREEN: "passed"}] * 2
    candidate = make_candidate(runs, {RED}, TREE, 123, "2026-12-01", 80, today=TODAY)
    assert candidate == listing([RED])
    cleanup = make_candidate(runs, set(), TREE, 123, "2026-12-01", 80, today=TODAY)
    assert cleanup == listing(cleanup=[RED])
    with pytest.raises(ValueError, match="CI_CLEANUP_ISSUE"):
        make_candidate(runs, set(), TREE, 123, "2026-12-01", 80, cleanup_issue=0, today=TODAY)
    with pytest.raises(ValueError, match="first two root runs"):
        make_candidate(runs, set(), TREE, 123, "2026-12-01", 80, cleanup_cap=0, today=TODAY)


def test_frozen_baseline_is_required_safe_and_hash_bound(tmp_path):
    path = tmp_path / "frozen-baseline-failures.json"
    with pytest.raises(ValueError, match="Missing frozen baseline"):
        ratchet.load_frozen_baseline(path)
    with pytest.raises(ValueError, match="Missing frozen baseline"):
        ratchet.preflight({"bootstrap": True, "base": None, "current": None}, smoke=False, limit=80,
                          issue=123, expires_on=(datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat(),
                          baseline_path=path)
    raw = json.dumps([RED]).encode()
    path.write_bytes(raw)
    assert ratchet.load_frozen_baseline(path) == ({RED}, hashlib.sha256(raw).hexdigest())
    path.write_text(json.dumps([RED + "[display label]"]))
    with pytest.raises(ValueError, match="anonymous node IDs"):
        ratchet.load_frozen_baseline(path)


@pytest.mark.parametrize("fault", [None, "missing-evidence", "missing-review", "missing-reason", "bad-evidence"])
def test_environment_exception_needs_reason_evidence_and_review(tmp_path, fault):
    entry = {"node": RED, "reason": "Documented platform difference",
             "evidence": "sha256:" + "a" * 64, "review": "https://example.org/review/1"}
    if fault == "bad-evidence":
        entry["evidence"] = "unverified"
    elif fault:
        entry.pop(fault.removeprefix("missing-"))
    path = tmp_path / "hosted-env-exceptions.json"
    path.write_text(json.dumps({"schema": 1, "exceptions": [entry]}))
    if fault:
        with pytest.raises(ValueError, match="reason, evidence and review"):
            ratchet.load_env_exceptions(path)
        return
    exceptions, _ = ratchet.load_env_exceptions(path)
    runs = [{RED: "failed"}] * 2
    candidate = make_candidate(runs, set(), TREE, 123, "2026-12-01", 80,
                                      exceptions=exceptions, today=TODAY)
    assert candidate == listing()
    assert check(candidate, None, runs, bootstrap_candidate=candidate,
                 bootstrap_add_only=True, exceptions=exceptions)
    with pytest.raises(ValueError, match="New failures"):
        check(candidate, None, runs, bootstrap_candidate=candidate, bootstrap_add_only=True)


def test_fails_to_unstable_requires_differing_results_on_two_runs():
    base, current = listing([RED], unstable=[GREEN]), listing(unstable=[RED])
    with pytest.raises(ValueError, match="two full runs"):
        check(current, base, [{RED: "failed"}])
    with pytest.raises(ValueError, match="different results"):
        check(current, base, [{RED: "failed"}] * 2)
    assert check(current, base, [{RED: "failed"}, {RED: "passed"}])
    assert check(current, base, [{RED: "passed"}], group=True, merge_group=True, recovered={RED: "failed"})
    with pytest.raises(ValueError, match="verified recovery on retry"):
        check(current, base, [{RED: "passed"}], group=True, merge_group=True)


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


def reports(tmp_path, *, replicas=(1, 2), red_suites=("unit", "browser")):
    plan = {"sha": "b" * 40, "tree": TREE, "replicas": list(replicas), "suites": {
        suite: {"shards": [{"files": [file]}]} for suite, file in (
            ("unit", "tests/test_sample.py"), ("browser", "tests/test_browser_sample.py"))}}
    for suite, row in plan["suites"].items():
        node = row["shards"][0]["files"][0] + "::test_red"
        for replica in plan["replicas"]:
            folder = tmp_path / f"ci-test-1-{suite}-1-{replica}"
            folder.mkdir(parents=True)
            report = folder / "junit.xml"
            xml = ET.Element("testsuite")
            case = ET.SubElement(xml, "testcase", classname=node.split("::")[0][:-3].replace("/", "."), name="test_red")
            if suite in red_suites:
                ET.SubElement(case, "failure")
            ET.ElementTree(xml).write(report)
            report.with_suffix(".selection.json").write_text(json.dumps([node]))
            report.with_suffix(".deselected.json").write_text("[]")
            files = [report, report.with_suffix(".selection.json"), report.with_suffix(".deselected.json")]
            receipt = {"schema": 1, "sha": plan["sha"], "tree": TREE, "plan_sha256": ratchet.digest(plan),
                       "suite": suite, "shard": 1, "replica": replica, "run_id": "fixture", "run_attempt": "1",
                       "reports": [{"file": "junit.xml", "exit_code": int(suite in red_suites),
                                    "sha256": {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}}]}
            (folder / "shard-receipt.json").write_text(json.dumps(receipt))
    return plan


def test_aggregate_requires_every_source_bound_shard(tmp_path, monkeypatch):
    plan = reports(tmp_path)
    collect = partial(ratchet.collect_reports, plan, tmp_path, "fixture", "1")
    runs = collect()
    assert len(runs) == 2 and len(runs[0]) == 2
    known = listing([next(node for node in runs[0] if node != RED)])
    with pytest.raises(ValueError, match="New failures"):
        check(known, known, runs)
    for red in ("failed", "error"):
        split = [{**runs[0], RED: "passed"}, {**runs[1], RED: red}]
        assert check(known, known, split, product_errors={RED})
        reason = "New errors" if red == "error" else "New failures"
        with pytest.raises(ValueError, match=reason):
            check(known, known, split, group=True, recovered={RED: red}, product_errors={RED})

    (tmp_path / "ci-test-1-browser-1-2/shard-receipt.json").unlink()
    with pytest.raises(ValueError, match="Missing shard"):
        collect()

    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    monkeypatch.setattr(ratchet, "at_revision", lambda revision: listing())
    group = tmp_path / "group"
    group_plan = reports(group, replicas=(1,), red_suites=("unit",))
    output = tmp_path / "verdict"
    group_collect = partial(ratchet.collect_reports, group_plan, group, "fixture", "2", output)
    with pytest.raises(ValueError, match="Missing merge-group retry evidence"):
        group_collect()
    without_retry = group_collect(listed={RED})
    assert without_retry[0][RED] == "failed"
    assert check(listing([RED]), listing([RED]), without_retry, group=True)
    folder = group / "ci-test-1-unit-1-1"
    receipt = folder / "shard-receipt.json"
    evidence = json.loads(receipt.read_text())
    retry = folder / "junit-retry.xml"
    selected = retry.with_suffix(".selection.json")
    deselected = retry.with_suffix(".deselected.json")
    deselected.write_text("[]")
    for name, outcome in (("test_green", "passed"), ("test_red", "failed"), ("test_red", "skipped"),
                          ("test_red", "error"), ("test_red", "passed")):
        xml = ET.Element("testsuite")
        case = ET.SubElement(xml, "testcase", classname="tests.test_sample", name=name)
        if outcome != "passed":
            tag = "failure" if outcome == "failed" else outcome
            error = ET.SubElement(case, tag)
            if outcome == "error":
                error.set("message", "error collecting synthetic test")
        ET.ElementTree(xml).write(retry)
        selected.write_text(json.dumps(["tests/test_sample.py::" + name]))
        evidence["reports"][0]["retry"] = {
            "file": retry.name, "exit_code": int(outcome in {"failed", "error"}),
            "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (retry, selected, deselected)},
        }
        receipt.write_text(json.dumps(evidence))
        if name != "test_red":
            with pytest.raises(ValueError, match="exactly"):
                group_collect()
        elif outcome == "error":
            with pytest.raises(ValueError, match="Collection error"):
                group_collect()
        elif outcome == "passed":
            with pytest.raises(ValueError, match="exactly the unlisted"):
                group_collect(listed={RED})
            updated = group_collect()
            assert len(updated) == 1 and all(value == "passed" for value in updated[0].values())
            assert json.loads((output / "recovered-tests.json").read_text()) == [
                {"node": RED, "class": "unstable", "observations": ["failed", "passed"]},
            ]
            recovered = {}
            ratchet.product_error_nodes(group, recovered=recovered, allow_retry=True)
            assert recovered == {RED: "failed"}
            assert check(listing(), listing(), updated, group=True, recovered=recovered)
            summary = output / "summary.md"
            monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
            proposal, = ratchet.report_outside(known, updated, output, recovered=recovered)
            assert proposal["node"] == RED and proposal["observations"] == ["failed", "passed"]
            assert proposal["class"] == "unstable"
            observed = json.loads((output / "outside-observations.json").read_text())
            assert observed["proposed_list_entries"] == [proposal]
            assert "recovered on retry" in summary.read_text()
            ratchet.report_outside(listing([RED]), updated, output, recovered=recovered)
            observed = json.loads((output / "outside-observations.json").read_text())
            assert observed["listed_unstable"][0]["observations"] == ["failed", "passed"]
            assert observed["listed_unstable"][0]["status"] == "PROPOSED_REMARK"
            with monkeypatch.context() as other:
                for event in ("push", "schedule"):
                    other.setenv("GITHUB_EVENT_NAME", event)
                    with pytest.raises(ValueError, match="Unexpected retry evidence"):
                        group_collect()
                other.setenv("GITHUB_EVENT_NAME", "pull_request")
                with pytest.raises(ValueError, match="Unexpected retry evidence"):
                    ratchet.recovered_nodes(retry.with_name("junit.xml"), evidence["reports"][0], [])
        else:
            with pytest.raises(ValueError, match="New failures outside the known-failure list"):
                check(known, known, group_collect(), group=True)

    original = deepcopy(evidence)
    evidence["reports"][0]["retry"]["sha256"].pop(retry.name)
    receipt.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="Missing or changed shard evidence"):
        group_collect()
    receipt.write_text(json.dumps(original))
    (group / "ci-test-1-browser-1-1/shard-receipt.json").unlink()
    with pytest.raises(ValueError, match="Missing shard"):
        group_collect()

    clean = tmp_path / "clean"
    clean_plan = reports(clean, replicas=(1,), red_suites=())
    assert len(ratchet.collect_reports(clean_plan, clean, "fixture", "1")) == 1


def configure_pr_verdict(tmp_path, reports_dir, monkeypatch, plan, current, *, attempt=1):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    ctx = {"base_sha": plan["base_sha"], "replicas": [1], "bootstrap": False, "base": current, "current": current}
    output = tmp_path / "verdict"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", str(attempt))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["make_known_failures.py", "--plan", str(plan_path), "--reports", str(reports_dir),
                                     "--output", str(output)])
    monkeypatch.setattr(ratchet, "git", lambda *args: {("rev-parse", "HEAD"): plan["sha"],
                                                       ("rev-parse", "HEAD^{tree}"): plan["tree"]}.get(args, ""))
    monkeypatch.setattr(ratchet, "context", lambda _: ctx)
    monkeypatch.setattr(ratchet, "at_revision", lambda _: current)
    monkeypatch.setattr(ratchet, "preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(ratchet, "load_frozen_baseline", lambda _: (set(), "fixture"))
    monkeypatch.setattr(ratchet, "load_env_exceptions", lambda _: (set(), "fixture"))
    monkeypatch.setattr(ratchet, "load_root_evidence", lambda *_: ({"runs": ROOT_RUNS, "counts": {"cleanup": 10}}, ROOT_SEAL))
    return output, summary


@pytest.mark.parametrize("outcome", ["passed", "failed", None])
def test_pr_verdict_uses_verified_retry(tmp_path, monkeypatch, outcome):
    plan = reports(tmp_path, replicas=(1,), red_suites=("unit",))
    plan.update(smoke=False, base_sha="c" * 40)
    for receipt in tmp_path.rglob("shard-receipt.json"):
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["plan_sha256"] = ratchet.digest(plan)
        if data["suite"] == "unit" and outcome is not None:
            retry = receipt.with_name("junit-retry.xml")
            xml = ET.Element("testsuite")
            case = ET.SubElement(xml, "testcase", classname="tests.test_sample", name="test_red")
            if outcome == "failed":
                ET.SubElement(case, "failure")
            ET.ElementTree(xml).write(retry)
            selected = retry.with_suffix(".selection.json")
            deselected = retry.with_suffix(".deselected.json")
            selected.write_text(json.dumps([RED]), encoding="utf-8")
            deselected.write_text("[]", encoding="utf-8")
            data["reports"][0]["retry"] = {
                "file": retry.name, "exit_code": int(outcome == "failed"),
                "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (retry, selected, deselected)},
            }
        receipt.write_text(json.dumps(data), encoding="utf-8")

    output, summary = configure_pr_verdict(tmp_path, tmp_path, monkeypatch, plan, listing())
    if outcome is None:
        with pytest.raises(ValueError, match="Missing pull-request retry evidence"):
            ratchet.main()
        assert not (output / "verdict.json").exists()
        return

    ratchet.main()
    verdict = json.loads((output / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["passed"] is (outcome == "passed")
    outside = json.loads((output / "outside-observations.json").read_text(encoding="utf-8"))
    if outcome == "failed":
        assert "New failures outside the known-failure list" in verdict["reason"]
        assert outside["proposed_list_entries"] == []
    else:
        proposal, = outside["proposed_list_entries"]
        assert proposal == {"node": RED, "test_file": "tests/test_sample.py", "observations": ["failed", "passed"],
                            "red_count": 1, "class": "unstable", "status": "PROPOSED"}
        assert "recovered on retry" in summary.read_text(encoding="utf-8")


def test_pr_verdict_reports_rerun_recovery_for_a_listed_node(tmp_path, monkeypatch):
    from latest_reports import keep_latest

    reports_dir = tmp_path / "reports"
    plan = reports(reports_dir, replicas=(1,), red_suites=("unit",))
    plan.update(smoke=False, base_sha="c" * 40)
    newer = tmp_path / "newer"
    reports(newer, replicas=(1,), red_suites=())
    for artifact in newer.iterdir():
        artifact.rename(reports_dir / artifact.name.replace("ci-test-1-", "ci-test-2-", 1))
    for receipt in reports_dir.rglob("shard-receipt.json"):
        data = json.loads(receipt.read_bytes())
        data["plan_sha256"] = ratchet.digest(plan)
        data["run_attempt"] = "2" if receipt.parent.name.startswith("ci-test-2-") else "1"
        receipt.write_text(json.dumps(data), encoding="utf-8")
    keep_latest(reports_dir, plan)
    output, summary = configure_pr_verdict(tmp_path, reports_dir, monkeypatch, plan, listing([RED]), attempt=2)
    ratchet.main()
    assert json.loads((output / "verdict.json").read_bytes())["passed"]
    remark, = json.loads((output / "outside-observations.json").read_bytes())["listed_unstable"]
    assert remark["node"] == RED and remark["status"] == "PROPOSED_REMARK"
    assert "recovered on re-run (attempt 1)" in summary.read_text(encoding="utf-8")


def test_pr_retry_cannot_move_a_baseline_entry_to_unstable(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    base, current = listing([RED], unstable=[GREEN]), listing(unstable=[RED])
    with pytest.raises(ValueError, match="two full runs with different results"):
        check(current, base, [{RED: "passed"}], group=ratchet.retry_run({"replicas": [1]}),
              recovered={RED: "failed"})


@pytest.mark.parametrize(("replicas", "smoke"), [([1, 2], False), ([1], True)])
def test_ineligible_pr_plan_refuses_retry_receipts(tmp_path, monkeypatch, replicas, smoke):
    from canary_evidence import verify_directory

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    plan = {"replicas": replicas, "smoke": smoke}
    receipt = {"reports": [{"file": "junit.xml", "retry": {"file": "junit-retry.xml", "exit_code": 0, "sha256": {}}}]}
    (tmp_path / "shard-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected retry evidence"):
        verify_directory(tmp_path, allow_retry=ratchet.retry_run(plan))


@pytest.mark.parametrize("fault", ["wrong-tree", "wrong-run", "changed-junit", "duplicate-shard"])
def test_wrong_or_duplicate_execution_evidence_never_becomes_green(tmp_path, fault):
    plan = reports(tmp_path)
    receipt = tmp_path / "ci-test-1-unit-1-1/shard-receipt.json"
    value = json.loads(receipt.read_text())
    if fault == "wrong-tree":
        value["tree"] = "c" * 40
    elif fault == "wrong-run":
        value["run_id"] = "other"
    elif fault == "changed-junit":
        (receipt.parent / "junit.xml").write_text("<testsuite/>")
    else:
        duplicate = tmp_path / "ci-test-1-unit-1-1/reports"
        duplicate.mkdir()
        (duplicate / "shard-receipt.json").write_text(receipt.read_text())
    receipt.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        ratchet.collect_reports(plan, tmp_path, "fixture", "1")


def test_cleanup_to_baseline_is_refused_and_cleanup_cap_only_falls():
    with pytest.raises(ValueError, match="cleanup entries to baseline"):
        check(listing([RED]), listing(cleanup=[RED]), [{RED: "failed"}])
    with pytest.raises(ValueError, match="cap may only go down"):
        check(listing(cleanup=[RED], cap=11), listing(cleanup=[RED]), [{RED: "failed"}])
    assert check(listing(cap=0), listing(cleanup=[RED]), [{RED: "passed"}])


@pytest.mark.parametrize("kind", ["baseline", "cleanup", "unstable"])
def test_every_individual_class_count_increase_is_refused(kind):
    current = listing([RED] if kind == "baseline" else (), [RED] if kind == "unstable" else (),
                      [RED] if kind == "cleanup" else ())
    base = listing([RED]) if kind == "unstable" else listing(unstable=[RED])
    with pytest.raises(ValueError, match="class count"):
        check(current, base, [{RED: "failed"}])


@pytest.mark.parametrize("fault", ["missing", "one-run", "duplicate", "empty-id"])
def test_list_requires_two_valid_root_run_identities(fault):
    data = listing()
    if fault == "missing":
        data.pop("root_runs")
    elif fault == "one-run":
        data["root_runs"].pop()
    elif fault == "duplicate":
        data["root_runs"][1] = deepcopy(data["root_runs"][0])
    else:
        data["root_runs"][0]["run_id"] = ""
    with pytest.raises(ValueError):
        ratchet.decode_list(ratchet.encoded(data))


@pytest.mark.parametrize("fault", [None, "reason", "node_count", "synthetic_version_owed"])
def test_removed_test_ledger_records_count_reason_and_replacement_obligation(tmp_path, fault):
    row = {"file": "tests/test_removed.py", "node_count": 3, "reason": "Replaced private fixtures", "synthetic_version_owed": True}
    if fault:
        row.pop(fault)
    path = tmp_path / "removed-tests.json"
    path.write_text(json.dumps({"schema": 1, "removed": [row]}))
    if fault:
        with pytest.raises(ValueError, match="Removed tests require"):
            ratchet.load_removed_tests(path)
    else:
        assert ratchet.load_removed_tests(path)[0] == [row]
        row["synthetic_version_owed"] = False
        path.write_text(json.dumps({"schema": 1, "removed": [row]}))
        assert ratchet.load_removed_tests(path)[0] == [row]


def test_every_result_summary_prints_all_class_counts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ci").mkdir()
    (tmp_path / ratchet.KNOWN).write_text(ratchet.encoded(listing([RED], [GREEN], ["tests/test_sample.py::test_cleanup"])))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert ratchet.write_summary() == {"baseline": 1, "cleanup": 1, "unstable": 1, "baseline-error": 0}
    text = summary.read_text()
    assert all(kind + "=1" in text for kind in ("baseline", "cleanup", "unstable"))
    (tmp_path / ratchet.KNOWN).unlink()
    assert set(ratchet.write_summary().values()) == {"UNKNOWN"}


def test_deleted_test_file_requires_a_removal_ledger_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(ratchet, "load_frozen_baseline", lambda _: (set(), "a" * 64))
    monkeypatch.setattr(ratchet, "load_env_exceptions", lambda _: (set(), "b" * 64))
    monkeypatch.setattr(ratchet, "load_root_evidence", lambda *args: ({"runs": ROOT_RUNS, "counts": {"cleanup": 10}}, ROOT_SEAL))
    monkeypatch.setattr(ratchet, "git", lambda *args: "tests/test_removed.py")
    path = tmp_path / "removed-tests.json"
    path.write_text(json.dumps({"schema": 1, "removed": []}))
    known = listing()
    ctx = {"bootstrap": False, "base": known, "current": known, "base_sha": "a" * 40}
    with pytest.raises(ValueError, match="Deleted test files require"):
        ratchet.preflight(ctx, smoke=False, limit=80, issue=0, expires_on="", removed_path=path)
    path.write_text(json.dumps({"schema": 1, "removed": [{"file": "tests/test_removed.py", "node_count": 1,
                    "reason": "Replaced private fixtures", "synthetic_version_owed": True}]}))
    ratchet.preflight(ctx, smoke=False, limit=80, issue=0, expires_on="", removed_path=path)


def test_bootstrap_class_is_checked_against_the_exported_baseline_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ratchet, "load_env_exceptions", lambda _: (set(), "b" * 64))
    monkeypatch.setattr(ratchet, "load_root_evidence", lambda *args: ({"runs": ROOT_RUNS, "counts": {"cleanup": 10}}, ROOT_SEAL))
    monkeypatch.setattr(ratchet, "git", lambda *args: "")
    baseline = tmp_path / "frozen-baseline-failures.json"
    baseline.write_text("[]")
    removed = tmp_path / "removed-tests.json"
    removed.write_text(json.dumps({"schema": 1, "removed": []}))
    ctx = {"bootstrap": True, "base": None, "current": listing([RED]), "base_sha": "a" * 40}
    args = {"smoke": False, "limit": 80, "issue": 123,
            "expires_on": (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat(),
            "baseline_path": baseline, "removed_path": removed}
    with pytest.raises(ValueError, match="class disagrees with the frozen baseline"):
        ratchet.preflight(ctx, **args)
    baseline.write_text(json.dumps([RED]))
    ratchet.preflight(ctx, **args)


def test_bootstrap_cli_produces_then_accepts_the_exact_add_only_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in (".github/scripts/hosted_ci.py", "ci/make_known_failures.py", ".github/scripts/canary_evidence.py",
                 ".github/scripts/node_census.py", ".github/scripts/latest_reports.py",
                 "ci/hosted-env-exceptions.json", "ci/removed-tests.json"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    baseline_path = repo / "ci/frozen-baseline-failures.json"
    baseline_path.write_text(json.dumps([RED]))

    def git(*args):
        return subprocess.check_output(["git", "-c", "commit.gpgsign=false", "-c",
                                        "core.hooksPath=" + str(repo / ".git/no-hooks"),
                                        "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(message):
        git("-c", "user.name=CI Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)

    git("init", "-q")
    git("add", ".")
    commit("fixture root")
    base = git("rev-parse", "HEAD")
    root_sha = base
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

    env = {**os.environ, "GITHUB_RUN_ID": "fixture", "GITHUB_RUN_ATTEMPT": "1",
           "GITHUB_EVENT_NAME": "workflow_dispatch"}
    command = [sys.executable, "ci/make_known_failures.py", "--plan", str(plan_path),
               "--reports", str(evidence), "--unstable-issue", "123", "--cleanup-issue", "456",
               "--unstable-expires-on", (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()]

    def run(*extra):
        done = subprocess.run([*command, "--base-sha", base, *extra], cwd=repo, env=env,
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stdout + done.stderr
        return done

    root_base_sha = base
    (repo / "pipeline.txt").write_text("prepared controls\n")
    git("add", "pipeline.txt")
    commit("fixture PR1")
    root_sha = git("rev-parse", "HEAD")
    bind()
    runs = ratchet.collect_reports(plan, evidence, "fixture", "1")
    groups = ratchet.classified_results(runs, {RED}, set())
    root_data = {"schema": 1, "sha": root_sha, "tree": plan["tree"], "base_sha": root_base_sha,
                 "baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
                 "runs": [{"run_id": "fixture", "run_attempt": "1", "replica": replica}
                          for replica in plan["replicas"]],
                 "counts": {kind: len(nodes) for kind, nodes in groups.items()},
                 "receipts_sha256": ratchet.digest(sorted(hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in evidence.rglob("shard-receipt.json")))}
    root_input = repo / "ci/root-run-evidence.json"
    root_input.write_text(ratchet.encoded(root_data))
    assert root_data["sha"] == root_sha
    assert root_data["counts"] == {"baseline": 1, "cleanup": 1, "unstable": 0, "baseline-error": 0}
    assert [row["replica"] for row in root_data["runs"]] == [1, 2]
    git("add", "ci/root-run-evidence.json")
    commit("root run evidence")
    base = git("rev-parse", "HEAD")
    bind()
    run()
    verdict = repo / ".run/ratchet/verdict.json"
    assert json.loads(verdict.read_text())["passed"] is False
    candidate = repo / ".run/ratchet/known-failures.candidate.json"
    proposed = json.loads(candidate.read_text())
    assert proposed["cleanup_cap"] == 1
    assert proposed["root_runs"] == root_data["runs"]
    assert proposed["root_evidence_sha256"] == hashlib.sha256(root_input.read_bytes()).hexdigest()
    assert ratchet.classes(proposed) == {"baseline": {RED}, "cleanup": {"tests/test_browser_sample.py::test_red"}, "unstable": set(), "baseline-error": set()}
    shutil.copyfile(candidate, repo / ratchet.KNOWN)
    git("add", ratchet.KNOWN)
    commit("classified baseline")
    bind()
    env["GITHUB_EVENT_NAME"] = "pull_request"
    run()
    assert json.loads(verdict.read_text())["passed"] is True
    comparison = json.loads((repo / ".run/ratchet/baseline-comparison.json").read_text())
    assert comparison["baseline_sha256"] == hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    original_root = root_input.read_bytes()
    inflated = json.loads(original_root)
    inflated["counts"]["cleanup"] += 1
    root_input.write_text(json.dumps(inflated))
    done = subprocess.run([*command, "--base-sha", base], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode != 0 and "sealed root evidence" in done.stderr
    root_input.write_bytes(original_root)
    baseline_path.unlink()
    done = subprocess.run([*command, "--base-sha", base], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode != 0 and "Missing frozen baseline" in done.stderr
    baseline_path.write_text(json.dumps([RED]))
    env["GITHUB_EVENT_NAME"] = "schedule"
    base = git("rev-parse", "HEAD")
    bind()
    run()
    assert json.loads(verdict.read_text())["passed"] is True
    env["GITHUB_EVENT_NAME"] = "merge_group"
    done = subprocess.run([*command, "--base-sha", base], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode != 0 and "context differs" in done.stderr
