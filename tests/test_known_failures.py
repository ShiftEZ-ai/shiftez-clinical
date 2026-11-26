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
from types import ModuleType
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import make_known_failures as ratchet
from quarantine_registry import decode_registry

RED = "tests/test_sample.py::test_red"
GREEN = "tests/test_sample.py::test_green"
TODAY = date(2026, 11, 16)
TREE = "a" * 40
# Pin the accepted dev parent so both verifiers use the same cross-attempt baseline.
PRE_QUARANTINE_DEV = "6c679904a82ec9f9c8b5fcf6e9796d405d3a01bf"


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


def reviewed_quarantine(*, blocker=False):
    return decode_registry(json.dumps([{"nodeid": RED, "reason": "baseline: reviewed", "owner": "maintainers",
                            "added": "2026-11-01", "expires": TODAY.isoformat(), "issue": 123,
                            "blocker": blocker}]), TODAY)


@pytest.mark.parametrize("outcome", ["failed", "error", "passed"])
def test_moved_quarantine_runs_and_reports_passing_without_refusal(outcome, capsys):
    assert check(listing(), listing([RED]), [{RED: outcome}], quarantine=reviewed_quarantine(), expected_replicas=1)
    summary = capsys.readouterr().out
    assert f"quarantined and passing now: {int(outcome == 'passed')}" in summary
    assert "expired quarantine (still active): 1" in summary
    assert f"{RED}: expired {TODAY}, issue 123" in summary


@pytest.mark.parametrize("outcome", ["failed", "error"])
def test_quarantine_does_not_allow_new_red_nodes(outcome):
    with pytest.raises(ValueError, match=r"New (failures|errors).*test_green"):
        check(listing(), listing([RED]), [{RED: "failed", GREEN: outcome}],
              quarantine=reviewed_quarantine(), expected_replicas=1)


def test_recovered_unquarantined_failure_is_still_proposed_as_unstable(tmp_path, capsys):
    runs = [{RED: "failed", GREEN: "passed"}]
    quarantine = reviewed_quarantine()
    assert check(listing(), listing([RED]), runs, quarantine=quarantine, group=True, recovered={GREEN})
    proposed = ratchet.report_outside(listing(), runs, tmp_path, recovered={GREEN: "failed"}, quarantined=quarantine)
    assert [(row["node"], row["class"], row["status"], row["observations"]) for row in proposed] == [
        (GREEN, "unstable", "PROPOSED", ["failed", "passed"])]
    assert json.loads((tmp_path / "outside-observations.json").read_bytes())["observations"] == [
        {key: value for key, value in proposed[0].items() if key not in {"class", "status"}}]
    assert "recovered on retry" in capsys.readouterr().out


def test_main_refuses_reviewed_nonblocking_quarantine_errors():
    import known_skips

    quarantine = reviewed_quarantine()
    runs, _ = known_skips.comparison_runs([{RED: "error"}], set(), release=False, quarantined=quarantine)
    assert check(listing(), listing([RED]), runs, quarantine=quarantine, expected_replicas=1)
    with pytest.raises(ValueError, match="Release/known-skip execution contains a failure or error"):
        known_skips.comparison_runs(runs, set(), release=True, quarantined=quarantine)


class PolicyClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.combine(TODAY, datetime.min.time(), tzinfo=tz)


# The audited first pipeline change is reachable only through its pull-request ref, which no CI checkout fetches (the
# live verdict fetches it by SHA first). Its two history facts are answered from the values verified at that commit.
ROOT_RUN_SHA, ROOT_RUN_BASE = "39fb3153b4556209265f3c41801067642f682e8a", "e3aa8fab6863bcc8ded33727c5f536a884d5ed27"
ROOT_RUN_FACTS = {("merge-base", ROOT_RUN_BASE, ROOT_RUN_SHA): ROOT_RUN_BASE.encode() + b"\n",
                  ("rev-parse", ROOT_RUN_SHA + "^{tree}"): b"1067e0fe84d4d34bfca9215a64ac4a6d25030aff\n"}


def historical_git(*args):
    if args in ROOT_RUN_FACTS:
        return ROOT_RUN_FACTS[args]
    pinned = [PRE_QUARANTINE_DEV + arg[4:] if arg == "HEAD" or arg.startswith(("HEAD:", "HEAD^")) else arg for arg in args]
    return subprocess.check_output(["git", *pinned], cwd=ROOT)


def historical_module(path, source):
    module = ModuleType(Path(path).stem)
    module.__file__ = str(ROOT / path)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("new_failure", [False, True])
def test_empty_quarantine_verdict_matches_pre_quarantine_dev(monkeypatch, tmp_path, capsys, new_failure):
    git = historical_git
    monkeypatch.setattr(sys, "path", sys.path.copy())
    old = historical_module("ci/make_known_failures.py", git("show", "HEAD:ci/make_known_failures.py"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ci").mkdir()
    for name in ("known-failures", "frozen-baseline-failures", "hosted-env-exceptions",
                 "root-run-evidence", "failure-classifications", "removed-tests"):
        path = f"ci/{name}.json"
        Path(path).write_bytes(git("show", "HEAD:" + path))
    current = ratchet.decode_list(Path(ratchet.KNOWN).read_text(encoding="utf-8"))
    runs = [{row["node"]: "error" if row["class"] == "baseline-error" else "failed"
             for row in current["fails"] + current["unstable"]}]
    runs[0][RED] = "passed"  # Verified recovery is already normalized in the collector.
    runs[0][GREEN] = "failed" if new_failure else "passed"
    plan = {"sha": PRE_QUARANTINE_DEV, "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "base_sha": PRE_QUARANTINE_DEV, "replicas": [1], "smoke": False}
    Path("plan.json").write_text(json.dumps(plan), encoding="utf-8")
    quarantine = decode_registry(b"[]", TODAY)

    def collect_reports(*_, evidence):
        evidence.recovered[RED] = "failed"
        evidence.reruns[RED] = 1
        return runs

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(ratchet, "utc_today", lambda: TODAY)

    def result(verifier, path, **options):
        summary = path.with_suffix(".md")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.setattr(sys, "argv", ["verifier", "--plan", "plan.json", "--reports", str(path),
                                         "--output", str(path), "--base-sha", PRE_QUARANTINE_DEV])
        monkeypatch.setattr(verifier, "git", lambda *args: git(*args).decode().strip())
        monkeypatch.setattr(verifier, "datetime", PolicyClock)
        monkeypatch.setattr(verifier, "collect_reports", collect_reports)
        monkeypatch.setattr(verifier, "product_error_nodes", lambda *_, **__: set())
        verifier.main(**options)
        verifier.write_summary()
        captured = capsys.readouterr()
        return captured.out, captured.err, summary.read_bytes(), {p.name: p.read_bytes() for p in path.iterdir()}

    expected = result(old, tmp_path / "old")
    actual = result(ratchet, tmp_path / "new", quarantine=quarantine)
    assert actual == expected
    assert json.loads(actual[3]["verdict.json"])["passed"] is not new_failure
    proposals = json.loads(actual[3]["outside-observations.json"])["proposed_list_entries"]
    assert [row["node"] for row in proposals] == [RED]


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


def reports(tmp_path, *, replicas=(1, 2), red_suites=("unit", "browser"), unit_node=RED,
            unit_outcome=None, unit_retry=None, base_sha=None, run_id="fixture", browser_shards=1):
    browser_files = ["tests/test_browser_sample.py"]
    browser_files.extend(f"tests/test_browser_sample_{index}.py" for index in range(1, browser_shards))
    plan = {"sha": "b" * 40, "tree": TREE, "replicas": list(replicas), "suites": {
        "unit": {"shards": [{"files": [unit_node.split("::")[0]]}]},
        "browser": {"shards": [{"files": [file]} for file in browser_files]},
    }}
    if base_sha is not None:
        plan.update(base_sha=base_sha, smoke=False)

    def invocation(report, node, outcome):
        if isinstance(outcome, Path):
            shutil.copyfile(outcome, report)
            red = any(case.find("failure") is not None or case.find("error") is not None
                      for case in ET.parse(report).getroot().iter("testcase"))
        else:
            red = outcome in ("failed", "error")
            file, name = node.split("::")
            xml = ET.Element("testsuite")
            case = ET.SubElement(xml, "testcase", classname=file[:-3].replace("/", "."), name=name)
            if outcome == "failed":
                ET.SubElement(case, "failure")
            elif outcome == "error":
                ET.SubElement(case, "error", message="ValueError").text = file + ":1"
            elif outcome == "skipped":
                ET.SubElement(case, "skipped", type="pytest.skip", message="QUARANTINED (fixture)")
            elif outcome in ("quarantine-xfail", "ordinary-xfail"):
                reason = "QUARANTINED (maintainers, expires 2026-12-19): baseline: Reviewed"
                ET.SubElement(case, "skipped", type="pytest.xfail",
                              message=reason if outcome == "quarantine-xfail" else "Expected by the test")
            ET.ElementTree(xml).write(report)
        selected = report.with_suffix(".selection.json")
        deselected = report.with_suffix(".deselected.json")
        selected.write_text(json.dumps([node]))
        deselected.write_text("[]")
        return {"file": report.name, "exit_code": int(red),
                "sha256": {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                           for file in (report, selected, deselected)}}

    for suite, layout in plan["suites"].items():
        outcome = unit_outcome if suite == "unit" and unit_outcome else ("failed" if suite in red_suites else "passed")
        for shard, row in enumerate(layout["shards"], 1):
            node = unit_node if suite == "unit" else row["files"][0] + "::test_red"
            for replica in plan["replicas"]:
                folder = tmp_path / f"ci-test-1-{suite}-{shard}-{replica}"
                folder.mkdir(parents=True)
                evidence = invocation(folder / "junit.xml", node, outcome)
                if suite == "unit" and unit_retry is not None:
                    evidence["retry"] = invocation(folder / "junit-retry.xml", node, unit_retry)
                receipt = {"schema": 1, "sha": plan["sha"], "tree": TREE, "plan_sha256": ratchet.digest(plan),
                           "suite": suite, "shard": shard, "replica": replica, "run_id": run_id, "run_attempt": "1",
                           "reports": [evidence]}
                (folder / "shard-receipt.json").write_text(json.dumps(receipt))
    return plan


@pytest.fixture(scope="module")
def historical_verdict_sources():
    paths = (".github/scripts/trusted_verdict.py", ".github/scripts/trusted_ci.py", ".github/scripts/known_skips.py", "ci/make_known_failures.py",
             "ci/known-skips.json", "ci/root-run-evidence.json", "ci/frozen-baseline-failures.json",
             "ci/hosted-env-exceptions.json", "ci/removed-tests.json", ".github/scripts/working_policy.py")
    return {path: historical_git("show", "HEAD:" + path) for path in paths}


@pytest.fixture
def trusted_result(monkeypatch, tmp_path, capsys, historical_verdict_sources):
    import trusted_ci
    import trusted_verdict as trusted

    def run(label, *, outcome, retry=None, branch="dev", event="pull_request", old=False, node=RED,
            base_kind=None, candidate_kind=None, base_registry=(), candidate_registry=None, reviewed_skip=False,
            base_retired=(), candidate_retired=(), base_legacy=False, candidate_legacy=False,
            browser_shards=1, merge_group_shards=None, default="dev", replicas=None, missing_browser=False,
            plan_base=None, event_base=None):
        sources = historical_verdict_sources
        folder = tmp_path / label
        base, head = "a" * 40, "c" * 40
        if replicas is None:
            replicas = (1, 2) if event == "push" and (old or branch != default) else (1,)
        plan = reports(folder / "reports", replicas=replicas, red_suites=(), unit_node=node, unit_outcome=outcome,
                       unit_retry=retry, base_sha=plan_base or base, run_id="1", browser_shards=browser_shards)
        if missing_browser:
            (folder / "reports/ci-test-1-browser-1-1").rename(folder / "missing-browser")
        candidate = plan["sha"]
        output = folder / "verdict"
        plan_path = folder / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        root_evidence = json.loads(sources["ci/root-run-evidence.json"])
        root_evidence["counts"]["cleanup"] = 1  # The synthetic cleanup fixture has one measured failure.
        root_raw = json.dumps(root_evidence).encode()
        metadata = {"node": node, "class": base_kind or candidate_kind, "owner": "maintainers", "reason": "Reviewed"}
        classifications = json.dumps({"schema": 1, "status": "REVIEWED",
                                      "entries": [metadata] if metadata["class"] else []}).encode()

        def known(kind):
            record = {**listing(cap=1), "schema": 3, "root_evidence_sha256": hashlib.sha256(root_raw).hexdigest(),
                      "root_runs": root_evidence["runs"], "classifications_sha256": hashlib.sha256(classifications).hexdigest()}
            if kind == "unstable":
                record["unstable"] = [{**metadata, "issue": 1, "expires_on": "2026-12-19"}]
            elif kind:
                record["fails"] = [{**metadata, **({"issue": 1} if kind == "cleanup" else {})}]
            return json.dumps(record).encode()

        known_at = {base: known(base_kind), candidate: known(candidate_kind)}
        registry_at = {base: base_registry, candidate: base_registry if candidate_registry is None else candidate_registry}

        skip_list = json.loads(sources["ci/known-skips.json"])
        if reviewed_skip:
            skip_list["skips"].append({"node": node, "observed_in": [1], "reason": "Reviewed", "review": "sha256:" + ROOT_SEAL})
            skip_list["skips"].sort(key=lambda row: row["node"])

        def read_file(revision, name):
            if reviewed_skip and name == "ci/known-skips.json":
                return json.dumps(skip_list).encode()
            if name == ratchet.KNOWN:
                return known_at[revision]
            if name == "tests/quarantine.json":
                rows = registry_at[revision]
                return rows if isinstance(rows, bytes) else json.dumps(list(rows)).encode()
            if name == "tests/quarantine-retired.json":
                retired = {base: base_retired, candidate: candidate_retired}[revision]
                if isinstance(retired, OSError):
                    raise retired
                return json.dumps(retired).encode() if isinstance(retired, (list, tuple)) else retired
            if name == ".github/scripts/working_policy.py":
                legacy = {base: base_legacy, candidate: candidate_legacy}[revision]
                return sources[name] if legacy else (ROOT / name).read_bytes()
            if name == "ci/root-run-evidence.json":
                return root_raw
            if name == "ci/failure-classifications.json":
                return classifications
            return sources.get(name)

        inventory = b"".join(b"100644 blob " + b"f" * 40 + b"\t" + path.encode() + b"\0"
                             for row in plan["suites"].values() for shard in row["shards"] for path in shard["files"])
        git_answers = {
            ("rev-parse", "HEAD"): PRE_QUARANTINE_DEV.encode(),
            ("rev-parse", candidate): candidate.encode(),
            ("rev-parse", candidate + "^{tree}"): TREE.encode(),
            ("rev-parse", base + "^{tree}"): TREE.encode(),
            ("rev-parse", root_evidence["sha"] + "^{tree}"): root_evidence["tree"].encode(),
            ("rev-list", "--parents", "-n", "1", candidate): f"{candidate} {base} {head}".encode(),
            ("merge-base", "--is-ancestor", event_base or base, base): b"",
            ("merge-base", "--is-ancestor", base, "refs/remotes/origin/" + branch): b"",
            ("rev-list", "--max-parents=0", candidate): root_evidence["base_sha"].encode(),
            ("merge-base", root_evidence["base_sha"], root_evidence["sha"]): root_evidence["base_sha"].encode(),
        }

        def git(*args):
            if args[0] == "ls-tree":
                if "--name-only" in args:
                    return ratchet.KNOWN.encode()
                return inventory
            if args[0] == "show":
                revision, name = args[1].split(":", 1)
                return read_file(revision, name)
            if args[0] == "diff":
                return b""
            return git_answers[args]

        data = {"before": base,
                "pull_request": {"base": {"ref": branch, "sha": event_base or base}, "head": {"sha": head}},
                "merge_group": {"base_ref": "refs/heads/" + branch, "base_sha": base},
                "repository": {"full_name": "fixture/project", "default_branch": default}}
        step_summary = folder / "step-summary.md"

        def fetch(ref, expected=None):
            if branch.startswith("release/"):
                assert ref == expected, "Release verdicts use event commits, not the trunk ref"
            return expected or base

        with monkeypatch.context() as patch:
            patch.setattr(sys, "path", sys.path.copy())
            if old:
                skips = ".github/scripts/known_skips.py"
                planner = ".github/scripts/trusted_ci.py"
                verifier = ".github/scripts/trusted_verdict.py"
                maker = "ci/make_known_failures.py"
                reader = historical_module(maker, sources[maker])
                patch.setitem(sys.modules, "known_skips", historical_module(skips, sources[skips]))
                patch.setitem(sys.modules, "trusted_ci", historical_module(planner, sources[planner]))
                target = historical_module(verifier, sources[verifier])
            else:
                reader, target = ratchet, trusted
            patch.setitem(sys.modules, "make_known_failures", reader)
            patch.setattr(reader, "git", reader.git)
            patch.setattr(reader, "collect_reports", reader.collect_reports)
            patch.setattr(reader, "datetime", PolicyClock)
            patch.setattr(ratchet, "utc_today", lambda: TODAY)
            patch.setattr(trusted_ci, "utc_today", lambda: date(2026, 11, 19))
            patch.setattr(target, "git", git)
            patch.setattr(target.policy, "git", git)
            patch.setattr(target.policy, "event", lambda: data)
            patch.setattr(target.policy, "fetch", fetch)
            patch.setattr(target.policy, "accepted_dev_commit", lambda tree, tip: base)
            patch.setattr(target.policy, "read_file", read_file)
            patch.setattr(target.hardening, "load", lambda: {"canaries": False, "node_census": False})
            settings = {"unit_shards": 1, "browser_shards": 1, "unstable_limit": 80,
                        "unstable_issue": 1, "unstable_expires_on": "2026-12-19", "cleanup_issue": 1}
            if merge_group_shards is None:
                patch.setattr(target, "settings", lambda **_: settings)
            else:
                config = folder / "ci/bootstrap-settings.json"
                config.parent.mkdir()
                config.write_text(json.dumps({"schema": 1, **settings, "merge_group_shards": merge_group_shards}),
                                  encoding="utf-8")
                patch.setattr(trusted_ci, "TRUSTED", folder)
            patch.setattr(sys, "argv", ["trusted_verdict", "--plan", str(plan_path), "--reports", str(folder / "reports"),
                                       "--output", str(output)])
            patch.delenv("GH_TOKEN", raising=False)
            for key, value in {
                "GITHUB_EVENT_NAME": event, "GITHUB_REF": "refs/heads/" + branch,
                "GITHUB_SHA": candidate, "CANDIDATE_SHA": candidate,
                "TRUSTED_BOOTSTRAP": "false", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_STEP_SUMMARY": str(step_summary), "RUNNER_TEMP": str(folder),
                "NEEDS_JSON": json.dumps({key: {"result": "success"} for key in ("checks", "static", "unit", "browser")}),
            }.items():
                patch.setenv(key, value)
            try:
                target.main()
            except ValueError as error:
                refusal = str(error)
            else:
                refusal = None
        captured = capsys.readouterr()
        artifacts = {path.name: path.read_bytes() for path in output.glob("*.json")}
        verdict = json.loads(artifacts["verdict.json"]) if "verdict.json" in artifacts else None
        return (refusal, verdict), (captured, step_summary.read_bytes() if step_summary.exists() else b"", artifacts)

    return run


def quarantine_row(node=RED, kind="baseline"):
    return {"nodeid": node, "reason": kind + ": Reviewed", "owner": "maintainers", "issue": 1, "blocker": False,
            "added": "2026-11-19", "expires": "2026-12-19"}


@pytest.mark.parametrize(("replicas", "outcome", "missing_browser", "refusal"), [
    ((1,), "passed", False, None),
    ((1,), "passed", True, "Missing or unexpected shard artifacts: browser-1-1"),
    ((1,), "failed", False, "Missing push retry evidence: junit.xml"),
    ((1, 2), "passed", False, "Replica count differs from the trusted reader context"),
])
def test_main_push_verdict(trusted_result, replicas, outcome, missing_browser, refusal):
    (error, verdict), _ = trusted_result("push", branch="main", default="main", event="push",
                                        replicas=replicas, outcome=outcome, missing_browser=missing_browser)
    assert error == refusal
    if refusal is None:
        assert verdict["passed"] is True
    else:
        assert verdict is None


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push"])
def test_release_verdict_requires_its_base_and_all_shard_evidence(trusted_result, event):
    options = {"branch": "release/v1.2.3", "event": event, "default": "main", "outcome": "passed"}
    (error, verdict), _ = trusted_result("exact", **options)
    assert error is None and verdict["passed"] is True
    (error, verdict), _ = trusted_result("foreign", **options, plan_base="e" * 40)
    assert error == "Evidence is not bound to the independently resolved candidate/target" and verdict is None
    (error, verdict), _ = trusted_result("incomplete", **options, missing_browser=True)
    assert "Missing or unexpected shard artifacts: browser-1-1" in error and verdict is None


def test_pr_verdict_binds_evidence_to_verified_base_with_stale_event(trusted_result):
    event_base = "e" * 40
    options = {"branch": "main", "default": "main", "outcome": "passed", "event_base": event_base}
    (error, verdict), _ = trusted_result("verified-base", **options)
    assert error is None and verdict["passed"] is True
    (error, verdict), _ = trusted_result("event-base", **options, plan_base=event_base)
    assert error == "Evidence is not bound to the independently resolved candidate/target" and verdict is None


@pytest.mark.parametrize("event", ["push", "merge_group"])
def test_default_branch_retry_evidence_reports_recovered_failure(trusted_result, event):
    (error, verdict), (_, _, artifacts) = trusted_result(
        "recovered", branch="main", default="main", event=event, outcome="failed", retry="passed")
    assert error is None
    assert verdict["passed"] is True
    assert json.loads(artifacts["outside-observations.json"])["unstable_count"] == 1
    assert json.loads(artifacts["recovered-tests.json"]) == [
        {"node": RED, "class": "unstable", "observations": ["failed", "passed"]},
    ]


@pytest.mark.parametrize("event", ["push", "merge_group"])
def test_default_branch_retry_evidence_keeps_persistent_failure_red(trusted_result, event):
    (error, verdict), (_, _, artifacts) = trusted_result(
        "persistent", branch="main", default="main", event=event, outcome="failed", retry="failed")
    assert error == "Trusted known-failure comparison refused the evidence"
    assert verdict["passed"] is False
    assert "New failures outside the known-failure list" in verdict["reason"]
    assert json.loads(artifacts["outside-observations.json"])["unstable_count"] == 0


def test_main_push_keeps_reviewed_quarantine(trusted_result):
    (error, verdict), (_, summary, _) = trusted_result(
        "push-quarantine", branch="main", default="main", event="push",
        outcome="quarantine-xfail", base_registry=[quarantine_row(kind="unstable")])
    assert error is None and verdict["passed"] is True
    assert b"quarantined and failing now: 1" in summary


@pytest.mark.parametrize("browser_shards", [2, 1])
def test_full_pr_verdict_checks_merge_group_shards(trusted_result, browser_shards):
    result, _ = trusted_result("full-pr", outcome="passed", browser_shards=browser_shards,
                               merge_group_shards={"unit_shards": 1, "browser_shards": 2})
    if browser_shards == 2:
        assert result[0] is None and result[1]["passed"]
    else:
        assert result == ("Candidate plan omits, duplicates or misclassifies committed test files", None)


@pytest.mark.parametrize("branch", ["dev", "preview"])
def test_trusted_verdict_reports_honoured_quarantine_xfail_as_failing(trusted_result, branch):
    result, (_, summary, _) = trusted_result("honoured", branch=branch, outcome="quarantine-xfail",
                                             base_registry=[quarantine_row()])
    assert result[0] is None and result[1]["passed"]
    assert b"quarantined and failing now: 1" in summary
    assert f"- {RED}: quarantined-failing".encode() in summary


def test_trusted_verdict_refuses_unhonoured_quarantine_xfail(trusted_result):
    node = "tests/test_new_feature.py::test_new_feature_works"
    row = {**quarantine_row(node, "unstable"), "reason": "unstable: recovered on retry", "recovered_runs": ["1"]}
    result, _ = trusted_result("unhonoured", node=node, outcome="quarantine-xfail", candidate_registry=[row])
    assert result[0] == "Trusted known-failure comparison refused the evidence"
    assert result[1]["reason"] == "New failures outside the known-failure list: " + node


def test_trusted_verdict_requires_registry_entry_for_quarantine_prefix(trusted_result):
    result, _ = trusted_result("unbound", outcome="quarantine-xfail", base_registry=[quarantine_row(GREEN)])
    assert result == ("Quarantine xfail has no source registry entry: " + RED, None)


@pytest.mark.parametrize("outcome", ["skipped", "ordinary-xfail"])
def test_trusted_verdict_applies_known_skips_to_real_skips_and_ordinary_xfails(trusted_result, outcome):
    result, _ = trusted_result("unreviewed", outcome=outcome)
    assert result == ("New skip outside the reviewed known-skips list", None)
    result, _ = trusted_result("registered", outcome=outcome, base_registry=[quarantine_row()])
    assert result == ("New skip outside the reviewed known-skips list", None)
    result, _ = trusted_result("reviewed", outcome=outcome, reviewed_skip=True)
    assert result[0] is None and result[1]["passed"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_main_quarantine_xfail_matches_historical_release_failure(trusted_result, event):
    execution = {"branch": "main", "event": event}
    historical, _ = trusted_result("old", old=True, outcome="failed", base_kind="baseline",
                                   candidate_kind="baseline", **execution)
    current, _ = trusted_result("new", outcome="quarantine-xfail", base_registry=[quarantine_row()], **execution)
    assert current == historical == ("Release/known-skip execution contains a failure or error", None)


@pytest.mark.parametrize("outcome", ["failed", "quarantine-xfail"])
def test_trusted_verdict_refuses_a_failing_retired_node(trusted_result, outcome):
    retry = "failed" if outcome == "failed" else None
    result, _ = trusted_result("retired", outcome=outcome, retry=retry,
                               base_registry=[quarantine_row()], base_retired=[RED])
    assert result[0] == "Trusted known-failure comparison refused the evidence"
    assert result[1]["reason"] == "New failures outside the known-failure list: " + RED


@pytest.mark.parametrize("branch", ["dev", "preview"])
def test_trusted_verdict_uses_base_and_candidate_retirements(trusted_result, branch):
    # A retirement in the candidate alone already removes the node's excuse in the same run.
    events = ("pull_request", "merge_group") if branch == "dev" else ("pull_request",)
    for event in events:
        result, _ = trusted_result(event, branch=branch, event=event, outcome="failed", retry="failed",
                                   base_registry=[quarantine_row()], candidate_retired=[RED])
        assert result[0] == "Trusted known-failure comparison refused the evidence"
        assert result[1]["reason"] == "New failures outside the known-failure list: " + RED


@pytest.mark.parametrize("raw", [b"[", b"{}", b"[1]", b'["not a node"]', b"\xff", None])
def test_trusted_verdict_refuses_a_malformed_retired_list(trusted_result, raw):
    for side in ("base_retired", "candidate_retired"):
        result, _ = trusted_result(side, outcome="passed", **{side: raw})
        assert result[0].startswith("Invalid quarantine retired list tests/quarantine-retired.json:")
        assert result[1] is None


def test_trusted_verdict_refuses_an_unreadable_retired_list(trusted_result):
    for side in ("base_retired", "candidate_retired"):
        result, _ = trusted_result(side, outcome="passed", **{side: OSError("fixture")})
        assert result == ("Cannot read trusted quarantine retired list: tests/quarantine-retired.json", None)


@pytest.mark.parametrize("branch,event,candidate_retired", [
    ("dev", "push", []),  # Landing push introduces the file.
    ("dev", "pull_request", None),
    ("dev", "merge_group", None),
    ("main", "pull_request", None),
    ("main", "merge_group", None),
])
def test_trusted_verdict_accepts_a_pre_mechanism_base(trusted_result, branch, event, candidate_retired):
    result, _ = trusted_result("legacy-base", branch=branch, event=event, outcome="passed",
                               base_legacy=True, base_retired=None, candidate_retired=candidate_retired,
                               candidate_legacy=candidate_retired is None)
    assert result[0] is None and result[1]["passed"]


@pytest.mark.parametrize("options", [
    {"event": "push", "base_legacy": True, "base_retired": None, "candidate_retired": None},
    {"candidate_legacy": True, "candidate_retired": None},
    {"branch": "main", "base_retired": None},
])
def test_trusted_verdict_refuses_a_missing_retired_list_after_activation(trusted_result, options):
    result, _ = trusted_result("missing-retired", outcome="passed", **options)
    assert result == ("Invalid quarantine retired list tests/quarantine-retired.json: retired list is missing", None)


def test_main_uses_the_accepted_dev_retired_list(trusted_result):
    execution = {"branch": "main", "outcome": "passed", "base_registry": [{**quarantine_row(), "blocker": True}]}
    for case, retired in (("accepted-retirement", {"base_retired": [RED]}),
                          ("candidate-retirement", {"candidate_retired": [RED]})):
        result, _ = trusted_result(case, **execution, **retired)
        assert result[0] == "Trusted known-failure comparison refused the evidence"
        assert result[1]["reason"] == "Release blockers remain in quarantine: " + RED


def test_canary_normalization_preserves_quarantine_xfail(tmp_path):
    import canary_evidence as canaries

    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.test_sample" name="test_red">'
        '<skipped type="pytest.xfail" message="QUARANTINED (fixture)"/></testcase></testsuite>', encoding="utf-8")
    raw = report.with_suffix(".raw.xml")
    raw_tree = ET.parse(report)
    sentinels = {kind: "sentinel_" + kind for kind in ("passed", "failed", "skipped", "error")}
    for kind, name in sentinels.items():
        case = ET.SubElement(raw_tree.getroot(), "testcase", name=name)
        if kind != "passed":
            ET.SubElement(case, "failure" if kind == "failed" else kind)
    raw_tree.write(raw)
    census = report.with_suffix(".collection.json")
    census.write_text(json.dumps([RED, *sentinels.values()]), encoding="utf-8")
    identity = {"sha": "a" * 40, "tree": TREE, "run_id": "1", "run_attempt": "1"}
    (tmp_path / "shard-receipt.json").write_text(
        json.dumps({**identity, "reports": [{"file": report.name}]}), encoding="utf-8")
    report.with_suffix(".canaries.json").write_text(json.dumps({**identity, "sentinels": sentinels,
        "raw_sha256": canaries.sha(raw), "collection_sha256": canaries.sha(census)}), encoding="utf-8")

    canaries.verify_directory(tmp_path)
    changed = ET.parse(report)
    changed.find("testcase/skipped").set("message", "Expected by the test")
    changed.write(report)
    with pytest.raises(ValueError, match="Normalization changed a product outcome"):
        canaries.verify_directory(tmp_path)


@pytest.mark.parametrize("hosted", [False, True], ids=["local-xfail", "ci-hosted-raw-failure"])
def test_real_quarantine_plugin_junit_reaches_trusted_verdict(tmp_path, trusted_result, hosted):
    shutil.copyfile(ROOT / "tests/quarantine.py", tmp_path / "quarantine.py")
    today = datetime.now(timezone.utc).date()
    row = {**quarantine_row(), "added": "2000-01-01", "expires": today.isoformat()}
    (tmp_path / "quarantine.json").write_text(json.dumps([row]), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_red():\n    assert False\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CI_CASE_", "PYTEST_")) and k != "FMUC_CLOUD_TEST_REPORTS"}
    env.update(PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / ".github/scripts"))),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", TMP=str(tmp_path), TEMP=str(tmp_path))
    report = tmp_path / "quarantined.xml"
    # The CI shard runners load hosted_ci, so the plugin keeps the raw failure there; a local run marks it xfail.
    plugins = ["-p", "hosted_ci", "-p", "quarantine"] if hosted else ["-p", "quarantine"]
    proc = subprocess.run([sys.executable, "-m", "pytest", *plugins, "-c", "pytest.ini", "-q",
                           "--basetemp=nested", "--junitxml=" + str(report), "tests/test_sample.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    case = ET.parse(report).getroot().find(".//testcase")
    if hosted:
        assert proc.returncode == 1 and "1 failed" in proc.stdout, proc.stdout + proc.stderr
        assert case.find("failure") is not None and case.find("skipped") is None
    else:
        assert proc.returncode == 0 and "1 xfailed" in proc.stdout, proc.stdout + proc.stderr
        assert case.find("skipped").get("type") == "pytest.xfail"
    result, (_, summary, _) = trusted_result("plugin", outcome=report, base_registry=[row])
    assert result[0] is None and result[1]["passed"]
    assert f"- {RED}: quarantined-failing".encode() in summary


def test_same_pr_quarantine_flake_does_not_hide_new_failure(trusted_result):
    node = "tests/test_new_feature.py::test_new_feature_works"
    row = {**quarantine_row(node, "unstable"), "reason": "unstable: recovered on retry", "recovered_runs": ["1"]}
    result, _ = trusted_result("new", node=node, outcome="failed", retry="failed", candidate_registry=[row])
    assert result[0] == "Trusted known-failure comparison refused the evidence"
    assert result[1]["reason"] == "New failures outside the known-failure list: " + node
    result, evidence = trusted_result("recovered", node=node, outcome="failed", retry="passed", candidate_registry=[row])
    assert result[0] is None and result[1]["passed"]
    proposal, = json.loads(evidence[2]["outside-observations.json"])["proposed_list_entries"]
    assert (proposal["node"], proposal["status"], proposal["observations"]) == (node, "PROPOSED", ["failed", "passed"])


def test_verdict_honours_only_same_class_moves(trusted_result):
    result, _ = trusted_result("move", outcome="failed", base_kind="baseline", candidate_registry=[quarantine_row()])
    assert result[0] is None and result[1]["passed"]
    result, _ = trusted_result("wrong-class", outcome="failed", retry="failed", base_kind="baseline",
                               candidate_registry=[quarantine_row(kind="cleanup")])
    assert result[1]["reason"] == "New failures outside the known-failure list: " + RED


def test_verdict_honours_only_byte_identical_base_entries(trusted_result):
    row = quarantine_row()
    another = quarantine_row(GREEN)
    result, _ = trusted_result("carried", outcome="failed", base_registry=[another, row], candidate_registry=[row])
    assert result[0] is None and result[1]["passed"]
    repacked = json.dumps([row], separators=(",", ":")).encode()
    result, _ = trusted_result("repacked", outcome="failed", retry="failed", base_registry=[row], candidate_registry=repacked)
    assert result[1]["reason"] == "New failures outside the known-failure list: " + RED


def test_verdict_refuses_known_failure_quarantine_overlap(trusted_result):
    result, _ = trusted_result("duplicate", outcome="passed", base_kind="baseline", candidate_kind="baseline",
                               candidate_registry=[quarantine_row(kind="unstable")])
    assert result == ("Known-failure and quarantine entries must be disjoint: " + RED, None)


@pytest.mark.parametrize(("outcome", "retry"), [("passed", None), ("failed", "failed"), ("error", "error"),
                                                ("failed", "passed")], ids=["passed", "failed", "error", "recovered"])
def test_empty_quarantine_trusted_verdict_matches_history(trusted_result, outcome, retry):
    expected = trusted_result("old", old=True, outcome=outcome, retry=retry)
    actual = trusted_result("new", outcome=outcome, retry=retry)
    assert actual == expected
    assert actual[0][1]["passed"] is (outcome == "passed" or retry == "passed")


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("kind", ["baseline", "cleanup", "baseline-error", "unstable"])
@pytest.mark.parametrize("outcome", ["passed", "failed", "error", "recovered"])
def test_main_quarantine_verdict_matches_known_failure_history(trusted_result, event, kind, outcome):
    execution = {"branch": "main", "event": event, "outcome": "failed" if outcome == "recovered" else outcome,
                 "retry": "passed" if outcome == "recovered" else None}
    expected, _ = trusted_result("old", old=True, base_kind=kind, candidate_kind=kind, **execution)
    actual, _ = trusted_result("new", base_registry=[quarantine_row(kind=kind)], **execution)
    assert actual == expected
    if outcome == "passed":
        assert actual[1]["passed"] is (kind == "unstable" or event == "merge_group")
    elif outcome == "recovered":
        assert actual == ("Retry must execute exactly the unlisted first-attempt failed nodes", None)
    else:
        assert actual == ("Release/known-skip execution contains a failure or error", None)


def test_main_refuses_verified_recovery_outside_known_failures(trusted_result):
    options = {"branch": "main", "outcome": "failed", "retry": "passed"}
    expected, _ = trusted_result("old", old=True, **options)
    actual, _ = trusted_result("new", **options)
    assert actual == expected == ("Release/known-skip execution contains a failure or error", None)


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
            collected = ratchet.ReportEvidence()
            group_collect(evidence=collected)
            recovered = collected.recovered
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
                other.delenv("GITHUB_REF", raising=False)
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
                 "ci/quarantine_registry.py", "ci/hosted-env-exceptions.json", "ci/removed-tests.json"):
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
