"""Shared quarantine data validation and anonymous pytest node identity."""
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import re

QUARANTINE = "tests/quarantine.json"
QUARANTINE_XFAIL_PREFIX = "QUARANTINED ("
TEXT_FIELDS = ("nodeid", "reason", "owner", "added", "expires")
REQUIRED_FIELDS = {*TEXT_FIELDS, "issue", "blocker"}
OPTIONAL_FIELDS = {"recovered_runs", "milestone"}

BASE_NODE = r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py::(?:[A-Za-z_][A-Za-z0-9_]*::)*test_[A-Za-z0-9_]+"
SAFE_NODE = re.compile(BASE_NODE + r"(?:\[case-[a-p]{64}\])?")


def canonical_node(node):
    base, marker, parameter = node.partition("[")
    if not re.fullmatch(BASE_NODE, base) or marker and not parameter.endswith("]"):
        raise ValueError("Invalid collected node identity")
    if not marker:
        return base
    # Keep every parameter case distinct without publishing its possibly private display label.
    fingerprint = hashlib.sha256(parameter.encode("utf-8")).hexdigest().translate(
        str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    return base + "[case-" + fingerprint + "]"


def utc_today():
    return datetime.now(timezone.utc).date()  # real clock: quarantine expiry is UTC policy


def decode_registry(raw, today):
    def unique_fields(pairs):
        row = dict(pairs)
        if len(row) != len(pairs):
            raise ValueError("duplicate quarantine field")
        return row

    rows = json.loads(raw, object_pairs_hook=unique_fields)
    if not isinstance(rows, list):
        raise ValueError("quarantine must be a JSON list")
    found = {}
    for row in rows:
        if (not isinstance(row, dict) or not REQUIRED_FIELDS <= row.keys()
                or row.keys() - REQUIRED_FIELDS - OPTIONAL_FIELDS):
            raise ValueError("quarantine entries require nodeid, reason, owner, added, expires, issue and blocker; "
                             "recovered_runs and milestone are optional")
        if "recovered_runs" in row:
            runs = row["recovered_runs"]
            if (not isinstance(runs, list) or not runs
                    or not all(isinstance(run, str) and re.fullmatch(r"[0-9]+", run) for run in runs)):
                raise ValueError("quarantine recovery evidence requires numeric run IDs")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in TEXT_FIELDS):
            raise ValueError("quarantine text fields must be nonempty strings")
        node = row["nodeid"]
        if not SAFE_NODE.fullmatch(node):
            raise ValueError("invalid quarantine nodeid: expected an anonymous test identity")
        if node in found:
            raise ValueError("duplicate quarantine nodeid: " + node)
        if type(row["issue"]) is not int or row["issue"] < 1:
            raise ValueError("quarantine issue must be an integer of at least 1: " + node)
        if "milestone" in row and (not isinstance(row["milestone"], str) or not row["milestone"].strip()):
            raise ValueError("quarantine milestone must be a nonempty string: " + node)
        if type(row["blocker"]) is not bool:  # required: a release verdict must never read a missing flag as "not a blocker"
            raise ValueError("quarantine blocker must be true or false: " + node)
        try:
            added, expires = date.fromisoformat(row["added"]), date.fromisoformat(row["expires"])
        except ValueError:
            raise ValueError("invalid quarantine date: " + node) from None
        if added > expires or added.isoformat() != row["added"] or expires.isoformat() != row["expires"]:
            raise ValueError("invalid quarantine date range: " + node)
        if added > today:
            raise ValueError("future quarantine: " + node)
        found[node] = row
    return found


def expired_entries(registry, today):
    return [row for row in registry.values() if date.fromisoformat(row["expires"]) <= today]


def expired_issue_summary(registry, today):
    counts = Counter(row["issue"] for row in expired_entries(registry, today))
    groups = [f"#{issue}: {count} {'node' if count == 1 else 'nodes'}"
              for issue, count in sorted(counts.items())]
    return "Expired quarantine entries by issue: " + (", ".join(groups) or "None")
