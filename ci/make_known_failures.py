"""Build and enforce a shrinking baseline from complete, source-bound hosted JUnits."""
import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import partial
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
from hosted_ci import TIMING_NODE, digest, unique_object
from canary_evidence import (collection_error, failed_nodes, node as report_node, outcome as report_outcome,
                             quarantine_xfail, recovered_nodes, report_cases)
from latest_reports import artifact_shard, earlier_reports, planned_shards, producing_attempt
from quarantine_registry import SAFE_NODE, canonical_node, expired_entries, utc_today

KNOWN = "ci/known-failures.json"
CLASSES = ("baseline", "cleanup", "unstable", "baseline-error")


def classes(data):
    return {kind: {row["node"] for row in data["fails"] + data["unstable"] if row["class"] == kind}
            for kind in CLASSES}


def listed_nodes(current):
    return set().union(*classes(current).values()) if current else set()


def single_replica_event():
    kind = os.getenv("GITHUB_EVENT_NAME")
    if kind == "push" and (ref := os.getenv("GITHUB_REF")):
        import working_policy as policy
        return ref == "refs/heads/" + policy.default_branch(policy.event())
    return kind in {"pull_request", "merge_group"}


def first_attempt_gate(plan):
    return plan["replicas"] == [1] and not plan.get("smoke") and single_replica_event()


def retry_nodes(cases, listed):
    return [node for node in failed_nodes(cases) if canonical_node(node) not in listed]


def require_root_runs(runs):
    if not isinstance(runs, list) or len(runs) != 2:
        raise ValueError("Root evidence requires two exact run identities")
    if any(not isinstance(row, dict) or set(row) != {"run_id", "run_attempt", "replica"}
           or not all(isinstance(row[key], str) and row[key] for key in ("run_id", "run_attempt"))
           or type(row["replica"]) is not int or row["replica"] not in (1, 2) for row in runs):
        raise ValueError("Root evidence requires two exact run identities")
    if len({tuple(row[key] for key in ("run_id", "run_attempt", "replica")) for row in runs}) != 2:
        raise ValueError("Root evidence requires two distinct full executions")


def decode_list(raw):
    data = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) != ({"schema", "tree", "root_evidence_sha256", "root_runs", "cleanup_cap", "fails", "unstable"} | ({"classifications_sha256"} if data.get("schema") == 3 else set()))
            or type(data["schema"]) is not int or data["schema"] not in (2,3)
            or not isinstance(data["tree"], str) or not re.fullmatch(r"[a-f0-9]{40}", data["tree"])
            or not isinstance(data["root_evidence_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["root_evidence_sha256"])
            or type(data["cleanup_cap"]) is not int or data["cleanup_cap"] < 0
            or not isinstance(data["fails"], list) or not isinstance(data["unstable"], list)):
        raise ValueError("Invalid classified known-failure file")
    if data["schema"]==3 and (not isinstance(data["classifications_sha256"],str) or not re.fullmatch(r"[a-f0-9]{64}",data["classifications_sha256"])):
        raise ValueError("Classifications provenance digest is missing")
    require_root_runs(data["root_runs"])
    for section in ("fails", "unstable"):
        for row in data[section]:
            if not isinstance(row, dict) or not isinstance(row.get("node"), str) or not SAFE_NODE.fullmatch(row["node"]):
                raise ValueError("Invalid or unredacted failure node")
            kind = row.get("class")
            keys = {"node", "class"} | ({"owner", "reason"} if data["schema"]==3 else set()) | ({"issue"} if kind in {"cleanup", "unstable"} else set())
            if data["schema"]==3 and any(not isinstance(row[k],str) or not row[k].strip() or "TODO" in row[k] for k in ("owner","reason")):
                raise ValueError("Every failure entry needs reviewed owner and reason")
            if kind == "unstable":
                keys.add("expires_on")
            if (kind not in ({"baseline", "cleanup", "baseline-error"} if section == "fails" else {"unstable"}) or set(row) != keys
                    or kind in {"cleanup", "unstable"} and (type(row["issue"]) is not int or row["issue"] < 1)):
                raise ValueError("Every entry requires its class and the class-specific issue/expiry metadata")
            if kind == "unstable":
                if (not isinstance(row["expires_on"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["expires_on"])
                        or date.fromisoformat(row["expires_on"]).isoformat() != row["expires_on"]):
                    raise ValueError("Unstable expiry must be an ISO date")
    nodes = [row["node"] for row in data["fails"] + data["unstable"]]
    if len(nodes) != len(set(nodes)) or any(rows != sorted(rows, key=lambda row: row["node"]) for rows in (data["fails"], data["unstable"])):
        raise ValueError("Known-failure entries must be distinct and sorted")
    return data


def require_class_ratchet(current, base):
    if current["schema"]!=base["schema"] or current.get("classifications_sha256")!=base.get("classifications_sha256"):
        raise ValueError("Reviewed classification provenance may not change in an ordinary ratchet")
    now, before = classes(current), classes(base)
    if before["cleanup"] & now["baseline"]:
        raise ValueError("Moving cleanup entries to baseline is refused")
    if set.union(*now.values()) - set.union(*before.values()):
        raise ValueError("Additions to the known-failure list are refused")
    if any(len(now[kind]) > len(before[kind]) for kind in now):
        raise ValueError("Every class count may only go down")
    if current["cleanup_cap"] > base["cleanup_cap"]:
        raise ValueError("The cleanup cap may only go down")
    if (current["tree"] != base["tree"] or current["root_evidence_sha256"] != base["root_evidence_sha256"]
            or current["root_runs"] != base["root_runs"]):
        raise ValueError("The original baseline tree and root evidence must be preserved")


def require_root_binding(current, evidence, evidence_sha):
    if current:
        if current["root_evidence_sha256"] != evidence_sha or current["root_runs"] != evidence["runs"]:
            raise ValueError("Known-failure list differs from the sealed root evidence")
        if len(classes(current)["baseline-error"]) > evidence["counts"].get("baseline-error", 0):
            raise ValueError("Baseline-error allowance exceeds the measured root ceiling")
        if current["cleanup_cap"] > evidence["counts"]["cleanup"]:
            raise ValueError("Cleanup cap exceeds the first-two-root-run measurement")


def encoded(data):
    return json.dumps(data, indent=2) + "\n"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def at_revision(revision):
    names = git("ls-tree", "-r", "--name-only", revision, "--", KNOWN).splitlines()
    return decode_list(git("show", revision + ":" + KNOWN)) if names else None


def context(base_sha):
    base_sha = base_sha or git("rev-parse", "HEAD")
    base, current = at_revision(base_sha), at_revision("HEAD")
    return {"base_sha": base_sha, "bootstrap": base is None, "base": base, "current": current,
            "replicas": [1] if base is not None and single_replica_event() else [1, 2]}


def require_bootstrap_inputs(issue, expires_on, *, today=None):
    message = ("Bootstrap requires positive integer CI_UNSTABLE_ISSUE and ISO CI_UNSTABLE_EXPIRES_ON "
               "1 to 90 days after the UTC run date")
    if (type(issue) is not int or issue < 1 or not isinstance(expires_on, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires_on)):
        raise ValueError(message)
    try:
        expiry = date.fromisoformat(expires_on)
    except ValueError:
        raise ValueError(message) from None
    today = today or datetime.now(timezone.utc).date()
    if not 1 <= (expiry - today).days <= 90:
        raise ValueError(message)


def preflight(ctx, *, smoke, limit, issue, expires_on,
              baseline_path=Path("ci/frozen-baseline-failures.json"),
              exceptions_path=Path("ci/hosted-env-exceptions.json"),
              root_path=Path("ci/root-run-evidence.json"), removed_path=Path("ci/removed-tests.json")):
    if limit < 1:
        raise ValueError("The unstable cap must be positive")
    if smoke:
        return
    current, base = ctx["current"], ctx["base"]
    if base and current is None:
        raise ValueError("Removing the known-failure file is refused")
    if current:
        if len(current["unstable"]) > limit:
            raise ValueError("Unstable entry cap exceeded")
        if any(date.fromisoformat(row["expires_on"]) <= datetime.now(timezone.utc).date() for row in current["unstable"]):
            raise ValueError("Expired unstable entries require a fix and removal from the allowance list")
    if base and current:
        require_class_ratchet(current, base)
    if ctx["bootstrap"]:
        require_bootstrap_inputs(issue, expires_on)
    baseline, baseline_sha = load_frozen_baseline(baseline_path)
    load_env_exceptions(exceptions_path)
    root_evidence, root_sha = load_root_evidence(root_path, baseline_sha)
    require_root_binding(current, root_evidence, root_sha)
    removed, _ = load_removed_tests(removed_path)
    deleted = set(git("diff", "--diff-filter=D", "--name-only", ctx["base_sha"], "HEAD", "--", "tests").splitlines())
    deleted = {name for name in deleted if re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py", name)}
    if deleted - {row["file"] for row in removed}:
        raise ValueError("Deleted test files require a reason and node count in ci/removed-tests.json")
    if current:
        groups = classes(current)
        if current["schema"]==3:
            from failure_classifications import validate
            validate(current,Path("ci/failure-classifications.json").read_bytes(),SAFE_NODE.fullmatch)
        elif groups["baseline"] - baseline or groups["cleanup"] & baseline:
            raise ValueError("Entry class disagrees with the frozen baseline input")


@dataclass
class ReportEvidence:
    recovered: dict[str, str] = field(default_factory=dict)
    reruns: dict[str, int] = field(default_factory=dict)
    unobserved: list[dict] = field(default_factory=list)
    first_receipts: dict[tuple, Path] = field(default_factory=dict)


def collect_reports(plan, directory, run_id, run_attempt, output=None, *, listed=None, historical_capture=False,
                    evidence=None, quarantine_nodes=None):
    first_attempt = not historical_capture and first_attempt_gate(plan)
    if evidence is None:
        evidence = ReportEvidence()
    expected = planned_shards(plan)
    seal = digest(plan)

    def read_shard(path, root, *, earlier=False):
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        identity = (receipt["suite"], receipt["shard"], receipt["replica"])
        if (receipt.get("schema") != 1 or type(receipt["shard"]) is not int or type(receipt["replica"]) is not int
                or identity not in expected or receipt["sha"] != plan["sha"]
                or receipt["tree"] != plan["tree"] or receipt["run_id"] != run_id):
            raise ValueError("Duplicate, unexpected or incorrectly bound shard")
        attempt = run_attempt if historical_capture else producing_attempt(root, path, identity, run_attempt)
        if receipt["run_attempt"] != attempt:
            raise ValueError("Duplicate, unexpected or incorrectly bound shard")
        if receipt["plan_sha256"] != seal:
            # "Re-run all jobs" replans from the moved dev tip; an older plan's outcomes are not comparable.
            if earlier:
                return None
            raise ValueError("Duplicate, unexpected or incorrectly bound shard")
        suite, index, _ = identity
        files = plan["suites"][suite]["shards"][index - 1]["files"]
        report_names = ["junit.xml"]
        if suite == "unit" and TIMING_NODE.split("::")[0] in files:
            report_names.append("junit-timing.xml")
        if [row["file"] for row in receipt["reports"]] != report_names:
            raise ValueError("Missing or unexpected invocation report")
        outcomes, retries = {}, {}
        for row in receipt["reports"]:
            report = path.parent / row["file"]
            cases = report_cases(report, row, selected_scope=files if "selection" in plan else ())
            recovered = set()
            if "retry" in row:
                eligible = listed
                if first_attempt and eligible is None:
                    eligible = listed_nodes(at_revision(plan["sha"]))
                recoverable = retry_nodes(cases, eligible) if first_attempt else ()
                recovered = recovered_nodes(report, row, recoverable, allow_retry=first_attempt)
            for case in cases:
                node = report_node(case)
                if node.split("::")[0] not in files:
                    raise ValueError("Reported node lies outside its planned shard")
                canonical = canonical_node(node)
                if canonical in outcomes:
                    raise ValueError("A node executed more than once in a full run")
                outcome = report_outcome(case)
                # Bind quarantine xfails to the source registry before recording the outcome.
                if quarantine_nodes is not None and outcome == "skipped" and quarantine_xfail(case):
                    if canonical not in quarantine_nodes:
                        raise ValueError("Quarantine xfail has no source registry entry: " + canonical)
                    outcome = "failed"
                outcomes[canonical] = outcome
                if node in recovered:
                    retries[canonical] = outcome
        return identity, int(attempt), outcomes, retries

    latest, first_outcomes = {}, {}
    runs = {replica: {} for replica in plan["replicas"]}
    for path in sorted(directory.rglob("shard-receipt.json")):
        identity, attempt, outcomes, retries = read_shard(path, directory)
        if identity in latest:
            raise ValueError("Duplicate, unexpected or incorrectly bound shard")
        latest[identity] = attempt
        run = runs[identity[2]]
        if run.keys() & outcomes.keys():
            raise ValueError("A node executed more than once in a full run")
        run.update(outcomes)
        if first_attempt:
            evidence.first_receipts[identity] = path
            first_outcomes[identity] = outcomes
        evidence.recovered.update(retries)
    if latest.keys() != expected:
        raise ValueError("Missing shard: every planned producer must report")
    if len({tuple(sorted(run)) for run in runs.values()}) != 1:
        raise ValueError("Full runs collected different node sets")

    if not historical_capture:
        archive = earlier_reports(directory)
        if archive.is_symlink():
            raise ValueError("Invalid earlier shard artifact directory")
        seen = {(identity, attempt) for identity, attempt in latest.items()}
        for folder in sorted(archive.iterdir()) if archive.exists() else ():
            named_attempt, identity = artifact_shard(folder)
            attempt = int(named_attempt)
            if identity not in expected or not 1 <= attempt < latest[identity]:
                raise ValueError("Earlier shard evidence differs from the artifact identity or attempt")
            for path in folder.rglob("shard-receipt.json"):
                observed = read_shard(path, archive, earlier=True)
                if observed is None:
                    continue
                _, _, outcomes, retries = observed
                if first_attempt and attempt == 1:
                    evidence.first_receipts[identity] = path
                    first_outcomes[identity] = outcomes
                    evidence.recovered.update(retries)
                if (identity, attempt) in seen:
                    raise ValueError("Duplicate shard artifact attempt: " + folder.name)
                seen.add((identity, attempt))
                for node, outcome in outcomes.items():
                    if (outcome in {"failed", "error"} and all(run.get(node) == "passed" for run in runs.values())
                            and (node not in evidence.reruns or attempt < evidence.reruns[node])):
                        evidence.recovered[node] = outcome
                        evidence.reruns[node] = attempt
        evidence.unobserved.extend(
            {"suite": suite, "shard": shard, "replica": replica, "attempt": attempt}
            for suite, shard, replica in sorted(expected) for attempt in range(1, int(run_attempt))
            if ((suite, shard, replica), attempt) not in seen)

    if evidence.recovered and output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "recovered-tests.json").write_text(encoded([
            {"node": node, "class": "unstable", "observations": [outcome, "passed"]}
            for node, outcome in sorted(evidence.recovered.items())
        ]), encoding="utf-8")
    if first_attempt:
        first_run = {}
        for outcomes in first_outcomes.values():
            if first_run.keys() & outcomes.keys():
                raise ValueError("A node executed more than once in a full run")
            first_run.update(outcomes)
        return [first_run]
    return [runs[replica] for replica in plan["replicas"]]


def differing(runs):
    return {node for node in runs[0] if len({run[node] for run in runs}) > 1}


def load_frozen_baseline(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError("Missing frozen baseline file; bootstrap is refused") from None
    nodes = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(nodes, list) or any(not isinstance(node, str) or not SAFE_NODE.fullmatch(node) for node in nodes)
            or nodes != sorted(set(nodes))):
        raise ValueError("Frozen baseline must contain only sorted, distinct, anonymous node IDs")
    return set(nodes), hashlib.sha256(raw).hexdigest()


def evidence_reference(value):
    if not isinstance(value, str):
        return False
    if re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        return True
    link = urlsplit(value)
    return (link.scheme == "https" and bool(link.hostname) and link.username is None
            and link.password is None and not any(character.isspace() for character in value))


def load_env_exceptions(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError("Missing hosted environment exception file") from None
    data = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {"schema", "exceptions"} or type(data["schema"]) is not int or data["schema"] != 1:
        raise ValueError("Invalid hosted environment exception file")
    rows = data["exceptions"]
    if (not isinstance(rows, list) or any(not isinstance(row, dict)
            or set(row) != {"node", "reason", "evidence", "review"}
            or not isinstance(row["node"], str) or not SAFE_NODE.fullmatch(row["node"])
            or not isinstance(row["reason"], str) or not row["reason"].strip()
            or not evidence_reference(row["evidence"]) or not evidence_reference(row["review"]) for row in rows)):
        raise ValueError("Environment exceptions require a safe node, reason, evidence and review reference")
    nodes = [row["node"] for row in rows]
    if nodes != sorted(set(nodes)):
        raise ValueError("Environment exception nodes must be sorted and distinct")
    return set(nodes), hashlib.sha256(raw).hexdigest()


def classified_results(runs, baseline, exceptions, recovered=()):
    errors = {node for run in runs for node, outcome in run.items() if outcome == "error"}
    ordinary = [{node:value for node,value in run.items() if node not in errors} for run in runs]
    unstable = (differing(ordinary) | set(recovered)) - set(exceptions)
    stable = {node for node in ordinary[0] if all(run[node] == "failed" for run in ordinary)} - unstable - set(exceptions)
    return {"baseline": stable & set(baseline), "cleanup": stable - set(baseline), "unstable": unstable, "baseline-error": errors}


def load_removed_tests(path):
    raw = path.read_bytes()
    data = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) != ({"schema", "removed", "removed_nodes"} if data.get("schema")==2 else {"schema", "removed"}) or type(data["schema"]) is not int
            or data["schema"] not in (1,2) or not isinstance(data["removed"], list)):
        raise ValueError("Invalid removed-tests ledger")
    if data["schema"]==2:
        from node_census import removed_nodes
        removed_nodes(raw,SAFE_NODE.fullmatch)
    rows = data["removed"]
    if any(not isinstance(row, dict) or set(row) != {"file", "node_count", "reason", "synthetic_version_owed"}
           or not isinstance(row["file"], str) or not re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py", row["file"])
           or type(row["node_count"]) is not int or row["node_count"] < 0
           or not isinstance(row["reason"], str) or not row["reason"].strip()
           or type(row["synthetic_version_owed"]) is not bool for row in rows):
        raise ValueError("Removed tests require file, node count, reason and synthetic-data obligation")
    names = [row["file"] for row in rows]
    if names != sorted(set(names)):
        raise ValueError("Removed test files must be sorted and distinct")
    return rows, hashlib.sha256(raw).hexdigest()


def load_root_evidence(path, baseline_sha):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError("Missing first-two-root-run evidence") from None
    data = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) - {"measurements", "performance"} != {"schema", "sha", "tree", "base_sha", "baseline_sha256", "runs", "counts", "receipts_sha256"}
            or type(data["schema"]) is not int or data["schema"] != 1 or data["baseline_sha256"] != baseline_sha
            or not all(isinstance(data[key], str) and re.fullmatch(r"[a-f0-9]{40}", data[key]) for key in ("sha", "tree", "base_sha"))
            or not isinstance(data["receipts_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["receipts_sha256"])
            or not isinstance(data["counts"], dict) or set(data["counts"]) not in ({"baseline", "cleanup", "unstable"}, {"baseline", "cleanup", "unstable", "baseline-error"})
            or any(type(value) is not int or value < 0 for value in data["counts"].values())
            or not isinstance(data["runs"], list) or len(data["runs"]) != 2):
        raise ValueError("Invalid or incorrectly bound first-two-root-run evidence")
    require_root_runs(data["runs"])
    if "measurements" in data:
        from known_skips import require_measurements
        require_measurements(data)
    if (git("rev-list", "--max-parents=0", "HEAD") != data["base_sha"]
            or git("merge-base", data["base_sha"], data["sha"]) != data["base_sha"]
            or git("rev-parse", data["sha"] + "^{tree}") != data["tree"]):
        raise ValueError("Measurement evidence must identify audited PR1 with this repository's root as its base and ancestor")
    return data, hashlib.sha256(raw).hexdigest()


def candidate_list(runs, baseline, tree, issue, expires_on, limit, *, cleanup_cap, root_evidence_sha256, root_runs,
                   cleanup_issue=0, exceptions=frozenset(), today=None):
    if len(runs) != 2 or set(runs[0]) != set(runs[1]):
        raise ValueError("Bootstrap requires two complete full runs of the same candidate")
    require_bootstrap_inputs(issue, expires_on, today=today)
    errors = {node for run in runs for node, outcome in run.items() if outcome == "error"}
    ordinary = [{node:outcome for node,outcome in run.items() if node not in errors} for run in runs]
    groups = classified_results(ordinary, baseline, exceptions)
    if len(groups["cleanup"]) > cleanup_cap:
        raise ValueError("Cleanup entry cap from the first two root runs exceeded")
    if groups["cleanup"] and (type(cleanup_issue) is not int or cleanup_issue < 1):
        raise ValueError("Cleanup entries require positive CI_CLEANUP_ISSUE")
    if len(groups["unstable"]) > limit:
        raise ValueError("Unstable entry cap exceeded")
    fails = ([{"node": node, "class": "baseline"} for node in groups["baseline"]]
             + [{"node": node, "class": "cleanup", "issue": cleanup_issue} for node in groups["cleanup"]]
             + [{"node": node, "class": "baseline-error"} for node in errors])
    data = {"schema": 2, "tree": tree, "root_evidence_sha256": root_evidence_sha256, "root_runs": root_runs, "cleanup_cap": cleanup_cap,
            "fails": sorted(fails, key=lambda row: row["node"]),
            "unstable": [{"node": node, "class": "unstable", "issue": issue, "expires_on": expires_on}
                         for node in sorted(groups["unstable"])]}
    return decode_list(encoded(data))


def outside_observations(current, runs, exceptions=frozenset()):
    known = set.union(*classes(current).values()) if current else set()
    outside = {node for run in runs for node, value in run.items() if value in {"failed", "error"}} - known - set(exceptions)
    return [{"node": node, "test_file": node.split("::", 1)[0],
             "observations": [run.get(node, "NOT_RUN") for run in runs],
             "red_count": sum(run.get(node) in {"failed", "error"} for run in runs)} for node in sorted(outside)]


def product_error_nodes(directory, *, listed=frozenset(), single_replica=False, quarantined=frozenset(), receipts=None):
    """Classify individual errors after the producer and report checks."""
    infrastructure = re.compile(r"connection refused|could not connect|server closed the connection|connection reset|could not translate host|temporary failure in name resolution|no route to host|network is unreachable|too many connections|remaining connection slots|password authentication failed|database is locked|database is busy|deadlock detected|no space left on device|out of memory|browser executable.*(?:missing|exist)|ECONNREFUSED|permission denied", re.I)
    product_types = {"AssertionError", "AttributeError", "KeyError", "NameError", "TypeError", "ValueError", "IndexError", "LookupError", "ZeroDivisionError", "NotImplementedError", "IntegrityError", "ProgrammingError", "DataError"}
    result = set()
    infrastructure_nodes = set()
    listed = set(listed) | set(quarantined)
    if receipts is None:
        receipts = directory.rglob("shard-receipt.json")
    for receipt_path in sorted(receipts):
        receipt = json.loads(receipt_path.read_bytes())
        for row in receipt["reports"]:
            report = receipt_path.parent / row["file"]
            cases = list(ET.parse(report).getroot().iter("testcase"))
            for case in cases:
                error = case.find("error")
                element = error if error is not None else case.find("failure")
                if element is None:
                    continue
                raw_node = report_node(case)
                node = canonical_node(raw_node)
                message, text = element.get("message", ""), element.text or ""
                if collection_error(case):
                    raise ValueError("Collection error in test evidence: " + node)
                if node in quarantined or (node in listed and not single_replica):
                    continue
                summary = message + "\n" + "\n".join(line for line in text.splitlines() if line.startswith("E "))
                if infrastructure.search(summary):
                    if not single_replica:
                        raise ValueError("Infrastructure failure in test evidence: " + node)
                    infrastructure_nodes.add(node)
                    if len(infrastructure_nodes) > 1:
                        raise ValueError("Infrastructure failure in multiple test nodes: "
                                         + ", ".join(sorted(infrastructure_nodes)))
                    if error is not None and node not in listed:
                        result.add(node)
                    continue
                if error is None or node in listed:
                    continue
                kinds = re.findall(r"(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception)\b", message or text)
                kind = kinds[0].rsplit(".", 1)[-1] if kinds else "UNKNOWN"
                source = re.search(r"(?<![A-Za-z0-9_-])(?:tests|apps|packages|\.github/scripts)/[A-Za-z0-9_./-]+\.py(?::\d+)?", text)
                schema_error = kind == "OperationalError" and re.search(r"no such (?:table|column)|undefined(?:table|column)|(?:table|column).*does not exist", summary, re.I)
                if source is None or kind not in product_types and not schema_error:
                    raise ValueError("Unclassified test error needs review: " + node)
                result.add(node)
    return result


def mixed_listed_nodes(current, runs, recovered=()):
    if not current:
        return set()
    listed = set.union(*classes(current).values())
    mixed = listed & set(recovered)
    if len(runs) == 2 and set(runs[0]) == set(runs[1]):
        mixed.update(node for node in listed & set(runs[0])
                     if {run[node] for run in runs} in ({"passed", "failed"}, {"passed", "error"}))
    return mixed


def report_outside(current, runs, output, exceptions=frozenset(), recovered=None, *, reruns=None, unobserved=(),
                   quarantined=frozenset()):
    recovered = recovered or {}
    exceptions = set(exceptions) | set(quarantined)
    reruns = reruns or {}

    def recovery_note(node, default=""):
        if node in reruns:
            return f" | recovered on re-run (attempt {reruns[node]})"
        return " | recovered on retry" if node in recovered else default

    groups = classes(current) if current else {}
    known = set().union(*groups.values())
    rows = {row["node"]: row for row in outside_observations(current, runs, exceptions)}
    rows.update({node: {"node": node, "test_file": node.split("::", 1)[0],
                        "observations": [recovered[node], "passed"], "red_count": 1}
                 for node in set(recovered) - known - exceptions})
    rows = [rows[node] for node in sorted(rows)]
    proposed = [{**row, "class": "unstable", "status": "PROPOSED"} for row in rows
                if len(row["observations"]) == 2 and row["red_count"] == 1 and "passed" in row["observations"]]
    unstable = groups.get("unstable", set())
    listed = [{"node": node, "test_file": node.split("::", 1)[0],
               "observations": [recovered[node], "passed"] if node in recovered else [run[node] for run in runs],
               "class": "unstable",
               "status": "ALREADY_UNSTABLE" if node in unstable else "PROPOSED_REMARK"}
              for node in sorted(mixed_listed_nodes(current, runs, recovered) - set(quarantined))]
    (output / "outside-observations.json").write_text(encoded({"observations": rows, "unstable_count": len(proposed),
        "proposed_list_entries": proposed, "listed_unstable_count": len(listed), "listed_unstable": listed,
        "unobserved_attempts": list(unobserved)}), encoding="utf-8")
    lines = ["UNSTABLE " + row["node"] + " | test file " + row["test_file"]
             + recovery_note(row["node"], " | red 1/2")
             + " | proposed for reviewed list" for row in proposed]
    lines.insert(0, "UNSTABLE outside-list nodes: " + str(len(proposed)))
    lines.append("UNSTABLE listed nodes: " + str(len(listed)))
    lines.extend("UNSTABLE " + row["node"] + " | test file " + row["test_file"] + " | " + row["status"]
                 + recovery_note(row["node"]) for row in listed)
    lines.extend("unobserved attempt | {suite} | shard {shard} | replica {replica} | attempt {attempt}".format(**row)
                 for row in unobserved)
    for line in lines:
        print(line)
    if lines and os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    return proposed


def stale_report_dir(output):
    return output if os.getenv("GITHUB_EVENT_NAME") in {"merge_group", "push"} else None


def write_stale_report(output, sha, nodes, *, reported=None):
    run_id = os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[a-f0-9]{40}", sha or ""):
        raise ValueError("Stale report requires the producing run and commit")
    report = {"run_id": run_id, "sha": sha, "nodes": sorted(nodes)}
    if reported:
        report["reported"] = dict(sorted(reported.items()))
    (output / "stale-report.json").write_text(encoded(report), encoding="utf-8")


def write_summary_lines(lines):
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(text + "\n")


def report_pr_stale(output, sha, lookup, nodes):
    reported = lookup(nodes) if nodes else {}
    if reported:
        write_stale_report(output, sha, nodes, reported=reported)
        write_summary_lines([f"- {node}: stale on dev (run {run_id}); removal candidate"
                             for node, run_id in sorted(reported.items())])
    return reported


def report_quarantine(runs, quarantine, today):
    passing = sum(all(run.get(node) == "passed" for run in runs) for node in quarantine)
    failing = sorted(node for node in quarantine if any(run.get(node) in {"failed", "error"} for run in runs))
    expired = expired_entries(quarantine, today)
    write_summary_lines([f"quarantined and passing now: {passing}",
                         f"quarantined and failing now: {len(failing)}",
                         *(f"- {node}: quarantined-failing" for node in failing),
                         f"expired quarantine (still active): {len(expired)}",
                         *(f"- {row['nodeid']}: expired {row['expires']}, issue {row['issue']}" for row in expired)])


def release_failure_list(current, quarantine):
    """Apply the release ratchet to the reviewed classes regardless of their storage."""
    current = {**current, "fails": list(current["fails"]), "unstable": list(current["unstable"])}
    for node, row in quarantine.items():
        kind = row["reason"].split(":", 1)[0].strip()
        if kind not in CLASSES:
            raise ValueError("Invalid quarantine failure class for release: " + node)
        entry = {"node": node, "class": kind}
        if kind == "unstable":
            current["unstable"].append({**entry, "expires_on": row["expires"]})
        else:
            current["fails"].append(entry)
    return current


def evaluate(current, base, runs, *, bootstrap_candidate=None, bootstrap_add_only=False,
             group=False, merge_group=False, recovered=frozenset(),
             limit=80, exceptions=frozenset(), today=None, product_errors=frozenset(),
             selected_files=None, stale_output=None, expected_replicas=2, stale_sha=None,
             observe_stale=None, quarantine=None, release=False, quarantine_registry=None):
    today = today or utc_today()
    quarantined = set() if release else set(quarantine or ())
    if quarantine:
        report_quarantine(runs, quarantine, today)
    if release:
        registry = (quarantine or {}) if quarantine_registry is None else quarantine_registry
        blockers = sorted(node for node, row in registry.items() if row["blocker"])
        if blockers:
            raise ValueError("Release blockers remain in quarantine: " + ", ".join(blockers))
    if current is None:
        raise ValueError("Bootstrap candidate uploaded; add only ci/known-failures.json in the first PR")
    if release and quarantine:
        current = release_failure_list(current, quarantine)
        if base is not None:
            base = release_failure_list(base, quarantine)
    groups = {kind: nodes - quarantined for kind, nodes in classes(current).items()}
    if len(groups["unstable"]) > limit:
        raise ValueError("Unstable entry cap exceeded")
    if len(groups["cleanup"]) > current["cleanup_cap"]:
        raise ValueError("Cleanup entry cap exceeded")
    if any(date.fromisoformat(row["expires_on"]) <= today for row in current["unstable"]):
        raise ValueError("Expired unstable entries require a fix and removal from the allowance list")
    current_nodes = set.union(*groups.values())
    if base is None:
        if not bootstrap_add_only or current != bootstrap_candidate:
            raise ValueError("Bootstrap must add only the exact candidate produced by its own two full runs")
    else:
        require_class_ratchet(current, base)
        moved = {row["node"] for row in base["fails"]} & groups["unstable"]
        if moved and (merge_group or len(runs) != 2 or not moved <= differing(runs)):
            raise ValueError("Moving failures to unstable requires two full runs with different results")
    if group:
        recovered = ()
        if base is not None:
            expected_replicas = 1
    observed = set(runs[0])
    mixed_listed = mixed_listed_nodes(current, runs, recovered)
    problems = []
    outside = outside_observations(current, runs, set(exceptions) | quarantined)
    transient = {row["node"] for row in outside if expected_replicas == len(runs) == 2 and set(runs[0]) == set(runs[1])
                 and row["red_count"] == 1 and "passed" in row["observations"]}
    if outside and (len(runs) != expected_replicas or any(set(run) != observed for run in runs)):
        required = "two complete replicas" if expected_replicas == 2 else "one complete replica"
        problems.append("New failures outside the known-failure list require " + required)
    expected = current_nodes if selected_files is None else {
        node for node in current_nodes if node.split("::", 1)[0] in selected_files}
    if expected - observed:
        problems.append("Vanished listed nodes: delete their lines from ci/known-failures.json")
    stable = (groups["baseline"] | groups["cleanup"] | groups["baseline-error"]) - set(recovered)
    all_stale = {node for node in stable if len(runs) == expected_replicas
                 and all(run.get(node) == "passed" for run in runs)}
    reported = all_stale & set(observe_stale(all_stale) or ()) if observe_stale is not None else set()
    if stale_output is not None:
        write_stale_report(stale_output, stale_sha, all_stale)
        write_summary_lines(["Stale passing failure entries: " + str(len(all_stale)),
                             *("- " + node for node in sorted(all_stale))])
    for run in runs:
        if any(run[node] == "skipped" for node in current_nodes & observed):
            problems.append("A listed test did not execute; skipped is not passing")
        errors = {node for node, outcome in run.items() if outcome == "error"}
        new_errors = errors - quarantined - groups["baseline-error"] - mixed_listed - (transient & set(product_errors))
        if new_errors:
            problems.append("New errors outside the baseline-error class: " + ", ".join(sorted(new_errors)))
        stale = stable & {node for node, outcome in run.items() if outcome == "passed"}
        stale -= mixed_listed | reported
        if stale and stale_output is None:
            problems.append("Stale passing failure entries: delete their lines from ci/known-failures.json: " + ", ".join(sorted(stale)))
        failures = {node for node, outcome in run.items() if outcome == "failed"}
        new_failures = failures - quarantined - (current_nodes - groups["baseline-error"]) - set(exceptions) - transient - mixed_listed
        if new_failures:
            problems.append("New failures outside the known-failure list: " + ", ".join(sorted(new_failures)))
    if problems:
        raise ValueError("; ".join(dict.fromkeys(problems)))
    return True


def write_summary():
    path = Path(KNOWN) if Path(KNOWN).exists() else Path(".run/ratchet/known-failures.candidate.json")
    try:
        data = decode_list(path.read_text(encoding="utf-8"))
        counts = {kind: len(nodes) for kind, nodes in classes(data).items()}
        cap = data["cleanup_cap"]
    except (OSError, ValueError, TypeError):
        counts, cap = dict.fromkeys(("baseline", "cleanup", "unstable", "baseline-error"), "UNKNOWN"), "UNKNOWN"
    text = "CI classes: " + ", ".join(kind + "=" + str(value) for kind, value in counts.items()) + "; cleanup_cap=" + str(cap)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text + "\n")
    return counts


def verify_privacy(data, matcher_path, table_path, staff_path):
    matcher_hash = hashlib.sha256(Path(matcher_path).read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("node_privacy_matcher", matcher_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    policy = module.load_policy(table_path, staff_path)
    nodes = data if isinstance(data, list) else [row["node"] for row in data["fails"] + data["unstable"]]
    if any(policy.deny(node) for node in nodes):
        raise ValueError("Node identity refused by the shared privacy matcher")
    policy.unchanged()
    if hashlib.sha256(Path(matcher_path).read_bytes()).hexdigest() != matcher_hash:
        raise ValueError("Shared privacy matcher changed during verification")
    return {**policy.hashes(), "matcher_sha256": matcher_hash}


def main(*, runs=None, evidence=None, product_errors=None, stale_lookup=None, nightly=False,
         quarantine=None, release=False, quarantine_registry=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".run/ratchet"))
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--unstable-limit", type=int, default=80)
    parser.add_argument("--cleanup-issue", type=int, default=0)
    parser.add_argument("--capture-root", action="store_true")
    parser.add_argument("--root-evidence", type=Path, default=Path("ci/root-run-evidence.json"))
    parser.add_argument("--removed-tests", type=Path, default=Path("ci/removed-tests.json"))
    parser.add_argument("--unstable-issue", type=int, default=0)
    parser.add_argument("--unstable-expires-on", default="")
    parser.add_argument("--frozen-baseline", type=Path, default=Path("ci/frozen-baseline-failures.json"))
    parser.add_argument("--env-exceptions", type=Path, default=Path("ci/hosted-env-exceptions.json"))
    parser.add_argument("--verify-privacy", type=Path)
    parser.add_argument("--deny-matcher", type=Path)
    parser.add_argument("--deny-table", type=Path)
    parser.add_argument("--staff-names", type=Path)
    args = parser.parse_args()
    if args.verify_privacy:
        raw = args.verify_privacy.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=unique_object)
        if isinstance(data, list):
            load_frozen_baseline(args.verify_privacy)
        else:
            data = decode_list(raw)
        verify_privacy(data,
                       args.deny_matcher, args.deny_table, args.staff_names)
        print("KNOWN-FAILURES PRIVACY PASS")
        return
    if args.unstable_limit < 1 or args.plan is None or args.reports is None:
        raise ValueError("A positive unstable cap and complete evidence paths are required")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if plan["sha"] != git("rev-parse", "HEAD") or plan["tree"] != git("rev-parse", "HEAD^{tree}"):
        raise ValueError("Aggregate checkout differs from its full-run source")
    if args.capture_root and plan["smoke"]:
        raise ValueError("Smoke cannot capture root evidence")
    ctx = context(args.base_sha)
    if not args.capture_root:
        preflight(ctx, smoke=plan["smoke"], limit=args.unstable_limit,
              issue=args.unstable_issue, expires_on=args.unstable_expires_on,
              baseline_path=args.frozen_baseline, exceptions_path=args.env_exceptions,
              root_path=args.root_evidence, removed_path=args.removed_tests)
    if plan["replicas"] != ([1] if plan["smoke"] else ctx["replicas"]) or plan["base_sha"] != ctx["base_sha"]:
        raise ValueError("Aggregate context differs from the checked plan")
    if evidence is None:
        evidence = ReportEvidence()
    if runs is None:
        runs = collect_reports(plan, args.reports, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"],
                               args.output, evidence=evidence)
    add_only = (os.getenv("GITHUB_EVENT_NAME") in {"pull_request", "merge_group"}
                and git("diff", "--name-status", "--no-renames", ctx["base_sha"], "HEAD") == "A\t" + KNOWN)
    tree = git("rev-parse", ctx["base_sha"] + "^{tree}") if add_only else plan["tree"]
    args.output.mkdir(parents=True, exist_ok=True)
    selected_files = ({path for row in plan["suites"].values() for shard in row["shards"] for path in shard["files"]}
                      if "selection" in plan else None)
    quarantined = (quarantine or {}).keys()
    listed = listed_nodes(ctx["current"]) | quarantined
    first_attempt = first_attempt_gate(plan)
    try:
        if plan["smoke"]:
            if any(state != "passed" for run in runs for state in run.values()):
                raise ValueError("Smoke must execute and pass its selected wiring tests")
        else:
            baseline, baseline_sha = load_frozen_baseline(args.frozen_baseline)
            exceptions, exception_sha = load_env_exceptions(args.env_exceptions)
            print("Frozen baseline sha256=" + baseline_sha)
            if exceptions:
                print("::warning::Applying " + str(len(exceptions)) + " explicit hosted environment exceptions; evidence and review references are committed")
            if product_errors is None:
                product_errors = set() if args.capture_root else product_error_nodes(
                    args.reports, listed=listed, single_replica=first_attempt, quarantined=() if release else quarantined,
                    receipts=evidence.first_receipts.values() or None)
            recovered = evidence.recovered
            groups = classified_results(runs, baseline, exceptions, () if first_attempt else recovered)
            counts = {kind: len(nodes) for kind, nodes in groups.items()}
            (args.output / "cleanup-failures.json").write_text(json.dumps(sorted(groups["cleanup"]), indent=2) + "\n", encoding="utf-8")
            comparison = {"baseline_sha256": baseline_sha, "env_exceptions_sha256": exception_sha, "counts": counts}
            (args.output / "baseline-comparison.json").write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
            if args.capture_root:
                if plan["replicas"] != [1, 2] or git("rev-list", "--max-parents=0", "HEAD") != plan["sha"]:
                    raise ValueError("Root capture requires the first two complete executions of the parentless root")
                root_evidence = {"schema": 1, "sha": plan["sha"], "tree": plan["tree"], "baseline_sha256": baseline_sha,
                    "runs": [{"run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "replica": replica}
                             for replica in plan["replicas"]], "counts": counts,
                    "receipts_sha256": digest(sorted(hashlib.sha256(path.read_bytes()).hexdigest()
                                             for path in args.reports.rglob("shard-receipt.json")))}
                (args.output / "root-run-evidence.json").write_text(json.dumps(root_evidence, indent=2) + "\n", encoding="utf-8")
                print("Root class counts: " + json.dumps(counts, sort_keys=True))
                return
            root_evidence, root_sha = load_root_evidence(args.root_evidence, baseline_sha)
            comparison_list = release_failure_list(ctx["current"], quarantine) if release and quarantine else ctx["current"]
            require_root_binding(comparison_list, root_evidence, root_sha)
            report_outside(comparison_list, runs, args.output, exceptions, recovered,
                           reruns=evidence.reruns, unobserved=evidence.unobserved,
                           quarantined=() if release else quarantined)
            if first_attempt and any(row["attempt"] == 1 for row in evidence.unobserved):
                raise ValueError("Missing first-attempt shard evidence")
            candidate = (candidate_list(runs, baseline, tree, args.unstable_issue, args.unstable_expires_on,
                                        args.unstable_limit, cleanup_cap=root_evidence["counts"]["cleanup"],
                                        root_evidence_sha256=root_sha, root_runs=root_evidence["runs"], cleanup_issue=args.cleanup_issue,
                                        exceptions=exceptions) if ctx["bootstrap"] else ctx["current"])
            if candidate:
                (args.output / "known-failures.candidate.json").write_text(encoded(candidate), encoding="utf-8")
            observe_stale = None
            if stale_lookup is not None:
                observe_stale = partial(report_pr_stale, args.output, plan["sha"], stale_lookup)
            elif nightly:
                observe_stale = partial(write_stale_report, args.output, plan["sha"])
            evaluate(ctx["current"], ctx["base"], runs, bootstrap_candidate=candidate,
                     bootstrap_add_only=add_only, limit=args.unstable_limit, exceptions=exceptions,
                     group=first_attempt, merge_group=os.getenv("GITHUB_EVENT_NAME") == "merge_group",
                     recovered=recovered,
                     product_errors=product_errors, selected_files=selected_files,
                     stale_output=stale_report_dir(args.output), expected_replicas=len(ctx["replicas"]),
                     stale_sha=plan["sha"], observe_stale=observe_stale, quarantine=quarantine, release=release,
                     quarantine_registry=quarantine_registry)
            if ctx["bootstrap"] and Path(KNOWN).read_text(encoding="utf-8") != encoded(candidate):
                raise ValueError("Bootstrap file must exactly equal the canonical candidate bytes")
    except ValueError as error:
        verdict = {"passed": False, "reason": str(error)}
    else:
        verdict = {"passed": True, "reason": "Smoke wiring passed; no baseline qualification" if plan["smoke"] else "Known-failure ratchet passed"}
    if selected_files is not None:
        verdict["selection"] = plan["selection"]
        verdict["not_observed"] = sorted(node for node in listed if node.split("::", 1)[0] not in selected_files)
    (args.output / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    if ctx["bootstrap"]:
        print("::warning::Bootstrap baseline is permitted only in the add-only first PR with exact two-run evidence")
    print(verdict["reason"])


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit("Known-failure evidence refused: " + str(error)) from None
    except Exception as error:
        raise SystemExit("Known-failure evidence refused: " + type(error).__name__) from None
