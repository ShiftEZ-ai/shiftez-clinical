import hashlib
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".github/scripts"), str(ROOT / "ci")]
import latest_reports
import make_known_failures as maker
import node_census


def write_shard(reports, plan, shard, attempt, *, replica=1, red=False, error=False, retry=None):
    folder = reports / f"ci-test-{attempt}-unit-{shard}-{replica}" / "reports"
    folder.mkdir(parents=True)
    file = plan["suites"]["unit"]["shards"][shard - 1]["files"][0]
    node = file + "::test_case"
    def invocation(name, tag):
        report = folder / name
        outcome = f"<{tag}/>" if tag else ""
        report.write_text(f'<testsuite><testcase classname="{file[:-3].replace("/", ".")}" name="test_case">'
                          f'{outcome}</testcase></testsuite>', encoding="utf-8")
        report.with_suffix(".selection.json").write_text(json.dumps([node]), encoding="utf-8")
        report.with_suffix(".deselected.json").write_text("[]", encoding="utf-8")
        files = (report, report.with_suffix(".selection.json"), report.with_suffix(".deselected.json"))
        return {"file": name, "exit_code": int(bool(tag)),
                "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}

    report = invocation("junit.xml", "error" if error else "failure" if red else "")
    if retry is not None:
        report["retry"] = invocation("junit-retry.xml", retry)
    census = folder / "base-census.json"
    census.write_text(json.dumps({"base_sha": plan["base_sha"], "head_sha": plan["sha"], "suite": "unit",
                                  "shard": shard, "replica": replica, "run_id": "10", "run_attempt": str(attempt),
                                  "files": [file], "nodes": [node]}), encoding="utf-8")
    receipt = {"schema": 1, "sha": plan["sha"], "tree": plan["tree"], "plan_sha256": maker.digest(plan),
               "suite": "unit", "shard": shard, "replica": replica, "run_id": "10", "run_attempt": str(attempt),
               "base_census_sha256": hashlib.sha256(census.read_bytes()).hexdigest(),
               "reports": [report]}
    (folder / "shard-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def no_ci_event(monkeypatch):
    """Tests that need retry semantics choose their event explicitly."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


@pytest.fixture
def plan():
    return {"sha": "a" * 40, "tree": "b" * 40, "base_sha": "c" * 40, "replicas": [1],
            "suites": {"unit": {"shards": [{"files": ["tests/test_a.py"]}, {"files": ["tests/test_b.py"]}]}}}


@pytest.fixture
def current(plan):
    return {"schema": 3, "tree": plan["tree"], "classifications_sha256": "d" * 64,
            "root_evidence_sha256": "e" * 64, "root_runs": [
                {"run_id": "1", "run_attempt": "1", "replica": replica} for replica in (1, 2)],
            "fails": [], "unstable": [], "cleanup_cap": 0}


@pytest.mark.parametrize("red", [False, True])
def test_mixed_attempts_read_the_latest_shard_outcomes(tmp_path, plan, current, red):
    write_shard(tmp_path, plan, 1, 1, red=not red)
    write_shard(tmp_path, plan, 1, 2, red=red)
    write_shard(tmp_path, plan, 2, 1)
    latest_reports.keep_latest(tmp_path, plan)
    assert {p.name for p in tmp_path.iterdir()} == {"ci-test-2-unit-1-1", "ci-test-1-unit-2-1"}
    latest_reports.validate_selected_reports(plan, tmp_path)
    runs = maker.collect_reports(plan, tmp_path, "10", "2")
    expected_files = {"tests/test_a.py", "tests/test_b.py"}
    census = node_census.base_nodes(plan, tmp_path, plan["base_sha"], "10", "2",
                                    maker.canonical_node, expected_files)
    assert census == set(runs[0])
    if red:
        assert runs[0]["tests/test_a.py::test_case"] == "failed"
        with pytest.raises(ValueError, match="New failures outside"):
            maker.evaluate(current, current, runs)
    else:
        assert maker.evaluate(current, current, runs)


@pytest.mark.parametrize(("folder", "message"), [
    (None, "unit-1-1"),
    ("bad-name", "Invalid shard artifact folder"),
    ("ci-test-2-unit-1-1", "unit-2-1"),
])
def test_refuses_empty_malformed_or_incomplete_artifacts(tmp_path, plan, folder, message):
    if folder is not None:
        (tmp_path / folder).mkdir()
    with pytest.raises(ValueError, match=message):
        latest_reports.keep_latest(tmp_path, plan)


@pytest.mark.parametrize("bad_attempt", [0, 3])
def test_readers_refuse_out_of_range_producing_attempt(tmp_path, plan, bad_attempt):
    write_shard(tmp_path, plan, 1, bad_attempt)
    write_shard(tmp_path, plan, 2, 1)
    with pytest.raises(ValueError, match="artifact identity or attempt"):
        maker.collect_reports(plan, tmp_path, "10", "2")
    with pytest.raises(ValueError, match="artifact identity or attempt"):
        node_census.base_nodes(plan, tmp_path, plan["base_sha"], "10", "2", maker.canonical_node,
                               {"tests/test_a.py", "tests/test_b.py"})


def test_readers_bind_evidence_to_the_exact_artifact_attempt(tmp_path, plan):
    folder = write_shard(tmp_path, plan, 1, 1)
    folder.parent.rename(tmp_path / "ci-test-2-unit-1-1")
    write_shard(tmp_path, plan, 2, 1)
    with pytest.raises(ValueError, match="incorrectly bound"):
        maker.collect_reports(plan, tmp_path, "10", "2")
    with pytest.raises(ValueError, match="census binding"):
        node_census.base_nodes(plan, tmp_path, plan["base_sha"], "10", "2", maker.canonical_node,
                               {"tests/test_a.py", "tests/test_b.py"})


NODE = "tests/test_a.py::test_case"


@pytest.fixture
def rerun(tmp_path, monkeypatch, plan, current):
    reports, output = tmp_path / "reports", tmp_path / "output"
    output.mkdir()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")

    def observe(attempt=2):
        write_shard(reports, plan, 2, 1)
        write_shard(reports, plan, 2, attempt)
        latest_reports.keep_latest(reports, plan)
        evidence = maker.ReportEvidence()
        runs = maker.collect_reports(plan, reports, "10", str(attempt), output,
                                     listed=maker.listed_nodes(current), evidence=evidence)
        maker.report_outside(current, runs, output, recovered=evidence.recovered,
                             reruns=evidence.reruns, unobserved=evidence.unobserved)
        return runs, evidence, json.loads((output / "outside-observations.json").read_bytes())

    return reports, output, summary, observe


def test_failed_then_passed_is_proposed_unstable_and_group_passes(plan, current, rerun):
    reports, output, summary, observe = rerun
    write_shard(reports, plan, 1, 1, red=True, retry="failure")
    write_shard(reports, plan, 1, 2)
    runs, evidence, outside = observe()
    assert evidence.recovered == {NODE: "failed"}
    assert maker.classified_results(runs, set(), set(), evidence.recovered)["unstable"] == {NODE}
    proposal, = outside["proposed_list_entries"]
    assert (proposal["node"], proposal["class"], proposal["status"]) == (NODE, "unstable", "PROPOSED")
    assert maker.evaluate(current, current, runs, group=True, merge_group=True, recovered=evidence.recovered)
    assert json.loads((output / "recovered-tests.json").read_bytes()) == [
        {"node": NODE, "class": "unstable", "observations": ["failed", "passed"]}]
    assert "recovered on re-run (attempt 1)" in summary.read_text(encoding="utf-8")


def test_failed_then_failed_stays_red(plan, current, rerun):
    reports, _, _, observe = rerun
    for attempt in (1, 2):
        write_shard(reports, plan, 1, attempt, red=True, retry="failure")
    runs, evidence, outside = observe()
    assert not evidence.recovered and not outside["proposed_list_entries"]
    assert runs[0][NODE] == "failed"
    with pytest.raises(ValueError, match="New failures outside"):
        maker.evaluate(current, current, runs, group=True, merge_group=True, recovered=evidence.recovered)


def test_error_then_passed_is_unstable_without_a_new_error(plan, current, rerun):
    reports, _, _, observe = rerun
    write_shard(reports, plan, 1, 1, error=True, retry="error")
    write_shard(reports, plan, 1, 2)
    runs, evidence, outside = observe()
    assert evidence.recovered == {NODE: "error"}
    assert outside["proposed_list_entries"][0]["observations"] == ["error", "passed"]
    groups = maker.classified_results(runs, set(), set(), evidence.recovered)
    assert groups["unstable"] == {NODE} and not groups["baseline-error"]
    assert maker.evaluate(current, current, runs, group=True, merge_group=True, recovered=evidence.recovered)


def test_passed_then_passed_has_no_recovery(plan, current, rerun):
    reports, output, _, observe = rerun
    for attempt in (1, 2):
        write_shard(reports, plan, 1, attempt)
    runs, evidence, outside = observe()
    assert runs[0][NODE] == "passed"
    assert not evidence.recovered and not outside["proposed_list_entries"]
    assert not (output / "recovered-tests.json").exists()
    assert maker.evaluate(current, current, runs, group=True, merge_group=True, recovered=evidence.recovered)


def test_rerun_does_not_hide_a_latest_failure_in_another_replica(tmp_path, plan):
    plan["replicas"] = [1, 2]
    for shard in (1, 2):
        for replica in plan["replicas"]:
            write_shard(tmp_path, plan, shard, 2, replica=replica, red=(shard, replica) == (1, 2))
    write_shard(tmp_path, plan, 1, 1, red=True)
    latest_reports.keep_latest(tmp_path, plan)
    evidence = maker.ReportEvidence()
    runs = maker.collect_reports(plan, tmp_path, "10", "2", evidence=evidence)
    assert [run[NODE] for run in runs] == ["passed", "failed"]
    assert not evidence.recovered


def test_rerun_recovery_keeps_a_listed_baseline_node_from_going_stale(plan, current, rerun):
    reports, output, summary, observe = rerun
    current["fails"].append({"node": NODE, "class": "baseline"})
    write_shard(reports, plan, 1, 1, red=True)
    write_shard(reports, plan, 1, 2)
    runs, evidence, outside = observe()
    remark, = outside["listed_unstable"]
    assert (remark["node"], remark["status"]) == (NODE, "PROPOSED_REMARK")
    assert maker.evaluate(current, current, runs, group=True, merge_group=True,
                          recovered=evidence.recovered, stale_output=output)
    assert json.loads((output / "stale-report.json").read_bytes()) == []
    assert "recovered on re-run (attempt 1)" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("has_artifact", [False, True])
def test_missing_earlier_report_is_unobserved(plan, current, rerun, has_artifact):
    reports, _, summary, observe = rerun
    if has_artifact:
        (reports / "ci-test-1-unit-1-1").mkdir(parents=True)
    write_shard(reports, plan, 1, 2)
    runs, evidence, outside = observe()
    assert outside["unobserved_attempts"] == [{"suite": "unit", "shard": 1, "replica": 1, "attempt": 1}]
    assert "unobserved attempt | unit | shard 1 | replica 1 | attempt 1" in summary.read_text(encoding="utf-8")
    assert maker.evaluate(current, current, runs, group=True, merge_group=True, recovered=evidence.recovered)


@pytest.mark.parametrize("field", ["sha", "tree", "run_id", "run_attempt"])
def test_earlier_report_refuses_foreign_binding(plan, rerun, field):
    reports, _, _, observe = rerun
    receipt = write_shard(reports, plan, 1, 1) / "shard-receipt.json"
    data = json.loads(receipt.read_bytes())
    data[field] = "0" * len(data[field])
    receipt.write_text(json.dumps(data), encoding="utf-8")
    write_shard(reports, plan, 1, 2)
    with pytest.raises(ValueError, match="incorrectly bound"):
        observe()


def test_earlier_report_from_an_older_plan_is_unobserved(plan, current, rerun):
    """Re-run all jobs replans from the moved dev tip; only the plan seal differs, so attempt 1 is unobserved."""
    reports, _, summary, observe = rerun
    receipt = write_shard(reports, plan, 1, 1, red=True) / "shard-receipt.json"
    data = json.loads(receipt.read_bytes())
    data["plan_sha256"] = "0" * len(data["plan_sha256"])
    receipt.write_text(json.dumps(data), encoding="utf-8")
    write_shard(reports, plan, 1, 2)
    runs, evidence, outside = observe()
    assert {"suite": "unit", "shard": 1, "replica": 1, "attempt": 1} in outside["unobserved_attempts"]
    assert NODE not in evidence.recovered
    assert "unobserved attempt | unit | shard 1 | replica 1 | attempt 1" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("attempt", [2, 3])
def test_earlier_report_must_precede_the_selected_attempt(tmp_path, plan, attempt):
    write_shard(tmp_path, plan, 1, 2)
    write_shard(tmp_path, plan, 2, 2)
    write_shard(latest_reports.earlier_reports(tmp_path), plan, 1, attempt)
    with pytest.raises(ValueError, match="artifact identity or attempt"):
        maker.collect_reports(plan, tmp_path, "10", "2")


def test_duplicate_earlier_receipts_are_refused(plan, rerun):
    reports, _, _, observe = rerun
    folder = write_shard(reports, plan, 1, 1)
    duplicate = folder.parent / "duplicate"
    duplicate.mkdir()
    for path in folder.iterdir():
        (duplicate / path.name).write_bytes(path.read_bytes())
    write_shard(reports, plan, 1, 2)
    with pytest.raises(ValueError, match="Duplicate shard artifact attempt"):
        observe()


def test_keep_latest_preserves_earlier_artifact_bytes(tmp_path, plan):
    reports = tmp_path / "reports"
    original = write_shard(reports, plan, 1, 1, red=True)
    expected = {path.name: path.read_bytes() for path in original.iterdir()}
    write_shard(reports, plan, 1, 2)
    write_shard(reports, plan, 2, 1)
    latest_reports.keep_latest(reports, plan)
    latest_reports.keep_latest(reports, plan)
    archived = latest_reports.earlier_reports(reports) / original.relative_to(reports)
    assert {path.name: path.read_bytes() for path in archived.iterdir()} == expected
    latest_reports.validate_selected_reports(plan, reports)


def test_earliest_red_with_in_job_recovery_counts_once(plan, rerun):
    reports, output, summary, observe = rerun
    write_shard(reports, plan, 1, 1, error=True, retry="")
    write_shard(reports, plan, 1, 2, red=True, retry="")
    write_shard(reports, plan, 1, 3)
    _, evidence, outside = observe(attempt=3)
    assert evidence.recovered == {NODE: "error"}
    assert outside["unstable_count"] == 1 and len(json.loads((output / "recovered-tests.json").read_bytes())) == 1
    assert summary.read_text(encoding="utf-8").count("recovered on re-run (attempt 1)") == 1


def test_ci_downloads_checks_artifacts_and_selects_reports_before_verdict():
    workflows = ROOT / ".github/workflows"
    ci = yaml.load((workflows / "ci.yml").read_bytes(), Loader=yaml.BaseLoader)["jobs"]
    reusable = yaml.load((workflows / "ci-tests.yml").read_bytes(), Loader=yaml.BaseLoader)
    assert ci["checks"]["outputs"]["artifact_attempt"] == "${{ github.run_attempt }}"
    source = "${{ needs.checks.outputs.artifact_attempt }}"
    for job in ("unit", "browser"):
        assert ci[job]["with"]["artifact_attempt"] == source
    assert reusable["on"]["workflow_call"]["inputs"]["artifact_attempt"] == {"type": "string", "required": "true"}
    names = set()
    for jobs, attempt in [(ci, source), (reusable["jobs"], "${{ inputs.artifact_attempt }}")]:
        for job in jobs.values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/download-artifact@"):
                    name = step["with"].get("name", "")
                    for prefix in ("ci-plan-", "ci-dependencies-", "ci-browser-", "ci-web-"):
                        if name.startswith(prefix):
                            names.add(prefix)
                            assert name == prefix + attempt
    assert names == {"ci-plan-", "ci-dependencies-", "ci-browser-", "ci-web-"}
    steps = ci["result"]["steps"]
    downloads = {step["with"]["pattern"]: step for step in steps if "pattern" in step.get("with", {})}
    assert downloads["ci-test-*"]["with"]["merge-multiple"] == "false"
    assert downloads["ci-test-*"]["if"] == "steps.reports.outputs.mixed_attempts == 'true'"
    assert downloads["ci-test-${{ github.run_attempt }}-*"]["if"] == "steps.reports.outputs.mixed_attempts != 'true'"
    detect = next(step["run"] for step in steps if step.get("id") == "reports")
    assert "test -f .trusted-verdict/.github/scripts/latest_reports.py" in detect
    verdict = next(step["run"] for step in steps if "trusted_verdict.py" in step.get("run", ""))
    assert verdict.index("Plan seal differs") < verdict.index("python3 -I .trusted-verdict/.github/scripts/latest_reports.py")
    assert verdict.index("--plan .candidate-evidence/plan/shard-plan.json") < verdict.index("trusted_verdict.py")
