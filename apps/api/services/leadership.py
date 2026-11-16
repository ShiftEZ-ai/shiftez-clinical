"""Read-only role context and the existing policy lifecycle's atomic ownership transfer."""
import uuid
from datetime import date
from sqlalchemy import select, tuple_
from packages.contracts import schemas as s
from packages.domain import leadership
from packages.domain.rule_catalogue_migration import plan_type_leads
from packages.domain.time import aware
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record, json_value
from apps.api.snapshots import domain_snapshot, snapshot_hash
from .common import scoped, history_many
from . import independence
from .planner_tools import PLANNER_ROLES


def ownership_id(policy_id, type_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'fmuc:lead-ownership:{policy_id}:{type_id}'))


def conversion_plan(db, actor, effective_on, *, lock=False):
    # Serialize department-wide initial ownership claims with every other claim.
    if lock:
        db.scalar(select(m.Department).where(m.Department.id == actor.department_id).with_for_update(key_share=True))
    owned = set(db.scalars(select(m.TypeLeadOwnership.assignment_type_id).where(
        m.TypeLeadOwnership.department_id == actor.department_id)))
    query = select(m.AssignmentType).where(m.AssignmentType.department_id == actor.department_id)
    rows = [record(row) for row in db.scalars(query.with_for_update() if lock else query) if row.id not in owned]
    histories = list(db.execute(select(m.DefinitionHistory.id, m.DefinitionHistory.object_id,
        m.DefinitionHistory.object_revision, m.DefinitionHistory.snapshot).where(m.DefinitionHistory.department_id == actor.department_id,
        m.DefinitionHistory.kind == 'assignment_types',
        tuple_(m.DefinitionHistory.object_id, m.DefinitionHistory.object_revision).in_(
            [(row['id'], row['revision']) for row in rows]))).mappings())
    current = {(row['id'], row['revision']): row for row in rows}
    for item in histories:
        lead = current[item['object_id'], item['object_revision']]['definition'].get('lead')
        if lead:
            saved = item['snapshot'].get('definition', {}).get('lead')
            require(saved is not None and s.TeamLead.model_validate(saved) == s.TeamLead.model_validate(lead),
                    'LEAD_HISTORY_MISMATCH', 'Retained duty type history does not match this leadership revision', 409)
    return plan_type_leads(rows, effective_on, actor.department_id, histories)


def proposal_conversion(db, actor, value, current):
    supplied = value.legacy_lead_conversion
    if value.catalogue_id != 'team_leads':
        require(supplied is None, 'SOURCE_BINDING_IMMUTABLE', 'Leadership provenance belongs to Team leads', 409)
        return None
    plan = conversion_plan(db, actor, supplied.effective_on if supplied else independence.today(db, actor.department_id), lock=True)
    if plan['legacy_lead_conversion']:
        require(current is None and supplied is not None, 'LEAD_CONVERSION_REQUIRED',
                'Preview the equivalent legacy leadership conversion with an explicit effective date first', 409)
        require(supplied.model_dump(mode='json') == plan['legacy_lead_conversion'], 'STALE_LEAD_CONVERSION',
                'Duty type leadership changed; preview the conversion again', 409)
        expected = s.CatalogueRule(kind='catalogue', id='team_leads', catalogue_version=value.catalogue_version,
                                  lines=plan['lines']).model_dump(mode='json')['lines']
        require(value.on and not value.settings and [line.model_dump(mode='json') for line in value.lines] == expected,
                'LEAD_CONVERSION_CHANGED', 'The first conversion must preserve every legacy leadership line exactly', 409)
        return plan['legacy_lead_conversion']
    previous = current.definition.get('legacy_lead_conversion') if current else None
    require(supplied is None or supplied.model_dump(mode='json') == previous,
            'SOURCE_BINDING_IMMUTABLE', 'Approved leadership provenance cannot be changed', 409)
    return previous


def activate_conversion(db, actor, policy):
    manifest = policy.definition.get('legacy_lead_conversion')
    if not manifest or policy.definition.get('merged_ids') or policy.definition.get('replaces_id'):
        return
    plan = conversion_plan(db, actor, manifest['effective_on'], lock=True)
    require(plan['legacy_lead_conversion'] == manifest, 'STALE_LEAD_CONVERSION',
            'Duty type leadership changed since proposal; propose the conversion again', 409)
    sources = manifest['sources']
    types = {row.id: row for row in db.scalars(select(m.AssignmentType).where(
        m.AssignmentType.department_id == actor.department_id, m.AssignmentType.id.in_([s['type_id'] for s in sources])))}
    # Receipts reference real immutable history even inside bulk's otherwise deferred history path.
    missing = [('assignment_types', types[source['type_id']], 'Legacy leadership retained for approved Rules ownership')
               for source in sources if source['source_history_id'] is None]
    if missing:
        history_many(db, actor, missing, immediate=True)
        db.flush()
    histories = dict(db.execute(select(m.DefinitionHistory.object_id, m.DefinitionHistory.id).where(
        m.DefinitionHistory.department_id == actor.department_id, m.DefinitionHistory.kind == 'assignment_types',
        tuple_(m.DefinitionHistory.object_id, m.DefinitionHistory.object_revision).in_(
            [(s['type_id'], s['type_revision']) for s in sources]))).all())
    db.add_all([m.TypeLeadOwnership(id=ownership_id(policy.id, source['type_id']), department_id=actor.department_id,
        assignment_type_id=source['type_id'], source_type_revision=source['type_revision'],
        source_history_id=histories[source['type_id']], source_lead_hash=source['lead_hash'],
        initial_policy_id=policy.id, initial_line_id=source['destination_line_id'],
        effective_on=date.fromisoformat(manifest['effective_on'])) for source in sources])
    db.flush()


def lead_context(db, actor, workspace_id, value: s.LeadContextInput) -> s.LeadContext:
    require_role(actor, *PLANNER_ROLES)
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    typ = scoped(db, m.AssignmentType, value.shift.assignment_type_id, actor)
    scoped(db, m.Location, value.shift.location_id, actor)
    # domain_snapshot applies the shared live-safety overlay for draft qualification.
    data = domain_snapshot(db, workspace)
    services = {ident for ident, row in data['services'].items() if row['department_id'] == actor.department_id}
    require(set(value.shift.service_ids) <= services, 'NOT_FOUND', 'Service not found in this department', 404)
    old = next((row for row in data['shifts'] if row['id'] == value.shift_id and row['id'] in data['focus_ids']), None)
    require(value.shift_id is None or old is not None, 'NOT_FOUND', 'Session not found in this rota', 404)
    shift = {**(old or {}), **value.shift.model_dump(mode='json'),
             'id': value.shift_id or str(uuid.uuid5(uuid.NAMESPACE_URL, workspace.id + snapshot_hash(value.shift.model_dump(mode='json')))), 'department_id': actor.department_id,
             'type_revision': old['type_revision'] if old and old['assignment_type_id'] == typ.id else typ.revision,
             'type_snapshot': old['type_snapshot'] if old and old['assignment_type_id'] == typ.id else record(typ)}
    # Domain input preparation is batched; unknown/foreign people never become an eligibility oracle.
    ids = list(dict.fromkeys(value.person_ids))
    members = {row['person_id'] for row in data['memberships'] if row['department_id'] == actor.department_id}
    require(set(ids) <= members, 'NOT_FOUND', 'Person not found in this department', 404)
    a, b = aware(shift['start']), aware(shift['end'])
    leadership.prepare(data)
    rows = leadership.segments(data, shift, a, b, people=members)
    segments = []
    for lo, hi, rules in rows:
        lead = leadership.summary(data, shift, lo, hi)
        segments.append({'start': lo, 'end': hi, 'enabled': bool(lead),
            'minimum': lead['minimum'] if lead else 0, 'maximum': lead['maximum'] if lead else None,
            'continuity': lead['continuity'] if lead else 'continuous',
            'sources': [leadership.source(rule) for rule in rules],
            'contradictory_bounds': bool(lead and lead['maximum'] is not None and lead['minimum'] > lead['maximum']),
            'empty_pool': bool(rules and not any(all(leadership.matches(data, pid, shift, rule, lo, hi)
                                                   for rule in rules) for pid in members))})
    people = []
    participation = {p['person_id']: p for p in shift['participants']}
    for pid in ids:
        person = participation.get(pid)
        start, end = (aware(person['start']), aware(person['end'])) if person else (a, b)
        failures = []
        for lo, hi, rules in leadership.segments(data, shift, start, end, people=[pid]):
            failed = [r for r in rules if not leadership.matches(data, pid, shift, r, lo, hi)]
            if failed:
                failures.append({'start': lo, 'end': hi, 'code': 'LEAD_NOT_QUALIFIED',
                                 'sources': [leadership.source(r) for r in failed]})
        enabled = leadership.qualified(data, pid, shift, start, end)
        if enabled and person and not leadership.covers_required(data, person, shift):
            enabled = False
            failures.append({'start': a, 'end': b, 'code': 'LEAD_NOT_QUALIFIED',
                             'sources': [leadership.source(r) for r in leadership.requirements(data, shift, a, b)
                                         if r['continuity'] == 'whole_duty']})
        if not enabled and not failures:
            failures.append({'start': start, 'end': end, 'code': 'LEAD_NOT_ENABLED', 'sources': []})
        people.append({'person_id': pid, 'can_lead': enabled, 'failures': failures})
    context = {'workspace_revision': workspace.revision, 'input_revision': data['organisation']['input_revision'],
               'segments': json_value(segments), 'people': json_value(people)}
    return s.LeadContext(**context, context_hash=snapshot_hash({'context': context, 'shift': shift,
                                                              'fingerprint': data['fingerprint']}))
