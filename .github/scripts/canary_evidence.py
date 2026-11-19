"""Verify raw sentinel outcomes and collected nodes before reading normalized product JUnit."""
import hashlib
import json
import os
import re
from pathlib import Path
import xml.etree.ElementTree as ET


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def outcome(case):
    return ("error" if case.find("error") is not None else "failed" if case.find("failure") is not None
            else "skipped" if case.find("skipped") is not None else "passed")


def node(case):
    parts = case.get("classname", "").split(".")
    module = next((i for i, part in enumerate(parts) if part.startswith("test_")), None)
    return ("/".join(parts[:module + 1]) + ".py::" + "::".join(parts[module + 1:] + [case.get("name", "")])
            if module is not None else case.get("name", "collection error"))


def verify_cases(cases, manifest, collected, *, allow_empty=False):
    if set(manifest) != {"passed", "failed", "skipped", "error"} or len(set(manifest.values())) != 4:
        raise ValueError("Four distinct random sentinels are required")
    seen, product = {}, []
    for case in cases:
        identity = node(case)
        matched = [kind for kind, token in manifest.items() if token in identity]
        if matched:
            if len(matched) != 1 or outcome(case) != matched[0]:
                raise ValueError("Sentinel outcome was forged")
            seen[matched[0]] = seen.get(matched[0], 0) + 1
        else:
            product.append(identity)
    # xdist may report the same collection error once per worker; every copy must be ERROR.
    if set(seen) != set(manifest) or any(seen[k] != 1 for k in ("passed", "failed", "skipped")):
        raise ValueError("Sentinel execution is missing or duplicated")
    expected = [n for n in collected if not any(t in n for t in manifest.values())]
    if (not expected and not allow_empty) or len(expected) != len(set(expected)) or sorted(product) != sorted(expected):
        raise ValueError("Reported node set differs from trusted collection of the same candidate")
    return expected


def collection_error(case):
    error = case.find("error")
    return error is not None and re.search(
        r"failed (?:during|on) collect(?:ion|ing)|error collecting", error.get("message", ""), re.I)


def report_cases(report, evidence, *, selected_scope=()):
    sidecars = (report, report.with_suffix(".selection.json"), report.with_suffix(".deselected.json"))
    if any(item.is_symlink() or not item.is_file() or sha(item) != evidence["sha256"].get(item.name)
           for item in sidecars):
        raise ValueError("Missing or changed shard evidence")
    selected = json.loads(sidecars[1].read_text(encoding="utf-8"))
    cases = list(ET.parse(report).getroot().iter("testcase"))
    intentionally_empty = False
    if not selected and selected_scope:
        deselected = json.loads(sidecars[2].read_text(encoding="utf-8"))
        intentionally_empty = bool(deselected) and all(node.split("::", 1)[0] in selected_scope for node in deselected)
    if ((not selected and not intentionally_empty) or len(selected) != len(set(selected))
            or sorted(node(case) for case in cases) != sorted(selected)
            or type(evidence["exit_code"]) is not int or evidence["exit_code"] not in (0, 1)
            or (evidence["exit_code"] == 1) != any(outcome(case) in {"failed", "error"} for case in cases)):
        raise ValueError("Incomplete execution or pytest infrastructure failure")
    if any(collection_error(case) for case in cases):
        raise ValueError("Collection error in test evidence")
    return cases


def retry_path(report, retry, *, allow_retry):
    path = report.with_name(report.stem + "-retry.xml")
    if (not allow_retry
            or set(retry) != {"file", "exit_code", "sha256"} or retry["file"] != path.name):
        raise ValueError("Unexpected retry evidence")
    return path


def failed_nodes(cases):
    return sorted(node(case) for case in cases if outcome(case) in {"failed", "error"})


def recovered_nodes(report, evidence, failures, *, allow_retry=False):
    if "retry" not in evidence:
        if failures:
            event = os.getenv("GITHUB_EVENT_NAME", "merge_group").replace("_", "-")
            raise ValueError("Missing " + event + " retry evidence: " + report.name)
        return set()
    retry = evidence["retry"]
    cases = report_cases(retry_path(report, retry, allow_retry=allow_retry), retry)
    if not failures or sorted(node(case) for case in cases) != failures:
        raise ValueError("Retry must execute exactly the unlisted first-attempt failed nodes")
    return {node(case) for case in cases if outcome(case) == "passed"}


def verify_directory(directory, *, allow_product_errors=False, partial=False, allow_retry=False):
    receipts = list(directory.rglob("shard-receipt.json"))
    if not receipts:
        raise ValueError("Missing shard receipts")
    for receipt_path in receipts:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        reports = []
        for row in receipt["reports"]:
            original = receipt_path.parent / row["file"]
            reports.append(original)
            if "retry" in row:
                reports.append(retry_path(original, row["retry"], allow_retry=allow_retry))
        for normalized in reports:
            ledger_path = normalized.with_suffix(".canaries.json")
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if (ledger["sha"] != receipt["sha"] or ledger["tree"] != receipt["tree"]
                    or ledger["run_id"] != receipt["run_id"] or ledger["run_attempt"] != receipt["run_attempt"]):
                raise ValueError("Canary/collection evidence has the wrong source/run")
            raw = normalized.with_suffix(".raw.xml")
            census = normalized.with_suffix(".collection.json")
            if ledger["raw_sha256"] != sha(raw) or ledger["collection_sha256"] != sha(census):
                raise ValueError("Raw sentinel/collection evidence changed")
            raw_cases = list(ET.parse(raw).getroot().iter("testcase"))
            selected = verify_cases(raw_cases, ledger["sentinels"],
                                    json.loads(census.read_text(encoding="utf-8")), allow_empty=partial)
            normal_cases = list(ET.parse(normalized).getroot().iter("testcase"))
            if not allow_product_errors and any(outcome(c) == "error" for c in normal_cases):
                raise ValueError("Product collection/setup errors are refused")
            if sorted(node(c) for c in normal_cases) != sorted(selected):
                raise ValueError("Normalized evidence dropped or added product nodes")
            raw_products = sorted((node(c), outcome(c)) for c in raw_cases
                                  if not any(t in node(c) for t in ledger["sentinels"].values()))
            if sorted((node(c), outcome(c)) for c in normal_cases) != raw_products:
                raise ValueError("Normalization changed a product outcome")
