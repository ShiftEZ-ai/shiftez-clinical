"""Keep the newest artifact for each planned test shard."""
import argparse
import json
from pathlib import Path
import re
import shutil


def planned_shards(plan):
    return {(suite, index, replica) for suite, row in plan["suites"].items()
            for index in range(1, len(row["shards"]) + 1) for replica in plan["replicas"]}


def artifact_shard(folder):
    match = re.fullmatch(r"ci-test-([0-9]+)-([a-z]+)-([0-9]+)-([0-9]+)", folder.name)
    if match is None or folder.is_symlink() or not folder.is_dir():
        raise ValueError("Invalid shard artifact folder: " + folder.name)
    attempt, suite, shard, replica = match.groups()
    return attempt, (suite, int(shard), int(replica))


def producing_attempt(directory, path, identity, current_attempt):
    attempt, shard = artifact_shard(directory / path.relative_to(directory).parts[0])
    if shard != identity or not 1 <= int(attempt) <= int(current_attempt):
        raise ValueError("Shard evidence differs from the artifact identity or attempt")
    return attempt


def require_planned_shards(expected, actual):
    if not actual or actual != expected:
        missing = ", ".join(f"{suite}-{index}-{replica}" for suite, index, replica in sorted(expected - actual))
        raise ValueError("Missing or unexpected shard artifacts" + (": " + missing if missing else ""))


def validate_selected_reports(plan, directory):
    identities = [artifact_shard(folder)[1] for folder in directory.iterdir()]
    if len(identities) != len(set(identities)):
        raise ValueError("More than one artifact for a planned shard")
    require_planned_shards(planned_shards(plan), set(identities))


def keep_latest(directory, plan):
    artifacts = {}
    for folder in sorted(directory.iterdir()):
        attempt, shard = artifact_shard(folder)
        attempts = artifacts.setdefault(shard, {})
        if int(attempt) in attempts:
            raise ValueError("Duplicate shard artifact attempt: " + folder.name)
        attempts[int(attempt)] = folder
    require_planned_shards(planned_shards(plan), artifacts.keys())
    for attempts in artifacts.values():
        newest = max(attempts)
        print("Keeping " + attempts[newest].name)
        for attempt, folder in attempts.items():
            if attempt != newest:
                shutil.rmtree(folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        keep_latest(args.directory, json.loads(args.plan.read_bytes()))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
