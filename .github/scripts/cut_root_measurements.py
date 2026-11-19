"""Import the audited PR1 hosted run, attempt 1, complete replicas [1,2]."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import os
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import known_skips
import trusted_ci
import working_policy as policy
import failure_classifications


def validate_capture(capture, root):
    run = capture["run"]
    if (capture.get("schema") != 1 or run["repository_id"] != policy.REPOSITORY
            or type(run["id"]) is not int or run["id"] < 1 or type(run["run_attempt"]) is not int
            or run["run_attempt"] != 1 or run["head_sha"] != root["sha"]
            or run["event"] != "pull_request"
            or run.get("pull_requests") != [{"head_sha":root["sha"],"base_sha":root["base_sha"]}]
            or run["status"] != "completed" or not run["path"].startswith(".github/workflows/ci.yml")):
        raise ValueError("Only the completed audited PR1 pull_request attempt 1 is a measurement")
    if capture.get("run_readback_after_download") != run:
        raise ValueError("Run identity changed during artifact download")
    artifacts = capture.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Root artifact download is missing")
    ids = [a["id"] for a in artifacts]
    if len(ids) != len(set(ids)) or any(type(i) is not int or i < 1 for i in ids):
        raise ValueError("Duplicate/invalid root artifact")
    return str(run["id"])


def unpack(capture, folder):
    total = 0
    for artifact in capture["artifacts"]:
        archive = Path(artifact["path"])
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != artifact["sha256"] or artifact["github_digest"] != "sha256:" + actual:
            raise ValueError("Root artifact digest differs from GitHub/capture")
        destination = folder / str(artifact["id"])
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                name = PurePosixPath(entry.filename)
                total += entry.file_size
                if (name.is_absolute() or ".." in name.parts or chr(92) in entry.filename or ":" in entry.filename
                        or stat.S_ISLNK(entry.external_attr >> 16) or total > 2_000_000_000):
                    raise ValueError("Root archive path/type/size refused")
                target = destination.joinpath(*name.parts)
                if not target.resolve().is_relative_to(destination.resolve()) or target.exists():
                    raise ValueError("Duplicate/escaping root archive entry")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(package.read(entry))


def load_plan(capture, reports):
    name = 'ci-plan-' + str(capture['run']['run_attempt'])
    artifacts = [row for row in capture['artifacts'] if row.get('name') == name]
    if len(artifacts) != 1:
        raise ValueError('Exactly one named original plan artifact is required: ' + name)
    primary = list((reports / str(artifacts[0]['id'])).rglob('shard-plan.json'))
    if len(primary) != 1:
        raise ValueError('Named plan artifact must contain exactly one shard-plan.json')
    raw = primary[0].read_bytes()
    expected = hashlib.sha256(raw).hexdigest()
    for path in reports.rglob('shard-plan.json'):
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Shard plan copy differs from the named plan artifact')
    for receipt in reports.rglob('shard-receipt.json'):
        if not (receipt.parent / 'shard-plan.json').is_file():
            raise ValueError('Original shard artifact is missing its plan copy')
    return policy.decode(raw)


def require_dispatch_expiry(capture, config):
    try:
        dispatched=datetime.fromisoformat(capture['run']['created_at'].replace('Z','+00:00'))
        if dispatched.utcoffset() is None:raise ValueError('Naive dispatch timestamp')
        expected=(dispatched.astimezone(timezone.utc).date()+timedelta(days=30)).isoformat()
    except (KeyError,TypeError,ValueError,AttributeError):
        raise ValueError('Original dispatch timestamp is required to bind every unstable expiry') from None
    if dispatched.utcoffset() is None or config['unstable_expires_on']!=expected:
        raise ValueError('Initial unstable expiry must equal original dispatch UTC day plus 30 days')


def import_runs(capture, plan, reports, root, maker):
    run_id = validate_capture(capture, root)
    if (plan["sha"] != root["sha"] or plan["tree"] != root["tree"] or plan["base_sha"] != root["base_sha"]
            or plan["replicas"] != [1, 2] or plan["smoke"] is not False):
        raise ValueError("PR1 plan requires complete replicas [1,2] of the audited head and root base")
    expected={(suite,index,replica) for suite,rows in plan['suites'].items()
              for index in range(1,len(rows['shards'])+1) for replica in (1,2)}
    timed={(row['suite'],row['shard'],row['replica']) for row in capture.get('performance',[]) if row['suite'] is not None}
    if timed!=expected or any(row['queue_seconds'] is None or row['runtime_seconds'] is None for row in capture.get('performance',[])):
        raise ValueError('Original root job/shard runtime and queue-wait evidence is incomplete')
    checks = [row for row in capture.get('performance',[]) if row.get('name') == 'Checks']
    if len(checks) != 1 or checks[0].get('status') != 'completed' or checks[0].get('conclusion') != 'success':
        raise ValueError('PR1 Checks must be completed and green')
    if any(row.get('status') != 'completed' or row.get('conclusion') not in {'success','failure'}
           for row in capture.get('performance',[]) if row.get('suite') is not None):
        raise ValueError('Every PR1 shard must complete without infrastructure cancellation/error')
    # Historical archives were unpacked by artifact ID rather than by name.
    runs = maker.collect_reports(plan, reports, run_id, "1", historical_capture=True)
    if len(runs) != 2 or not runs[0] or set(runs[0]) != set(runs[1]):
        raise ValueError("Both replicas must execute the same complete node set")
    measurements = []
    for replica in (1,2):
        seals = [hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(reports.rglob("shard-receipt.json"))
                 if policy.decode(p.read_bytes())["replica"] == replica]
        measurements.append({"run_id":run_id,"run_attempt":"1","replica":replica,"sha":root["sha"],
            "tree":root["tree"],"trusted_sha":root["sha"],"receipts_sha256":maker.digest(sorted(seals)),
            "measurement_receipt_sha256":maker.digest({"run_id":run_id,"attempt":1,"replica":replica,"receipts":sorted(seals)})})
    return runs, measurements


def build_cut(runs, measurements, root, config, maker, baseline, baseline_sha, exceptions, reviews, classifications,performance):
    evidence = {"schema":1,"sha":root["sha"],"tree":root["tree"],"base_sha":root["base_sha"],"baseline_sha256":baseline_sha,
        "runs":[{k:m[k] for k in ("run_id","run_attempt","replica")} for m in measurements],
        "measurements":measurements,"receipts_sha256":maker.digest([m["receipts_sha256"] for m in measurements]),"counts":{},'performance':performance}
    known_skips.require_measurements(evidence)
    skips = known_skips.build_list(runs,evidence,reviews,maker.SAFE_NODE.fullmatch)
    errors = {node for run in runs for node,value in run.items() if value == "error"}
    compared,_ = known_skips.comparison_runs(runs,{r["node"] for r in skips["skips"]},release=False,allowed_errors=errors)
    failures = maker.candidate_list(compared,baseline,root["tree"],config["unstable_issue"],config["unstable_expires_on"],
        config["unstable_limit"],cleanup_cap=len(compared[0]),root_evidence_sha256="0"*64,
        root_runs=evidence["runs"],cleanup_issue=config["cleanup_issue"],exceptions=exceptions)
    failures=failure_classifications.attach(failures,classifications,maker.SAFE_NODE.fullmatch,config["cleanup_issue"])
    evidence["counts"]={kind:len(nodes) for kind,nodes in maker.classes(failures).items()}
    raw=maker.encoded(evidence).encode()
    failures["cleanup_cap"]=evidence["counts"]["cleanup"]
    failures["root_evidence_sha256"]=hashlib.sha256(raw).hexdigest()
    maker.decode_list(maker.encoded(failures))
    return raw,failures,skips


def validate_source(root):
    if (policy.git("rev-parse", root["sha"] + "^{tree}").decode().strip() != root["tree"]
            or policy.git("merge-base", root["base_sha"], root["sha"]).decode().strip() != root["base_sha"]):
        raise ValueError("Audited PR1 tree/root-ancestor identity differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr1-head",required=True)
    parser.add_argument("--pr1-tree",required=True)
    parser.add_argument("--capture",type=Path,required=True)
    parser.add_argument("--skip-reviews",type=Path,required=True)
    parser.add_argument("--classifications",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    approved,config = trusted_ci.pinned_root(),trusted_ci.settings()
    root = {"sha":policy.commit(args.pr1_head),"tree":policy.commit(args.pr1_tree),"base_sha":approved["sha"]}
    capture = policy.decode(args.capture.read_bytes())
    validate_capture(capture,root)
    require_dispatch_expiry(capture,config)
    validate_source(root)
    sys.path.insert(0,str(trusted_ci.TRUSTED / "ci"))
    import make_known_failures as maker
    from trusted_verdict import assert_inventory
    baseline,baseline_sha = maker.load_frozen_baseline(trusted_ci.TRUSTED / "ci/frozen-baseline-failures.json")
    exceptions,_ = maker.load_env_exceptions(trusted_ci.TRUSTED / "ci/hosted-env-exceptions.json")
    with tempfile.TemporaryDirectory(prefix="root-artifacts-",dir=os.environ["RUNNER_TEMP"]) as scratch:
        reports = Path(scratch)
        unpack(capture,reports)
        plan = load_plan(capture,reports)
        assert_inventory(plan,policy.git("ls-tree","-r","-z",root["sha"],"--","tests"),
            {"unit":config["unit_shards"],"browser":config["browser_shards"]})
        runs,measurements = import_runs(capture,plan,reports,root,maker)
        baseline={node for run in runs for node,outcome in run.items() if outcome in {'failed','error'}}
        baseline_bytes=maker.encoded(sorted(baseline)).encode()
        baseline_sha=hashlib.sha256(baseline_bytes).hexdigest()
        raw,failures,skips = build_cut(runs,measurements,root,config,maker,baseline,baseline_sha,exceptions,
                                     policy.decode(args.skip_reviews.read_bytes()),args.classifications.read_bytes(),capture['performance'])
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output / "root-run-evidence.json").write_bytes(raw)
    (args.output / "frozen-baseline-failures.json").write_bytes(baseline_bytes)
    for name,data in (("known-failures.json",failures),("known-skips.json",skips)):
        (args.output / name).write_text(maker.encoded(data),encoding="utf-8")
    print("ROOT_CUT_PREPARED run="+str(capture["run"]["id"])+" attempt=1 replicas=1,2")


if __name__ == "__main__":
    main()
