"""Plan full, file-balanced CI shards and run each exactly once."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import urllib.request


SMOKE_FILES = {"unit": ["tests/test_about.py", "tests/test_api_startup.py"],
               "browser": ["tests/test_browser_guards.py", "tests/test_browser_offline.py"]}
TIMING_NODE = "tests/test_contact_preview_perf.py::test_contact_preview_at_department_scale_and_checked_undo[pg]"


def install_runtime(tool, pin, directory):
    if (not re.fullmatch(r"[a-f0-9]{64}", pin["sha256"]) or not pin["url"].startswith("https://")
            or not re.fullmatch(r"[\w.+-]+", pin["root"])
            or set(pin["files"]) != ({"bin/node"} if tool == "node" else {"bin/python3.13", "lib/libpython3.13.so.1.0"})
            or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in pin["files"].values())):
        raise ValueError("Invalid runtime hash lock")
    archives = directory / "runtime-archives"
    archives.mkdir(parents=True, exist_ok=True)
    archive = archives / (pin["sha256"] + ".tar")
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != pin["sha256"]:
        with urllib.request.urlopen(pin["url"], timeout=60) as source, archive.open("wb") as output:
            shutil.copyfileobj(source, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError("Runtime archive differs from its hash lock")
    destination = directory / ("runtime-" + tool + "-" + pin["sha256"])
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(destination, filter="data")
    prefix = destination / pin["root"]
    if {name: hashlib.sha256((prefix / name).read_bytes()).hexdigest() for name in pin["files"]} != pin["files"]:
        raise ValueError("Runtime binary differs from its hash lock")
    return prefix / "bin"


def execution_marker(suite):
    if suite not in ("unit", "browser"):
        raise ValueError("Unknown functional suite")
    return ("not browser" if suite == "unit" else "browser") + " and not quarantine and not cpu_budget and not wall_benchmark"


def case_rows(paths):
    import xml.etree.ElementTree as ET
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            parts = case.get("classname", "").split(".")
            module = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
            node = ("/".join(parts[:module + 1]) + ".py::" + "::".join(parts[module + 1:] + [case.get("name", "")])
                    if module is not None else case.get("name", "collection error"))
            yield node, {"outcome": "failed" if case.find("failure") is not None or case.find("error") is not None else (
                "skipped" if case.find("skipped") is not None else "passed")}


def pytest_addoption(parser):
    parser.addoption("--hosted-selection-output")
    parser.addoption("--hosted-deselected-output")


def pytest_configure(config):
    import pytest

    class Recorder:
        def __init__(self):
            self.selected = None
            self.deselected = set()

        def pytest_deselected(self, items):
            self.deselected.update(item.nodeid for item in items)

        def pytest_collection_finish(self, session):
            self.selected = sorted(item.nodeid for item in session.items)

        @pytest.hookimpl(optionalhook=True)
        def pytest_xdist_node_collection_finished(self, node, ids):
            selected = sorted(ids)
            if self.selected is not None and self.selected != selected:
                raise pytest.UsageError("Workers collected different node selections")
            self.selected = selected

        @pytest.hookimpl(optionalhook=True)
        def pytest_testnodedown(self, node, error):
            self.deselected.update(node.workeroutput.get("hosted_deselected", []))

        def pytest_sessionfinish(self, session):
            if hasattr(config, "workerinput"):
                config.workeroutput["hosted_deselected"] = sorted(self.deselected)
                return
            if self.selected is None or len(self.selected) != len(set(self.selected)):
                raise pytest.UsageError("Incomplete or duplicate node selection")
            for option, values in (("hosted_selection_output", self.selected),
                                   ("hosted_deselected_output", sorted(self.deselected))):
                output = config.getoption(option)
                if output:
                    path = Path(output)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(values) + "\n", encoding="utf-8")

    config.pluginmanager.register(Recorder(), "hosted-selection")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate duration key: " + key)
        result[key] = value
    return result


def load_durations(path):
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if data.get("schema") != 1 or set(data.get("suites", {})) != {"unit", "browser"}:
        raise ValueError("Missing or unsupported duration map")
    for suite, row in data["suites"].items():
        seconds = row["files"]
        if not seconds or any(not re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py", name)
                              or not isinstance(value, (int, float)) or isinstance(value, bool)
                              or not math.isfinite(value) or value < 0
                              for name, value in seconds.items()):
            raise ValueError("Invalid per-file duration: " + suite)
        if not re.fullmatch(r"[a-f0-9]{64}", row["junit_sha256"]):
            raise ValueError("Invalid duration-source hash")
    return data


def balance(files, seconds, count):
    if type(count) is not int or not 1 <= count <= min(256, len(files)):
        raise ValueError("Shard count must be 1..min(256, suite files)")
    if len(files) != len(set(files)) or not files:
        raise ValueError("Empty or duplicate file inventory")
    median = statistics.median(seconds.values())
    weights = {name: seconds.get(name, median) for name in files}
    shards, totals = [[] for _ in range(count)], [0.0] * count
    for name in sorted(files, key=lambda name: (-weights[name], name)):
        index = min(range(count), key=lambda i: (totals[i], len(shards[i]), i))
        shards[index].append(name)
        totals[index] += weights[name]
    if sorted(name for shard in shards for name in shard) != sorted(files) or not all(shards):
        raise ValueError("Incomplete shard coverage")
    return [{"files": sorted(shard), "expected_seconds": round(total, 3)}
            for shard, total in zip(shards, totals)]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def require_smoke(private, unit_count, browser_count):
    if private is not True or (unit_count, browser_count) != (2, 2):
        raise ValueError("Smoke requires a private repository and exactly two shards per suite")


def plan(unit_count, browser_count, smoke=False, base_sha=""):
    sys.path.insert(0, str(Path.cwd() / "ci"))
    from make_known_failures import context
    ratchet = context(base_sha)
    duration_path = Path("ci/hosted-durations.json")
    data = load_durations(duration_path)
    files = sorted(name for name in git("ls-files", "tests").splitlines()
                   if re.fullmatch(r"tests/(?:[\w-]+/)*test_[\w-]+\.py", name))
    if not files or any(Path(name).is_symlink() or not Path(name).is_file() for name in files):
        raise ValueError("Missing or linked tracked test file")
    result = {"schema": 1, "smoke": smoke, "sha": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
              "base_sha": ratchet["base_sha"], "replicas": [1] if smoke else ratchet["replicas"],
              "duration_sha256": hashlib.sha256(duration_path.read_bytes()).hexdigest(),
              "inventory_sha256": digest(files), "suites": {}}
    for suite, count in (("unit", unit_count), ("browser", browser_count)):
        selected = [name for name in files if (Path(name).name.startswith("test_browser")) == (suite == "browser")]
        if smoke:
            if not set(SMOKE_FILES[suite]) <= set(selected):
                raise ValueError("A required rehearsal smoke file is missing")
            selected = SMOKE_FILES[suite]
        seconds = data["suites"][suite]["files"]
        result["suites"][suite] = {"shards": balance(selected, seconds, count),
                                   "median_files": sorted(set(selected) - seconds.keys())}
    return result


def require_results(needs):
    if set(needs) != {"checks", "unit", "browser"} or any(
            row.get("result") != "success" for row in needs.values()):
        raise ValueError("Every CI producer must succeed (failure, cancellation and skip are blocking)")


def require_execution(report, targets, case_rows, *, allow_failures=False):
    selected = json.loads(report.with_suffix(".selection.json").read_text(encoding="utf-8"))
    deselected = json.loads(report.with_suffix(".deselected.json").read_text(encoding="utf-8"))
    rows = list(case_rows([report]))
    if (not selected or len(selected) != len(set(selected))
            or sorted(node for node, _ in rows) != sorted(selected)
            or not allow_failures and any(row["outcome"] == "failed" for _, row in rows)
            or not all(any(node == target or node.startswith(target + "::")
                           for node in selected + deselected) for target in targets)):
        raise ValueError("Incomplete or inconsistent first-attempt execution evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "run", "result", "runtime"))
    parser.add_argument("--tool", choices=("python", "node"))
    parser.add_argument("--unit-count", type=int, default=25)
    parser.add_argument("--browser-count", type=int, default=18)
    parser.add_argument("--suite", choices=("unit", "browser"))
    parser.add_argument("--shard", type=int)
    parser.add_argument("--replica", type=int, default=1)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--smoke", choices=("true", "false"), default="false")
    args = parser.parse_args()
    if args.mode == "runtime":
        import platform
        if sys.platform != "linux" or platform.machine() != "x86_64" or args.tool is None:
            raise ValueError("Runtime bootstrap requires Linux x86_64 and an explicit tool")
        pins = json.loads(Path("requirements/ci-runtimes.json").read_text(encoding="utf-8"))
        if pins.get("schema") != 1 or pins.get("platform") != "linux-x86_64":
            raise ValueError("Unsupported runtime lock")
        binary = install_runtime(args.tool, pins[args.tool], Path(os.environ["RUNNER_TEMP"]))
        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as output:
            output.write(str(binary) + "\n")
        return
    if args.mode == "result":
        sys.path.insert(0, str(Path.cwd() / "ci"))
        from make_known_failures import write_summary
        write_summary()
        require_results(json.loads(os.environ["NEEDS_JSON"]))
        verdict = json.loads(Path(".run/ratchet/verdict.json").read_text(encoding="utf-8"))
        if verdict.get("passed") is not True:
            raise SystemExit(verdict["reason"])
        return
    smoke = args.smoke == "true"
    if smoke:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        require_smoke(event["repository"]["private"], args.unit_count, args.browser_count)
    candidate = plan(args.unit_count, args.browser_count, smoke, args.base_sha)
    seal = digest(candidate)
    reports = Path(".run/reports")
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "shard-plan.json").write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
    if args.mode == "plan":
        from make_known_failures import context, preflight
        preflight(context(candidate["base_sha"]), smoke=smoke,
                  limit=int(os.getenv("UNSTABLE_LIMIT", "80")),
                  issue=int(os.getenv("UNSTABLE_ISSUE") or "0"),
                  expires_on=os.getenv("UNSTABLE_EXPIRES_ON", ""))
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("plan_sha256=" + seal + "\n")
            output.write("smoke=" + args.smoke + "\n")
            output.write("replicas=" + json.dumps(candidate["replicas"]) + "\n")
            output.write("base_sha=" + candidate["base_sha"] + "\n")
            for suite in ("unit", "browser"):
                count = len(candidate["suites"][suite]["shards"])
                output.write(suite + "_matrix=" + json.dumps(list(range(1, count + 1))) + "\n")
                output.write(suite + "_count=" + str(count) + "\n")
        print("Plan", seal, "tree=" + candidate["tree"])
        return
    if (seal != args.plan_sha256 or args.suite is None or args.shard is None
            or args.replica not in candidate["replicas"]):
        raise ValueError("Shard is not bound to the checks job's exact plan")
    shards = candidate["suites"][args.suite]["shards"]
    if not 1 <= args.shard <= len(shards):
        raise ValueError("Shard index is outside its plan")
    root = Path.cwd()
    sys.path.insert(0, str(root))
    selected = shards[args.shard - 1]["files"]
    marker = execution_marker(args.suite)
    commands = [("junit.xml", selected, "4", [])]
    # Preserve the existing isolated PostgreSQL timing case; it runs once, after this shard's workers stop.
    if args.suite == "unit" and TIMING_NODE.split("::")[0] in selected:
        commands[0][3].append("--deselect=" + TIMING_NODE)
        commands.append(("junit-timing.xml", [TIMING_NODE], "0", []))
    receipt = {"schema": 1, "sha": candidate["sha"], "tree": candidate["tree"], "plan_sha256": seal,
               "suite": args.suite, "shard": args.shard, "replica": args.replica,
               "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "reports": []}
    for report, targets, workers, extra in commands:
        command = [sys.executable, "-m", "pytest", "-p", "hosted_ci", "-q",
                   *targets, "-m", marker, "-n", workers, "--dist", "worksteal",
                   "--timeout=900", "--timeout-method=thread", "-o", "faulthandler_timeout=600",
                   "--hosted-selection-output=" + str((reports / report).with_suffix(".selection.json")),
                   "--hosted-deselected-output=" + str((reports / report).with_suffix(".deselected.json")),
                   "--junitxml=" + str(reports / report), *extra]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (
            str(root / ".github/scripts"), str(root), os.environ.get("PYTHONPATH"))))}
        code = subprocess.call(command, env=env)
        if code not in (0, 1):
            raise SystemExit(code)
        report_path = reports / report
        require_execution(report_path, targets, case_rows, allow_failures=True)
        if (code == 1) != any(row["outcome"] == "failed" for _, row in case_rows([report_path])):
            raise ValueError("Pytest exit code differs from its original JUnit")
        paths = [report_path, report_path.with_suffix(".selection.json"), report_path.with_suffix(".deselected.json")]
        receipt["reports"].append({"file": report, "exit_code": code,
                                   "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}})
    (reports / "shard-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
