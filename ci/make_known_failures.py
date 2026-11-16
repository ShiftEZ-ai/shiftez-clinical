"""Build and enforce a shrinking baseline from complete, source-bound hosted JUnits."""
import argparse
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github/scripts"))
from hosted_ci import TIMING_NODE, case_rows, digest, unique_object

KNOWN = "ci/known-failures.json"
BASE_NODE = r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py::(?:[A-Za-z_][A-Za-z0-9_]*::)*test_[A-Za-z0-9_]+"
SAFE_NODE = re.compile(BASE_NODE + r"(?:\[case-[a-p]{64}\])?")


def canonical_node(node):
    base, marker, parameter = node.partition("[")
    if not re.fullmatch(BASE_NODE, base) or marker and not parameter.endswith("]"):
        raise ValueError("Invalid collected node identity")
    if not marker:
        return base
    # Keep every parameter case distinct without publishing its possibly private display label.
    fingerprint = hashlib.sha256(parameter.encode("utf-8")).hexdigest().translate(
        str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    return base + "[case-" + fingerprint + "]"


def decode_list(raw):
    data = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) != {"schema", "tree", "fails", "unstable"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or not isinstance(data["tree"], str) or not re.fullmatch(r"[a-f0-9]{40}", data["tree"])
            or not isinstance(data["fails"], list) or not isinstance(data["unstable"], list)):
        raise ValueError("Invalid known-failure file")
    fails, unstable = data["fails"], data["unstable"]
    if any(not isinstance(node, str) or not SAFE_NODE.fullmatch(node) for node in fails):
        raise ValueError("Invalid or unredacted failure node")
    if any(not isinstance(row, dict) or set(row) != {"node", "issue", "expires_on"}
           or not isinstance(row["node"], str) or not SAFE_NODE.fullmatch(row["node"])
           or type(row["issue"]) is not int or row["issue"] < 1
           or not isinstance(row["expires_on"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["expires_on"])
           for row in unstable):
        raise ValueError("Unstable entries require a safe node, issue number and expiry")
    for row in unstable:
        if date.fromisoformat(row["expires_on"]).isoformat() != row["expires_on"]:
            raise ValueError("Expiry must be an ISO date")
    nodes = fails + [row["node"] for row in unstable]
    if (len(nodes) != len(set(nodes)) or fails != sorted(fails)
            or unstable != sorted(unstable, key=lambda row: row["node"])):
        raise ValueError("Known-failure entries must be distinct and sorted")
    return data


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
    moved = (set(base["fails"]) & {row["node"] for row in current["unstable"]}) if base and current else set()
    return {"base_sha": base_sha, "bootstrap": base is None, "base": base, "current": current,
            "replicas": [1, 2] if base is None or moved else [1]}


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


def preflight(ctx, *, smoke, limit, issue, expires_on):
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
            raise ValueError("Expired unstable entries require a fix or reviewed renewal")
    if base and current:
        before = set(base["fails"]) | {row["node"] for row in base["unstable"]}
        after = set(current["fails"]) | {row["node"] for row in current["unstable"]}
        if after - before:
            raise ValueError("Additions to the known-failure list are refused")
    if ctx["bootstrap"]:
        require_bootstrap_inputs(issue, expires_on)


def collect_reports(plan, directory, run_id, run_attempt):
    expected = {(suite, index, replica)
                for suite, row in plan["suites"].items()
                for index in range(1, len(row["shards"]) + 1) for replica in plan["replicas"]}
    seen, runs = set(), {replica: {} for replica in plan["replicas"]}
    for path in sorted(directory.rglob("shard-receipt.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        identity = (receipt["suite"], receipt["shard"], receipt["replica"])
        if (receipt.get("schema") != 1 or type(receipt["shard"]) is not int or type(receipt["replica"]) is not int
                or identity not in expected or identity in seen or receipt["sha"] != plan["sha"]
                or receipt["tree"] != plan["tree"] or receipt["plan_sha256"] != digest(plan)
                or receipt["run_id"] != run_id or receipt["run_attempt"] != run_attempt):
            raise ValueError("Duplicate, unexpected or incorrectly bound shard")
        seen.add(identity)
        suite, index, replica = identity
        files = plan["suites"][suite]["shards"][index - 1]["files"]
        report_names = ["junit.xml"]
        if suite == "unit" and TIMING_NODE.split("::")[0] in files:
            report_names.append("junit-timing.xml")
        if [row["file"] for row in receipt["reports"]] != report_names:
            raise ValueError("Missing or unexpected invocation report")
        for row in receipt["reports"]:
            report = path.parent / row["file"]
            sidecars = [report, report.with_suffix(".selection.json"), report.with_suffix(".deselected.json")]
            if any(item.is_symlink() or not item.is_file() or hashlib.sha256(item.read_bytes()).hexdigest()
                   != row["sha256"].get(item.name) for item in sidecars):
                raise ValueError("Missing or changed shard evidence")
            selected = json.loads(sidecars[1].read_text(encoding="utf-8"))
            cases = list(case_rows([report]))
            if (not selected or len(selected) != len(set(selected))
                    or sorted(node for node, _ in cases) != sorted(selected)
                    or type(row["exit_code"]) is not int or row["exit_code"] not in (0, 1)
                    or (row["exit_code"] == 1) != any(item["outcome"] == "failed" for _, item in cases)):
                raise ValueError("Incomplete execution or pytest infrastructure failure")
            for node, outcome in cases:
                if node.split("::")[0] not in files:
                    raise ValueError("Reported node lies outside its planned shard")
                canonical = canonical_node(node)
                if canonical in runs[replica]:
                    raise ValueError("A node executed more than once in a full run")
                runs[replica][canonical] = outcome["outcome"]
    if seen != expected:
        raise ValueError("Missing shard: every planned producer must report")
    if len({tuple(sorted(run)) for run in runs.values()}) != 1:
        raise ValueError("Full runs collected different node sets")
    return [runs[replica] for replica in plan["replicas"]]


def differing(runs):
    return {node for node in runs[0] if len({run[node] for run in runs}) > 1}


def candidate_list(runs, seeds, tree, issue, expires_on, limit, *, today=None):
    if len(runs) != 2 or set(runs[0]) != set(runs[1]):
        raise ValueError("Bootstrap requires two complete full runs of the same candidate")
    require_bootstrap_inputs(issue, expires_on, today=today)
    unstable = (set(seeds) & set(runs[0])) | differing(runs)
    if len(unstable) > limit:
        raise ValueError("Unstable entry cap exceeded")
    data = {"schema": 1, "tree": tree,
            "fails": sorted(node for node in runs[0] if all(run[node] == "failed" for run in runs) and node not in unstable),
            "unstable": [{"node": node, "issue": issue, "expires_on": expires_on} for node in sorted(unstable)]}
    return decode_list(encoded(data))


def evaluate(current, base, runs, *, bootstrap_candidate=None, bootstrap_add_only=False,
             limit=80, today=None):
    today = today or datetime.now(timezone.utc).date()
    if current is None:
        raise ValueError("Bootstrap candidate uploaded; add only ci/known-failures.json in the first PR")
    if len(current["unstable"]) > limit:
        raise ValueError("Unstable entry cap exceeded")
    expired = [row["node"] for row in current["unstable"] if date.fromisoformat(row["expires_on"]) <= today]
    if expired:
        raise ValueError("Expired unstable entries: fix them or renew them through review")
    current_nodes = set(current["fails"]) | {row["node"] for row in current["unstable"]}
    if base is None:
        if not bootstrap_add_only or current != bootstrap_candidate:
            raise ValueError("Bootstrap must add only the exact candidate produced by its own two full runs")
    else:
        base_nodes = set(base["fails"]) | {row["node"] for row in base["unstable"]}
        if current_nodes - base_nodes:
            raise ValueError("Additions to the known-failure list are refused")
        if current["tree"] != base["tree"]:
            raise ValueError("The original baseline tree must be preserved")
        moved = set(base["fails"]) & {row["node"] for row in current["unstable"]}
        if moved and (len(runs) != 2 or not moved <= differing(runs)):
            raise ValueError("Moving fails to unstable requires two full runs with different results")
    observed = set(runs[0])
    problems = []
    if current_nodes - observed:
        problems.append("Vanished listed nodes: delete their lines from ci/known-failures.json")
    for run in runs:
        if any(run[node] == "skipped" for node in current_nodes & observed):
            problems.append("A listed test did not execute; skipped is not passing")
        stale = set(current["fails"]) & {node for node, outcome in run.items() if outcome == "passed"}
        if stale:
            problems.append("Stale passing failure entries: delete their lines from ci/known-failures.json: " + ", ".join(sorted(stale)))
        failures = {node for node, outcome in run.items() if outcome == "failed"}
        if failures - current_nodes:
            problems.append("New failures outside the known-failure list: " + ", ".join(sorted(failures - current_nodes)))
    if problems:
        raise ValueError("; ".join(dict.fromkeys(problems)))
    return True


def verify_privacy(data, matcher_path, table_path, staff_path):
    matcher_hash = hashlib.sha256(Path(matcher_path).read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("node_privacy_matcher", matcher_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    policy = module.load_policy(table_path, staff_path)
    nodes = data["fails"] + [row["node"] for row in data["unstable"]]
    if any(policy.deny(node) for node in nodes):
        raise ValueError("Node identity refused by the shared privacy matcher")
    policy.unchanged()
    if hashlib.sha256(Path(matcher_path).read_bytes()).hexdigest() != matcher_hash:
        raise ValueError("Shared privacy matcher changed during verification")
    return {**policy.hashes(), "matcher_sha256": matcher_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".run/ratchet"))
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--unstable-limit", type=int, default=80)
    parser.add_argument("--unstable-issue", type=int, default=0)
    parser.add_argument("--unstable-expires-on", default="")
    parser.add_argument("--verify-privacy", type=Path)
    parser.add_argument("--deny-matcher", type=Path)
    parser.add_argument("--deny-table", type=Path)
    parser.add_argument("--staff-names", type=Path)
    args = parser.parse_args()
    if args.verify_privacy:
        verify_privacy(decode_list(args.verify_privacy.read_text(encoding="utf-8")),
                       args.deny_matcher, args.deny_table, args.staff_names)
        print("KNOWN-FAILURES PRIVACY PASS")
        return
    if args.unstable_limit < 1 or args.plan is None or args.reports is None:
        raise ValueError("A positive unstable cap and complete evidence paths are required")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if plan["sha"] != git("rev-parse", "HEAD") or plan["tree"] != git("rev-parse", "HEAD^{tree}"):
        raise ValueError("Aggregate checkout differs from its full-run source")
    ctx = context(args.base_sha)
    preflight(ctx, smoke=plan["smoke"], limit=args.unstable_limit,
              issue=args.unstable_issue, expires_on=args.unstable_expires_on)
    if plan["replicas"] != ([1] if plan["smoke"] else ctx["replicas"]) or plan["base_sha"] != ctx["base_sha"]:
        raise ValueError("Aggregate context differs from the checked plan")
    runs = collect_reports(plan, args.reports, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    seed_data = json.loads(Path("ci/unstable-seeds.json").read_text(encoding="utf-8"))
    seeds = seed_data["nodes"]
    if (seed_data.get("schema") != 1 or not isinstance(seeds, list) or seeds != sorted(set(seeds))
            or any(not isinstance(node, str) or not SAFE_NODE.fullmatch(node) for node in seeds)):
        raise ValueError("Invalid or unredacted unstable seeds")
    add_only = (os.getenv("GITHUB_EVENT_NAME") in {"pull_request", "merge_group"}
                and git("diff", "--name-status", "--no-renames", ctx["base_sha"], "HEAD") == "A\t" + KNOWN)
    tree = git("rev-parse", ctx["base_sha"] + "^{tree}") if add_only else plan["tree"]
    candidate = (candidate_list(runs, seeds, tree, args.unstable_issue, args.unstable_expires_on, args.unstable_limit)
                 if ctx["bootstrap"] and not plan["smoke"] else ctx["current"])
    args.output.mkdir(parents=True, exist_ok=True)
    if candidate:
        (args.output / "known-failures.candidate.json").write_text(encoded(candidate), encoding="utf-8")
    try:
        if plan["smoke"]:
            if any(state != "passed" for run in runs for state in run.values()):
                raise ValueError("Smoke must execute and pass its selected wiring tests")
        else:
            evaluate(ctx["current"], ctx["base"], runs, bootstrap_candidate=candidate,
                     bootstrap_add_only=add_only, limit=args.unstable_limit)
            if ctx["bootstrap"] and Path(KNOWN).read_text(encoding="utf-8") != encoded(candidate):
                raise ValueError("Bootstrap file must exactly equal the canonical candidate bytes")
    except ValueError as error:
        verdict = {"passed": False, "reason": str(error)}
    else:
        verdict = {"passed": True, "reason": "Smoke wiring passed; no baseline qualification" if plan["smoke"] else "Known-failure ratchet passed"}
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
