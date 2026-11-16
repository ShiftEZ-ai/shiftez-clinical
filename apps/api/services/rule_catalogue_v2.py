"""One independently reviewed, atomic catalogue representation conversion per department."""
from .workspace_access import shared_clause
from collections import Counter
from datetime import date

from sqlalchemy import Text, cast, func, select, tuple_
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import load_only

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.db import allow_long_idle
from apps.api.errors import require
from apps.api.serialization import record, json_value
from apps.api.snapshots import domain_snapshot, snapshot_hash
from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import catalogue_counts
from packages.domain import rule_catalogue_migration as migration
from packages.domain.rule_serialization import stored_definition
from .common import bump_inputs, expected, history, history_many, scoped
from .policies import advance_revision
from . import independence

INPUT_TABLES = (m.Policy, m.PolicyException, m.Workspace, m.Publication, m.SchedulingFact, m.RuleSourceBinding, m.PolicySeedLine)


def _audit_refs(db, actor, source_ids):
    """Return stable identifiers for retained source history, bindings and audit rows."""
    source_ids = set(source_ids)
    if not source_ids:
        return {}
    history_rows = db.execute(select(m.DefinitionHistory.id, m.DefinitionHistory.object_id,
        m.DefinitionHistory.object_revision).where(m.DefinitionHistory.department_id == actor.department_id,
            m.DefinitionHistory.kind == 'policy', m.DefinitionHistory.object_id.in_(source_ids))).all()
    audit_rows = db.execute(select(m.AuditEvent.id, m.AuditEvent.target_id, m.AuditEvent.action).where(
        m.AuditEvent.department_id == actor.department_id, m.AuditEvent.target_id.in_(source_ids))).all()
    binding_rows = db.execute(select(m.RuleSourceBinding.id, m.RuleSourceBinding.source_policy_id,
        m.RuleSourceBinding.migration_proposal_id).where(m.RuleSourceBinding.department_id == actor.department_id,
            m.RuleSourceBinding.source_policy_id.in_(source_ids))).all()
    refs = {source_id: {'definition_history_ids': [], 'audit_event_ids': [],
                        'rule_source_binding_ids': [], 'migration_proposal_ids': []} for source_id in source_ids}
    for row in sorted(history_rows, key=lambda value: (str(value.object_id), value.object_revision, str(value.id))):
        refs[row.object_id]['definition_history_ids'].append(row.id)
    for row in sorted(audit_rows, key=lambda value: (str(value.target_id), str(value.action), str(value.id))):
        refs[row.target_id]['audit_event_ids'].append(row.id)
    for row in sorted(binding_rows, key=lambda value: (str(value.source_policy_id), str(value.id))):
        refs[row.source_policy_id]['rule_source_binding_ids'].append(row.id)
        if row.migration_proposal_id:
            refs[row.source_policy_id]['migration_proposal_ids'].append(row.migration_proposal_id)
    return refs


def _preview_plan(candidate):
    """Navigation and review summary; immutable definitions stay in the full receipt."""
    return {**{key: value for key, value in candidate.items()
               if key not in {'source_manifest', 'targets', 'scheduling_facts', 'bindings', 'proof'}},
        'targets': [{'catalogue_id': target['catalogue_id'], 'row': {'id': target['row']['id']}}
                    for target in candidate['targets']],
        'scheduling_facts': [{key: fact.get(key) for key in ('id', 'kind', 'person_id', 'calendar_entry_id')}
                             for fact in candidate['scheduling_facts']],
        'bindings': [{key: binding[key] for key in ('source_policy_id', 'source_revision', 'destinations')}
                     for binding in candidate['bindings']],
        **({'proof': _preview_proof(candidate['proof']) if candidate['proof'] else None}
           if 'proof' in candidate else {})}


def _conversion_record(row, *, details=False):
    if details:
        return record(row)
    return {**record(row, exclude=('plan', 'proof')),
            'plan': _preview_plan(row.plan), 'proof': _preview_proof(row.proof)}


def read(db, actor, conversion_id=None, *, include_history=False, details=False):
    require_role(actor, 'coordinator', 'admin', 'approver', 'auditor', 'programme_lead')
    if conversion_id:
        return {'record': _conversion_record(scoped(db, m.CatalogueConversion, conversion_id, actor), details=details)}
    query = select(m.CatalogueConversion).where(m.CatalogueConversion.department_id == actor.department_id)
    if not include_history:
        query = query.where(m.CatalogueConversion.status.in_(('proposed', 'active')))
    query = query.options(load_only(*(getattr(m.CatalogueConversion, column.key)
        for column in m.CatalogueConversion.__table__.columns if column.key not in {'plan', 'proof', 'manifest'})))
    return {'records': [record(row, exclude=('plan', 'proof', 'manifest'))
                        for row in db.scalars(query.order_by(m.CatalogueConversion.created_at.desc(), m.CatalogueConversion.seq.desc()))]}


def _input_rows(db, actor):
    # Read fresh columns: the session identity map can retain an earlier revision during proof.
    return {cls.__tablename__: [
        {key: json_value(value) if isinstance(value, date) else value for key, value in row.items()}
        for row in db.execute(select(*cls.__table__.columns).where(
            cls.department_id == actor.department_id, *([shared_clause()] if cls is m.Workspace else [])).order_by(cls.id)).mappings()]
        for cls in INPUT_TABLES}


def _input_guard(db, actor):
    """Exact row-content freshness without transferring large JSON evidence a second time."""
    if db.get_bind().dialect.name != 'postgresql':
        return None
    def digest(value):
        return func.encode(func.sha256(func.convert_to(value, 'UTF8')), 'hex')
    digests = [select(digest(func.coalesce(func.string_agg(digest(cast(cls.__table__.table_valued(), Text)),
        aggregate_order_by('', cls.id)), ''))).where(cls.department_id == actor.department_id,
        *([shared_clause()] if cls is m.Workspace else [])).scalar_subquery()
        for cls in INPUT_TABLES]
    return tuple(db.execute(select(m.Organisation.input_revision, m.Organisation.availability_revision, *digests)
        .where(m.Organisation.id == actor.organisation_id)).one())


def _guard_manifest(guard):
    return {'input_revision': guard[0], 'availability_revision': guard[1],
            'records': {cls.__tablename__: value for cls, value in zip(INPUT_TABLES, guard[2:])}}


def _inputs(db, actor, rows=None):
    """Hash operative inputs and historical references; lifecycle/audit additions are separate evidence."""
    guard = _input_guard(db, actor)
    if guard is not None:
        return _guard_manifest(guard)
    revision, availability = db.execute(select(m.Organisation.input_revision, m.Organisation.availability_revision)
        .where(m.Organisation.id == actor.organisation_id)).one()
    rows = _input_rows(db, actor) if rows is None else rows
    return {"input_revision": revision, "availability_revision": availability,
            "records": {name: snapshot_hash({"rows": values}) for name, values in rows.items()}}


def plan(db, actor, *, proof=False, background=False, expected_manifest=None):
    require_role(actor, "coordinator", "admin", "approver", "auditor")
    proof = proof or background
    db.flush()
    guard = _input_guard(db, actor)
    if guard is None:
        inputs = _input_rows(db, actor)
        manifest = _inputs(db, actor, inputs)
        rows = inputs[m.Policy.__tablename__]
    else:
        manifest = _guard_manifest(guard)
        rows = [{key: json_value(value) if isinstance(value, date) else value for key, value in row.items()}
                for row in db.execute(select(*m.Policy.__table__.columns).where(
                    m.Policy.department_id == actor.department_id).order_by(m.Policy.id)).mappings()]
    if expected_manifest is not None:
        require(manifest == expected_manifest, "STALE_INPUTS", "Conversion inputs changed; decline and preview again", 409)
    from .scoring_defaults import seeded_rows
    rows = seeded_rows(db, rows)
    audit_refs = _audit_refs(db, actor, [row['id'] for row in rows])
    # The manifest is fresh; operative ORM inputs must not retain older loaded values.
    db.flush()
    db.expire_all()
    exceptions = set(db.scalars(select(m.PolicyException.policy_id).where(
        m.PolicyException.department_id == actor.department_id,
        m.PolicyException.status.in_(['proposed', 'approved']),
        m.PolicyException.ends_on > independence.today(db, actor.department_id))))
    rotas = list(db.scalars(select(m.Workspace).where(shared_clause(), m.Workspace.department_id == actor.department_id)
        .options(load_only(m.Workspace.id, m.Workspace.status, m.Workspace.rule_adjustments, m.Workspace.target_overrides))))
    adjusted = {r["policy_id"] for w in rotas for r in w.rule_adjustments or []}
    targets = {r["policy_id"] for w in rotas for r in w.target_overrides or []}
    held = migration.held_reasons(rows, exceptions, adjusted, targets)
    org = db.get(m.Organisation, actor.organisation_id)
    tag_ids = set(db.scalars(select(m.Tag.id).where(m.Tag.department_id == actor.department_id)))
    from scripts.system_command import MAINTENANCE_ISSUER, MAINTENANCE_SUBJECT
    default_actor_id = db.scalar(select(m.Identity.id).where(m.Identity.issuer == MAINTENANCE_ISSUER,
        m.Identity.subject == MAINTENANCE_SUBJECT, m.Identity.disabled.is_(True)))
    today = independence.today(db, actor.department_id)
    publication_start = db.scalar(select(func.min(m.Publication.starts_on)).where(
        m.Publication.department_id == actor.department_id, m.Publication.current.is_(True)))
    candidate = migration.plan_v2(rows, held, org.week_start, actor.department_id,
                                  tag_ids=tag_ids, default_actor_id=default_actor_id,
                                  today=today, publication_start=publication_start)
    candidate['census'] = migration.census(rows, audit_refs=audit_refs)
    candidate['catalogue_counts'] = catalogue_counts()
    today = str(today)
    lines = [line for p in rows if p['status'] == 'active' and p['kind'] == 'catalogue'
             for line in p['definition']['rule'].get('lines') or []]
    candidate['counts'] = {'current_sources': sum(p['status'] == 'active' for p in rows),
        'retired_sources': sum(p['status'] == 'retired' for p in rows),
        'empty_types': sum(p['status'] == 'active' and p['kind'] == 'catalogue' and not p['definition']['rule'].get('lines') for p in rows),
        'expired_lines': sum(bool(line.get('ends_on') and line['ends_on'] <= today) for line in lines),
        'target_rules': len(candidate['targets']),
        'facts': dict(Counter(fact['kind'] for fact in candidate['scheduling_facts']))}
    result = {**candidate, "manifest": manifest}
    result["plan_hash"] = snapshot_hash(result)
    result['proof'] = None
    ready = None
    if proof and not candidate['blocked']:
        ready = _proof_job(db, actor, result)
        if ready is not None and ready.status == 'succeeded':
            result['proof'] = ready.result['proof']
        elif not background or not any(w.status in {'draft', 'submitted', 'approved'} for w in rotas):
            result['proof'] = _proof(db, actor, candidate, rotas)
    if background:
        job = ready
        result['proof_job'] = s.CatalogueProofJob.model_validate({
            key: getattr(job, key) for key in ('id', 'status', 'progress', 'error_code')}).model_dump(mode='json') if job else None
        result['proof_state'] = ('blocked' if candidate['blocked'] else
            'passed' if result['proof'] and result['proof']['identical'] else
            'different' if result['proof'] else job.status if job else 'not_checked')
        if result['proof'] is not None:
            result['proof'] = _preview_proof(result['proof'])
    unchanged = _input_guard(db, actor) == guard if guard is not None else _inputs(db, actor) == manifest
    require(unchanged, "STALE_INPUTS", "Inputs changed during the preview; preview again", 409)
    return result


def _preview_proof(proof):
    """Keep proof outcomes inspectable; full captured objectives remain in the saved receipt."""
    return {**proof, 'rotas': [{**rota, 'model': {**rota['model'], **{
        side: {key: value for key, value in rota['model'][side].items() if key != 'tiers'}
        for side in ('before', 'after') if side in rota['model']}}}
        for rota in proof.get('rotas', [])]}


def _proof_job(db, actor, candidate):
    from .jobs import catalogue_proof_identity
    jobs = list(db.scalars(select(m.Job).where(m.Job.department_id == actor.department_id,
        m.Job.kind == 'catalogue_proof', m.Job.input_fingerprint == candidate['plan_hash']).order_by(m.Job.created_at.desc(), m.Job.seq.desc())))
    if not jobs:
        return None
    identity = catalogue_proof_identity()
    if identity is None:
        return None
    frozen = {key: value for key, value in candidate.items()
              if key not in {'manifest', 'proof', 'plan_hash', 'proof_job', 'proof_state', 'proof_status'}}
    candidate_hash = snapshot_hash(frozen)
    for job in jobs:
        snapshot = job.input_snapshot
        if (snapshot.get('manifest') != candidate['manifest'] or snapshot.get('toolchain') != identity
                or snapshot.get('candidate') != frozen or snapshot.get('candidate_hash') != candidate_hash):
            continue
        if job.status == 'succeeded' and (not job.result or job.result.get('manifest') != candidate['manifest']
                or job.result.get('plan_hash') != candidate['plan_hash'] or job.result.get('toolchain') != identity
                or not isinstance(job.result.get('proof'), dict)):
            continue
        return job
    return None


def check(db, actor, command):
    from .jobs import enqueue_catalogue_proof
    require_role(actor, 'coordinator', 'approver')
    value = s.RuleMigrationInput.model_validate(command.payload)
    require(value.version == 2, 'CATALOGUE_VERSION', 'Use the v2 department conversion payload')
    candidate = plan(db, actor)
    require(value.plan_hash == candidate['plan_hash'], 'STALE_INPUTS', 'Use a fresh conversion preview', 409)
    return {'job': enqueue_catalogue_proof(db, actor, candidate)}


def _proof(db, actor, candidate, rotas, *, cancelled=None, progress=None):
    allow_long_idle(db, 30)
    from packages.domain.rule_migration_proof import strict_expanded, strict_proof
    from packages.domain.solver_control import Cancelled
    results = []
    included = [workspace for workspace in rotas if workspace.status in {'draft', 'submitted', 'approved'}]
    for index, workspace in enumerate(included):
        if cancelled and cancelled():
            raise Cancelled()
        if progress:
            progress({'stage': 'checking', 'completed_rotas': index, 'total_rotas': len(included)})
        # Conversion proof compares the current department representation. Draft
        # evaluation remains baseline-pinned; proof must not mix that frozen view
        # with a later source revision after an undo.
        before = domain_snapshot(db, workspace, current_scheduling=True)
        after = migration.after_snapshot(before, candidate)
        results.append({"workspace_id": workspace.id, **strict_proof(before, after, cancelled=cancelled,
            retirement_cutoff=candidate.get('retirement_cutoff'))})
        if cancelled and cancelled():
            raise Cancelled()
    # A department without a current rota still needs exact obligation/multiplicity preservation.
    if not results:
        from apps.api.snapshots import scheduling_inputs
        before = {**scheduling_inputs(db, [actor.department_id]),
            "policies": [record(p) for p in db.scalars(select(m.Policy).where(
            m.Policy.department_id == actor.department_id))], "people": {}, "tags": {}}
        after = migration.after_snapshot(before, candidate)
        equal = strict_expanded(before, after, retirement_cutoff=candidate.get('retirement_cutoff'))['equal']
        return {"identical": equal, "expanded_equal": equal, "rotas": [], "model": "No current rota to compile"}
    return {"identical": all(r["identical"] for r in results), "rotas": results}


def propose(db, actor, command):
    require_role(actor, "coordinator", "admin")
    value = s.RuleMigrationInput.model_validate(command.payload)
    require(value.version == 2, "CATALOGUE_VERSION", "Use the v2 department conversion payload")
    require(db.scalar(select(m.CatalogueConversion.id).where(m.CatalogueConversion.department_id == actor.department_id,
            m.CatalogueConversion.status == "proposed")) is None, "PROPOSAL_WAITING", "A department conversion is waiting for review", 409)
    candidate = plan(db, actor, proof=True)
    require(not candidate["blocked"], "CONVERSION_BLOCKED", "Resolve the department conversion blockers first", 409, candidate["blocked"])
    require(candidate["bindings"], "NOTHING_TO_MOVE", "No legacy sources remain to convert", 409)
    require(value.plan_hash == candidate["plan_hash"], "STALE_INPUTS", "Use the hash from a fresh v2 preview", 409)
    require(candidate["proof"] and candidate["proof"]["identical"], "EQUIVALENCE_FAILED", "Conversion must preserve every result and objective", 409,
            candidate["proof"])
    frozen = {k: v for k, v in candidate.items() if k not in {"manifest", "proof", "plan_hash"}}
    row = m.CatalogueConversion(department_id=actor.department_id, proposed_by=actor.identity_id,
        status="proposed", reason=value.reason, input_revision=candidate["manifest"]["input_revision"],
        manifest=candidate["manifest"], plan=frozen, proof=candidate["proof"], plan_hash=candidate["plan_hash"])
    db.add(row)
    history(db, actor, "catalogue_conversion", row, value.reason)
    return {"record": _conversion_record(row), "message": "One department conversion proposed; existing sources still apply."}


def transition(db, actor, command):
    action = command.kind.rsplit(".", 1)[-1]
    require_role(actor, *("approver", "coordinator", "admin") if action == "decline" else ("approver",))
    value = s.Decision.model_validate(command.payload)
    row = scoped(db, m.CatalogueConversion, command.target_id, actor)
    expected(row, command.expected_revision)
    if action == "decline":
        require(row.status == "proposed", "WORKFLOW_STATE", "Only a pending conversion can be declined", 409)
        row.status = "declined"
    elif action == "activate":
        require(row.status == "proposed", "WORKFLOW_STATE", "Only a pending conversion can be activated", 409)
        independence.ensure(db, actor, "rule.activate", independence.person_key(db, row.proposed_by) !=
            independence.person_key(db, actor.identity_id), "Another authorised person must approve this conversion", value.reason, row.id)
        fresh = plan(db, actor, proof=True, expected_manifest=row.manifest)
        require(fresh["plan_hash"] == row.plan_hash and fresh["proof"] and fresh["proof"]["identical"],
                "EQUIVALENCE_FAILED", "The exact conversion no longer has passing proof", 409, fresh["proof"])
        row.proof = fresh['proof']
        _activate(db, actor, row, value.reason)
        row.status, row.approved_by, row.approved_at = "active", actor.identity_id, m.now()
        bump_inputs(db, actor)
    else:
        require(action in {"undo", "retire"} and row.status == "active", "WORKFLOW_STATE", "Only an active conversion can be undone", 409)
        _undo(db, actor, row, value.reason)
        row.status = "retired"
        bump_inputs(db, actor)
    row.revision += 1
    history(db, actor, "catalogue_conversion", row, value.reason)
    return {"record": _conversion_record(row)}


def _activate(db, actor, conversion, reason):
    moved = {b["source_policy_id"] for b in conversion.plan["bindings"]}
    sources = {p.id: p for p in db.scalars(select(m.Policy).where(
        m.Policy.department_id == actor.department_id, m.Policy.id.in_(moved)))}
    require(set(sources) == moved, 'NOT_FOUND', 'Record not found', 404)
    manifests = [source for source in conversion.plan['source_manifest'] if source['id'] in moved]
    retained = set(db.execute(select(m.DefinitionHistory.object_id, m.DefinitionHistory.object_revision).where(
        m.DefinitionHistory.kind == 'policy', tuple_(m.DefinitionHistory.object_id, m.DefinitionHistory.object_revision)
        .in_([(source['id'], source['revision']) for source in manifests]))).tuples())
    for source in manifests:
        old = sources[source['id']]
        require(old.revision == source["revision"], "STALE_INPUTS", "A source rule changed", 409)
    history_many(db, actor, [('policy', sources[source['id']], 'Source retained before catalogue conversion')
        for source in manifests if (source['id'], source['revision']) not in retained],
        snapshot_extra={'approval_transition': False})
    for source in manifests:
        old = sources[source['id']]
        old.status, old.active = "retired", False
        advance_revision(db, old)
    history_many(db, actor, [('policy', old, f'Catalogue conversion {conversion.id}: {reason}'[:500])
        for old in sources.values()])
    destinations = []
    for target in conversion.plan["targets"]:
        payload = s.PolicyInput.model_validate({**target['payload'],
            'definition': stored_definition(target['payload']['definition'])}).model_dump(mode='json')
        payload["starts_on"] = date.fromisoformat(payload["starts_on"])
        payload["ends_on"] = date.fromisoformat(payload["ends_on"]) if payload.get("ends_on") else None
        policy = m.Policy(id=target["row"]["id"], department_id=actor.department_id, kind="catalogue", status="active",
            created_by=conversion.proposed_by, approved_by=actor.identity_id, **payload)
        db.add(policy)
        destinations.append(('policy', policy, reason))
    for fact in conversion.plan["scheduling_facts"]:
        data = stored_definition(fact['data'])
        line = data.get("line") or {}
        row = m.SchedulingFact(id=fact["id"], department_id=actor.department_id, kind=fact["kind"],
            person_id=fact.get("person_id"), calendar_entry_id=fact.get("calendar_entry_id"), data=data,
            on=fact.get("on", True), status="active", proposed_by=conversion.proposed_by,
            approved_by=actor.identity_id, approved_at=m.now(), reason=reason, migration_proposal_id=conversion.id,
            starts_on=date.fromisoformat(line["starts_on"]) if line.get("starts_on") else None,
            ends_on=date.fromisoformat(line["ends_on"]) if line.get("ends_on") else None)
        db.add(row)
        destinations.append(('scheduling_fact', row, reason))
    history_many(db, actor, destinations, snapshot_extra={'approval_transition': True})
    for binding in conversion.plan["bindings"]:
        db.add(m.RuleSourceBinding(department_id=actor.department_id, migration_proposal_id=conversion.id,
            source_policy_id=binding["source_policy_id"], source_revision=binding["source_revision"], data=binding))
    db.flush()


def _undo(db, actor, conversion, reason):
    moved = {b["source_policy_id"] for b in conversion.plan["bindings"]}
    target_ids = {t['row']['id'] for t in conversion.plan['targets']}
    all_policies = {p.id: p for p in db.scalars(select(m.Policy).where(
        m.Policy.department_id == actor.department_id, m.Policy.id.in_(moved | target_ids)))}
    require(set(all_policies) == moved | target_ids, 'NOT_FOUND', 'Record not found', 404)
    policies = [all_policies[t['row']['id']] for t in conversion.plan['targets']]
    facts = list(db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.migration_proposal_id == conversion.id)))
    ids = {p.id for p in policies} | {f.id for f in facts}
    for policy in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id, m.Policy.status.in_(["active", "proposed"]))):
        refs = set(policy.definition.get("merged_ids") or []) | {policy.definition.get("replaces_id")}
        require(not refs & ids, "LATER_EDIT", "A later policy edit prevents simple rollback", 409)
    require(not db.scalar(select(m.SchedulingFact.id).where(m.SchedulingFact.replaces_id.in_(ids))),
            "LATER_EDIT", "A later fact edit prevents simple rollback", 409)
    require(all(p.status == "active" and p.revision == 1 for p in policies) and
            all(f.status == "active" and f.revision == 1 for f in facts), "LATER_EDIT", "A destination changed; propose a compensating conversion", 409)
    manifests = [s for s in conversion.plan["source_manifest"] if s["id"] in moved]
    sources = [all_policies[s['id']] for s in manifests]
    require(all(p.status == "retired" and p.revision == s["revision"] + 1 for p, s in zip(sources, manifests)),
            "LATER_EDIT", "A predecessor changed; propose a compensating conversion", 409)
    from .scheduling_facts import guard_goal_change
    guard_goal_change(db, actor, moved)
    for policy in policies:
        policy.status, policy.active = "retired", False
        advance_revision(db, policy)
    for fact in facts:
        fact.status, fact.revision = "retired", fact.revision + 1
    # Release active catalogue entries before restoring sources with the same entry id.
    db.flush()
    for source, manifest in zip(sources, manifests):
        source.status, source.active = manifest["status"], manifest["active"]
        advance_revision(db, source)
    history_many(db, actor, [('policy', policy, reason) for policy in policies + sources] +
        [('scheduling_fact', fact, reason) for fact in facts])
