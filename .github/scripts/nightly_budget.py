"""Check the available job budget before starting the scheduled CI matrix."""

import os
from pathlib import Path
import subprocess

JOB_LIMIT = 120
NIGHTLY_JOBS = 19


def can_start(running):
    return JOB_LIMIT - running >= NIGHTLY_JOBS


def github_ids(repository, resource, query):
    result = subprocess.check_output(
        ["gh", "api", "--method", "GET", "--paginate", "--jq", query,
         f"repos/{repository}/actions/{resource}"],
        text=True, stderr=subprocess.PIPE, timeout=30,
    )
    return {int(line) for line in result.splitlines()}


def running_jobs(repository):
    runs = github_ids(repository, "runs?status=in_progress&per_page=100", ".workflow_runs[].id")
    if len(runs) >= 1000:
        raise ValueError("The workflow run query reached its result limit")
    jobs = set()
    for run in sorted(runs):
        jobs.update(github_ids(repository, f"runs/{run}/jobs?filter=latest&per_page=100",
                               '.jobs[] | select(.status == "in_progress") | .id'))
    return len(jobs)


def main():
    admitted = False
    try:
        running = running_jobs(os.environ["GITHUB_REPOSITORY"])
    except (subprocess.SubprocessError, OSError, ValueError):
        message = "Nightly skipped: could not read the current job budget."
    else:
        admitted = can_start(running)
        action = "admitted" if admitted else "skipped"
        message = f"Nightly {action}: {running} running jobs, {NIGHTLY_JOBS} free slots required."
    print(message)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"admitted={str(admitted).lower()}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(message + "\n")


if __name__ == "__main__":
    main()
