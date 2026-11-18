"""Verify raw sentinel outcomes and collected nodes before reading normalized product JUnit."""
import hashlib
import json
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


def verify_cases(cases, manifest, collected):
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
    if not expected or len(expected) != len(set(expected)) or sorted(product) != sorted(expected):
        raise ValueError("Reported node set differs from trusted collection of the same candidate")
    return expected


def verify_directory(directory, *, allow_product_errors=False):
    receipts = list(directory.rglob("shard-receipt.json"))
    if not receipts:
        raise ValueError("Missing shard receipts")
    for receipt_path in receipts:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for report in receipt["reports"]:
            normalized = receipt_path.parent / report["file"]
            ledger_path = normalized.with_suffix(".canaries.json")
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if (ledger["sha"] != receipt["sha"] or ledger["tree"] != receipt["tree"]
                    or ledger["run_id"] != receipt["run_id"] or ledger["run_attempt"] != receipt["run_attempt"]):
                raise ValueError("Canary/collection evidence has the wrong source/run")
            raw = normalized.with_suffix(".raw.xml")
            census = normalized.with_suffix(".collection.json")
            if ledger["raw_sha256"] != sha(raw) or ledger["collection_sha256"] != sha(census):
                raise ValueError("Raw sentinel/collection evidence changed")
            selected = verify_cases(list(ET.parse(raw).getroot().iter("testcase")), ledger["sentinels"],
                                    json.loads(census.read_text(encoding="utf-8")))
            normal_cases = list(ET.parse(normalized).getroot().iter("testcase"))
            if not allow_product_errors and any(outcome(c) == "error" for c in normal_cases):
                raise ValueError("Product collection/setup errors are refused")
            if sorted(node(c) for c in normal_cases) != sorted(selected):
                raise ValueError("Normalized evidence dropped or added product nodes")
            raw_products = sorted((node(c), outcome(c)) for c in ET.parse(raw).getroot().iter("testcase")
                                  if not any(t in node(c) for t in ledger["sentinels"].values()))
            if sorted((node(c), outcome(c)) for c in normal_cases) != raw_products:
                raise ValueError("Normalization changed a product outcome")
