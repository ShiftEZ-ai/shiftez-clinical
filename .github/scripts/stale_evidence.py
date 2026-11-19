"""Read bound stale-test evidence from the protected dev CI history."""
from io import BytesIO
import re
import subprocess
from zipfile import BadZipFile, ZipFile

import working_policy as policy
from make_known_failures import SAFE_NODE

WORKFLOW = ".github/workflows/ci.yml"


def github_get(repository, resource):
    return subprocess.check_output(
        ["gh", "api", "--hostname", "github.com", "--method", "GET", f"repos/{repository}/actions/{resource}"],
        stderr=subprocess.PIPE,
        timeout=60,
    )


def workflow_runs(repository, event, page=1):
    branch = "&branch=dev" if event == "schedule" else ""
    response = policy.decode(github_get(repository, f"workflows/ci.yml/runs?event={event}&status=completed"
                                                     f"{branch}&per_page=100&page={page}"))
    rows = response["workflow_runs"]
    if not isinstance(rows, list):
        raise ValueError("Invalid workflow run list")
    return rows


def ancestor(git, older, newer):
    try:
        git("merge-base", "--is-ancestor", older, newer)
    except subprocess.CalledProcessError:
        return False
    return True


def dev_run(run, event, git, tip, base=None):
    branch = run["head_branch"]
    if (run["event"] != event or run["path"] != WORKFLOW or run["status"] != "completed"
            or run["repository"]["id"] != policy.REPOSITORY or run["head_repository"]["id"] != policy.REPOSITORY
            or type(run["id"]) is not int or run["id"] < 1
            or type(run["run_attempt"]) is not int or run["run_attempt"] < 1
            or not isinstance(branch, str)):
        raise ValueError("Invalid dev CI run identity")
    if event == "merge_group" and not branch.startswith("gh-readonly-queue/dev/"):
        return False
    if event == "schedule" and branch != "dev":
        return False
    sha = policy.commit(run["head_sha"])
    return ancestor(git, sha, tip) and (base is None or ancestor(git, base, sha))


def run_artifacts(repository, run):
    page = 1
    while True:
        rows = policy.decode(github_get(repository, f"runs/{run['id']}/artifacts?per_page=100&page={page}"))["artifacts"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid run artifact list")
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def artifact_bytes(repository, run, artifact):
    source = artifact["workflow_run"]
    if (artifact["expired"] is not False or type(artifact["id"]) is not int or artifact["id"] < 1
            or source["repository_id"] != policy.REPOSITORY or source["head_repository_id"] != policy.REPOSITORY
            or source["id"] != run["id"] or source["head_sha"] != run["head_sha"]
            or source["head_branch"] != run["head_branch"]):
        raise ValueError("Missing or foreign run artifact")
    return github_get(repository, f"artifacts/{artifact['id']}/zip")


def read_stale_report(archive, run):
    name = "stale-report.json"
    if archive.namelist().count(name) != 1:
        raise ValueError("Expected one stale report")
    report = policy.decode(archive.read(name))
    if (set(report) != {"run_id", "sha", "nodes"} or report["run_id"] != str(run["id"])
            or report["sha"] != run["head_sha"]):
        raise ValueError("Stale report binding differs from its dev run")
    nodes = report["nodes"]
    if (not isinstance(nodes, list) or any(not isinstance(node, str) or not SAFE_NODE.fullmatch(node) for node in nodes)
            or len(set(nodes)) != len(nodes)):
        raise ValueError("Invalid stale test inventory")
    return set(nodes)


def qualification_archive(repository, run, artifacts):
    name = f"known-failures-candidate-{run['run_attempt']}"
    matches = [row for row in artifacts if row["name"] == name]
    if len(matches) != 1:
        raise ValueError("Expected one qualification artifact for the current attempt")
    return ZipFile(BytesIO(artifact_bytes(repository, run, matches[0])))


def confirmed_nodes(repository, git, tip, base, nodes):
    try:
        policy.commit(tip)
        policy.commit(base)
        if not ancestor(git, base, tip):
            return {}
        # ponytail: one page per event bounds the PR lookup; older retained evidence can use pagination if needed.
        for event in ("merge_group", "schedule"):
            runs = [run for run in workflow_runs(repository, event)
                    if dev_run(run, event, git, tip, base)
                    and (run["conclusion"] == "success" or event == "schedule" and run["conclusion"] == "failure")]
            if runs:
                break
        if not runs:
            return {}
        if any(not isinstance(r["created_at"], str)
               or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", r["created_at"]) for r in runs):
            return {}
        run = max(runs, key=lambda row: (row["created_at"], row["id"]))
        with qualification_archive(repository, run, run_artifacts(repository, run)) as archive:
            return dict.fromkeys(nodes & read_stale_report(archive, run), str(run["id"]))
    except (BadZipFile, KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
        return {}
