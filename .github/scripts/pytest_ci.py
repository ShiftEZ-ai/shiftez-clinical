"""Retry only reviewed flaky failures on non-partitioned PR/dev pushes; retain both attempts and report FLAKY."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


def case_node(case):
    parts = case.get("classname", "").split(".")
    module = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
    return ("/".join(parts[:module + 1]) + ".py::" + "::".join(parts[module + 1:] + [case.get("name", "")])
            if module is not None else case.get("name", "collection error"))


def case_rows(paths):
    """Retain each JUnit case, including duplicate executions."""
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            node = case_node(case)
            # An interrupted, unidentified row cannot establish a completed pass.
            outcome = "failed" if not case.get("name") or case.find("failure") is not None or case.find("error") is not None else (
                "skipped" if case.find("skipped") is not None else "passed")
            yield node, {"seconds": float(case.get("time", 0)), "outcome": outcome}


def cases(paths):
    rows = {}
    for node, row in case_rows(paths):
        previous = rows.get(node, {"seconds": 0, "outcome": "passed"})
        rows[node] = {"seconds": previous["seconds"] + row["seconds"],
                      "outcome": row["outcome"] if row["outcome"] == "failed" or previous["outcome"] == "passed" else previous["outcome"]}
    return rows


def case_properties(path, name):
    return {case_node(case): prop.get("value", "") for case in ET.parse(path).getroot().iter("testcase")
            for prop in case.findall("properties/property") if prop.get("name") == name}


def scratch_files(directory):
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                yield path, info


def scratch_snapshot(directory, phase, report):
    try:
        usage = shutil.disk_usage(directory)
        files = list(scratch_files(directory))
        data = {"phase": phase, "utc_epoch_seconds": time.time(), "path": str(directory),
                "free_bytes": usage.free, "total_bytes": usage.total,
                # Retained files at this instant, not the active test/WAL high-water mark.
                "retained_file_bytes": sum(info.st_size for _, info in files),
                "retained_regular_file_allocated_bytes": (
                    sum(info.st_blocks * 512 for _, info in files) if all(hasattr(info, "st_blocks") for _, info in files) else None)}
        try:
            mount = subprocess.run(["findmnt", "-n", "-T", str(directory), "-o", "TARGET,SOURCE,FSTYPE"],
                                   capture_output=True, text=True, timeout=5)
            data["mount"] = mount.stdout[:4096] if mount.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            data["mount"] = "unavailable"
        for metric in ("memory.max", "memory.current", "memory.peak", "memory.events", "memory.stat", "memory.swap.max", "cpu.max"):
            try:
                with (Path("/sys/fs/cgroup") / metric).open() as stream:
                    data[metric] = stream.read(4096)
            except OSError:
                data[metric] = "unavailable"
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data) + "\n")
    except Exception as exc:
        # Diagnostic failures must not replace pytest's exit status or original exception.
        print("Resource snapshot unavailable: " + type(exc).__name__, file=sys.stderr)


def run(args):
    parent = os.getenv("FMUC_BROWSER_SCRATCH_ROOT")
    if not parent:
        return run_tests(args)
    if not Path(parent).is_absolute():
        raise ValueError("Browser scratch root must be an absolute directory")
    base_args = [arg for arg in args if arg.startswith("--basetemp=")]
    if len(base_args) != 1 or not base_args[0].split("=", 1)[1]:
        raise ValueError("Browser scratch requires one explicit --basetemp= evidence destination")
    destination = Path(base_args[0].split("=", 1)[1])
    report = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml=")))
    # This is a free-space policy guard, not a measured peak or the runner's ephemeral-storage quota.
    if shutil.disk_usage(parent).free < 2 * 1024 ** 3:
        raise RuntimeError("Browser scratch needs at least 2 GiB free (policy guard)")
    with tempfile.TemporaryDirectory(prefix="browser-", dir=parent, ignore_cleanup_errors=True) as owned:
        directory = Path(owned)
        args = ["--basetemp=" + str(directory / "pytest-tmp") if arg == base_args[0] else arg for arg in args]
        snapshot = report.parent / "browser-scratch.jsonl"
        scratch_snapshot(directory, "before", snapshot)
        status = None
        try:
            status = run_tests(args)
            return status
        finally:
            scratch_snapshot(directory, "after", snapshot)
            try:
                # Include initial and -retry evidence, including successful tests' deliberate screenshots.
                for path, _ in scratch_files(directory):
                    if path.suffix in (".png", ".log"):
                        target = destination / directory.name / path.relative_to(directory)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
            except Exception as exc:
                if status == 0:
                    raise
                print("Browser scratch evidence copy failed: " + type(exc).__name__, file=sys.stderr)


def run_tests(args):
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    single_attempt = args[:1] == ["--single-attempt"]
    args = args[1:] if single_attempt else args
    report_arg = next(arg for arg in args if arg.startswith("--junitxml="))
    report = Path(report_arg.split("=", 1)[1])
    partitioned = bool(os.getenv("CI_CASE_MANIFEST"))
    selected_output = report.with_suffix(".selection.json")
    deselected_output = report.with_suffix(".deselected.json")
    selected_output.unlink(missing_ok=True)
    selected_output.with_suffix(".retry.json").unlink(missing_ok=True)
    deselected_output.unlink(missing_ok=True)
    deselected_output.with_suffix(".retry.json").unlink(missing_ok=True)
    args = [*args, "-p", "scripts.ci_partition_plugin", "--ci-case-output", str(selected_output),
            "--ci-case-deselected-output", str(deselected_output)]
    if partitioned:
        args = [*args, "--ci-case-manifest", os.environ["CI_CASE_MANIFEST"],
                "--ci-case-producer", os.environ["GITHUB_JOB"], "--ci-case-suite", os.environ.get("CI_CASE_SUITE", os.environ["GITHUB_JOB"]),
                "--ci-case-shard", os.environ["SHARD"].split("/")[0]]
    report.parent.mkdir(parents=True, exist_ok=True)
    retry_report = report.with_name(report.stem + "-retry.xml")
    flaky_report = report.with_name(report.stem + "-flaky.json")
    for path in (report, retry_report, flaky_report):
        path.unlink(missing_ok=True)
    # Each invocation (including the isolated timing test) owns a fresh cache, never a previous shard's failures.
    with tempfile.TemporaryDirectory(prefix="pytest-ci-") as cache:
        command = [sys.executable, "-m", "pytest", *args, "-o", "cache_dir=" + cache]
        # Only this wrapper records receipts; nested test subprocesses must opt in themselves.
        cloud_reports = os.getenv("FMUC_CLOUD_TEST_REPORTS") == "yes"
        child_env = {"env": {k: v for k, v in os.environ.items()
                             if k != "FMUC_CLOUD_TEST_REPORTS" and not k.startswith("CI_CASE_")}}
        child_env['env']['PYTHONPATH'] = os.pathsep.join(filter(None, (
            str(root), child_env['env'].get('PYTHONPATH'))))
        status = subprocess.call(command, **child_env)
        exit_codes = [status]
        flaky = []
        quarantined = case_properties(report, "fmuc_quarantine") if report.exists() else {}
        eligible_event = os.getenv("GITHUB_EVENT_NAME") == "pull_request" or (
            os.getenv("GITHUB_EVENT_NAME") == "push" and os.getenv("GITHUB_REF") == "refs/heads/dev")
        if (not single_attempt and not partitioned and eligible_event and status == 1 and report.exists()
                and os.getenv("GITHUB_JOB") not in ("cpu-budget", "solver-cap")):
            last_failed = Path(cache) / "v/cache/lastfailed"
            failed = {node: True for node, row in cases([report]).items() if row["outcome"] == "failed"}
            no_retry = case_properties(report, "fmuc_no_retry")
            from tests.quarantine import entries
            # The reviewed registry is the classification source; issue observations alone are advisory.
            try:
                classified = entries(root / "tests/quarantine.json",
                                     datetime.now(timezone.utc).date(), current=False)  # real clock: registry expiry is UTC policy
            except (ValueError, OSError) as exc:
                classified = {}
                print("Retry classification unavailable: " + type(exc).__name__, file=sys.stderr)
            retryable = {node: True for node in failed if node in classified and node not in no_retry}
            if retryable:
                # A teardown skip can clear pytest's cache after a call failure; JUnit retains the failure wave.
                last_failed.parent.mkdir(parents=True, exist_ok=True)
                last_failed.write_text(json.dumps(retryable), encoding="utf-8")
                retry = ["--junitxml=" + str(retry_report) if arg == report_arg else
                         str(selected_output.with_suffix(".retry.json")) if arg == str(selected_output) else
                         str(deselected_output.with_suffix(".retry.json")) if arg == str(deselected_output) else
                         arg + "-retry" if arg.startswith("--basetemp=") else arg for arg in command]
                retry += ["--last-failed", "--last-failed-no-failures=none"]
                if any(arg.startswith("--cov=") for arg in args):
                    retry.append("--cov-append")
                retry_status = subprocess.call(retry, **child_env)
                exit_codes.append(retry_status)
                outcomes = cases([retry_report]) if retry_report.exists() else {}
                flaky = sorted(node for node in retryable if outcomes.get(node, {}).get("outcome") == "passed")
                # Skipped, missing, interrupted or uncollected failures cannot turn a shard green.
                status = 0 if retry_status == 0 and set(flaky) == set(failed) else (retry_status if retry_status not in (0, 5) else 1)
        if not single_attempt and partitioned and status == 5:
            from scripts import ci_partition
            manifest = json.loads(Path(os.environ["CI_CASE_MANIFEST"]).read_text(encoding="utf-8"))
            ci_partition.validate(manifest, manifest["source"], manifest["selection"])
            if (selected_output.is_file() and json.loads(selected_output.read_text(encoding="utf-8")) == []
                    and report.is_file() and not list(case_rows([report]))):
                status = 0
        outcomes = cases([report]) if quarantined else {}
        flaky_report.write_text(json.dumps({"tests": flaky,
            **({"single_attempt": True, "exit_codes": exit_codes} if single_attempt else {}),
            "quarantined": [{**json.loads(entry), "outcome": outcomes[node]["outcome"]}
                            for node, entry in sorted(quarantined.items())]}, indent=2) + "\n", encoding="utf-8")
        if cloud_reports:
            from scripts.cloud_test_plan import record_execution
            record_execution(report, exit_codes)
        return status


def nonblocking(report):
    """Quarantine and wall-benchmark reports are executed and reported, never a gating or blamed outcome."""
    return "quarantine" in report.stem or "wall-benchmark" in report.stem


def summary(directory):
    directory.mkdir(parents=True, exist_ok=True)
    reports = sorted(directory.glob("junit-*.xml"))
    original = [p for p in reports if not p.stem.endswith("-retry") and not nonblocking(p)]
    slowest = sorted(cases(original).items(), key=lambda row: (-row[1]["seconds"], row[0]))[:30]
    flaky = sorted({test for p in directory.glob("*-flaky.json")
                    for test in json.loads(p.read_text(encoding="utf-8"))["tests"]})
    text = "\n### Slowest 30 tests in this shard (first attempt, setup + call + teardown)\n\n| Test | Seconds |\n| --- | ---: |\n"
    text += "".join(f"| `{node.replace('|', '&#124;')}` | {row['seconds']:.3f} |\n" for node, row in slowest)
    text += "\n### Passed on the single failed-test rerun (FLAKY)\n\n" + ("".join(f"- FLAKY `{node}`\n" for node in flaky) or "None.\n")
    quarantined = [entry for p in directory.glob("*-flaky.json")
                   for entry in json.loads(p.read_text(encoding="utf-8")).get("quarantined", [])]
    text += "\n### Reviewed quarantine (tests executed)\n\n" + ("".join(
        f"- `{entry['nodeid']}`: {entry['outcome']}; owner {entry['owner']}; expires {entry['expires']}\n"
        for entry in quarantined) or "None.\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"flaky={'true' if flaky else 'false'}\n")
    (directory / "flaky.json").write_text(json.dumps({"tests": flaky, **({"quarantined": quarantined} if quarantined else {}), "attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
        "shard": os.getenv("SHARD"), "suite": os.getenv("GITHUB_JOB", "tests")}, indent=2) + "\n", encoding="utf-8")


def shard_reports(directory):
    for path in directory.rglob("flaky.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        yield int(report["attempt"]), (report.get("suite", "tests"), int(report["shard"])), path.parent, report["tests"]


def durations(directory):
    latest = {}
    for attempt, shard, path, _ in shard_reports(directory):
        if shard[0] in ('cpu-budget', 'solver-cap'):
            continue  # separate qualification invocations must not replace ordinary shard duration weights
        if shard not in latest or attempt > latest[shard][0]:
            latest[shard] = (attempt, path)
    for shard in sorted(latest):
        for report in sorted(latest[shard][1].glob("junit-*.xml")):
            if not report.stem.endswith("-retry") and not nonblocking(report):
                print(report)


def api(method, path, payload=None):
    request = urllib.request.Request("https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + path,
        data=json.dumps(payload).encode() if payload is not None else None, method=method,
        headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


START = "<!-- flaky-report\n"
STATE_END = "\n-->"
END = "<!-- /flaky-report -->"


def issue_body(body, tests, run_key, run_url):
    # ponytail: history grows per workflow attempt; move it out before reaching the issue-body limit.
    begin = body.find(START)
    end = body.index(END, begin) + len(END) if begin >= 0 else len(body)
    state = json.loads(body[begin + len(START):body.index(STATE_END, begin)]) if begin >= 0 else {"runs": [], "tests": {}}
    if run_key in state["runs"]:
        return body
    state["runs"].append(run_key)
    for node in tests:
        row = state["tests"].setdefault(node, {"count": 0})
        row.update(count=row["count"] + 1, run=run_url)
    table = "\n| Test id | Count | Latest run |\n| --- | ---: | --- |\n"
    table += "".join(f"| `{node.replace('|', '&#124;')}` | {row['count']} | [run]({row['run']}) |\n"
                     for node, row in sorted(state["tests"].items()))
    block = START + json.dumps(state, sort_keys=True) + STATE_END + table + END
    # Preserve maintainer notes outside the generated block; observations are not permanent exclusions.
    return body[:begin] + block + body[end:] if begin >= 0 else body + "\n" + block


def issue(directory):
    observations = {}
    for attempt, _, _, tests in shard_reports(directory):
        if tests:
            observations.setdefault(attempt, set()).update(tests)
    if not observations:
        return
    found = None
    page = 1
    while True:
        rows = api("GET", f"/issues?state=all&per_page=100&page={page}")
        found = next((row for row in rows if row["title"] == "Flaky tests" and "pull_request" not in row), None)
        if found or len(rows) < 100:
            break
        page += 1
    try:
        api("GET", "/labels/flaky")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        api("POST", "/labels", {"name": "flaky", "color": "fbca04", "description": "Failed once, passed the single CI rerun"})
    run_url = "https://github.com/" + os.environ["GITHUB_REPOSITORY"] + "/actions/runs/" + os.environ["GITHUB_RUN_ID"]
    body = (found or {}).get("body") or (
        "CI observations: each listed test failed, then passed one rerun.\n"
        "Tests run on every attempt; reviewed quarantine entries are reported as expected failures. Investigate intermittent product defects as well as test flakiness.\n"
        "Count is the number of workflow attempts observing that transition.")
    for attempt, tests in sorted(observations.items()):
        body = issue_body(body, sorted(tests), os.environ["GITHUB_RUN_ID"] + ":" + str(attempt), run_url)
    payload = {"title": "Flaky tests", "body": body, "state": "open",
               "labels": sorted({"flaky", *(label["name"] for label in (found or {}).get("labels", []))})}
    api("PATCH" if found else "POST", f"/issues/{found['number']}" if found else "/issues", payload)


def main(argv):
    if argv[:1] == ["run"]:
        return run(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("summary", "issue", "durations"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    return {"summary": summary, "issue": issue, "durations": durations}[args.mode](args.directory)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
