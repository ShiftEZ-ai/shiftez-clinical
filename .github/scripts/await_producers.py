"""Wait for the jobs named by the protected CI plan before judging their evidence."""

import argparse
from contextlib import closing
import http.client
import json
import os
import re
import time
from urllib.parse import urlsplit


POLL_SECONDS = 25
PROGRESS_SECONDS = 180
CONCLUSIONS = {"success", "failure", "cancelled", "skipped", "timed_out", "action_required",
               "neutral", "stale", "startup_failure"}


class ApiError(Exception):
    def __init__(self, status, headers):
        super().__init__(f"HTTP {status}")
        self.delay = 0
        if status in (403, 429):
            retry_after = headers.get("Retry-After", "")
            reset = headers.get("X-RateLimit-Reset", "")
            self.delay = int(retry_after) if retry_after.isdecimal() else 60
            if headers.get("X-RateLimit-Remaining") == "0" and reset.isdecimal():
                self.delay = max(self.delay, int(reset) - time.time())


def positive_ints(raw):
    values = json.loads(raw)
    if (not isinstance(values, list) or not values
            or any(type(value) is not int or value < 1 for value in values)
            or len(set(values)) != len(values)):
        raise ValueError("Invalid producer matrix")
    return values


def expected_jobs(outputs):
    replicas = positive_ints(outputs["replicas"])
    expected = {"Static checks"}
    for suite in ("unit", "browser"):
        count = int(outputs[f"{suite}_count"])
        shards = positive_ints(outputs[f"{suite}_matrix"])
        if max(shards) > count:
            raise ValueError("Producer matrix exceeds planned shard count")
        expected.update(f"{suite.title()} ({shard}/{count}, run {replica}) / {suite}"
                        for shard in shards for replica in replicas)
    return expected


def recent_jobs(api, run_path, token, expected, deadline):
    jobs = {}
    page = 1
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        path = f"{run_path}/jobs?filter=latest&per_page=100&page={page}"
        with closing(http.client.HTTPSConnection(api.netloc, timeout=min(10, remaining))) as connection:
            connection.request("GET", path, headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json", "User-Agent": "ci-result"})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ApiError(response.status, response.headers)
                rows = json.load(response)["jobs"]
                has_next = 'rel="next"' in response.getheader("Link", "")
        for row in rows:
            name = row["name"]
            if name not in expected:
                continue
            if name in jobs:
                raise ValueError("Duplicate producer in latest jobs response: " + name)
            jobs[name] = row
        if not has_next:
            return jobs
        # Rebuild the next request against the same run and API origin.
        page += 1


def wait_for_jobs(api, run_path, token, expected, deadline_minutes):
    deadline = time.monotonic() + deadline_minutes * 60
    next_progress = time.monotonic() + PROGRESS_SECONDS
    missing, pending = expected, set()
    delay = POLL_SECONDS
    api_status = ""
    while time.monotonic() < deadline:
        try:
            jobs = recent_jobs(api, run_path, token, expected, deadline)
        except (ApiError, OSError, http.client.HTTPException) as error:
            api_status = str(error) if isinstance(error, ApiError) else type(error).__name__
            delay = min(2 * delay, 120)
            wait = max(delay, error.delay) if isinstance(error, ApiError) else delay
        else:
            api_status = ""
            wait = delay = POLL_SECONDS
            missing = expected - jobs.keys()
            pending = {name for name, row in jobs.items() if row["status"] != "completed"}
            failed = []
            for name in sorted(jobs.keys() - pending):
                conclusion = jobs[name]["conclusion"]
                if conclusion != "success":
                    label = conclusion if conclusion in CONCLUSIONS else "unexpected conclusion"
                    failed.append(f"{name} ({label})")
            if failed:
                raise SystemExit("CI producers did not succeed: " + ", ".join(failed))
            if not (missing or pending) and time.monotonic() < deadline:
                print(f"All {len(expected)} CI producers succeeded")
                return
        now = time.monotonic()
        if now >= next_progress:
            detail = f"; API {api_status}" if api_status else ""
            print(f"Waiting for CI producers: {len(pending)} pending, {len(missing)} missing{detail}", flush=True)
            next_progress = now + PROGRESS_SECONDS
        time.sleep(max(0, min(wait, deadline - time.monotonic())))

    unfinished = [f"{name} (missing)" for name in sorted(missing)]
    unfinished += [f"{name} (pending)" for name in sorted(pending)]
    if api_status:
        unfinished.append(f"jobs API unavailable ({api_status})")
    raise SystemExit("CI producer deadline passed: " + (", ".join(unfinished) or "completion arrived too late"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deadline-minutes", type=int, required=True)
    args = parser.parse_args()
    try:
        needs = json.loads(os.environ["NEEDS_JSON"])
        if set(needs) != {"checks"} or needs["checks"]["result"] != "success":
            raise SystemExit("CI producer did not succeed: Checks")
        expected = expected_jobs(needs["checks"]["outputs"])
        repo, run_id = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"]
        api = urlsplit(os.environ["GITHUB_API_URL"])
        token = os.environ["ACTIONS_TOKEN"]
        if (api.scheme != "https" or not api.hostname or api.username or api.query or api.fragment
                or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[0-9]+", run_id)
                or args.deadline_minutes <= 0 or not token):
            raise ValueError("Invalid jobs API configuration")
        run_path = f"{api.path.rstrip('/')}/repos/{repo}/actions/runs/{run_id}"
    except (KeyError, TypeError, ValueError):
        raise SystemExit("Invalid or missing Checks outputs or jobs API configuration") from None

    try:
        wait_for_jobs(api, run_path, token, expected, args.deadline_minutes)
    except (KeyError, TypeError, ValueError):
        raise SystemExit("CI producers could not be verified: invalid jobs API response") from None

    # Preserve the producer contract of the protected verdict script.
    needs.update({job: {"result": "success"} for job in ("static", "unit", "browser")})
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print("verified_needs=" + json.dumps(needs, separators=(",", ":")), file=output)


if __name__ == "__main__":
    main()
