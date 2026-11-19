"""On-demand recorded facts for a planning period; never a second check or solver proof."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import Problem, require
from apps.api.snapshots import domain_snapshot, fingerprint, snapshot_hash
from packages.contracts import schemas as s
from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.generation_scope import has_population, population_allows, selected_session_ids
from packages.domain.recurrence import inactive
from packages.domain.rota_summary import normalized_check, requirement_references
from packages.domain.time import aware, local_midnight
from packages.domain.validation import prepare_assignment_trials
from .common import scoped
from .planner_tools import PLANNER_ROLES
from .rota_summary import reusable_check
from .staffing import MAX_DIRECTORY_MATCHES, MAX_SCOPE_SHIFTS


def context(db, actor, workspace_id, value):
    require_role(actor, *PLANNER_ROLES)
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    count = db.scalar(select(func.count()).select_from(m.Shift).where(m.Shift.workspace_id == workspace.id))
    require(count <= MAX_SCOPE_SHIFTS, 'STAFFING_SCOPE_LIMIT', 'Narrow the planning period to inspect recorded facts.', 409)
    people_count = db.scalar(select(func.count()).select_from(m.Person).where(m.Person.organisation_id == actor.organisation_id))
    require(people_count <= MAX_DIRECTORY_MATCHES, 'STAFFING_DIRECTORY_LIMIT',
            'The recorded directory exceeds the supported context size; no complete readiness claim is available.', 409)
    source = domain_snapshot(db, workspace, mandatory_rules=True)
    current = source['fingerprint']
    require(current == value.expected_fingerprint, 'STALE_INPUTS', 'Planning inputs changed; reload the rota.', 409)
    if value.generation_scope is not None:
        from .policies import condition_references
        try:
            condition_references(db, actor, value.generation_scope.conditions)
        except Problem as error:
            raise Problem(422, 'INVALID_SCOPE', 'Choose a population from this department.') from error
    require(len(source['shifts']) <= MAX_SCOPE_SHIFTS * 2, 'STAFFING_SCOPE_LIMIT', 'Narrow the planning period to inspect recorded facts.', 409)
    require(set(value.session_ids) <= set(source['focus_ids']), 'INVALID_SCOPE', 'Choose sessions from this rota.')
    source['generation_session_ids'] = value.session_ids or source['focus_ids']
    if value.generation_scope is not None:
        source['generation_scope'] = value.generation_scope.model_dump(mode='json')
        require(has_population(source), 'INVALID_SCOPE', 'No people match over the selected duty intervals.')
    source = prepare_assignment_trials(source)
    selected = set(selected_session_ids(source))
    shifts = [shift for shift in source['shifts'] if shift['id'] in selected and not inactive(shift)]
    checked = reusable_check(workspace.validation, current, source['department'].get('generation_defaults'))
    checked = checked and {row['shift_id'] for row in workspace.validation['coverage']} == set(source['focus_ids'])
    result = {'workspace_id': workspace.id, 'fingerprint': current,
        'context_fingerprint': snapshot_hash([current, sorted(selected), source.get('generation_scope')]),
        'scope_window': {'start': workspace.starts_on, 'end': workspace.ends_on},
        'generation_scope': source.get('generation_scope'), 'session_ids': sorted(selected), 'sessions': len(shifts),
        'checked_at': workspace.validation['evaluated_at'] if checked else None, 'checked': bool(checked),
        'complete': False, 'basis': 'known_records_not_solver_proof', 'records_coverage': 'assigned_people_only',
        'groups': [], 'check_action': 'workspace.validate'}
    return workspace, source, shifts, result


def candidate(source, shift, person_id):
    row = candidate_evidence(source, person_id, shift)
    retained = any(p['person_id'] == person_id for p in shift['participants'])
    reasons = list(row['exclusions'])
    if retained:
        reasons = [r for r in reasons if r['code'] != 'OVERTIME_NOT_ALLOWED']
    if not retained and not population_allows(source, shift, person_id):
        reasons.append({'code': 'OUTSIDE_GENERATION_POPULATION'})
    return {'person_id': person_id, 'start': shift['start'], 'end': shift['end'],
            'retained': retained, 'exclusions': reasons}


def readiness(db, actor, workspace_id, value):
    _, source, shifts, result = context(db, actor, workspace_id, value)
    groups = {}
    for shift in shifts:
        for pid in {p['person_id'] for p in shift['participants']}:
            for reason in candidate(source, shift, pid)['exclusions']:
                key = (reason['code'], reason.get('service_id'), reason.get('rule_id'),
                       shift['assignment_type_id'], shift['start'], shift['end'])
                groups.setdefault(key, set()).add(pid)
    result['groups'] = [{'code': code, 'service_id': service, 'rule_id': rule, 'assignment_type_id': typ,
        'start': start, 'end': end, 'people': len(ids),
        'sample_person_ids': sorted(ids)[:s.AUTOSCHEDULE_PRECHECK_LIMITS['samples']]}
        for (code, service, rule, typ, start, end), ids in groups.items()]
    return result


def exclusion(source, actor, shift, person_id, reason):
    """Related same-department dated records only; private absences supply no identity or reason."""
    code = reason['code']
    collection = {'MEMBERSHIP_MISSING': 'memberships', 'RANK_NOT_ALLOWED': 'memberships',
        'ELIGIBILITY_MISSING': 'eligibilities', 'CAPABILITY_NOT_GRANTED': 'grants',
        'EXTERNAL_ROTATION': 'rotations'}.get(code)
    rows = source['_person_rows'].get(collection, {}).get(person_id, ()) if collection else ()
    rows = [row for row in rows if row.get('department_id') == actor.department_id
        and (not reason.get('service_id') or row.get('service_id') == reason['service_id'])
        and (not reason.get('capability_id') or row.get('capability_id') == reason['capability_id'])]
    if code == 'ELIGIBILITY_MISSING' and rows and all(row.get('ends_on') and
            local_midnight(date.fromisoformat(row['ends_on']), source['organisation']['timezone'])
            <= aware(shift['start']) for row in rows):
        code = 'ELIGIBILITY_ENDED'
    refs = [{'collection': collection, **{key: row[key] for key in s.PlanningRecordReference.model_fields
                                         if key != 'collection' and key in row}} for row in rows]
    action = {'ELIGIBILITY_MISSING': ('open_eligibility', 'eligibility.create'),
              'ELIGIBILITY_ENDED': ('open_eligibility', 'eligibility.create'),
              'UNAVAILABLE': ('open_leave', 'availability.update'),
              'RESTRICTION': ('review_assignments', 'rule.propose')}.get(code)
    roles = s.PERMISSIONS[action[1]] if action else s.PERMISSIONS['membership.create'] if code in {
        'MEMBERSHIP_MISSING', 'RANK_NOT_ALLOWED'} else ()
    hints = []
    if action and actor.permits(*roles):
        zone = ZoneInfo(source['organisation']['timezone'])
        hints = [{'action': action[0], 'prefill': {
            'starts_on': aware(shift['start']).astimezone(zone).date().isoformat(),
            'ends_on': ((aware(shift['end']).astimezone(zone) - timedelta(microseconds=1)).date() + timedelta(days=1)).isoformat(),
            'person_id': person_id, 'shift_ids': [shift['id']], 'rule_id': reason.get('rule_id'),
            'service_ids': [reason['service_id']] if reason.get('service_id') else shift['service_ids'],
            'assignment_type_ids': [shift['assignment_type_id']]}}]
    return {'code': code, **{key: reason[key] for key in ('service_id', 'rule_id', 'capability_id', 'allowed_category_ids') if key in reason},
            'related_records': refs, 'owner_roles': list(roles), 'fix_hints': hints}


def evidence(db, actor, workspace_id, observation_id, value):
    workspace, source, shifts, result = context(db, actor, workspace_id, value)
    require(result['checked'], 'CHECK_REQUIRED', 'Run the current rota check before inspecting this requirement.', 409)
    check = normalized_check(workspace.validation)
    _, observations, _ = requirement_references(check['findings'], set())
    observation = next((o for o in observations if o['id'] == observation_id), None)
    require(observation is not None, 'NOT_FOUND', 'Requirement not found in this rota check.', 404)
    shift = next((row for row in shifts if row['id'] == observation['shift_id']), None)
    require(shift is not None, 'INVALID_SCOPE', 'Select the duty named by this requirement.')
    # Duty qualification is not a scoped/pooled rule proof; keep the requirement and interval explicit.
    people = sorted({r['person_id'] for r in source['memberships'] if r['department_id'] == actor.department_id}
                    & source['people'].keys())
    rows = []
    for pid in people[value.offset:value.offset + value.limit]:
        row = candidate(source, shift, pid)
        rows.append({**row, 'exclusions': [exclusion(source, actor, shift, pid, r) for r in row['exclusions']]})
    primary = next((f for f in check['findings'] if requirement_references([f], set())[1][0]['id'] == observation_id), None)
    hints = (primary or {}).get('fix_hints', []) if actor.permits('coordinator') else []
    return {**result, 'records_coverage': 'paged_duty_candidates', 'observation': observation,
        'candidates': rows, 'total': len(people), 'offset': value.offset, 'limit': value.limit,
        'has_more': value.offset + len(rows) < len(people), 'requirement_scope_complete': False, 'fix_hints': hints}
