"""Bind a main push to the protected merge-group verdict for the same plan inputs."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipDataError

TRUSTED = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(TRUSTED / "ci")]
import hosted_ci as hosted
from quarantine_registry import utc_today
import stale_evidence as evidence
from trusted_ci import output
from trusted_verdict import assert_inventory
import working_policy as policy

MAIN = "refs/heads/main"
QUEUE_ACTOR = ("github-merge-queue[bot]", "118344674")
CONTROLS = (evidence.WORKFLOW, ".github/workflows/ci-tests.yml", ".github/actions",
            ".github/scripts", "ci", "requirements/policy.lock")
PROOF = "queue-proof.json"
READ_ERRORS = (BadZipFile, ZipDataError, KeyError, OSError, RuntimeError, TypeError, ValueError,
               AttributeError, subprocess.SubprocessError)


def log_text(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "ACTIONS_RUNTIME_TOKEN"):
        if token := os.getenv(key):
            text = text.replace(token, "[redacted]")
    text = re.sub(r"(?i)https?://\S+|\b(?:gh[pousr]_|github_pat_)[a-z0-9_]+", "[redacted]", text)
    text = re.sub(r"(?i)\b((?:bearer|basic)\s+|[a-z_]*token\s*[:=]\s*)\S+", r"\1[redacted]", text)
    return " ".join("".join(char if char.isprintable() else " " for char in text).split())


def refusal(error):
    if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        command = error.cmd
        if isinstance(command, (list, tuple)):
            if tuple(command[:2]) == ("git", "-C"):
                command = ["git", "-C", "<trusted checkout>", *command[3:]]
            command = shlex.join(command)
        if isinstance(error, subprocess.CalledProcessError):
            status = f"exit {error.returncode}"
        else:
            status = f"timed out after {error.timeout}s"
        stderr = error.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = log_text(stderr)[:240] or "empty"
        return f"{log_text(command)} ({status}, stderr: {detail})"
    if type(error) is ValueError:
        return log_text(str(error))
    return f"{type(error).__name__}: {log_text(str(error))[:240]}"


def git(*args):
    return subprocess.check_output(["git", "-C", str(TRUSTED), *args], stderr=subprocess.PIPE)


def revision(ref):
    return policy.commit(git("rev-parse", ref).decode().strip())


def bind_plan(plan, sha, tree, base):
    if (plan["schema"] != 1 or (plan["sha"], plan["tree"], plan["base_sha"]) != (sha, tree, base)
            or plan["replicas"] != [1] or plan["smoke"] is not False or "selection" in plan):
        raise ValueError("Expected the full single-replica plan for the candidate and base")


def record(plan, reports, plan_attempt):
    if os.environ["GITHUB_EVENT_NAME"] != "merge_group":
        return
    event = policy.event()
    group = event["merge_group"]
    if policy.default_branch(event) != "main" or group["base_ref"] != MAIN:
        return
    sha = policy.commit(group["head_sha"])
    base = policy.commit(group["base_sha"])
    if sha != os.environ["GITHUB_SHA"]:
        raise ValueError("Queue plan head differs from the event")
    tree = revision(sha + "^{tree}")
    bind_plan(plan, sha, tree, base)
    if policy.decode((reports / "verdict.json").read_bytes()).get("passed") is not True:
        raise ValueError("Queue verdict did not pass")
    proof = {
        "schema": 1, "sha": sha, "tree": tree, "base_sha": base,
        "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "plan_attempt": int(plan_attempt), "plan_sha256": hosted.digest(plan),
        "trusted_sha": revision("HEAD"), "workflow_sha": policy.commit(os.environ["GITHUB_WORKFLOW_SHA"]),
    }
    (reports / PROOF).write_text(json.dumps(proof) + "\n", encoding="utf-8")


def check_controls(base, *revisions):
    for sha in dict.fromkeys(revisions):
        changed = git("diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "--no-renames",
                      base, sha, "--", *CONTROLS)
        if changed:
            raise ValueError("control files changed: " + ", ".join(os.fsdecode(path) for path in policy.paths(changed)))


def qualified_run(run, sha):
    if (run["head_sha"] != sha or run["conclusion"] != "success"
            or not evidence.dev_run(run, "merge_group", git, sha, None, "main")):
        raise ValueError("No successful merge-group run for the exact main commit")


def verified_reports(push_plan, required_run=None):
    if (os.getenv("GITHUB_EVENT_NAME") != "push" or os.getenv("GITHUB_REF") != MAIN
            or (os.getenv("GITHUB_ACTOR"), os.getenv("GITHUB_ACTOR_ID")) != QUEUE_ACTOR):
        return None
    event = policy.event()
    sha = policy.commit(os.environ["GITHUB_SHA"])
    base = policy.push_base(event, "main", sha)
    if (policy.default_branch(event) != "main" or event["ref"] != MAIN or event["after"] != sha
            or any(event[key] is not False for key in ("created", "deleted", "forced"))
            or os.environ["GITHUB_WORKFLOW_SHA"] != sha):
        raise ValueError("Expected a protected main queue landing")
    tree = revision(sha + "^{tree}")
    bind_plan(push_plan, sha, tree, base)
    check_controls(base, sha, revision("HEAD"))

    repository = event["repository"]["full_name"]
    # ponytail: one exact-SHA page. Ambiguous or missing evidence runs the full suites.
    query = "workflows/ci.yml/runs?event=merge_group&head_sha=" + sha + "&per_page=100"
    response = policy.decode(evidence.github_get(repository, query))
    runs = response["workflow_runs"]
    if response["total_count"] != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise ValueError("Expected one merge-group run for the exact commit")
    run = runs[0]
    qualified_run(run, sha)
    run_id, attempt = run["id"], run["run_attempt"]
    identity = f"{run_id}:{attempt}"
    if required_run is not None and required_run != identity:
        raise ValueError("Queue run or attempt changed after Checks")
    jobs = policy.decode(evidence.github_get(repository, f"runs/{run_id}/attempts/{attempt}/jobs?per_page=100"))
    gates = [job for job in jobs["jobs"] if job["name"] == "ci-gate"]
    if (jobs["total_count"] != len(jobs["jobs"]) or len(gates) != 1
            or gates[0]["status"] != "completed" or gates[0]["conclusion"] != "success"
            or (gates[0]["run_id"], gates[0]["run_attempt"], gates[0]["head_sha"]) != (run_id, attempt, sha)):
        raise ValueError("The required queue gate did not complete successfully")
    if not gates[0]["started_at"].startswith(utc_today().isoformat() + "T"):
        raise ValueError("Queue verdict predates today's allowance evaluation")

    gate_id = gates[0]["id"]
    if type(gate_id) is not int or gate_id < 1:
        raise ValueError("Invalid queue gate job id")
    # Capture the raw log in memory. Only the exact qualification marker is used.
    log = evidence.github_get(repository, f"jobs/{gate_id}/logs", raw=True)
    recorded_ids = policy.re.findall(rb"^\S+Z Queue qualification artifact id ([1-9][0-9]*)\r?$", log, policy.re.M)
    artifacts = list(evidence.run_artifacts(repository, run))
    qualification = [a for a in artifacts if a["name"] == f"known-failures-candidate-{attempt}"]
    if (len(recorded_ids) != 1 or len(qualification) != 1
            or qualification[0]["id"] != int(recorded_ids[0])):
        raise ValueError("Qualification artifact differs from the trusted gate upload")
    with evidence.qualification_archive(repository, run, qualification) as archive:
        names = archive.namelist()
        if (len(names) != len(set(names))
                or any(not policy.re.fullmatch(r"[a-z][a-z0-9.-]*\.json", name) for name in names)
                or sum(member.file_size for member in archive.infolist()) > 8_000_000):
            raise ValueError("Invalid queue qualification archive")
        reports = {name: archive.read(name) for name in names}
    proof = policy.decode(reports[PROOF])
    if (proof["schema"] != 1 or (proof["run_id"], proof["run_attempt"]) != (run_id, attempt)
            or (proof["sha"], proof["tree"], proof["base_sha"]) != (sha, tree, base)
            or policy.decode(reports["verdict.json"]).get("passed") is not True):
        raise ValueError("Queue verdict evidence differs from the landed commit or run")
    check_controls(base, policy.commit(proof["workflow_sha"]), policy.commit(proof["trusted_sha"]))
    plan_attempt = proof["plan_attempt"]
    if type(plan_attempt) is not int or not 1 <= plan_attempt <= attempt:
        raise ValueError("Invalid queue plan attempt")
    plans = [row for row in artifacts if row["name"] == f"ci-plan-{plan_attempt}"]
    if len(plans) != 1:
        raise ValueError("Expected one original queue plan")
    with ZipFile(BytesIO(evidence.artifact_bytes(repository, run, plans[0]))) as archive:
        if archive.namelist() != ["shard-plan.json"] or archive.infolist()[0].file_size > 2_000_000:
            raise ValueError("Invalid queue plan archive")
        queue_plan = policy.decode(archive.read("shard-plan.json"))
    bind_plan(queue_plan, sha, tree, base)
    if (proof["plan_sha256"] != hosted.digest(queue_plan)
            or any(queue_plan[key] != push_plan[key] for key in ("inventory_sha256", "duration_sha256"))):
        raise ValueError("Queue plan seal or planner inputs differ")
    inventory = git("ls-tree", "-r", "-z", sha, "--", "tests")
    for plan in (queue_plan, push_plan):
        # Both plans cover the committed files, with event-specific shard widths.
        assert_inventory(plan, inventory, {suite: len(row["shards"]) for suite, row in plan["suites"].items()})
    current = policy.decode(evidence.github_get(repository, f"runs/{run_id}"))
    qualified_run(current, sha)
    if (current["id"], current["run_attempt"], current["head_branch"], current["workflow_id"]) != (
            run_id, attempt, run["head_branch"], run["workflow_id"]):
        raise ValueError("Queue run changed while reading evidence")
    return identity, reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--record-attempt")
    parser.add_argument("--require-run")
    args = parser.parse_args()
    try:
        plan = policy.decode(args.plan.read_bytes())
        if args.record_attempt:
            record(plan, args.reports, args.record_attempt)
            return
        found = verified_reports(plan, args.require_run)
    except READ_ERRORS as error:
        reason = refusal(error)
        if args.record_attempt:
            message = f"Queue reuse proof could not be recorded ({reason})"
        elif args.require_run is not None:
            raise SystemExit("Queue reuse verification failed: " + reason) from None
        else:
            message = f"Queue proof unavailable ({reason}); running the full suites"
        print("::notice::" + message.replace("%", "%25"))
        return
    if args.require_run is not None:
        if found is None:
            raise SystemExit("Queue reuse is only valid for a main queue landing")
        identity, reports = found
        args.reports.mkdir(parents=True, exist_ok=True)
        for name, raw in reports.items():
            (args.reports / name).write_bytes(raw)
        url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{identity.split(':')[0]}"
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            print(f"Reused verified [merge-group CI]({url}) (run and attempt {identity}).", file=summary)
    elif found is not None:
        output({"run": found[0]})


if __name__ == "__main__":
    main()
