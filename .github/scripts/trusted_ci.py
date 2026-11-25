"""Protected planner/runner; candidate code supplies test outcomes, never policy code."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import secrets
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[2] / "ci")]
import hosted_ci as hosted
import working_policy as policy
import canary_evidence as canaries
import hardening
import make_known_failures as maker
from quarantine_registry import QUARANTINE, decode_registry, utc_today

TRUSTED = Path(__file__).resolve().parents[2]
PR_SMOKE = {"tests/test_domain.py", "tests/test_migration_ids.py", "tests/test_browser_schedule_smoke.py"}


def settings(*, plan=None):
    data = policy.decode((TRUSTED / "ci/bootstrap-settings.json").read_bytes())
    group_shards = data.pop("merge_group_shards", None)
    if (set(data) != {"schema", "unit_shards", "browser_shards", "unstable_limit", "unstable_issue",
                     "unstable_expires_on", "cleanup_issue"} or data["schema"] != 1
            or any(type(data[k]) is not int or data[k] < 1 for k in ("unit_shards", "browser_shards", "unstable_limit"))):
        raise ValueError("Protected bootstrap configuration is invalid")
    if group_shards is not None:
        if (not isinstance(group_shards, dict) or set(group_shards) != {"unit_shards", "browser_shards"}
                or any(type(n) is not int or n < 1 for n in group_shards.values())):
            raise ValueError("Protected merge-group shard counts are invalid")
        event = os.getenv("GITHUB_EVENT_NAME")
        if event == "merge_group" or (event == "pull_request" and plan is not None and "selection" not in plan):
            data.update(group_shards)
    return data


def pinned_root():
    data = policy.decode((TRUSTED / "ci/approved-root.json").read_bytes())
    if data["repository_id"] != policy.REPOSITORY or data["approval"] != "APPROVED":
        raise ValueError("Approved root has not been confirmed by Delivery")
    policy.commit(data["sha"])
    policy.commit(data["tree"])
    return data


def output(values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as destination:
        for key, value in values.items():
            destination.write(key + "=" + str(value) + "\n")


def select():
    data = policy.decode(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    kind = os.environ["GITHUB_EVENT_NAME"]
    trusted_sha = subprocess.check_output(["git", "-C", str(TRUSTED), "rev-parse", "HEAD"], text=True).strip()
    measurement = (data.get("inputs") or {}).get("root_measurement", False) in (True, "true")
    if measurement:
        if kind != "workflow_dispatch" or os.environ["GITHUB_REF"] != "refs/heads/" + policy.default_branch(data):
            raise ValueError("Root measurements dispatch only the protected dev workflow")
        candidate = pinned_root()["sha"]
    elif kind == "schedule":
        candidate = policy.commit(trusted_sha)
    elif kind == "pull_request" and os.getenv("TRUSTED_BOOTSTRAP") == "true":
        candidate = policy.commit(data["pull_request"]["head"]["sha"])
    else:
        candidate = policy.commit(os.environ["GITHUB_SHA"])
    config = settings()
    output({"candidate_sha": candidate, "measurement": str(measurement).lower(),
            "trusted_sha": trusted_sha,
            "unit_count": config["unit_shards"], "browser_count": config["browser_shards"]})


def pr_changed_files(base, candidate, git):
    diff = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z")
    return {path.decode() for path in policy.paths(git(*diff, base, candidate, "--"))}


def quarantine_revision(branch, default, base, candidate):
    """A release reads the accepted base's quarantine; integration reads the candidate's. main releases only while it is not the default."""
    return base if branch == "main" and default != "main" else candidate


def pr_scope(data, git, trusted_sha, base, candidate):
    pr = data["pull_request"]
    branch = pr["base"]["ref"]
    if ((branch != policy.default_branch(data) and not policy.release_branch(branch))
            or any(label["name"] == "full-ci" for label in pr["labels"])):
        return None
    changed = pr_changed_files(base, candidate, git)
    if any(policy.protected(path.encode()) or path.rsplit("/", 1)[-1] == "generated.ts"
           or path.startswith(("migrations/", "packages/contracts/")) or path.endswith((".lock", "-lock.json"))
           for path in changed):
        return None
    inventory = policy.control_inventory(base if policy.release_branch(branch) else trusted_sha)
    for path in sorted(changed):
        before, after = policy.read_file(base, path), policy.read_file(candidate, path)
        tier, _ = policy.tier(path, before, after, inventory)
        if tier == 1 or path.rsplit("/", 1)[-1] == "conftest.py":
            return None
        if tier == 2 and not (
            path.startswith(("apps/", "packages/")) and path.endswith((".py", ".ts", ".css"))
            or ordinary_test_module(path, before, after, inventory)
        ):
            return None
    return changed


def ordinary_test_module(path, before, after, inventory):
    """Allow ordinary top-level test modules while keeping Working Policy's tiers."""
    if (after is None or path in inventory["product_imports"]
            or not policy.re.fullmatch(r"tests/test_[\w-]+\.py", path)):
        return False
    try:
        if "\0" in (before or b"").decode("utf-8") or "\0" in after.decode("utf-8"):
            return False
        return not (policy.python_controls(after) or policy.mechanisms(after) or policy.reporter_controls(after))
    except (SyntaxError, ValueError):
        return False


def affected_files(changed, files):
    required = PR_SMOKE | (changed & files)
    if not PR_SMOKE <= files:
        return None
    if not changed:
        return required
    try:
        selector = runpy.run_path(str(TRUSTED / "scripts/affected_tests.py"))
        owners = selector["direct_test_owners"](sorted(changed), files)
    except Exception as error:
        print("::warning::PR selector failed (" + type(error).__name__ + "); running the full suite")
        return None
    return required | owners if isinstance(owners, set) and owners <= files else None


def test_inventory():
    files = sorted(p for p in hosted.git("ls-files", "tests").splitlines()
                   if policy.re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py", p))
    if not files or any(Path(p).is_symlink() or not Path(p).is_file() for p in files):
        raise ValueError("Invalid candidate file inventory")
    return files


def plan(base, measurement):
    candidate = hosted.git("rev-parse", "HEAD")
    candidate_tree = hosted.git("rev-parse", "HEAD^{tree}")
    event = None
    if (not measurement and os.getenv("GITHUB_EVENT_NAME") == "pull_request"
            and os.getenv("TRUSTED_BOOTSTRAP") != "true"):
        event = policy.event()
        merge_base = policy.pr_test_base(event, candidate)
        branch = event["pull_request"]["base"]["ref"]
        if branch == policy.default_branch(event) or policy.release_branch(branch):
            base = merge_base
    if measurement:
        root = pinned_root()
        if (candidate, candidate_tree) != (root["sha"], root["tree"]):
            raise ValueError("Root measurement tree changed")
        replicas = [1]
    else:
        # Existing reader decides replica count from Git objects; no candidate Python import.
        context = maker.context(base)
        replicas = context["replicas"]
    durations = TRUSTED / "ci/hosted-durations.json"
    data = hosted.load_durations(durations)
    files = test_inventory()
    result = {"schema": 1, "sha": candidate, "tree": candidate_tree, "base_sha": base,
              "replicas": replicas, "smoke": False, "inventory_sha256": hosted.digest(files),
              "duration_sha256": canaries.sha(durations), "suites": {}}
    selected_files = None
    if event is not None and not context["bootstrap"]:
        trusted_sha = subprocess.check_output(["git", "-C", str(TRUSTED), "rev-parse", "HEAD"], text=True).strip()
        changed = pr_scope(event, policy.git, trusted_sha, base, candidate)
        if changed is not None:
            selected_files = affected_files(changed, set(files))
    if selected_files is not None:
        result["selection"] = {"reason": "affected"}
        files = sorted(selected_files)
    config = settings(plan=None if measurement else result)
    for suite in ("unit", "browser"):
        selected = [p for p in files if Path(p).name.startswith("test_browser") == (suite == "browser")]
        seconds = data["suites"][suite]["files"]
        count = config[suite + "_shards"]
        if selected_files is not None:
            median = statistics.median(seconds.values())
            total = sum(seconds.get(path, median) for path in selected)
            count = min(count, len(selected), max(1, math.ceil(total / 120)))
        result["suites"][suite] = {"shards": hosted.balance(selected, seconds, count),
                                  "median_files": sorted(set(selected) - seconds.keys())}
    return result


def runner_plan(base, measurement, suite, shard, seal):
    path = os.getenv("CI_CHECKS_PLAN")
    # The first unit shard independently checks the entire plan on every PR.
    if (not path or measurement or os.getenv("GITHUB_EVENT_NAME") != "pull_request"
            or os.getenv("TRUSTED_BOOTSTRAP") == "true" or (suite == "unit" and shard == 1)):
        return plan(base, measurement)
    event = policy.event()
    branch = event["pull_request"]["base"]["ref"]
    # Legacy promotion plans may use the integration branch as their baseline.
    if branch != policy.default_branch(event) and not policy.release_branch(branch):
        return plan(base, measurement)
    candidate = policy.decode(Path(path).read_bytes())
    if hosted.digest(candidate) != seal:
        raise ValueError("Runner plan identity differs from the trusted plan")
    sha, tree = hosted.git("rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    if (candidate["sha"], candidate["tree"], candidate["base_sha"]) != (sha, tree, base):
        raise ValueError("Checks plan differs from the shard candidate or base")
    if candidate["base_sha"] != policy.pr_test_base(event, sha):
        raise ValueError("Checks plan differs from the verified target parent")
    if candidate["inventory_sha256"] != hosted.digest(test_inventory()):
        raise ValueError("Checks plan differs from the shard test inventory")
    print("Using the sealed Checks plan")
    return candidate


def census_files(plan, files):
    if "selection" in plan:
        scope = {path for row in plan["suites"].values() for shard in row["shards"] for path in shard["files"]}
        return set(files) & scope
    return set(files)


def invocation(targets, suite, report, extra, workers, base_targets=None, *, partial=False):
    """Keep raw deliberate FAIL/ERROR; normalization removes only verified random sentinels."""
    flags=hardening.load()
    config_file = Path.cwd() / "pyproject.toml"
    if config_file.is_symlink() or not config_file.is_file():
        raise ValueError("Candidate pytest configuration is missing or linked")
    # An outside sentinel changes pytest config discovery even with --rootdir; bind it explicitly.
    with tempfile.TemporaryDirectory(prefix="ci-canary-", dir=os.environ["RUNNER_TEMP"]) as scratch:
        folder = Path(scratch)
        manifest = {kind: "test_" + secrets.token_hex(20) for kind in ("passed", "failed", "skipped", "error")} if flags['canaries'] else {}
        if flags['canaries']:
            functions = folder / ("test_" + secrets.token_hex(20) + ".py")
            error = folder / (manifest["error"] + ".py")
            marker = "pytestmark = pytest.mark.browser\n" if suite == "browser" else ""
            timing = targets == [hosted.TIMING_NODE]
            timeout_setup = "from threading import Event\n" if timing else ""
            timeout_marker = "@pytest.mark.timeout(1)\n" if timing else ""
            failure = "    Event().wait(5)\n" if timing else "    assert False\n"
            functions.write_text("import pytest\n" + marker + timeout_setup
                + "def " + manifest["passed"] + "():\n    assert True\n"
                + timeout_marker + "def " + manifest["failed"] + "():\n" + failure
                + "def " + manifest["skipped"] + "():\n    pytest.skip('trusted sentinel')\n", encoding="utf-8")
            error.write_text("raise RuntimeError('trusted collection sentinel')\n", encoding="utf-8")
        raw = report.with_suffix(".raw.xml")
        census = report.with_suffix(".collection.json")
        deselected = report.with_suffix(".deselected.json")
        collection_raw = report.with_suffix(".collection.xml")
        def pytest_command(project, selected, options):
            return [sys.executable,"-m","pytest","-p","hosted_ci","-p","sentinel_plugin","-q",
                    *selected,*([str(folder)] if flags['canaries'] else []),"-m",hosted.execution_marker(suite),"-c",str(project/"pyproject.toml"),
                    "--rootdir="+str(project),"--continue-on-collection-errors",
                    "--timeout=600","--timeout-method=signal","-o","faulthandler_timeout=540",*options]
        def collection_ok(code,cases):
            if not flags['canaries']:return code in ((0, 5) if partial else (0,)) and not cases
            return code in (1,2) and bool(cases) and all(manifest['error'] in canaries.node(c) and canaries.outcome(c)=='error' for c in cases)
        command=pytest_command(Path.cwd(),targets,extra)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parent), str(Path.cwd())]),
               "TRUSTED_COLLECTION_OUTPUT": str(census.resolve())}
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        if flags['node_census'] and base_targets is not None:
            base_dir=Path(os.environ["CENSUS_BASE_DIR"]).resolve()
            base_raw=report.with_suffix(".base.collection.xml")
            base_census=report.with_suffix(".base.collection.json")
            base_env={**env,"PYTHONPATH":os.pathsep.join([str(Path(__file__).resolve().parent),str(base_dir)]),
                      "TRUSTED_COLLECTION_OUTPUT":str(base_census.resolve())}
            base_nodes = []
            if base_targets:
                base_code=subprocess.call([*pytest_command(base_dir,base_targets,[]),"-n","0","--collect-only","--junitxml="+str(base_raw.resolve())],cwd=base_dir,env=base_env)
                base_cases=list(ET.parse(base_raw).getroot().iter("testcase"))
                if not collection_ok(base_code,base_cases):
                    raise ValueError("BASE COLLECTION ERROR: incomplete node census; baseline-error covers setup/runtime errors only")
                base_nodes=[n for n in json.loads(base_census.read_text()) if not any(t in n for t in manifest.values())]
            ledger={"schema":1,"base_sha":policy.commit(os.environ["CENSUS_BASE_SHA"]),
                    "head_sha":hosted.git("rev-parse","HEAD"),"run_id":os.environ["GITHUB_RUN_ID"],
                    "run_attempt":os.environ["GITHUB_RUN_ATTEMPT"],"suite":suite,
                    "shard":int(os.environ.get("SHARD_INDEX","1")),"replica":int(os.environ.get("REPLICA","1")),
                    "files":base_targets,"nodes":base_nodes}
            (report.parent/"base-census.json").write_text(json.dumps(ledger,indent=2)+"\n",encoding="utf-8")
        collected=[]
        if flags['canaries'] or flags['node_census']:
            collection_code = subprocess.call([*command, "-n", "0", "--collect-only", "--junitxml=" + str(collection_raw)], env=env)
            collection_cases = list(ET.parse(collection_raw).getroot().iter("testcase"))
            if not collection_ok(collection_code,collection_cases):
                raise ValueError("HEAD COLLECTION ERROR: incomplete node census; baseline-error covers setup/runtime errors only")
            collected = json.loads(census.read_text(encoding="utf-8"))
        # The parent retains the collection snapshot; the test process cannot change this in-memory copy.
        env.pop("TRUSTED_COLLECTION_OUTPUT")
        code = subprocess.call([*command, "-n", workers, "--dist", "worksteal", "--hosted-deselected-output=" + str(deselected),
                                "--junitxml=" + str(raw)], env=env)
        if flags['canaries'] and code != 1:
            raise ValueError("Intentional FAIL/ERROR sentinels must produce pytest exit 1")
        if not flags['canaries'] and code not in ((0, 1, 5) if partial else (0, 1)):
            raise ValueError('Pytest execution/collection did not complete')
        parsed = ET.parse(raw)
        cases=list(parsed.getroot().iter('testcase'))
        selected = canaries.verify_cases(cases, manifest, collected, allow_empty=partial) if flags['canaries'] else [canaries.node(c) for c in cases]
        if not flags['canaries'] and flags['node_census'] and sorted(selected)!=sorted(collected):
            raise ValueError('Reported nodes differ from trusted collection')
        for parent in parsed.getroot().iter():
            for child in list(parent):
                if child.tag == "testcase" and any(t in canaries.node(child) for t in manifest.values()):
                    parent.remove(child)
        # Recompute suite counts; raw report remains the authoritative sentinel record.
        for parent in parsed.getroot().iter("testsuite"):
            cases = list(parent.iter("testcase"))
            parent.set("tests", str(len(cases)))
            for attribute, value in (("failures", "failed"), ("errors", "error"), ("skipped", "skipped")):
                parent.set(attribute, str(sum(canaries.outcome(c) == value for c in cases)))
        parsed.write(report, encoding="utf-8", xml_declaration=True)
        census.write_text(json.dumps(collected) + "\n", encoding="utf-8")
        report.with_suffix(".selection.json").write_text(json.dumps(selected) + "\n", encoding="utf-8")
        filtered_deselected = [n for n in json.loads(deselected.read_text()) if not any(t in n for t in manifest.values())]
        deselected.write_text(json.dumps(filtered_deselected) + "\n", encoding="utf-8")
        if flags['canaries']:
            ledger = {"schema": 1, "sha": hosted.git("rev-parse", "HEAD"), "tree": hosted.git("rev-parse", "HEAD^{tree}"),
                      "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
                      "sentinels": manifest, "raw_sha256": canaries.sha(raw), "collection_sha256": canaries.sha(census)}
            report.with_suffix(".canaries.json").write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
        return int(any(canaries.outcome(c) in {"failed", "error"} for c in parsed.getroot().iter("testcase")))


def invocation_receipt(targets, suite, report, extra, workers, base_targets, *, candidate, partial=False, listed=frozenset()):
    def attempt(selected, path, options, parallelism, before, *, partial=False):
        code = invocation(selected, suite, path, options, parallelism, before, partial=partial)
        files = (path, path.with_suffix(".selection.json"), path.with_suffix(".deselected.json"))
        return {"file": path.name, "exit_code": code, "sha256": {p.name: canaries.sha(p) for p in files}}

    first = attempt(targets, report, extra, workers, base_targets, partial=partial)
    if first["exit_code"] == 1 and maker.retry_run(candidate):
        original = canaries.report_cases(report, first)
        failures = maker.retry_nodes(original, listed)
        if failures:
            path = report.with_name(report.stem + "-retry.xml")
            first["retry"] = attempt(failures, path, [], "0", None)
            canaries.recovered_nodes(report, first, failures, allow_retry=True)
    return first


def registry_at_revision(revision):
    try:
        raw = policy.read_file(revision, QUARANTINE)
        if raw is None:
            raise ValueError("registry is missing")
        text = raw.decode("utf-8")
        registry = decode_registry(text, utc_today())
        # Compare the source of each row without making its neighbours or position part of its identity.
        decoder = json.JSONDecoder()
        separators = re.compile(r"[ \t\r\n,]*")
        end = text.index("[") + 1
        entries = {}
        for node, row in registry.items():
            start = separators.match(text, end).end()
            _, end = decoder.raw_decode(text, start)
            entries[node] = (row, text[start:end])
        return entries
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Cannot read trusted quarantine registry: " + QUARANTINE) from None
    except (ValueError, RecursionError) as error:
        raise ValueError(f"Invalid quarantine registry {QUARANTINE}: {error}") from None


def quarantine_policy(base, candidate, known_before, known_after):
    base_raw, base_retired = policy.retired_at_revision(base)
    retired = set(base_retired)
    if candidate != base:
        _, candidate_retired = policy.retired_at_revision(candidate, allow_legacy=base_raw is None)
        retired.update(candidate_retired)
    entries = registry_at_revision(candidate)
    current = policy.known_failure_classes(known_after)
    overlap = current.keys() & entries.keys()
    if overlap:
        raise ValueError("Known-failure and quarantine entries must be disjoint: " + ", ".join(sorted(overlap)))
    before = entries if base == candidate else registry_at_revision(base)
    moved = policy.moved_failure_classes(known_before, current)
    honoured = {node: row for node, (row, source) in entries.items()
                if node not in retired and ((node in before and source == before[node][1])
                                            or moved.get(node) == row["reason"].split(":", 1)[0].strip())}
    return {node: row for node, (row, _) in entries.items()}, honoured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("select", "context", "plan", "run"))
    parser.add_argument("--base-sha")
    parser.add_argument("--measurement", choices=("true", "false"), default="false")
    parser.add_argument("--suite", choices=("unit", "browser"))
    parser.add_argument("--shard", type=int)
    parser.add_argument("--replica", type=int, default=1)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    if args.mode == "select":
        select()
        return
    if args.mode == "context":
        if args.measurement == "true":
            output({"base_sha": pinned_root()["sha"]})
        else:
            policy.ci_context()
        return
    base = policy.commit(args.base_sha)
    measurement = args.measurement == "true"
    if args.mode == "plan":
        candidate = plan(base, measurement)
    else:
        candidate = runner_plan(base, measurement, args.suite, args.shard, args.plan_sha256)
    reports = Path(".run/reports")
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "shard-plan.json").write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
    seal = hosted.digest(candidate)
    if args.mode == "plan":
        values = {"plan_sha256": seal, "replicas": json.dumps(candidate["replicas"]), "base_sha": candidate["base_sha"]}
        values.update({suite + "_matrix": json.dumps(list(range(1, len(candidate["suites"][suite]["shards"]) + 1)))
                       for suite in ("unit", "browser")})
        values.update({suite + "_count": len(row["shards"]) for suite, row in candidate["suites"].items()})
        output(values)
        return
    if seal != args.plan_sha256 or args.replica not in candidate["replicas"] or args.suite is None:
        raise ValueError("Runner plan identity differs from the trusted plan")
    shards = candidate["suites"][args.suite]["shards"]
    if type(args.shard) is not int or not 1 <= args.shard <= len(shards):
        raise ValueError("Invalid shard")
    workers = os.getenv(f"CI_{args.suite.upper()}_WORKERS", "")
    if not policy.re.fullmatch(r"[1-8]", workers):
        workers = "4"
    listed = frozenset()
    if maker.retry_run(candidate):
        event = policy.event()
        kind = os.environ["GITHUB_EVENT_NAME"]
        if kind == "pull_request":
            branch = event["pull_request"]["base"]["ref"]
        elif kind == "merge_group":
            branch = event["merge_group"]["base_ref"].removeprefix("refs/heads/")
        else:
            branch = os.environ["GITHUB_REF"].removeprefix("refs/heads/")
        base = candidate["base_sha"]
        current = policy.read_file(candidate["sha"], policy.KNOWN)
        old = policy.read_file(base, policy.KNOWN)
        _, quarantine = quarantine_policy(base, quarantine_revision(branch, policy.default_branch(event), base, candidate["sha"]),
                                          old, current)
        listed = maker.listed_nodes(maker.decode_list(current) if current is not None else None) | quarantine.keys()
    selected = shards[args.shard - 1]["files"]
    commands = [("junit.xml", selected, [])]
    if args.suite == "unit" and hosted.TIMING_NODE.split("::")[0] in selected:
        commands[0][2].append("--deselect=" + hosted.TIMING_NODE)
        commands.append(("junit-timing.xml", [hosted.TIMING_NODE], []))
    receipt = {"schema": 1, "sha": candidate["sha"], "tree": candidate["tree"], "plan_sha256": seal,
               "suite": args.suite, "shard": args.shard, "replica": args.replica,
               "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "reports": []}
    for name, targets, extra in commands:
        report = reports / name
        base_targets=None
        if hardening.load()['node_census'] and name=="junit.xml":
            base_dir=Path(os.environ["CENSUS_BASE_DIR"]).resolve()
            files=subprocess.check_output(["git","-C",str(base_dir),"ls-files","tests"],text=True).splitlines()
            files=sorted(p for p in files if policy.re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py",p)
                         and Path(p).name.startswith("test_browser")== (args.suite=="browser"))
            files = sorted(census_files(candidate, files))
            count=len(candidate["suites"][args.suite]["shards"])
            base_targets=[p for index,p in enumerate(files) if index%count==args.shard-1]
        receipt["reports"].append(invocation_receipt(
            targets, args.suite, report, extra, "0" if name == "junit-timing.xml" else workers,
            base_targets, candidate=candidate, partial="selection" in candidate, listed=listed))
    if hardening.load()['node_census']:receipt["base_census_sha256"]=canaries.sha(reports/"base-census.json")
    (reports / "shard-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
