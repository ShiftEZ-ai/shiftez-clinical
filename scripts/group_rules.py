#!/usr/bin/env python3
"""Similar rules become one rule, through the normal governance.

    python scripts/group_rules.py --as <coordinator>                     # dry run: groups, counts and the proof
    python scripts/group_rules.py --as <coordinator> --model             #... and compare the CP-SAT model of every rota
    python scripts/group_rules.py --as <coordinator> --changes           #... and show the changes that are not identical
    python scripts/group_rules.py --as <coordinator> --propose --reason "..." [--changes] [--name "Group=New name"]
    python scripts/group_rules.py --as <approver> --activate --reason "..." [--changes]   # checks the rotas, then activates
    python scripts/group_rules.py --as <approver> --undo --reason "..." [--only-reason "<proposal reason>"]
    python scripts/group_rules.py --as <credentialer> --eligibility --reason "..."      # open clinic permissions (below)
    python scripts/group_rules.py --baseline [--propose] --reason "..." [--changes] [--name...]   # empty department only

Run it with the environment's own settings (inside its API container: docker exec <api> python scripts/group_rules.py …).
`--as` names an identity by id, subject or display name; it acts with that identity's roles in its department.
Proposing changes nothing that applies: every grouped rule waits for an independent approver, and activating one
retires the rules it replaces. The proof (validator findings, quota rows, the schedule score and the rules the
scheduler reads, for every rota being planned; with --model the CP-SAT model too) must show no difference before
anything is proposed or activated.

Changes that are not identical (--changes) are listed apart, each with what changes, and proposed only with --changes:
an allocation balance becomes a Balance goal; an overtime offer switched off by retired limits is pointed at the
limits in force that check as much; companion covers join (a line another line implies is dropped); a preference's
priority joins the allocation order (its balancing a Balance goal); a weekly limit becomes a line of the weekly
ceiling. Retiring the new rule brings the old one back. Rules that have ended (dates past, no rota covers them) are
retired by the approver with --activate --changes; --undo restores them.

Clinic permissions: a clinic nobody may work at after a date while rules in force still staff it there is a gap in
the data (e.g. a trial month's permissions). The dry run lists it; --eligibility (a credentialer) opens those
permissions until changed; --undo --eligibility ends them on their earlier date again.

--baseline is for a department with no rotas and no people who can sign in (a fresh production copy before its first
admin): it proposes as "System baseline · proposes", activates and retires as "System baseline · activates", and opens
clinic permissions as "System baseline · clinic permissions" — disabled identities that can never sign in — through
the same commands as the app, each with a command receipt and an audit event, in rounds until nothing is left. The
dry run (without --propose) runs everything and rolls it back, so it shows every round. It refuses when any rota,
enabled sign-in, role, account or open invitation exists. Undo with --baseline --undo --reason "..." [--only-reason...].
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import func, or_, select  # noqa: E402
from apps.api import models as m  # noqa: E402
from apps.api.auth import Actor, current_roles  # noqa: E402
from apps.api.db import allow_long_idle, make_engine, transaction  # noqa: E402
from apps.api.serialization import record  # noqa: E402
from apps.api.settings import secret_from_env  # noqa: E402

SYSTEM_ISSUER = "urn:fmuc:maintenance"
BASELINE = {"proposer": ("baseline-proposer", "System baseline · proposes (not a user)", ["coordinator"]),
            "approver": ("baseline-approver", "System baseline · activates (not a user)", ["approver"]),
            "credentialer": ("baseline-credentialer", "System baseline · clinic permissions (not a user)", ["credentialer"])}


class DryRun(Exception):
    """Raised at the end of a baseline dry run: everything it did is rolled back."""


def actor_for(db, who, department=None):
    identity = db.scalars(select(m.Identity).where(or_(m.Identity.id == who, m.Identity.subject == who, m.Identity.display_name == who))).all()
    if len(identity) != 1:
        raise SystemExit(f"--as must name exactly one identity; {len(identity)} match")
    identity = identity[0]
    bindings = {b.department_id for b in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity.id))}
    if department:
        bindings &= {department}
    if len(bindings) != 1:
        raise SystemExit("The identity works in several departments (or none): pass --department")
    dept = db.get(m.Department, bindings.pop())
    roles = current_roles(db, identity.id, dept.id)
    return Actor(identity.id, dept.id, dept.organisation_id, identity.display_name, roles, identity.person_id, "operator-cli")


def summary(preview, changes=False):
    lines = [f"{preview['active']} active rules -> {preview['after']} ({preview['rules']} rules become "
             f"{sum(1 for g in preview['groups'] if 'payload' in g and g['shape'] != 'protect')})"]
    for g in preview["groups"]:
        if "payload" not in g:
            lines.append(f"  kept apart  {len(g['rule_ids']):3}  {g['name']}: {g['problem'][:120]}")
            continue
        extra = [f"{g['lines']} lines" if g["lines"] else "", f"{g['people']} people" if g["people"] else "",
                 "overtime switched off (as now)" if g["switched_off"] else "",
                 f"academic calendar: {g['calendar']['name']}{'' if g['calendar']['entry_id'] else ' (new entry)'}" if g.get("calendar") else ""]
        lines.append(f"  {g['shape']:12} {len(g['rule_ids']):3} -> 1  {g['name']}  " + " · ".join(x for x in extra if x))
    if changes:
        lines.append(f"Changes that are not identical ({len(preview['changes'])}; proposed only with --changes):")
        for c in preview["changes"]:
            made = 0 if c.get("retire") else 1 + len(c.get("extra") or [])
            lines.append(f"  {c['shape']:12} {len(c['rule_ids']):3} -> {made}  {c['name']}" + ("  (keeps the preference: propose it from the app)" if c.get("keeps") else "")
                         + f"\n      was: {c['was']}\n      now: {c['now']}")
    return "\n".join(lines)


def proof_lines(proof, what="IDENTICAL (findings, quota rows, score, scheduler inputs)"):
    out = [f"Proof over {len(proof['rotas'])} rotas being planned: " + (what if proof["identical"] else "EQUIVALENCE NOT ESTABLISHED")
           + f"; rules {proof['rules_before']} -> {proof['rules_after']}"]
    for r in proof["rotas"]:
        same = not r["findings_only_before"] and not r["findings_only_after"] and r["quota_equal"] and r["solver_inputs_equal"]
        out.append(f"  {r['name']} ({r['status']}): findings {r['findings']} -> {r.get('findings_after', r['findings'])}, {r['quota_rows']} quota rows, score {r['score_before']} -> {r['score_after']}"
                   + (f", CP-SAT model {'identical' if r['model_equal'] else 'not confirmed identical'}" if "model_equal" in r else "")
                   + (", same" if same else f", DIFFERENT: +{len(r['findings_only_after'])} / -{len(r['findings_only_before'])} findings"
                      + ("" if r["quota_equal"] else ", quota rows differ") + ("" if r["solver_inputs_equal"] else ", scheduler inputs differ")))
    return "\n".join(out)


def gap_lines(db, gaps):
    """The clinic permissions that end while rules still staff the clinic, per clinic."""
    if not gaps:
        return "Clinic permissions: no clinic loses everyone who may work there while rules still staff it."
    out = [f"Clinic permissions: {len(gaps)} end while rules in force still staff the clinic (opened until changed with --eligibility):"]
    for service_id in dict.fromkeys(g["service_id"] for g in gaps):
        here = [g for g in gaps if g["service_id"] == service_id]
        out.append(f"  {db.get(m.Service, service_id).name}: {len(here)} people, each until {here[0]['ends_on']} -> until changed")
    return "\n".join(out)


def names_from(args):
    out = {}
    for text in args.name or []:
        old, sep, new = text.partition("=")
        if not sep or not new.strip():
            raise SystemExit('--name takes "Group name=New name"')
        out[old.strip()] = new.strip()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, fromfile_prefix_chars="@")
    parser.add_argument("--database-url", default=secret_from_env("DATABASE_URL", "sqlite:///./var/rota.db"))
    parser.add_argument("--as", dest="who", help="Identity id, subject or display name to act as")
    parser.add_argument("--department", help="Department id, when the identity works in several")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--propose", action="store_true", help="Propose every group (nothing applies until approved)")
    action.add_argument("--activate", action="store_true", help="Approver: check the rotas, then activate the proposed grouped rules")
    action.add_argument("--undo", action="store_true", help="Approver: retire the active grouped rules (the rules they replaced apply again)")
    parser.add_argument("--eligibility", action="store_true", help="Credentialer: open the clinic permissions that end while rules still staff the clinic")
    parser.add_argument("--baseline", action="store_true", help="Empty department only: every round as the system baseline actors")
    parser.add_argument("--reason", default="")
    parser.add_argument("--changes", action="store_true", help="Also show (and with --propose, propose) the changes that are not identical")
    parser.add_argument("--model", action="store_true", help="Also compare the CP-SAT model of every rota being planned")
    parser.add_argument("--name", action="append", help='Rename a group: "Group name=New name" (repeatable)')
    parser.add_argument("--only-reason", help="--undo: only rules proposed with this reason")
    parser.add_argument("--no-calendar", action="store_true", help="Do not link protected academic days to the academic calendar")
    parser.add_argument("--acknowledge-unresolved", action="store_true", help="Activate even if a rota check is unresolved (reviewed)")
    parser.add_argument("--json", action="store_true", help="Print the full preview and proof as JSON")
    args = parser.parse_args()
    if (args.propose or args.activate or args.undo or args.eligibility and not args.baseline) and len(args.reason.strip()) < 3:
        parser.error("--reason is required to change anything")
    if not args.baseline and not args.who:
        parser.error("--as is required (or --baseline for an empty department)")
    from apps.api.services.policies import duplicate_groups, group_rules, grouping_proof, activate_policies, policy_transition
    from apps.api.services.catalogue import eligibility_gaps
    from apps.api.services.jobs import create_job
    from apps.api.settings import Settings
    engine = make_engine(args.database_url)
    try:
        if args.baseline:
            return baseline(engine, args)
        if args.activate:
            return activate(engine, args, create_job, activate_policies, policy_transition, Settings)
        with transaction(engine, write=bool(args.propose or args.undo or args.eligibility)) as db:
            allow_long_idle(db, 30)
            actor = actor_for(db, args.who, args.department)
            if args.eligibility and not args.undo:
                return open_eligibility(db, actor, args)
            if args.undo:
                return undo(db, actor, args, policy_transition)
            preview = duplicate_groups(db, actor)
            proof = grouping_proof(db, actor, preview["groups"], model=args.model)
            print(json.dumps({"preview": preview, "proof": proof}, default=str, indent=1) if args.json else summary(preview, args.changes))
            print(proof_lines(proof))
            if args.changes and preview["changes"]:
                effect = grouping_proof(db, actor, preview["changes"], model=False)
                print("What the changes do to the rotas being planned:\n" + "\n".join(proof_lines(effect, "no difference").splitlines()[1:] or ["  (no rotas being planned)"]))
            print(gap_lines(db, eligibility_gaps(db, actor.department_id)))
            if args.propose:
                if not proof["identical"]:
                    raise SystemExit(2)
                out = propose(db, actor, preview, args, group_rules)
                print(out)
    finally:
        engine.dispose()


def proposal(preview, args):
    """What --propose sends: every group, and with --changes every change a coordinator proposes (not ended rules,
    which an approver retires, nor a balance that keeps its preference, which the app proposes rule by rule)."""
    rename = names_from(args)
    chosen = [g for g in preview["groups"] if "payload" in g]
    changes = [c["rule_ids"] for c in preview["changes"] if not c.get("keeps") and not c.get("retire")] if args.changes else []
    unknown = set(rename) - {g["name"] for g in chosen}
    if unknown:
        raise SystemExit(f"--name: no group is called {sorted(unknown)}")
    return chosen, changes, {"groups": [g["rule_ids"] for g in chosen], "changes": changes, "reason": args.reason,
                             "link_calendar": not args.no_calendar,
                             "names": {g["rule_ids"][0]: rename[g["name"]] for g in chosen if g["name"] in rename}}


def propose(db, actor, preview, args, group_rules):
    chosen, changes, payload = proposal(preview, args)
    if not chosen and not changes:
        return "Nothing to group."
    out = group_rules(db, actor, SimpleNamespace(payload=payload))
    return out["message"] + (f" {out['calendar_entries']} academic calendar entries added." if out["calendar_entries"] else "")


def made_rules(db, department_id, only_reason=None):
    """The active rules that replaced others (grouped, converted, joined or switched on), newest first."""
    rows = [p for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department_id, m.Policy.status == "active"))
            if p.definition.get("merged_ids")]
    if only_reason:
        rows = [p for p in rows if (p.source or "").endswith(": " + only_reason)]
    return sorted(rows, key=lambda p: p.created_at, reverse=True)


def last_reason(db, kind, row):
    last = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == kind, m.DefinitionHistory.object_id == row.id)
                     .order_by(m.DefinitionHistory.object_revision.desc()).limit(1))
    return last.reason if last else ""


def ended_rules(db, department_id, only_reason=None):
    """Rules retired because they had ended (the undo restores them)."""
    from apps.api.services.policies import ENDED_REASON
    rows = db.scalars(select(m.Policy).where(m.Policy.department_id == department_id, m.Policy.status == "retired"))
    return [p for p in rows if last_reason(db, "policy", p).startswith(ENDED_REASON + (": " + only_reason if only_reason else ""))]


def opened_permissions(db, department_id, only_reason=None):
    """Clinic permissions opened until changed, with the date each ended before (the undo ends them there again)."""
    import re
    out = []
    for row in db.scalars(select(m.Eligibility).where(m.Eligibility.department_id == department_id, m.Eligibility.ends_on.is_(None))):
        said = re.match(r"Until changed \(was until (\d{4}-\d{2}-\d{2})\): (.*)$", last_reason(db, "eligibility.extend", row), re.S)
        if said and (not only_reason or said.group(2) == only_reason):
            out.append((row, said.group(1)))
    return out


def retirement_pending(result):
    return bool(result.get("approval") and not result["approval"]["complete"])


def undo_all(db, department_id, only_reason, retire):
    """Retire the rules that replaced others until none is left (retiring one can bring back a rule it replaced that
    itself replaced others, from the same round)."""
    retired = 0
    pending = set()
    for _ in range(10):
        grouped = [p for p in made_rules(db, department_id, only_reason) if p.id not in pending]
        if not grouped:
            break
        for p in grouped:
            if retirement_pending(retire(p)):
                pending.add(p.id)
            else:
                retired += 1
        db.flush()
    return retired, active_count(db, department_id), len(pending)


def active_count(db, department_id):
    return db.scalar(select(func.count()).select_from(select(m.Policy.id).where(m.Policy.department_id == department_id, m.Policy.status == "active").subquery()))


def undo(db, actor, args, policy_transition):
    from apps.api.services.catalogue import end_relationship
    if args.eligibility:
        rows = opened_permissions(db, actor.department_id, args.only_reason)
        for row, ended_on in rows:
            end_relationship(db, actor, SimpleNamespace(kind="eligibility.end", target_id=row.id, expected_revision=row.revision,
                                                        payload={"ends_on": ended_on, "reason": args.reason}))
        print(f"Ended {len(rows)} clinic permissions on their earlier dates again.")
        return
    restored = 0
    for p in ended_rules(db, actor.department_id, args.only_reason):
        policy_transition(db, actor, SimpleNamespace(kind="policy.restore", target_id=p.id, expected_revision=p.revision, payload={"reason": args.reason}))
        restored += 1
    retired, active, pending = undo_all(db, actor.department_id, args.only_reason, lambda p: policy_transition(
        db, actor, SimpleNamespace(kind="policy.retire", target_id=p.id, expected_revision=p.revision, payload={"reason": args.reason})))
    print(f"Retired {retired} rules that replaced others (the rules they replaced apply again) and restored {restored} ended rules. "
          f"Active rules now {active}.")
    if pending:
        print(f"{pending} retirement approvals need another authorised person.")


def open_eligibility(db, actor, args):
    from apps.api.services.catalogue import eligibility_gaps, extend_eligibility
    from apps.api.services.policies import OPENED_REASON
    gaps = eligibility_gaps(db, actor.department_id)
    print(gap_lines(db, gaps))
    for g in gaps:
        extend_eligibility(db, actor, SimpleNamespace(target_id=g["id"], expected_revision=g["revision"], payload={
            "ends_on": None, "reason": f"{OPENED_REASON.format(g['ends_on'])} {args.reason}"}))
    print(f"Opened {len(gaps)} clinic permissions until changed. Undo: --undo --eligibility --only-reason \"{args.reason}\"")


def activate(engine, args, create_job, activate_policies, policy_transition, Settings):
    """Run the rule check each rota being planned needs (the worker does it), then activate every proposed grouped rule;
    with --changes, also retire the rules that have ended and exact copies of another rule."""
    from apps.api.services.policies import ENDED_REASON, duplicate_groups
    settings = Settings.from_env()
    with transaction(engine) as db:
        actor = actor_for(db, args.who, args.department)
        waiting = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id, m.Policy.status == "proposed"))
                   if p.definition.get("merged_ids") or p.definition.get("calendar_entry_id")]
        workspaces = [w.id for w in db.scalars(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
                      m.Workspace.status.in_(["draft", "submitted", "approved"]), m.Workspace.archived_at.is_(None)))]
    if waiting:
        jobs = []
        if settings.generation_engine == "cp_sat":
            for workspace_id in workspaces:
                with transaction(engine, write=True) as db:
                    actor = actor_for(db, args.who, args.department)
                    w = db.get(m.Workspace, workspace_id)
                    job = create_job(db, actor, SimpleNamespace(expected_revision=w.revision, payload={
                        "kind": "feasibility", "workspace_id": w.id, "check_seconds": 60,
                        "rule_changes": [{"policy_id": p["id"], "expected_revision": p["revision"]} for p in waiting]}), settings)
                    jobs.append(job["job"]["id"])
            deadline = time.time() + 900
            states = []
            while jobs:
                with transaction(engine) as db:
                    states = [db.get(m.Job, j).status for j in jobs]
                if all(s in ("succeeded", "failed", "cancelled") for s in states) or time.time() > deadline:
                    break
                time.sleep(5)
            print(f"Rule checks: {states}")
        with transaction(engine, write=True) as db:
            actor = actor_for(db, args.who, args.department)
            commands = [SimpleNamespace(kind="policy.activate", target_id=p["id"], expected_revision=db.get(m.Policy, p["id"]).revision,
                                        payload={"reason": args.reason, "check_job_ids": jobs, "acknowledge_unresolved": args.acknowledge_unresolved})
                        for p in waiting]
            out = activate_policies(db, actor, commands, require_checks=settings.generation_engine == "cp_sat")
            print(f"Activated {len(out['records'])} rules; the rules they replace are retired (retire one to undo it).")
    else:
        print("No proposed grouped rules.")
    if args.changes:
        with transaction(engine, write=True) as db:
            actor = actor_for(db, args.who, args.department)
            ended = [c for c in duplicate_groups(db, actor)["changes"] if c.get("retire")]
            retired = []
            for c in ended:
                p = db.get(m.Policy, c["rule_ids"][0])
                reason = (f"Duplicate of {c['kept_name']}" if c["shape"] == "duplicate" else ENDED_REASON)
                result = policy_transition(db, actor, SimpleNamespace(kind="policy.retire",
                    target_id=p.id, expected_revision=p.revision, payload={"reason": reason[:300] + f": {args.reason}"}))
                if not retirement_pending(result):
                    retired.append(c["name"])
            print(f"Retired {len(retired)} rules that have ended or copy another rule ({', '.join(retired) or 'none'}); --undo restores them.")
            if pending := len(ended) - len(retired):
                print(f"{pending} retirement approvals need another authorised person.")


# Baseline: an empty department (no rotas, nobody who can sign in) -------------------------------------------------------

def baseline_refusals(db):
    """Why the baseline mode may not run here (empty: it may)."""
    count = lambda q: db.scalar(select(func.count()).select_from(q.subquery()))  # noqa: E731
    found = {
        "rotas": count(select(m.Workspace.id)),
        "enabled sign-ins": count(select(m.Identity.id).where(m.Identity.disabled.is_(False))),
        "roles": count(select(m.RoleBinding.id).where(m.RoleBinding.revoked.is_(False))),
        "accounts": count(select(m.Account.id)),
        "open invitations": count(select(m.AccountLink.id).where(m.AccountLink.used_at.is_(None), m.AccountLink.revoked_at.is_(None))),
    }
    return {k: v for k, v in found.items() if v}


def system_identity(db, role):
    subject, name, _ = BASELINE[role]
    row = db.scalar(select(m.Identity).where(m.Identity.issuer == SYSTEM_ISSUER, m.Identity.subject == subject))
    if row is None:
        row = m.Identity(issuer=SYSTEM_ISSUER, subject=subject, display_name=name, disabled=True)
        db.add(row)
        db.flush()
    return row


def run_as(db, role, department, command, correlation_id, settings):
    """One product command as a baseline system identity, with its command receipt and audit event (as the HTTP bus)."""
    from apps.api.commands import dispatch
    from apps.api.serialization import json_value
    from apps.api.services.audit_log import event_action
    from apps.api.snapshots import snapshot_hash
    from packages.contracts import schemas as s
    identity = system_identity(db, role)
    command = s.Command.model_validate(command)
    actor = Actor(identity.id, department.id, department.organisation_id, identity.display_name, frozenset(BASELINE[role][2]), None, "baseline")
    canonical = command.model_dump(mode="json")
    if command.expected_fingerprint is None:
        canonical.pop("expected_fingerprint")
    result = json_value(dispatch(db, actor, command, settings))
    receipt_id = m.uid()
    db.add(m.CommandReceipt(id=receipt_id, department_id=department.id, actor_id=identity.id, command_kind=command.kind,
                            required_roles=BASELINE[role][2], key="baseline-" + uuid.uuid4().hex, payload_hash=snapshot_hash(canonical), result=result))
    db.add(m.AuditEvent(department_id=department.id, actor_id=identity.id,
                        action=event_action(command.kind, result), target_id=command.target_id,
                        reason=str(command.payload.get("reason", command.kind))[:500],
                        detail={"receipt_id": receipt_id, "payload_fields": sorted(command.payload), "baseline": True,
                                **({"approval": result["approval"]} if result.get("approval") else {})},
                        correlation_id=correlation_id))
    db.flush()
    return result


def baseline(engine, args):
    """Dry run (default: everything, then rolled back), --propose (propose, activate and retire in rounds until nothing
    is left, then open clinic permissions) or --undo."""
    from apps.api import live
    from apps.api.services.catalogue import eligibility_gaps
    from apps.api.services.policies import ENDED_REASON, OPENED_REASON, duplicate_groups, grouping_proof
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
            department = db.scalars(select(m.Department)).one()  # shortcut: single-department install; add --department when a second exists

            def run(role, command):
                return run_as(db, role, department, command, correlation, settings)
            if args.undo:
                return baseline_undo(db, department, args, run)
            before, rename, made_total, ended_total = active_count(db, department.id), names_from(args), 0, 0
            pending = set()
            for round_no in range(1, 7):
                proposer = system_identity(db, "proposer")
                actor = Actor(proposer.id, department.id, department.organisation_id, proposer.display_name, frozenset(["coordinator"]), None, "baseline")
                preview = duplicate_groups(db, actor)
                chosen = [g for g in preview["groups"] if "payload" in g]
                changes = [c for c in preview["changes"] if not c.get("keeps")
                           and c["rule_ids"][0] not in pending] if args.changes else []
                if not chosen and not changes:
                    break
                proof = grouping_proof(db, actor, preview["groups"], model=args.model)
                print(f"Round {round_no}:\n" + summary(preview, args.changes) + "\n" + proof_lines(proof))
                if not proof["identical"]:
                    raise SystemExit(2)
                names = {g["rule_ids"][0]: rename[g["name"]] for g in chosen if g["name"] in rename}
                proposed = [c["rule_ids"] for c in changes if not c.get("retire")]
                if chosen or proposed:
                    made = run("proposer", {"kind": "policy.group", "payload": {
                        "groups": [g["rule_ids"] for g in chosen], "changes": proposed, "reason": reason,
                        "link_calendar": not args.no_calendar, "names": names}})
                    for rec in made["records"]:
                        run("approver", {"kind": "policy.activate", "target_id": rec["id"], "expected_revision": db.get(m.Policy, rec["id"]).revision,
                                         "payload": {"reason": reason}})
                    made_total += len(made["records"])
                for c in changes:
                    if c.get("retire"):
                        p = db.get(m.Policy, c["rule_ids"][0])
                        result = run("approver", {"kind": "policy.retire", "target_id": p.id,
                            "expected_revision": p.revision, "payload": {"reason": f"{ENDED_REASON}: {reason}"}})
                        if retirement_pending(result):
                            pending.add(p.id)
                        else:
                            ended_total += 1
                db.flush()
            gaps = eligibility_gaps(db, department.id)
            print(gap_lines(db, gaps))
            for g in gaps:
                run("credentialer", {"kind": "eligibility.extend", "target_id": g["id"], "expected_revision": g["revision"],
                                     "payload": {"ends_on": None, "reason": f"{OPENED_REASON.format(g['ends_on'])} {reason}"}})
            if made_total or ended_total or gaps or pending:
                live.signal(db, department.id)
            after = active_count(db, department.id)
            print(f"Baseline ({correlation}{'' if args.propose else ', dry run'}): active rules {before} -> {after}; {made_total} rules proposed by "
                  f"'{BASELINE['proposer'][1]}' and activated by '{BASELINE['approver'][1]}', {ended_total} ended rules retired, "
                  f"{len(gaps)} clinic permissions opened until changed by '{BASELINE['credentialer'][1]}'.")
            if pending:
                print(f"{len(pending)} retirement approvals need another authorised person.")
            if not args.propose:
                raise DryRun()
            print(f"Undo: --baseline --undo --reason \"...\" --only-reason \"{reason}\"")
    except DryRun:
        print("Dry run: everything above was rolled back; nothing changed. Add --propose --reason \"...\" to apply it.")


def baseline_undo(db, department, args, run):
    """Everything a baseline run did, in reverse: clinic permissions end on their earlier dates, ended rules are
    restored, and the rules that replaced others are retired (their rules apply again)."""
    opened = opened_permissions(db, department.id, args.only_reason)
    for row, ended_on in opened:
        run("credentialer", {"kind": "eligibility.end", "target_id": row.id, "expected_revision": row.revision,
                             "payload": {"ends_on": ended_on, "reason": args.reason}})
    ended = ended_rules(db, department.id, args.only_reason)
    for p in ended:
        run("approver", {"kind": "policy.restore", "target_id": p.id, "expected_revision": p.revision, "payload": {"reason": args.reason}})
    retired, active, pending = undo_all(db, department.id, args.only_reason, lambda p: run(
        "approver", {"kind": "policy.retire", "target_id": p.id, "expected_revision": p.revision, "payload": {"reason": args.reason}}))
    from apps.api import live
    live.signal(db, department.id)
    print(f"Baseline undo: {len(opened)} clinic permissions ended on their earlier dates, {len(ended)} ended rules restored, "
          f"{retired} rules retired (the rules they replaced apply again). Active rules now {active}.")
    if pending:
        print(f"{pending} retirement approvals need another authorised person.")


if __name__ == "__main__":
    main()
