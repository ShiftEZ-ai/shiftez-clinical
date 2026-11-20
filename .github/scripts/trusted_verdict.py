"""Compare candidate evidence with target policy using only the protected dev verifier."""
import argparse
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TRUSTED = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import working_policy as policy
import known_skips
import canary_evidence
import node_census
import hardening
from trusted_ci import PR_SMOKE, census_files, pr_changed_files, pr_scope, require_pr_merge, settings
from latest_reports import validate_selected_reports
import stale_evidence


def git(*args):
    return subprocess.check_output(["git", "-C", str(TRUSTED), *args], stderr=subprocess.DEVNULL)


def policy_context(data, kind, candidate):
    if kind == "pull_request":
        return data["pull_request"]["base"]["ref"], policy.commit(data["pull_request"]["base"]["sha"])
    if kind == "merge_group":
        return data["merge_group"]["base_ref"].removeprefix("refs/heads/"), policy.commit(data["merge_group"]["base_sha"])
    if kind == "schedule":
        if candidate != git("rev-parse", "HEAD").decode().strip():
            raise ValueError("Nightly evidence must use the pinned dev source")
        return "dev", candidate
    branch = os.environ["GITHUB_REF"].removeprefix("refs/heads/")
    return branch, policy.commit(data["before"]) if kind == "push" and branch == "dev" else candidate


def assert_inventory(plan, raw_inventory, counts, scope=None):
    if plan.get("smoke") is not False or set(plan.get("suites", {})) != {"unit", "browser"}:
        raise ValueError("Public verdict requires both functional suites")
    selected = {"unit": [], "browser": []}
    for row in policy.paths(raw_inventory):
        metadata, _, raw_name = row.partition(b"\t")
        name = raw_name.decode()
        if not policy.re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py", name):
            continue
        if not metadata.startswith((b"100644 blob ", b"100755 blob ")):
            raise ValueError("Selected test must be a regular committed file")
        suite = "browser" if name.rsplit("/", 1)[-1].startswith("test_browser") else "unit"
        selected[suite].append(name)
    partial = "selection" in plan
    selected_files = set()
    for suite, expected in selected.items():
        shards = plan["suites"][suite]["shards"]
        actual = [path for row in shards for path in row["files"]]
        count_ok = 1 <= len(shards) <= counts[suite] if partial else len(shards) == counts[suite]
        files_ok = set(actual) <= set(expected) if partial else set(actual) == set(expected)
        if not count_ok or not files_ok or len(actual) != len(set(actual)) or not all(row["files"] for row in shards):
            raise ValueError("Candidate plan omits, duplicates or misclassifies committed test files")
        selected_files.update(actual)
    if partial:
        if scope is None:
            raise ValueError("Partial selection requires an ordinary dev pull request")
        required = PR_SMOKE | (scope & (set(selected["unit"]) | set(selected["browser"])))
        if plan["selection"] != {"reason": "affected"} or not required <= selected_files:
            raise ValueError("Partial selection disagrees with the PR scope or omits required test files")
    return selected_files


def required_producers(raw):
    needs = policy.decode(raw)
    if set(needs) != {"checks", "static", "unit", "browser"} or any(row.get("result") != "success" for row in needs.values()):
        raise ValueError("Every producer must run and succeed")


def pr_stale_evidence(nodes, *, repository, base, candidate, scope):
    if not os.getenv("GH_TOKEN"):
        return {}
    try:
        changed = scope if scope is not None else pr_changed_files(base, candidate, git)
        if changed is None or policy.KNOWN in changed:
            return {}
        untouched = {node for node in nodes if node.split("::", 1)[0] not in changed}
        if not untouched:
            return {}
        tip = policy.fetch("refs/heads/dev")
        return stale_evidence.confirmed_nodes(repository, git, tip, base, untouched)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def stale_options(kind, branch, data, base, candidate, scope):
    if kind == "pull_request" and branch == "dev":
        return {"stale_lookup": partial(pr_stale_evidence, repository=data["repository"]["full_name"],
                                        base=base, candidate=candidate, scope=scope)}
    if kind == "schedule" and branch == "dev" and candidate == os.getenv("GITHUB_SHA"):
        return {"nightly": True}
    return {}


def bootstrap_verdict(plan, reports, output, candidate, base, census_base, maker, flags):
    if plan["replicas"] != [1, 2] or policy.read_file(candidate, policy.KNOWN) is not None:
        raise ValueError("Bootstrap measurement requires two replicas and no allowance list")
    if flags['canaries']:
        canary_evidence.verify_directory(reports, allow_product_errors=False)
    runs = maker.collect_reports(plan, reports, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    known_skips.comparison_runs(runs, set(), release=False)
    if flags['node_census']:
        raw_files = policy.paths(git("ls-tree", "-r", "--name-only", "-z", census_base, "--", "tests"))
        expected = {p.decode() for p in raw_files if policy.re.fullmatch(rb"tests/(?:[\w-]+/)*test_[\w-]+\.py", p)}
        before = node_census.base_nodes(plan, reports, census_base, os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"], maker.canonical_node, expected)
        ledger = policy.read_file(candidate, "ci/removed-tests.json")
        if ledger is None:
            raise ValueError("Bootstrap node-removal ledger is missing")
        allowed = node_census.removed_nodes(ledger, maker.SAFE_NODE.fullmatch)
        node_census.emit(node_census.compare(before, set(runs[0]), allowed), output)
    output.mkdir(parents=True, exist_ok=True)
    evidence = {"schema": 1, "bootstrap": True, "sha": candidate, "tree": plan["tree"],
        "base_sha": base, "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "replicas": plan["replicas"], "runs": runs}
    (output / "bootstrap-measurement.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    (output / "verdict.json").write_text(json.dumps({"passed": True, "bootstrap": True,
        "reason": "Complete bootstrap measurement; list-cut admission remains required"}) + "\n", encoding="utf-8")
    print("BOOTSTRAP: complete measurement; no existing failure allowance was assumed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    flags=hardening.load()
    required_producers(os.environ["NEEDS_JSON"])
    plan_path, reports, output = args.plan.resolve(), args.reports.resolve(), args.output.resolve()
    plan = policy.decode(plan_path.read_bytes())
    candidate = policy.commit(os.environ["CANDIDATE_SHA"])
    policy.git = git
    policy.fetch(candidate, candidate)
    data = policy.event()
    branch, base = policy_context(data, os.environ["GITHUB_EVENT_NAME"], candidate)
    kind=os.environ["GITHUB_EVENT_NAME"]
    if kind == "pull_request" and os.getenv("TRUSTED_BOOTSTRAP") != "true":
        require_pr_merge(data, git, candidate)
    census_base=(policy.commit(data["pull_request"]["base"]["sha"]) if kind=="pull_request" else
                 policy.commit(data["merge_group"]["base_sha"]) if kind=="merge_group" else
                 policy.commit(data["before"]) if kind=="push" else candidate)
    policy.fetch(census_base,census_base)
    if branch not in {"dev", "preview", "main"}:
        raise ValueError("Unsupported branch")
    if branch in {"preview", "main"}:
        dev_tip = policy.fetch("refs/heads/dev")
        base = policy.accepted_dev_commit(policy.tree(candidate), dev_tip) if branch == "main" else dev_tip
    source_tree = policy.tree(candidate)
    if plan["sha"] != candidate or plan["tree"] != source_tree or plan["base_sha"] != base:
        raise ValueError("Evidence is not bound to the independently resolved candidate/target")
    partial = "selection" in plan
    scope = (pr_scope(data, git, policy.commit(git("rev-parse", "HEAD").decode().strip()), candidate)
             if partial and kind == "pull_request" else None)
    config = settings()
    selected_files = assert_inventory(plan, git("ls-tree", "-r", "-z", candidate, "--", "tests"),
                                      {suite: config[suite + "_shards"] for suite in ("unit", "browser")}, scope)
    if policy.bootstrap(data, kind, base):
        if partial:
            raise ValueError("Bootstrap requires the full plan")
        validate_selected_reports(plan, reports)
        sys.path[:0] = [str(TRUSTED / "ci"), str(TRUSTED / ".github/scripts")]
        import make_known_failures as maker
        bootstrap_verdict(plan, reports, output, candidate, base, census_base, maker, flags)
        return
    old, current = policy.read_file(base, policy.KNOWN), policy.read_file(candidate, policy.KNOWN)
    if old is not None and current != old and not (policy.removal_only(old,current)
            or branch=='dev' and policy.renewal_only(old,current)):
        raise ValueError("Candidate allowance data must only remove entries or extend unstable expiry through the audited Tier-1 route")
    # All code imports resolve under this trusted source, never the candidate or artifact directory.
    sys.path[:0] = [str(TRUSTED / "ci"), str(TRUSTED / ".github/scripts")]
    import make_known_failures as maker
    retry = maker.retry_run(plan)
    if flags['canaries']:
        canary_evidence.verify_directory(reports, allow_product_errors=True, partial=partial, allow_retry=retry)
    root_raw = policy.read_file(base, "ci/root-run-evidence.json")
    skips_raw = policy.read_file(base, "ci/known-skips.json")
    if root_raw is None or skips_raw is None:
        raise ValueError("Reviewed target known-skips/root evidence is missing")
    root_evidence = policy.decode(root_raw)
    measured_sha = policy.commit(root_evidence["sha"])
    policy.fetch(measured_sha, measured_sha)
    known_skips.require_measurements(root_evidence)
    skip_list = known_skips.decode_list(skips_raw, root_evidence, maker.SAFE_NODE.fullmatch)
    allowed_skips = {row["node"] for row in skip_list["skips"]}
    if current is not None:
        failure_list = policy.decode(current)
        if failure_list.get("schema")!=3:raise ValueError("Current failure policy needs class, owner and reason (schema 3)")
        failure_nodes = {row["node"] if isinstance(row, dict) else row
                         for section in ("fails", "unstable") for row in failure_list[section]}
        if allowed_skips & failure_nodes:
            raise ValueError("Known-skip and failure classes must be disjoint")
    def candidate_git(*args):
        return git(*(candidate + a[4:] if a == "HEAD" or a.startswith(("HEAD^", "HEAD:")) else a for a in args)).decode().strip()
    maker.git = candidate_git
    with tempfile.TemporaryDirectory(prefix="trusted-verdict-data-", dir=os.environ["RUNNER_TEMP"]) as scratch:
        data_root = Path(scratch).resolve()
        assert data_root.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve())
        for path in policy.LISTS:
            raw = current if path == policy.KNOWN else policy.read_file(candidate,path) if flags['node_census'] and path=='ci/removed-tests.json' and branch=='dev' else policy.read_file(base, path)
            if raw is not None:
                policy.decode(raw)
                destination = data_root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
        previous = Path.cwd()
        previous_argv = sys.argv
        try:
            os.chdir(data_root)
            context = maker.context(base)
            if plan["replicas"] != context["replicas"]:
                raise ValueError("Replica count differs from the trusted reader context")
            # After the replica check, so the more specific refusal names a wrong replica count.
            validate_selected_reports(plan, reports)
            runs = maker.collect_reports(plan, reports, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], output)
            if flags['node_census']:
                raw_files=policy.paths(git("ls-tree","-r","--name-only","-z",census_base,"--","tests"))
                expected_files={p.decode() for p in raw_files if policy.re.fullmatch(rb"tests/(?:[\w-]+/)*test_[\w-]+\.py",p)}
                before_nodes=node_census.base_nodes(plan,reports,census_base,os.environ["GITHUB_RUN_ID"],
                    os.environ["GITHUB_RUN_ATTEMPT"],maker.canonical_node,census_files(plan, expected_files))
                ledger=policy.read_file(base,"ci/removed-tests.json")
                if ledger is None:raise ValueError("Target node-removal ledger is missing")
                candidate_ledger=policy.read_file(candidate,'ci/removed-tests.json') if branch=='dev' else ledger
                allowed=node_census.allowed_removals(ledger,candidate_ledger,before_nodes,set(runs[0]),maker.SAFE_NODE.fullmatch)
                census=node_census.compare(before_nodes,set(runs[0]),allowed)
                node_census.emit(census,output)
            if branch == "main" and policy.tree(base) != source_tree:
                raise ValueError("Main execution tree differs from the accepted dev source")
            error_nodes = {row["node"] for row in policy.decode(current)["fails"] if row["class"] == "baseline-error"}
            recovered = {}
            product_errors = maker.product_error_nodes(reports, listed=failure_nodes, recovered=recovered,
                                                     allow_retry=retry)
            if recovered and (branch == "main" or allowed_skips & recovered.keys()):
                raise ValueError("Release/known-skip execution contains a failure or error")
            error_nodes |= maker.mixed_listed_nodes(policy.decode(current), runs)
            outside = {row["node"] for row in maker.outside_observations(policy.decode(current), runs)}
            error_nodes |= product_errors & outside
            compared, skipped = known_skips.comparison_runs(runs, allowed_skips, release=branch == "main", allowed_errors=error_nodes)
            maker.collect_reports = lambda *args: compared
            print("Known skips: reviewed=" + str(len(allowed_skips)) + "; observed=" + str(len(skipped)))
            if os.getenv("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
                    summary.write("Reviewed skips=" + str(len(allowed_skips)) + "; observed SKIP=" + str(len(skipped)) + "\n")
            sys.argv = [str(TRUSTED / "ci/make_known_failures.py"), "--plan", str(plan_path), "--reports", str(reports),
                        "--output", str(output), "--base-sha", base,
                        "--unstable-limit", str(config["unstable_limit"]),
                        "--unstable-issue", str(config["unstable_issue"] or 0),
                        "--unstable-expires-on", str(config["unstable_expires_on"] or ""),
                        "--cleanup-issue", str(config["cleanup_issue"] or 0)]
            maker.main(**stale_options(kind, branch, data, base, candidate, scope))
            verdict = policy.decode((output / "verdict.json").read_bytes())
            if partial:
                missing_skips = sorted(node for node in allowed_skips if node.split("::", 1)[0] not in selected_files)
                verdict["not_observed"] = sorted(verdict["not_observed"] + missing_skips)
                (output / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
                print("Listed nodes outside the PR selection (not observed): " + str(len(verdict["not_observed"])))
            if verdict.get("passed") is not True:
                raise ValueError("Trusted known-failure comparison refused the evidence")
        finally:
            try:
                maker.write_summary()
            finally:
                sys.argv = previous_argv
                os.chdir(previous)
    print("Trusted verdict: PASS; verifier source=" + git("rev-parse", "HEAD").decode().strip()
          + "; policy base=" + base + "; candidate=" + candidate)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        raise SystemExit("Trusted verdict refused: " + (str(error) if isinstance(error, ValueError) else type(error).__name__)) from None
