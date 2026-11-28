#!/usr/bin/env python3
"""Rules in force move into the rule catalogue, through the normal governance.

    python scripts/catalogue_rules.py --version 2 --as <coordinator> --json     # strict dry run; one department proposal
    python scripts/catalogue_rules.py --version 2 --as <coordinator> --propose --reason "..."
    python scripts/catalogue_rules.py --version 2 --as <approver> --activate --conversion <id> --reason "..."
    python scripts/catalogue_rules.py --as <coordinator>                       # dry run: the plan, the report and the proof
    python scripts/catalogue_rules.py --as <coordinator> --solve               #... and solve every rota both ways (CP-SAT, greedy)
    python scripts/catalogue_rules.py --as <coordinator> --propose --reason "..." [--only duties_per_week...]
    python scripts/catalogue_rules.py --as <approver> --activate --reason "..." [--acknowledge-unresolved]
    python scripts/catalogue_rules.py --as <approver> --undo --reason "..." [--only-reason "<proposal reason>"]
    python scripts/catalogue_rules.py --baseline [--propose] --reason "..."    # empty department only (as group_rules.py)
    python scripts/catalogue_rules.py... --json                                # the full plan, report and proof as JSON

Run it with the environment's own settings (inside its API container: docker exec <api> python scripts/catalogue_rules.py …).
Version 2 uses strict source/findings/quota/fairness/score/full-objective model proof and one atomic independently
reviewed department envelope. All examples below describe version 1.
Each catalogue entry becomes one proposed catalogue rule whose lines are the rules (and their lines) it takes over. A
rule that would not read exactly the same as lines is kept as it is and listed with the reason ("Not in the catalogue
yet"). The proof — validator findings, quota rows, the scheduler's inputs and the CP-SAT model of every rota being
planned (and with --solve the same assignments from a deterministic CP-SAT solve and from greedy generation) — must
show no difference before anything is proposed or activated. The schedule score may move (it reads how many rules
there are): shown as was -> now. Activating retires the rules each catalogue rule takes over; retiring it (--undo)
brings them back.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select  # noqa: E402
from apps.api import models as m  # noqa: E402
from apps.api.auth import Actor  # noqa: E402
from apps.api.db import allow_long_idle, make_engine, transaction  # noqa: E402
from apps.api.serialization import record  # noqa: E402
from apps.api.settings import secret_from_env  # noqa: E402
from scripts.group_rules import BASELINE, DryRun, active_count, actor_for, baseline_refusals, retirement_pending, run_as, system_identity  # noqa: E402


def plan_lines(plan):
    lines = [f"{plan['active']} rules in force: {plan['moving']} move into {len(plan['proposals'])} catalogue rules, {plan['kept']} stay as they are"]
    for x in plan["proposals"]:
        lines.append(f"  {x['catalogue_id']:26} {len(x['rule_ids']):3} rules -> {x['lines']} lines")
    if plan["report"]:
        lines.append("Not in the catalogue yet (kept as they are):")
        lines += [f"  {r['name'][:60]:60} {r['kind']:24} {r['reason'].removeprefix('Not in the catalogue yet: ')}" for r in plan["report"]]
    return "\n".join(lines)


SAME = {True: "same", False: "DIFFERENT", None: "not compared (stopped at its time limit)"}


def proof_lines(proof):
    out = [f"Proof over {len(proof['rotas'])} rotas being planned: " + ("IDENTICAL" if proof["identical"] else "EQUIVALENCE NOT ESTABLISHED")
           + f" (rules the checks read {'identical' if proof['solver_inputs_equal'] else 'DIFFERENT'}); rules {proof['rules_before']} -> {proof['rules_after']}"]
    for r in proof["rotas"]:
        same = not r["findings_only_before"] and not r["findings_only_after"] and r["quota_equal"] and r["solver_inputs_equal"]
        out.append(f"  {r['name']} ({r['status']}): findings {r['findings']} -> {r['findings_after']}, {r['quota_rows']} quota rows, "
                   f"score {r['score_before']} -> {r['score_after']}"
                   + (f", CP-SAT model {'identical' if r['model_equal'] else 'not confirmed identical'}" if "model_equal" in r else "")
                   + (f", CP-SAT solve ({r['cp_sat_status']}" + (f"; fill what can be filled: {r['fill_status']}" if r.get("fill_status") else "")
                      + f") {r['cp_sat_assignments']} assignments {'same' if r['cp_sat_equal'] else 'DIFFERENT'}"
                      + f", greedy ({r['greedy_status']}) {r['greedy_assignments']} assignments {SAME[r['greedy_equal']]}"
                      + f"; from empty: CP-SAT ({r['refill_status']}) {r['refill_assignments']} assignments {SAME[r['refill_equal']]}"
                      + f", greedy ({r['refill_greedy_status']}) {r['refill_greedy_assignments']} assignments {SAME[r['refill_greedy_equal']]}"
                      if "cp_sat_equal" in r else "")
                   + (", same" if same else f", DIFFERENT: +{len(r['findings_only_after'])} / -{len(r['findings_only_before'])} findings"
                      + ("" if r["quota_equal"] else ", quota rows differ") + ("" if r["solver_inputs_equal"] else ", scheduler inputs differ")))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=secret_from_env("DATABASE_URL", "sqlite:///./var/rota.db"))
    parser.add_argument("--as", dest="who", help="Identity id, subject or display name to act as")
    parser.add_argument("--department", help="Department id, when the identity works in several")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--propose", action="store_true", help="Propose the catalogue rules (nothing applies until approved)")
    action.add_argument("--activate", action="store_true", help="Approver: activate the proposed catalogue rules")
    action.add_argument("--undo", action="store_true", help="Approver: retire the catalogue rules (the rules they took over apply again)")
    action.add_argument("--decline", action="store_true", help="V2: decline the department conversion proposal")
    parser.add_argument("--version", type=int, choices=[1, 2], default=1, help="2: one reviewed department conversion with strict equivalence")
    parser.add_argument("--conversion", help="V2 conversion id to activate, decline or undo")
    parser.add_argument("--idempotency-key", default=None, help="V2 command retry key (16–128 ASCII characters)")
    parser.add_argument("--baseline", action="store_true", help="Empty department only: propose and activate as the system baseline actors")
    parser.add_argument("--only", nargs="*", default=[], help="Only these catalogue entries (ids)")
    parser.add_argument("--reason", default="")
    parser.add_argument("--only-reason", help="--undo: only catalogue rules proposed with this reason")
    parser.add_argument("--solve", action="store_true", help="Also solve every rota both ways (deterministic CP-SAT, greedy)")
    parser.add_argument("--no-model", action="store_true", help="Skip the CP-SAT model comparison")
    parser.add_argument("--acknowledge-unresolved", action="store_true", help="Activate even if a rota check is unresolved (reviewed)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if (args.propose or args.activate or args.undo or args.decline) and len(args.reason.strip()) < 3:
        parser.error("--reason is required to change anything")
    if args.version == 2 and (args.baseline or args.only or args.no_model or args.acknowledge_unresolved or args.only_reason):
        parser.error("V2 converts the complete department and requires full proof; baseline, partial conversion and proof overrides are unsupported")
    if args.version == 1 and (args.decline or args.conversion or args.idempotency_key):
        parser.error("--decline, --conversion and --idempotency-key require --version 2")
    if not args.baseline and not args.who:
        parser.error("--as is required (or --baseline for an empty department)")
    engine = make_engine(args.database_url)
    try:
        if args.version == 2:
            with transaction(engine, write=bool(args.propose or args.activate or args.undo or args.decline)) as db:
                allow_long_idle(db, 30)
                return v2(db, actor_for(db, args.who, args.department), args)
        if args.baseline:
            return baseline(engine, args)
        if args.activate:
            return activate(engine, args)
        with transaction(engine, write=bool(args.propose or args.undo)) as db:
            allow_long_idle(db, 30)
            actor = actor_for(db, args.who, args.department)
            if args.undo:
                return undo(db, actor, args)
            return preview(db, actor, args)
    finally:
        engine.dispose()


def _v2_command(db, actor, raw, key=None):
    """The operator CLI uses the same command, receipt and scoped audit transaction."""
    from apps.api import live
    from apps.api.auth import require_role
    from apps.api.commands import authorize_receipt, command_roles, dispatch
    from apps.api.errors import require
    from apps.api.schema import require_current_schema
    from apps.api.serialization import json_value
    from apps.api.settings import Settings
    from apps.api.snapshots import snapshot_hash
    from packages.contracts.schemas import Command
    require_current_schema(db)
    command = Command.model_validate(raw)
    allowed = command_roles(command)
    require_role(actor, *allowed)
    key = key or 'catalogue-v2-' + uuid.uuid4().hex
    require(16 <= len(key) <= 128 and key.isascii() and key.isprintable(), 'IDEMPOTENCY_KEY_REQUIRED', 'Use a 16–128 character command key', 428)
    canonical = command.model_dump(mode='json')
    if command.expected_fingerprint is None:
        canonical.pop('expected_fingerprint')
    payload_hash = snapshot_hash(canonical)
    existing = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.actor_id == actor.identity_id, m.CommandReceipt.key == key))
    if existing:
        require(existing.department_id == actor.department_id and existing.payload_hash == payload_hash,
                'IDEMPOTENCY_CONFLICT', 'This key was used for another command', 409)
        authorize_receipt(db, actor, existing)
        return existing.result
    result = json_value(dispatch(db, actor, command, Settings.from_env()))
    result['receipt_id'] = m.uid()
    db.add(m.CommandReceipt(id=result['receipt_id'], department_id=actor.department_id, actor_id=actor.identity_id,
                            command_kind=command.kind, required_roles=allowed, key=key, payload_hash=payload_hash, result=result))
    db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id, action=command.kind,
                       target_id=command.target_id or result.get('record', {}).get('id'), reason=command.payload['reason'],
                       detail={'receipt_id': result['receipt_id'], 'payload_fields': sorted(command.payload)}, correlation_id=key))
    live.signal(db, actor.department_id)
    db.flush()
    return result


def v2(db, actor, args):
    from apps.api.services import rule_catalogue_v2
    action = 'undo' if args.undo else 'decline' if args.decline else 'activate' if args.activate else 'propose' if args.propose else None
    if action and args.idempotency_key:
        from apps.api.commands import authorize_receipt
        from apps.api.errors import require
        existing = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.actor_id == actor.identity_id,
                                                            m.CommandReceipt.key == args.idempotency_key))
        if existing:
            kind = 'rule.migrate_to_catalogue' if action == 'propose' else 'rule.catalogue.' + action
            audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.actor_id == actor.identity_id,
                                                        m.AuditEvent.correlation_id == args.idempotency_key))
            require(existing.department_id == actor.department_id and existing.command_kind == kind and audit
                    and audit.reason == args.reason and (not args.conversion or audit.target_id == args.conversion),
                    'IDEMPOTENCY_CONFLICT', 'This key was used for another conversion command', 409)
            authorize_receipt(db, actor, existing)
            print(json.dumps(existing.result, default=str, indent=1) if args.json else existing.result.get('message', 'Previous command receipt returned.'))
            return existing.result
    if args.activate or args.undo or args.decline:
        state = 'active' if args.undo else 'proposed'
        rows = list(db.scalars(select(m.CatalogueConversion).where(m.CatalogueConversion.department_id == actor.department_id,
                                                                 m.CatalogueConversion.status == state)))
        rows = [r for r in rows if not args.conversion or r.id == args.conversion]
        if len(rows) != 1:
            raise SystemExit(f'Expected one {state} department conversion; found {len(rows)}. Use --conversion with its id.')
        [row] = rows
        result = _v2_command(db, actor, {'kind': 'rule.catalogue.' + action, 'target_id': row.id,
                                       'expected_revision': row.revision, 'payload': {'reason': args.reason}}, args.idempotency_key)
        print(json.dumps(result, default=str, indent=1) if args.json else result.get('message', f'Department conversion {action}: {row.id}'))
        return result
    plan = rule_catalogue_v2.plan(db, actor, proof=True)
    if args.solve and plan.get('proof') and plan['proof'].get('rotas'):
        from apps.api.snapshots import domain_snapshot
        from packages.domain.rule_catalogue_migration import after_snapshot
        from packages.domain.rule_migration_proof import strict_proof
        plan['proof']['rotas'] = [{**r, **strict_proof(before, after_snapshot(before, plan), solve=True)}
                                 for r in plan['proof']['rotas']
                                 for before in [domain_snapshot(db, db.get(m.Workspace, r['workspace_id']))]]
        plan['proof']['identical'] = all(r['identical'] for r in plan['proof']['rotas'])
    if args.json:
        print(json.dumps(plan, default=str, indent=1))
    else:
        print(f"Catalogue v2: {len(plan.get('targets', []))} rule types; {len(plan.get('scheduling_facts', []))} person/calendar facts; "
              f"{len(plan.get('bindings', []))} preserved sources. Plan {plan['plan_hash']}")
        print('STRICT EQUIVALENCE ' + ('ESTABLISHED' if (plan.get('proof') or {}).get('identical') else 'NOT ESTABLISHED'))
        for blocked in plan.get('blocked', []):
            print('Blocked: ' + json.dumps(blocked, default=str))
        for r in (plan.get('proof') or {}).get('rotas', []):
            model = r['model']
            print(f"  Rota {r['workspace_id']}: {r['findings']['count_before']} complete findings; "
                  f"model {'identical' if model['equal'] else 'not equivalent'}; "
                  f"{len(model.get('before', {}).get('tiers', []))} complete objective tiers")
    if args.propose:
        if plan.get('blocked') or not (plan.get('proof') or {}).get('identical'):
            raise SystemExit(2)
        result = _v2_command(db, actor, {'kind': 'rule.migrate_to_catalogue',
                                       'payload': {'version': 2, 'reason': args.reason, 'plan_hash': plan['plan_hash']}}, args.idempotency_key)
        print(result['message'])
        return plan, plan['proof'], result
    return plan, plan.get('proof'), None


def preview(db, actor, args, propose=None):
    """The plan, the report and the proof; with --propose (and an identical proof) the proposals."""
    from apps.api.services.rule_catalogue import migration_plan, migration_proof, migrate_to_catalogue
    plan = migration_plan(db, actor)
    if args.only:
        plan["proposals"] = [x for x in plan["proposals"] if x["catalogue_id"] in args.only]
    proof = migration_proof(db, actor, plan, model=not args.no_model, solve=args.solve)
    if args.json:
        print(json.dumps({"plan": plan, "proof": proof}, default=str, indent=1))
    else:
        print(plan_lines(plan) + "\n" + proof_lines(proof))
    if args.propose or propose:
        if not proof["identical"]:
            raise SystemExit(2)
        if not plan["proposals"]:
            print("Nothing to move into the catalogue.")
            return plan, proof, None
        result = (propose or (lambda payload: migrate_to_catalogue(db, actor, SimpleNamespace(payload=payload))))(
            {"catalogue_ids": [x["catalogue_id"] for x in plan["proposals"]], "reason": args.reason})
        print(result["message"])
        return plan, proof, result
    return plan, proof, None


def proposed_catalogue(db, department_id):
    return [p for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department_id, m.Policy.kind == "catalogue",
                                                       m.Policy.status == "proposed"))]


def activate(engine, args):
    """Activate every proposed catalogue rule (with a fresh rule check of each rota being planned when CP-SAT schedules)."""
    import time
    from apps.api.services.jobs import create_job
    from apps.api.services.policies import activate_policies
    from apps.api.settings import Settings
    settings = Settings.from_env()
    with transaction(engine) as db:
        actor = actor_for(db, args.who, args.department)
        waiting = [record(p) for p in proposed_catalogue(db, actor.department_id)]
        rotas = [w.id for w in db.scalars(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
                 m.Workspace.status.in_(["draft", "submitted", "approved"]), m.Workspace.archived_at.is_(None)))]
    if not waiting:
        print("No proposed catalogue rules.")
        return
    jobs = []
    if settings.generation_engine == "cp_sat":
        for workspace_id in rotas:
            with transaction(engine, write=True) as db:
                actor = actor_for(db, args.who, args.department)
                w = db.get(m.Workspace, workspace_id)
                jobs.append(create_job(db, actor, SimpleNamespace(expected_revision=w.revision, payload={
                    "kind": "feasibility", "workspace_id": w.id, "check_seconds": 60,
                    "rule_changes": [{"policy_id": p["id"], "expected_revision": p["revision"]} for p in waiting]}), settings)["job"]["id"])
        deadline, states = time.time() + 900, []
        while jobs:
            with transaction(engine) as db:
                states = [db.get(m.Job, j).status for j in jobs]
            if all(x in ("succeeded", "failed", "cancelled") for x in states) or time.time() > deadline:
                break
            time.sleep(5)
        print(f"Rule checks: {states}")
    with transaction(engine, write=True) as db:
        actor = actor_for(db, args.who, args.department)
        commands = [SimpleNamespace(kind="policy.activate", target_id=p["id"], expected_revision=db.get(m.Policy, p["id"]).revision,
                                    payload={"reason": args.reason, "check_job_ids": jobs, "acknowledge_unresolved": args.acknowledge_unresolved})
                    for p in waiting]
        out = activate_policies(db, actor, commands, require_checks=settings.generation_engine == "cp_sat")
        print(f"Activated {len(out['records'])} catalogue rules; the rules they take over are retired (--undo brings them back).")


def made_catalogue(db, department_id, only_reason=None):
    rows = [p for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department_id, m.Policy.kind == "catalogue",
                                                       m.Policy.status == "active")) if p.definition.get("merged_ids")]
    return [p for p in rows if not only_reason or (p.source or "").endswith(": " + only_reason)]


def undo(db, actor, args, transition=None):
    from apps.api.services.policies import policy_transition
    transition = transition or (lambda p: policy_transition(db, actor, SimpleNamespace(
        kind="policy.retire", target_id=p.id, expected_revision=p.revision, payload={"reason": args.reason})))
    rows = made_catalogue(db, actor.department_id, args.only_reason)
    pending = sum(retirement_pending(transition(p)) for p in rows)
    db.flush()
    print(f"Retired {len(rows) - pending} catalogue rules (the rules they took over apply again). Active rules now {active_count(db, actor.department_id)}.")
    if pending:
        print(f"{pending} retirement approvals need another authorised person.")


def baseline(engine, args):
    """An empty department (no rotas, nobody who can sign in): propose as "System baseline · proposes" and activate as
    "System baseline · activates", each through the product commands with a receipt and an audit event. Without
    --propose everything is rolled back at the end (a dry run that shows the result)."""
    from apps.api import live
    from apps.api.settings import Settings
    settings = Settings.from_env()
    correlation = "baseline-" + uuid.uuid4().hex[:12]
    reason = args.reason.strip() or "Baseline dry run"
    try:
        with transaction(engine, write=True) as db:
            allow_long_idle(db, 30)
            refusals = baseline_refusals(db)
            if refusals:
                raise SystemExit("Baseline refused: this department is in use (" + ", ".join(f"{v} {k}" for k, v in refusals.items())
                                 + "). Use --as with a coordinator, then an approver, instead.")
            department = db.scalars(select(m.Department)).one()  # shortcut: single-department install, as group_rules.py

            def run(role, command):
                return run_as(db, role, department, command, correlation, settings)
            proposer = system_identity(db, "proposer")
            actor = Actor(proposer.id, department.id, department.organisation_id, proposer.display_name, frozenset(["coordinator"]), None, "baseline")
            if args.undo:
                return undo(db, actor, args, lambda p: run("approver", {"kind": "policy.retire", "target_id": p.id, "expected_revision": p.revision,
                                                                        "payload": {"reason": args.reason}}))
            before = active_count(db, department.id)
            _, _, result = preview(db, actor, args, propose=lambda payload: run("proposer", {"kind": "rule.migrate_to_catalogue",
                                                                                            "payload": {**payload, "reason": reason}}))
            for rec in (result or {}).get("records", []):
                run("approver", {"kind": "policy.activate", "target_id": rec["id"], "expected_revision": db.get(m.Policy, rec["id"]).revision,
                                 "payload": {"reason": reason}})
            if result:
                live.signal(db, department.id)
            print(f"Baseline ({correlation}{'' if args.propose else ', dry run'}): active rules {before} -> {active_count(db, department.id)}; "
                  f"{len((result or {}).get('records', []))} catalogue rules proposed by '{BASELINE['proposer'][1]}' and activated by "
                  f"'{BASELINE['approver'][1]}'.")
            if not args.propose:
                raise DryRun()
            print(f"Undo: --baseline --undo --reason \"...\" --only-reason \"{reason}\"")
    except DryRun:
        print("Dry run: everything above was rolled back; nothing changed. Add --propose --reason \"...\" to apply it.")


if __name__ == "__main__":
    main()
