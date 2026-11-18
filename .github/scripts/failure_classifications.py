"""Reviewed class/owner/reason metadata for the observed root failures; no invented nodes."""
import hashlib
import json

KINDS={"baseline","cleanup","unstable","baseline-error"}


def load(raw, valid_node):
    data=json.loads(raw)
    if (not isinstance(data,dict) or set(data)!={"schema","status","entries"}
            or data["schema"]!=1 or data["status"]!="REVIEWED" or not isinstance(data["entries"],list)):
        raise ValueError("Reviewed failure classifications are missing/TODO")
    result={}
    for row in data["entries"]:
        if (set(row)!={"node","class","owner","reason"} or not isinstance(row["node"],str)
                or not valid_node(row["node"]) or row["node"] in result or row["class"] not in KINDS
                or any(not isinstance(row[k],str) or not row[k].strip() or "TODO" in row[k] for k in ("owner","reason"))):
            raise ValueError("Every failure needs a unique node, class, owner and reviewed reason")
        result[row["node"]]=row
    return result,hashlib.sha256(raw if isinstance(raw,bytes) else raw.encode()).hexdigest()


def attach(listing, raw, valid_node, cleanup_issue):
    reviewed,digest=load(raw,valid_node)
    result={**listing,"schema":3,"classifications_sha256":digest,"fails":[],"unstable":[]}
    for section in ("fails","unstable"):
        for row in listing[section]:
            meta=reviewed.get(row["node"])
            if (meta is None or (row["class"] in {"unstable","baseline-error"} and meta["class"]!=row["class"])
                or (row["class"] not in {"unstable","baseline-error"} and meta["class"] not in {"baseline","cleanup"})):
                raise ValueError("An observed root failure lacks matching reviewed classification")
            entry={**meta}
            if meta["class"]=="cleanup":
                entry["issue"]=cleanup_issue
            elif section=="unstable":
                entry.update(issue=row["issue"],expires_on=row["expires_on"])
            result[section].append(entry)
    return result


def validate(listing, raw, valid_node):
    reviewed,digest=load(raw,valid_node)
    if listing["classifications_sha256"]!=digest:
        raise ValueError("Failure metadata provenance changed")
    for row in listing["fails"]+listing["unstable"]:
        if reviewed.get(row["node"])!={k:row[k] for k in ("node","class","owner","reason")}:
            raise ValueError("Failure class/owner/reason differs from the reviewed root policy")
