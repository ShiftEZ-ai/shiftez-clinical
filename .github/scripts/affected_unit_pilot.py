"""Prove and run the fixture-free quota assertion pilot from protected controls."""
from datetime import date
from hashlib import sha256
import os
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trusted_ci


TEST = "tests/test_quota_windows.py"
SCOPE = "quota-unit-expectation"
EXPECTED = re.compile(rb"(?m)^    assert len\(windows\(rule, '([0-9-]{10})', '([0-9-]{10})'\)\) == (?:[1-9][0-9]|[0-9])$")
SOURCE_SHA256 = "f0d7feb0184129709e8b5b041d218ae23e9f7f33f6df1ce65d4de2fe90620b91"
OWNERS = {
    ".github/scripts/affected_unit_pilot.py",
    "ci/hosted-durations.json",
    "tests/durations.json",
    "tests/test_quota_counting_periods.py",
    "tests/test_quota_repeating_validity.py",
}
policy = trusted_ci.policy


def assertion_source(source):
    if source is None:
        return False
    matches = list(EXPECTED.finditer(source))
    if len(matches) != 1:
        return False
    span = matches[0]
    try:
        start, end = (date.fromisoformat(value.decode()) for value in span.groups()[:2])
    except ValueError:
        return False
    if not date(2026, 10, 4) <= start < end <= date(2026, 11, 1):
        return False
    original = b"    assert len(windows(rule, '2026-10-11', '2026-10-12')) == 0"
    normalized = source[:span.start()] + original + source[span.end():]
    return sha256(normalized).hexdigest() == SOURCE_SHA256


def quota_expectation_change(before, after):
    return before != after and assertion_source(before) and assertion_source(after)


def qualified_base():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request" or os.getenv("TRUSTED_BOOTSTRAP") == "true":
        return None
    data = policy.event()
    pr = data["pull_request"]
    if (pr["base"]["ref"] != policy.default_branch(data)
            or (pr["head"].get("repo") or {}).get("id") != data["repository"]["id"]
            or any(label["name"] == "full-ci" for label in pr["labels"])):
        return None
    candidate = policy.git("rev-parse", "HEAD").decode().strip()
    base = policy.pr_test_base(data, candidate)
    if trusted_ci.pr_changed_files(base, candidate, policy.git) != {TEST}:
        return None
    if any(not policy.git("ls-tree", rev, "--", TEST).startswith(b"100644 blob ") for rev in (base, candidate)):
        return None
    if not quota_expectation_change(policy.read_file(base, TEST), policy.read_file(candidate, TEST)):
        return None
    references = policy.git("grep", "-zlF", Path(TEST).stem, base, "--", "apps", "packages", "scripts", "tests", ".github", "ci")
    if {path.decode().split(":", 1)[1] for path in policy.paths(references)} != OWNERS:
        return None
    return base


def run_unit(base):
    hardening = trusted_ci.hardening.load()
    if not all(hardening[key] for key in ("canaries", "node_census")):
        raise ValueError("The affected unit pilot needs the trusted census and canaries")
    with tempfile.TemporaryDirectory(prefix="unit-base-", dir=os.environ["RUNNER_TEMP"]) as root:
        base_dir = Path(root) / "source"
        policy.git("worktree", "add", "--detach", str(base_dir), base)
        try:
            os.environ.update(CENSUS_BASE_DIR=str(base_dir), CENSUS_BASE_SHA=base, SHARD_INDEX="1", REPLICA="1")
            reports = Path(".run/reports")
            reports.mkdir(parents=True, exist_ok=True)
            report = reports / "junit.xml"
            status = trusted_ci.invocation([TEST], "unit", report, [], "0", [TEST])
            selected = policy.decode(report.with_suffix(".selection.json").read_bytes())
            census = policy.decode((reports / "base-census.json").read_bytes())
            verify_unit_report(status, selected, census["nodes"], list(ET.parse(report).iter("testcase")))
            print(f"Affected unit pilot passed: {len(selected)} nodes")
        finally:
            policy.git("worktree", "remove", "--force", str(base_dir))


def verify_unit_report(status, selected, base_nodes, cases):
    observed = sorted(trusted_ci.canaries.node(case) for case in cases)
    if (status or len(selected) != 4 or len(set(selected)) != 4
            or observed != sorted(selected) or observed != sorted(base_nodes)
            or any(trusted_ci.canaries.outcome(case) != "passed" for case in cases)):
        raise ValueError("The affected unit checks did not pass with unchanged node coverage")


if __name__ == "__main__":
    base = qualified_base()
    if sys.argv[1:] == ["scope"]:
        if base is not None:
            trusted_ci.output({"scope": SCOPE})
    elif sys.argv[1:] == ["test"] and base is not None:
        run_unit(base)
    else:
        raise SystemExit("The candidate did not qualify for the affected unit pilot")
