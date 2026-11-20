"""Reviewed skip class, bound to the original two complete hosted root executions."""
import json
import re


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate known-skip field")
        result[key] = value
    return result


def decode_list(raw, root_evidence, valid_node):
    data = json.loads(raw, object_pairs_hook=_object)
    if (not isinstance(data, dict) or set(data) != {"schema", "root_sha", "root_tree", "root_runs", "skips"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or data["root_sha"] != root_evidence["sha"] or data["root_tree"] != root_evidence["tree"]
            or json.dumps(data["root_runs"], sort_keys=True) != json.dumps(root_evidence["runs"], sort_keys=True)
            or not isinstance(data["skips"], list)):
        raise ValueError("Known-skips list differs from the original root evidence")
    nodes = []
    for row in data["skips"]:
        if (not isinstance(row, dict) or set(row) != {"node", "observed_in", "reason", "review"}
                or not isinstance(row["node"], str) or not valid_node(row["node"])
                or not isinstance(row["reason"], str) or not row["reason"].strip()
                or not isinstance(row["review"], str) or not re.fullmatch(r"(?:https://\S+|sha256:[a-f0-9]{64})", row["review"])
                or not isinstance(row["observed_in"], list) or not row["observed_in"]
                or any(type(value) is not int or value not in (1, 2) for value in row["observed_in"])
                or row["observed_in"] != sorted(set(row["observed_in"]))):
            raise ValueError("Known skip requires an anonymous node, original observations and a reviewed reason")
        nodes.append(row["node"])
    if nodes != sorted(set(nodes)):
        raise ValueError("Known-skip nodes must be distinct and sorted")
    return data


def require_measurements(evidence):
    measurements = evidence.get("measurements")
    if not isinstance(measurements, list) or len(measurements) != 2 or len(evidence["runs"]) != 2:
        raise ValueError("Root evidence requires two per-run measurement/digest bindings")
    if (len({r["run_id"] for r in evidence["runs"]}) != 1
            or any(r["run_attempt"] != "1" for r in evidence["runs"])
            or [r["replica"] for r in evidence["runs"]] != [1,2]):
        raise ValueError("Root evidence requires one run, attempt 1, replicas [1,2]")
    for measured, run in zip(measurements, evidence["runs"]):
        if (not isinstance(measured, dict) or set(measured) != {"run_id", "run_attempt", "replica", "sha", "tree", "receipts_sha256", "trusted_sha", "measurement_receipt_sha256"}
                or type(measured["replica"]) is not int or measured["replica"] != run["replica"]
                or measured["run_id"] != run["run_id"] or measured["run_attempt"] != run["run_attempt"]
                or measured["sha"] != evidence["sha"] or measured["tree"] != evidence["tree"]
                or not all(isinstance(measured[k], str) and re.fullmatch(r"[a-f0-9]{64}", measured[k])
                           for k in ("receipts_sha256", "measurement_receipt_sha256"))
                or not isinstance(measured["trusted_sha"], str) or not re.fullmatch(r"[a-f0-9]{40}", measured["trusted_sha"])):
            raise ValueError("Root measurement is not bound to its run, source and receipt digest")


def build_list(runs, root_evidence, reviews, valid_node):
    if len(runs) != 2 or not all(runs) or set(runs[0]) != set(runs[1]) or len(root_evidence["runs"]) != 2:
        raise ValueError("Known skips require the original two complete root executions")
    observed = sorted({node for run in runs for node, outcome in run.items() if outcome == "skipped"})
    if set(reviews) != set(observed):
        raise ValueError("Every observed skip needs review; do not add unobserved nodes")
    rows = [{"node": node, "observed_in": [i for i, run in enumerate(runs, 1) if run[node] == "skipped"],
             "reason": reviews[node]["reason"], "review": reviews[node]["review"]} for node in observed]
    data = {"schema": 1, "root_sha": root_evidence["sha"], "root_tree": root_evidence["tree"],
            "root_runs": root_evidence["runs"], "skips": rows}
    return decode_list(json.dumps(data), root_evidence, valid_node)


def comparison_runs(runs, allowed, *, release, allowed_errors=frozenset(), quarantined=frozenset()):
    if not runs or any(not run for run in runs):
        raise ValueError("Complete selected execution is missing")
    result, observed = [], set()
    for run in runs:
        compared = {}
        for node, outcome in run.items():
            if outcome not in {"passed", "failed", "error", "skipped"}:
                raise ValueError("Unknown execution outcome")
            if outcome in {"failed", "error"} and (release or node not in quarantined and (
                    node in allowed or outcome == "error" and node not in allowed_errors)):
                raise ValueError("Release/known-skip execution contains a failure or error")
            if outcome == "skipped":
                if node not in allowed:
                    raise ValueError("New skip outside the reviewed known-skips list")
                observed.add(node)
            # Compare failure classes separately. Raw JUnit/outcomes retain SKIP, never invented PASS.
            if node not in allowed:
                compared[node] = outcome
        result.append(compared)
    return result, observed
