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


def write_shard(reports, plan, shard, attempt, *, red=False):
    folder = reports / f"ci-test-{attempt}-unit-{shard}-1" / "reports"
    folder.mkdir(parents=True)
    file = plan["suites"]["unit"]["shards"][shard - 1]["files"][0]
    node = file + "::test_case"
    report = folder / "junit.xml"
    outcome = "<failure/>" if red else ""
    report.write_text(f'<testsuite><testcase classname="{file[:-3].replace("/", ".")}" name="test_case">'
                      f'{outcome}</testcase></testsuite>', encoding="utf-8")
    report.with_suffix(".selection.json").write_text(json.dumps([node]), encoding="utf-8")
    report.with_suffix(".deselected.json").write_text("[]", encoding="utf-8")
    census = folder / "base-census.json"
    census.write_text(json.dumps({"base_sha": plan["base_sha"], "head_sha": plan["sha"], "suite": "unit",
                                  "shard": shard, "replica": 1, "run_id": "10", "run_attempt": str(attempt),
                                  "files": [file], "nodes": [node]}), encoding="utf-8")
    report_files = [report, report.with_suffix(".selection.json"), report.with_suffix(".deselected.json")]
    receipt = {"schema": 1, "sha": plan["sha"], "tree": plan["tree"], "plan_sha256": maker.digest(plan),
               "suite": "unit", "shard": shard, "replica": 1, "run_id": "10", "run_attempt": str(attempt),
               "base_census_sha256": hashlib.sha256(census.read_bytes()).hexdigest(),
               "reports": [{"file": report.name, "exit_code": int(red),
                            "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in report_files}}]}
    (folder / "shard-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def no_ci_event(monkeypatch):
    """These readers are tested without retry semantics; the hosted job's own event must not select them."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


@pytest.fixture
def plan():
    return {"sha": "a" * 40, "tree": "b" * 40, "base_sha": "c" * 40, "replicas": [1],
            "suites": {"unit": {"shards": [{"files": ["tests/test_a.py"]}, {"files": ["tests/test_b.py"]}]}}}


@pytest.mark.parametrize("red", [False, True])
def test_mixed_attempts_read_the_latest_shard_outcomes(tmp_path, plan, red):
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
    current = {"schema": 3, "tree": plan["tree"], "classifications_sha256": "d" * 64,
               "root_evidence_sha256": "e" * 64, "root_runs": [
                   {"run_id": "1", "run_attempt": "1", "replica": replica} for replica in (1, 2)],
               "fails": [], "unstable": [], "cleanup_cap": 0}
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
