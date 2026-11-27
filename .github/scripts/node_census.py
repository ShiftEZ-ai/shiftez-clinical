"""Compare target-base and candidate collection; no unlisted in-file disappearance."""
import json
import hashlib
import os
from pathlib import Path
from latest_reports import earlier_reports, planned_shards, producing_attempt


def ledger_additions(before_raw,after_raw):
    """Preliminary structural exception; final census must prove every new node disappeared."""
    if before_raw is None or after_raw is None:return None
    def unique(pairs):
        data={}
        for key,value in pairs:
            if key in data:raise ValueError('Duplicate node-ledger field')
            data[key]=value
        return data
    try:
        before=json.loads(before_raw,object_pairs_hook=unique)
        after=json.loads(after_raw,object_pairs_hook=unique)
        if (set(before)!=set(after) or set(after)!={'schema','removed','removed_nodes'}
                or before['schema']!=2 or after['schema']!=2 or before['removed']!=after['removed']):return None
        # Existing node metadata cannot change or be deleted.
        removed_nodes(json.dumps(before),lambda n:bool(n))
        removed_nodes(json.dumps(after),lambda n:bool(n))
        old={row['node']:row for row in before['removed_nodes']}
        new={row['node']:row for row in after['removed_nodes']}
        if any(new.get(n)!=row for n,row in old.items()):return None
        added=set(new)-set(old)
        return added if added else None
    except (ValueError,KeyError,TypeError):return None


def allowed_removals(before_raw,after_raw,before,after,valid_node):
    accepted=removed_nodes(before_raw,valid_node)
    if after_raw==before_raw:return accepted
    added=ledger_additions(before_raw,after_raw)
    if added is None or not added<=set(before)-set(after):
        raise ValueError('Candidate removal ledger may only add nodes this PR actually removes')
    # Validate exact anonymous identities as well as the structural guard exception.
    return removed_nodes(after_raw,valid_node)


def removed_nodes(raw, valid_node):
    data=json.loads(raw)
    if data.get("schema")==1 and set(data)=={"schema","removed"}:
        return set()
    if data.get("schema")!=2 or set(data)!={"schema","removed","removed_nodes"} or not isinstance(data["removed_nodes"],list):
        raise ValueError("Node-level removed-tests ledger is invalid")
    nodes=[]
    for row in data["removed_nodes"]:
        if (set(row)!={"node","owner","reason"} or not isinstance(row["node"],str) or not valid_node(row["node"])
                or any(not isinstance(row[k],str) or not row[k].strip() for k in ("owner","reason"))):
            raise ValueError("Removed node needs exact identity, owner and reason")
        nodes.append(row["node"])
    if nodes!=sorted(set(nodes)):
        raise ValueError("Removed node entries must be sorted and distinct")
    return set(nodes)


def compare(before,after,allowed):
    if not before or not after:
        raise ValueError("Complete base/head node census is missing")
    added,removed=set(after)-set(before),set(before)-set(after)
    if removed-set(allowed):
        raise ValueError("Unlisted disappeared nodes: "+", ".join(sorted(removed-set(allowed))))
    return {"added":sorted(added),"removed":sorted(removed),"tier2":sorted(removed),
            "summary":"collect=+"+str(len(added))+"/-"+str(len(removed)),
            "label_request":"changes what the checks run on" if removed else None}


def base_nodes(plan,reports,base_sha,run_id,attempt,canonical,expected_files,*,receipts=()):
    expected=planned_shards(plan)
    seen=set();runs={r:set() for r in plan["replicas"]};files={r:[] for r in plan["replicas"]}
    census = (path.with_name("base-census.json") for path in receipts) if receipts else reports.rglob("base-census.json")
    for path in census:
        root = reports if path.is_relative_to(reports) else earlier_reports(reports)
        data=json.loads(path.read_bytes())
        receipt=json.loads((path.parent/"shard-receipt.json").read_bytes())
        if receipt.get("base_census_sha256")!=hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Base node census digest differs from the shard receipt")
        identity=(data["suite"],data["shard"],data["replica"])
        if (identity not in expected or identity in seen or data["base_sha"]!=base_sha
                or data["head_sha"]!=plan["sha"] or data["run_id"]!=run_id
                or data["run_attempt"]!=producing_attempt(root,path,identity,attempt)
                or not isinstance(data["nodes"],list) or len(data["nodes"])!=len(set(data["nodes"]))):
            raise ValueError("Base node census binding/completeness differs")
        seen.add(identity)
        if any(p not in expected_files or p.rsplit('/',1)[-1].startswith('test_browser')!=(data["suite"]=="browser") for p in data["files"]):
            raise ValueError("Base census file universe/suite differs")
        files[data["replica"]].extend(data["files"])
        if any(n.split('::')[0] not in data["files"] for n in data["nodes"]):
            raise ValueError("Base node lies outside its trusted file selection")
        nodes={canonical(n) for n in data["nodes"]}
        if runs[data["replica"]]&nodes:
            raise ValueError("Base node occurs in multiple shards")
        runs[data["replica"]].update(nodes)
    if (seen!=expected or len({tuple(sorted(nodes)) for nodes in runs.values()})!=1
            or any(sorted(values)!=sorted(expected_files) for values in files.values())):
        raise ValueError("Missing or inconsistent full base collection")
    return runs[plan["replicas"][0]]


def emit(result,output):
    output.mkdir(parents=True,exist_ok=True)
    (output/"node-census.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(result["summary"])
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a",encoding="utf-8") as stream:
            stream.write(result["summary"]+"\n")
            for kind in ("added","removed"):
                for node in result[kind]:stream.write("- "+kind+": "+json.dumps(node)+"\n")
