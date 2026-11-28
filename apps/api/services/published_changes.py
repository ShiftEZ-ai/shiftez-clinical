"""Publication deltas share the draft validator and the ordinary independent review workflow."""
import copy
import json
from collections import defaultdict
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.db import current_request_parent
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import domain_snapshot, project_series_shifts, snapshot_hash, workspace_content, workspace_shifts
from packages.contracts import schemas as s
from packages.domain.time import aware, local_midnight
from packages.domain.participation import replaced_participants
from packages.domain.assignment_override import assignment_override_allowed
from packages.domain.candidate_evidence import candidate_evidence
from .common import bump_inputs, expected, history, outbox, scoped
from .workspace_access import access_record
from . import planning, reviews, schedule, swaps
from .planning_preparation import command_id
from .leave_disclosure import leave_wording


def slot(row):
    return (row['assignment_type_id'], row['location_id'], aware(row['start']), aware(row['end']),
            tuple(sorted(row['service_ids'])))


def pairs(before, after):
    """Identity first; legacy clones use recurrence/content before singleton slot fallback."""
    old, new = list(before), list(after)
    for key_of, identical in ((lambda r: r.get('lineage_id') or r['id'], False),
                   (lambda r: (r['series_id'], r.get('occurrence_on')) if r.get('series_id') else None, False),
                   (lambda r: snapshot_hash([swaps.semantic_content([r]), r.get('series_snapshot')]), True),
                   (slot, False)):
        a, b = defaultdict(list), defaultdict(list)
        for row in old:
            key = key_of(row)
            if key is not None:
                a[key].append(row)
        for row in new:
            key = key_of(row)
            if key is not None:
                b[key].append(row)
        matched_old, matched_new = set(), set()
        for key in sorted(a.keys() & b.keys(), key=str):
            # Identical legacy duplicates are interchangeable; ambiguous changed rows stay explicit.
            count = min(len(a[key]), len(b[key])) if identical else 0
            if len(a[key]) == len(b[key]) == 1:
                count = 1
            for left, right in zip(a[key][:count], b[key][:count]):
                matched_old.add(left['id'])
                matched_new.add(right['id'])
                yield left, right
        old = [r for r in old if r['id'] not in matched_old]
        new = [r for r in new if r['id'] not in matched_new]
    yield from ((r, None) for r in old)
    yield from ((None, r) for r in new)


def delta(before, after):
    return [{'before': a, 'after': b} for a, b in pairs(before, after)
            if a is None or b is None or swaps.semantic_content([a]) != swaps.semantic_content([b])
            or a.get('series_snapshot') != b.get('series_snapshot') or a.get('closed', False) != b.get('closed', False)]


def active_change(change):
    return change['status'] != 'withdrawn'


def owns_publication_changes(db, workspace, publication):
    row = change_list(db, publication.id)
    if row and (row.workspace_id == workspace.id or any(c['workspace_id'] == workspace.id for c in row.changes)):
        return True
    if db.scalar(select(m.SwapRequest.id).where(
        m.SwapRequest.workspace_id == workspace.id,
        m.SwapRequest.publication_id == publication.id)):
        return True
    return (not row or not any(active_change(c) for c in row.changes)) and (
        getattr(workspace, 'base_publication_id', None) == publication.id
        and getattr(workspace, 'archived_at', None) is None
        and getattr(workspace, 'status', None) in schedule.LIVE)


def validation_scope(db, workspace, data, *, own=None):
    if not getattr(workspace, 'base_publication_id', None):
        return False
    publication = db.get(m.Publication, workspace.base_publication_id)
    if publication is None or publication.department_id != workspace.department_id:
        return False
    before = schedule.in_force_snapshot(db, publication)['shifts']
    preview = own is not None
    own = own if own is not None else [r for r in data['shifts'] if r.get('workspace_id') == workspace.id]
    changes = delta(before, own)
    changed = {r['after']['id'] for r in changes if r['after']}
    people = {p['person_id'] for r in changes for sh in (r['before'], r['after']) if sh for p in sh['participants']}
    spans = [(aware(sh['start']), aware(sh['end'])) for r in changes for sh in (r['before'], r['after']) if sh]
    # Pooled cover and supervision depend on simultaneous peers, even if a removed session no longer exists.
    from packages.domain.validation_scope import affected_sessions
    checks = changed | affected_sessions(own, [sh for r in changes for sh in (r['before'], r['after']) if sh])
    focus = checks | {row['id'] for row in data['shifts'] if any(p['person_id'] in people for p in row['participants'])}
    issued = {b['id'] for a, b in pairs(before, own) if a and b
              and a['type_revision'] == b['type_revision'] and a['type_snapshot'] == b['type_snapshot']}
    data['_issued_type_versions'] = issued
    if not preview and not owns_publication_changes(db, workspace, publication):
        return False
    data.update(focus_ids=sorted(focus), person_rule_subjects=people, quota_subjects=people,
                _published_session_checks=checks, _issued_type_versions=issued)
    if spans:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(data['organisation']['timezone'])
        data['quota_dates'] = (min(a for a, _ in spans).astimezone(zone).date().isoformat(),
                               max(b for _, b in spans).astimezone(zone).date().isoformat())
    return True


def retain_issued_checks(db, workspace, data, result):
    """Carry the unchanged issued checks forward; never re-evaluate them under today's configuration."""
    publication = db.get(m.Publication, workspace.base_publication_id)
    own = [r for r in data['shifts'] if r.get('workspace_id') == workspace.id]
    checks, people = data['_published_session_checks'], data['person_rule_subjects']
    mapping = {a['id']: b['id'] for a, b in pairs(publication.snapshot['shifts'], own)
               if a and b and b['id'] not in checks and swaps.semantic_content([a]) == swaps.semantic_content([b])
               and a.get('series_snapshot') == b.get('series_snapshot')}
    retained = [{**f, 'shift_id': mapping.get(f.get('shift_id'))} for f in publication.validation.get('findings', [])
                if (f.get('shift_id') is None or f['shift_id'] in mapping) and f.get('person_id') not in people
                and f['code'] != 'TYPE_VERSION_CHANGED']
    findings = retained + result['findings']
    covered = {r['shift_id'] for r in result['coverage']}
    coverage = [{**r, 'shift_id': mapping[r['shift_id']]} for r in publication.validation.get('coverage', [])
                if r['shift_id'] in mapping and mapping[r['shift_id']] not in covered] + result['coverage']
    from packages.domain.quotas import quota_identity
    old_report, new_report = publication.validation.get('quota_report'), result.get('quota_report')
    report = copy.deepcopy(new_report or old_report)
    if report:
        fresh = {(quota_identity(r), r['person_id']) for r in (new_report or {}).get('rows', [])}
        lo, hi = data.get('quota_dates', (workspace.starts_on.isoformat(), workspace.ends_on.isoformat()))
        report['rows'] = [r for r in (old_report or {}).get('rows', [])
                          if (r['person_id'] not in people or r['ends_on'] <= lo or hi < r['starts_on'])
                          and (quota_identity(r), r['person_id']) not in fresh] + (new_report or {}).get('rows', [])
        result = {**result, 'quota_report': report}
    from packages.domain.fairness import goal_reports, quota_goal_reports
    return {**result, 'findings': findings, 'coverage': coverage,
            # Inherited publication evidence stays intact, but it is not a new
            # amendment cause. The Fix list/toast describes only current checks.
            'change_findings': result['findings'],
            'goals': goal_reports({**data, 'focus_ids': [sh['id'] for sh in own]}) + quota_goal_reports(data, report or {}),
            'error_count': sum(f['severity'] == 'error' for f in findings),
            'warning_count': sum(f['severity'] == 'warning' for f in findings),
            'retained_checks': {'publication_id': publication.id, 'session_count': len(mapping)}}


def urgent_route(db, workspace):
    trial = SimpleNamespace(id=workspace.id, department_id=workspace.department_id,
        starts_on=workspace.starts_on, ends_on=workspace.ends_on, urgency='urgent')
    from apps.api.errors import Problem
    try:
        route = reviews.resolve_route(db, trial)
    except Problem:
        return {'active': False, 'approver_count': 0}
    roles = {step['role'] for step in route['steps']}
    from apps.api.auth import department_roles
    holders = department_roles(db, workspace.department_id)
    ids = [key for key, held in holders.items() if held & roles]
    identities = db.scalars(select(m.Identity).where(m.Identity.id.in_(ids), m.Identity.disabled.is_(False)))
    count = len({person.person_id or person.id for person in identities})
    return {'active': True, 'approver_count': count, 'route_id': route['route_id'],
            'route_revision': route['route_revision']}


def _disclosed_change(change, db, actor):
    if 'absence_reason' not in change:
        return change
    if actor is None:
        return {key: value for key, value in change.items() if key not in {'absence_reason', 'reason'}}
    department = current_request_parent(db, m.Department, actor.department_id)
    row = SimpleNamespace(kind='absence_report', person_id=None, private_reason=change.get('absence_reason'),
                          details={'reporter_category': change.get('absence_reason')})
    wording = leave_wording(department, actor, row, own=False)
    return {key: value for key, value in change.items()
            if key not in {'absence_reason', 'reason'}} | (
        {'absence_reason': wording['absence_reason']} if wording.get('absence_reason') else {})


def review_payload(db, workspace, actor=None, *, projected_shifts=None):
    publication = db.get(m.Publication, workspace.base_publication_id) if workspace.base_publication_id else None
    sessions = []
    if publication:
        before = schedule.in_force_snapshot(db, publication)['shifts']
        after = (projected_shifts if projected_shifts is not None
                 else project_series_shifts(db, workspace, workspace_shifts(db, workspace.id)))
        sessions = delta(before, after)
        if projected_shifts is not None:
            # The full response decorates participants after projecting the review diff.
            sessions = copy.deepcopy(sessions)
    org = db.get(m.Organisation, db.get(m.Department, workspace.department_id).organisation_id)
    first = min((aware(sh['start']).astimezone(ZoneInfo(org.timezone)).date()
                 for row in sessions for sh in (row['before'], row['after']) if sh), default=None)
    week = first - timedelta(days=(first.weekday() - org.week_start) % 7) if first else None
    # ME-26: each changed session names the change(s) behind it (kind and the coordinator's reason, e.g. Sick), so the
    # approver reads why beside was/now. A submitted change's sessions are the candidate's (submitted_session_ids).
    row = change_list(db, publication.id) if publication else None
    why = {}
    for c in row.changes if row else []:
        if c.get('workspace_id') == workspace.id and active_change(c):
            for sid in {sh['id'] for sh in c['after']} | set(c.get('submitted_session_ids', {}).values()):
                disclosed = _disclosed_change(c, db, actor)
                why.setdefault(sid, []).append({'kind': disclosed['kind'], **(
                    {'absence_reason': disclosed['absence_reason']} if disclosed.get('absence_reason') else {})})
    sessions = [{**r, 'changes': why.get((r['after'] or {}).get('id'), [])} for r in sessions]
    return {'change_diff': {'base_publication_id': workspace.base_publication_id, 'sessions': sessions,
                            'first_changed_date': first.isoformat() if first else None,
                            'review_week_start': week.isoformat() if week else None} if publication else None,
            'urgent_route': urgent_route(db, workspace)}


def change_list(db, publication_id):
    return db.scalar(select(m.PublicationChangeList).where(m.PublicationChangeList.publication_id == publication_id))


def pending(db, actor, publication_id):
    require_role(actor, 'coordinator', 'approver', 'programme_lead', 'auditor')
    publication = scoped(db, m.Publication, publication_id, actor)
    row = change_list(db, publication.id)
    workspace = db.get(m.Workspace, row.workspace_id) if row else db.get(m.Workspace, publication.workspace_id)
    projected = row
    if row:
        candidates = {c['workspace_id'] for c in row.changes if c['status'] in {'submitted', 'approved'}}
        # Older returns updated only the candidate. Read its authoritative note
        # without rewriting the already-recorded review or publication evidence.
        notes = dict(db.execute(select(m.Workspace.id, m.Workspace.return_note).where(
            m.Workspace.id.in_(candidates), m.Workspace.status == 'draft')).all()) if candidates else {}
        projected = SimpleNamespace(changes=[{**c, 'return_note': notes[c['workspace_id']]}
            if notes.get(c['workspace_id']) and c['status'] in {'submitted', 'approved'} else c for c in row.changes])
    from .common import visible_rota_name
    workspace_record = ({**record(workspace), 'name': visible_rota_name(db, actor, workspace.id, workspace.name)} if row else None)
    return {'workspace': workspace_record, 'changes': public_changes(projected, workspace.validation, db=db, actor=actor),
            'urgent_route': urgent_route(db, workspace)}


def public_changes(row, validation=None, *, db=None, actor=None):
    from packages.domain.rota_summary import rota_summary
    places = {r['shift_id']: r['places_open'] for r in rota_summary(validation)['sessions']} if validation else {}
    rows = []
    for change in row.changes if row else []:
        change = _disclosed_change(change, db, actor) if db is not None else change
        sessions = []
        aliases = change.get('submitted_session_ids', {})
        old = {r['id']: r for r in change['before']}
        people = set()
        for sh in change['after']:
            before = old.get(sh['id']) or {}
            first = [p['person_id'] for p in before.get('participants', [])]
            last = [p['person_id'] for p in sh['participants']]
            people.update(first + last)
            definition = ((sh.get('series_snapshot') or {}).get('definition') or
                          (sh.get('type_snapshot') or {}).get('definition') or {})
            closure_configured = bool(definition.get('close_when_all_usual_unavailable'))
            # A session closes only through the explicit coordinator close/cancel
            # command. `closes_session` is the warning fact for an empty result,
            # so an absence keeps the place open even when the retired setting is on.
            explicitly_closed = bool(sh.get('cancelled', False))
            # A cancelled session retains assignment evidence, but nobody is still
            # on duty for it.  `closes_session` is the warning fact, never the
            # persisted closure state.
            closes = explicitly_closed or not last
            sessions.append({key: sh[key] for key in ('id', 'start', 'end', 'location_id', 'service_ids')} |
                            {'before_person_ids': first, 'after_person_ids': last, 'cancelled': sh['cancelled'],
                             'closed': bool(sh.get('closed', False)), 'closes_session': closes,
                             'closure_configured': closure_configured,
                             'closure_reason': sh.get('closure_reason'),
                             'removes_needed_place': explicitly_closed,
                             'open_places': 0 if explicitly_closed or sh.get('closed', False) else
                                 places.get(aliases.get(sh['id'], sh['id']), places.get(sh['id'], max(0, len(first) - len(last)))),
                             'replace_available': bool(first) and not explicitly_closed and not sh.get('closed', False)})
        ids = {sh['id'] for sh in change['after']} | set(aliases.values())
        context = (validation or {}).get('fix_list_context') or {}
        zone = ZoneInfo(context.get('timezone', 'UTC'))
        shifts = {sh['id']: sh for sh in change['before'] + change['after']}
        shifts.update({candidate: shifts[hidden] for hidden, candidate in aliases.items() if hidden in shifts})
        findings = []
        for f in (validation or {}).get('findings', []):
            if f.get('shift_id') not in ids and f.get('person_id') not in people:
                continue
            detail = f.get('detail') or {}
            source = context.get('sources', {}).get(json.dumps([f.get('rule_id'), detail.get('line'), detail.get('slot')]), {})
            sh = shifts.get(f.get('shift_id'), {})
            start, end = f.get('start') or sh.get('start'), f.get('end') or sh.get('end')
            findings.append({**f, 'person_name': context.get('people', {}).get(f.get('person_id')),
                'starts_on': aware(start).astimezone(zone).date().isoformat() if start else context.get('workspace', {}).get('starts_on'),
                'ends_on': aware(end).astimezone(zone).date().isoformat() if end else context.get('workspace', {}).get('ends_on'),
                'level': 'must' if f['severity'] == 'error' else 'could', 'rule_name': source.get('name') or f.get('message')})
        returned = change.get('return_note')
        status = change.get('status', 'draft')
        editable = status == 'draft' or status in {'submitted', 'approved'} and bool(returned)
        rows.append({key: val for key, val in change.items() if key not in {'before', 'after', 'availability_ids', 'absences', 'submitted_session_ids', 'return_note'}} |
                    {'status': 'submitted' if returned and status == 'draft' else status,
                     'returned': returned,
                     'can_edit': editable, 'can_undo': editable, 'can_resubmit': editable,
                     'sessions': sessions, 'must_fix': [f for f in findings if f['severity'] == 'error'], 'findings': findings})
    return rows


def new_workspace(db, actor, publication):
    from .rota_adjustments import inherited_adjustments
    workspace = m.Workspace(department_id=actor.department_id, created_by=actor.identity_id,
        last_edited_by=actor.identity_id, name=db.get(m.Workspace, publication.workspace_id).name,
        starts_on=publication.starts_on, ends_on=publication.ends_on, base_publication_id=publication.id,
        target_overrides=inherited_adjustments(publication, 'target_overrides'),
        rule_adjustments=inherited_adjustments(publication, 'rule_adjustments'))
    db.add(workspace)
    db.flush()
    from apps.api.workspace_baselines import capture_draft_baseline
    capture_draft_baseline(db, workspace, actor.identity_id,
                           reason="Captured draft baseline for published-change workspace")
    planning.copy_snapshot_into(db, actor, workspace, schedule.in_force_snapshot(db, publication))
    return workspace


def mapped_shift(rows, issued):
    matches = [row for row in rows if (row.get('lineage_id') or row['id']) == (issued.get('lineage_id') or issued['id'])]
    if not matches and issued.get('series_id'):
        matches = [row for row in rows if row.get('series_id') == issued['series_id'] and row.get('occurrence_on') == issued.get('occurrence_on')]
    if not matches:
        matches = [row for row in rows if swaps.semantic_content([row]) == swaps.semantic_content([issued])]
    if not matches:
        matches = [row for row in rows if slot(row) == slot(issued)]
    require(len(matches) == 1, 'CHANGE_SESSION_AMBIGUOUS', 'This session changed; reload the current rota', 409)
    return matches[0]


def participant(rows, assignment):
    matches = [r for r in rows if r['person_id'] == assignment.person_id
               and aware(r['start']) == aware(assignment.start) and aware(r['end']) == aware(assignment.end)]
    require(len(matches) == 1, 'CHANGE_ASSIGNMENT_CHANGED', 'This assignment changed; reload the change list', 409)
    return matches[0]


def restore_rows(db, actor, workspace, rows):
    """Restore only owned draft rows, retaining their IDs and the existing shift writer's provenance."""
    for data in rows:
        row = db.get(m.Shift, data['id'])
        require(row is not None and row.workspace_id == workspace.id, 'NOT_FOUND', 'Session not found', 404)
        db.execute(delete(m.Assignment).where(m.Assignment.shift_id == row.id))
        db.flush()
        row.cancelled, row.cancellation_reason = data['cancelled'], data.get('cancellation_reason')
        row.series_modified = data.get('series_modified', False)
        row.series_snapshot = data.get('series_snapshot')
        row.revision += 1
        for p in data['participants']:
            db.add(m.Assignment(**{key: val for key, val in p.items() if key in m.Assignment.__table__.columns
                                  and key not in {'created_at', 'start', 'end', 'source_at'}},
                                start=aware(p['start']), end=aware(p['end']), created_at=aware(p['created_at']),
                                source_at=aware(p['source_at']) if p.get('source_at') else None))
    db.flush()


def change_assignment_blockers(data, before, after):
    old = {row['id']: {p['id']: p['person_id'] for p in row['participants']} for row in before}
    findings = []
    for row in after:
        previous = old.get(row['id'], {})
        for participant in row['participants']:
            if previous.get(participant['id']) == participant['person_id']:
                continue
            findings.extend(finding for finding in candidate_evidence(data, participant['person_id'], row)['exclusions']
                            if finding['code'] in {'UNAVAILABLE', 'EXTERNAL_ROTATION'}
                            and not assignment_override_allowed(data, finding, change_list=True))
    return findings


def command(db, actor, command, settings):
    if command.kind == 'absence_report.settings':
        return save_report_settings(db, actor, command)
    require_role(actor, 'coordinator')
    require(command.kind in {'publication.change', 'publication.change.undo', 'publication.changes.prepare',
            'publication.changes.submit', 'publication.changes.withdraw'}, 'UNKNOWN_COMMAND', 'Unsupported published change command')
    publication = scoped(db, m.Publication, command.target_id, actor)
    require(publication.current, 'SUPERSEDED_BASE', 'Open the current published rota', 409)
    row = change_list(db, publication.id)
    if row is None:
        require(command.kind in {'publication.change', 'publication.changes.prepare'}, 'NOT_FOUND', 'No changes have been started', 404)
        workspace = new_workspace(db, actor, publication)
        row = m.PublicationChangeList(department_id=actor.department_id, publication_id=publication.id,
                                     workspace_id=workspace.id, changes=[])
        db.add(row)
        db.flush()
    else:
        workspace = planning.draft(db, actor, row.workspace_id, command.expected_revision)
    if command.kind == 'publication.changes.prepare':
        s.Decision.model_validate(command.payload)
        return {**pending(db, actor, publication.id), 'shifts': workspace_shifts(db, workspace.id)}
    if command.kind == 'publication.changes.withdraw':
        value = s.Decision.model_validate(command.payload)
        submitted = {c['workspace_id'] for c in row.changes if c['status'] in {'submitted', 'approved'}}
        returned = {c['workspace_id'] for c in row.changes if c.get('return_note') and c['status'] == 'draft'}
        candidates = submitted | returned
        require(len(candidates) == 1, 'CHANGE_IN_REVIEW', 'There is no submitted or returned change set', 409)
        candidate = db.get(m.Workspace, next(iter(candidates)))
        if candidate.status != 'draft':
            planning.lifecycle(db, actor, s.Command(kind='workspace.reopen', target_id=candidate.id,
                expected_revision=candidate.revision, payload={'reason': value.reason}))
        candidate.archived_at = m.now()
        row.changes = [{**c, 'status': 'draft', 'workspace_id': workspace.id, 'urgency': None, 'submitted_at': None,
                        'return_note': None}
                       if c['workspace_id'] == candidate.id else c for c in row.changes]
        row.revision += 1
        workspace.revision += 1
        history(db, actor, 'publication_change_lists', row, value.reason)
        return pending(db, actor, publication.id)
    if command.kind == 'publication.changes.submit':
        return submit(db, actor, row, workspace, command)
    if command.kind == 'publication.change.undo':
        return undo(db, actor, row, workspace, command)
    value = s.PublicationChangeInput.model_validate(command.payload)
    issued = next((r for r in schedule.in_force_snapshot(db, publication)['shifts'] if r['id'] == value.shift_id), None)
    require(issued is not None and aware(issued['end']) > m.now(), 'NOT_FOUND', 'Choose a current published session', 404)
    rows = workspace_shifts(db, workspace.id)
    source = mapped_shift(rows, issued)
    require(not source['locked'] and not source['cancelled'], 'ASSIGNMENT_LOCKED', 'This session cannot be changed', 409)
    target = None
    if value.kind in {'move', 'swap'}:
        destination = next((r for r in publication.snapshot['shifts'] if r['id'] == value.target_shift_id), None)
        require(destination is not None, 'NOT_FOUND', 'Choose a target session on this publication', 404)
        target = mapped_shift(rows, destination)
        require(target['id'] != source['id'] and not target['locked'] and not target['cancelled'],
                'INVALID_CHANGE', 'Choose a different open, unlocked session', 409)
    ids = {source['id']} | ({target['id']} if target else set())
    require(not any(c['status'] in {'submitted', 'approved'} and ids & {r['id'] for r in c['after']} for c in row.changes),
            'CHANGE_IN_REVIEW', 'Withdraw the submitted change before editing its sessions', 409)
    before = workspace_content(db, workspace)
    kept = copy.deepcopy([source, *([target] if target else [])])
    changed = copy.deepcopy(kept)
    source = changed[0]
    selected = None
    if value.kind in {'replace', 'absence', 'move', 'swap'}:
        assignment = db.get(m.Assignment, value.assignment_id)
        require(assignment is not None and assignment.shift_id == issued['id'], 'NOT_FOUND', 'Choose a person on this session', 404)
        selected = participant(source['participants'], assignment)
    if value.kind == 'cancel':
        require(value.assignment_id is None, 'SESSION_ACTION_REQUIRED', 'Cancel session is a session-level action')
        source.update(cancelled=True, cancellation_reason=value.reason)
    elif value.kind == 'absence':
        source['participants'].remove(selected)
    elif value.kind in {'replace', 'add'}:
        require(value.person_id is not None, 'PERSON_REQUIRED', 'Choose a replacement')
        from apps.api.auth import person_access
        person_access(db, actor, value.person_id)
        require(value.person_id not in {p['person_id'] for p in source['participants']}, 'DUPLICATE_PARTICIPATION', 'Already assigned')
        if selected:
            source['participants'] = replaced_participants(source['participants'], {selected['id']: value.person_id})
        else:
            source['participants'].append({**source['participants'][0], 'id': command_id(db), 'person_id': value.person_id,
                'start': source['start'], 'end': source['end'], 'fixed': False, 'supervisor_person_id': None,
                'team_lead': False, 'segments': []} if source['participants'] else {
                    'id': command_id(db), 'shift_id': source['id'], 'person_id': value.person_id, 'revision': 1,
                    'created_at': m.now().isoformat(), 'start': source['start'], 'end': source['end'],
                    'participation': 'staff', 'overtime': False, 'fixed': False, 'supervisor_person_id': None,
                    'team_lead': False, 'segments': [], **planning.assignment_source(actor, 'manual')})
    elif value.kind == 'move':
        moved = {**selected, 'id': command_id(db), 'shift_id': changed[1]['id'], 'start': changed[1]['start'],
                 'end': changed[1]['end'], 'segments': [], 'team_lead': False}
        source['participants'].remove(selected)
        changed[1]['participants'].append(moved)
    elif value.kind == 'swap':
        other = db.get(m.Assignment, value.target_assignment_id)
        require(other is not None and other.shift_id == destination['id'], 'NOT_FOUND', 'Choose a target assignment', 404)
        other = participant(changed[1]['participants'], other)
        replacements = {selected['id']: other['person_id'], other['id']: selected['person_id']}
        for sh in changed:
            sh['participants'] = replaced_participants(sh['participants'], replacements)
    for sh in changed:
        sh['series_modified'] = bool(sh.get('series_id')) or sh.get('series_modified', False)
        if value.kind in {'replace', 'add', 'move', 'swap'} and sh.get('series_snapshot'):
            sh['series_snapshot'] = {**sh['series_snapshot'], 'definition': {**sh['series_snapshot']['definition'], 'close_when_all_usual_unavailable': False}}
    if value.kind in {'replace', 'add', 'move', 'swap'}:
        blockers = change_assignment_blockers(domain_snapshot(db, workspace), kept, changed)
        require(not blockers, 'PERSON_UNAVAILABLE',
                'A published change cannot assign someone during leave or an external rotation', 409,
                {'findings': blockers})
    restore_rows(db, actor, workspace, changed)
    absences = []
    if value.kind == 'absence' or value.kind == 'replace' and value.absence_reason:
        old = db.get(m.Assignment, value.assignment_id)
        absences.append({'id': command_id(db), 'person_id': old.person_id, 'start': aware(old.start).isoformat(),
                         'end': aware(old.end).isoformat(), 'kind': 'actual_absence',
                         'private_reason': value.absence_reason or value.reason})
    change = {'id': command_id(db), **value.model_dump(mode='json'), 'status': 'draft', 'workspace_id': workspace.id,
              'created_at': m.now().isoformat(), 'actor_id': actor.identity_id, 'before': kept, 'after': workspace_shifts(db, workspace.id),
              'availability_ids': [], 'absences': absences}
    change['after'] = [r for r in change['after'] if r['id'] in ids]
    row.changes = [*row.changes, change]
    result = planning.changed(db, actor, workspace, before, value.reason)
    states = {sh['id']: sh for sh in project_series_shifts(db, workspace, workspace_shifts(db, workspace.id))}
    change['after'] = [states[sh['id']] for sh in change['after']]
    row.changes = [*row.changes[:-1], change]
    row.revision += 1
    history(db, actor, 'publication_change_lists', row, value.reason)
    return {**result, 'change': public_changes(SimpleNamespace(changes=[change]), result['validation'], db=db, actor=actor)[0],
            'changes': public_changes(row, result['validation'], db=db, actor=actor),
            'urgent_route': urgent_route(db, workspace), **review_payload(db, workspace, actor)}


def undo(db, actor, row, workspace, command):
    value = s.PublicationChangeUndo.model_validate(command.payload)
    changes = copy.deepcopy(row.changes)
    change = next((c for c in changes if c['id'] == value.change_id), None)
    require(change is not None and change['status'] == 'draft', 'CHANGE_IN_REVIEW', 'Only an unsent change can be undone', 409)
    ids = {r['id'] for r in change['after']}
    index = changes.index(change)
    require(not any(active_change(c) and ids & {r['id'] for r in c['after']} for c in changes[index + 1:]),
            'CHANGE_HAS_DEPENDENTS', 'Undo the later change to this session first', 409)
    before = workspace_content(db, workspace)
    restore_rows(db, actor, workspace, change['before'])
    for aid in change['availability_ids']:
        absence = db.get(m.Availability, aid)
        absence.retracted, absence.revision = True, absence.revision + 1
        history(db, actor, 'availability', absence, value.reason)
        bump_inputs(db, actor, availability=True)
    change['status'] = 'withdrawn'
    change.pop('return_note', None)
    row.changes, row.revision = changes, row.revision + 1
    history(db, actor, 'publication_change_lists', row, value.reason)
    result = planning.changed(db, actor, workspace, before, value.reason)
    return {**result, 'changes': public_changes(row, result['validation'], db=db, actor=actor)}


def submit(db, actor, row, workspace, command):
    value = s.PublicationChangesSubmit.model_validate(command.payload)
    draft = [c for c in row.changes if c['status'] == 'draft']
    selected = [c for c in draft if value.change_ids is None or c['id'] in value.change_ids]
    require(selected and (value.change_ids is None or {c['id'] for c in selected} == set(value.change_ids)),
            'CHANGE_SELECTION', 'Choose current unsent changes')
    require(not any(c['status'] in {'submitted', 'approved'} for c in row.changes),
            'CHANGE_IN_REVIEW', 'Finish or withdraw the existing review first', 409)
    require(len(selected) == len(draft) or value.urgency == 'urgent', 'CHANGE_SELECTION', 'Only urgent changes may be sent alone')
    chosen = {sh['id'] for c in selected for sh in c['after']}
    require(not any(chosen & {sh['id'] for sh in c['after']} for c in draft if c not in selected),
            'CHANGE_HAS_DEPENDENTS', 'Send related changes to the same session together', 409)
    publication = db.get(m.Publication, row.publication_id)
    candidate = new_workspace(db, actor, publication)
    candidate.urgency, candidate.urgency_reason = value.urgency, value.reason if value.urgency == 'urgent' else ''
    rows = workspace_shifts(db, candidate.id)
    edits, aliases = [], {}
    for sh in workspace_shifts(db, workspace.id):
        if sh['id'] not in chosen:
            continue
        target = mapped_shift(rows, sh)
        aliases[sh['id']] = target['id']
        edits.append({**sh, 'id': target['id'], 'participants': [{**p, 'id': command_id(db), 'shift_id': target['id']} for p in sh['participants']]})
    restore_rows(db, actor, candidate, edits)
    for author_id in {c['actor_id'] for c in selected}:
        db.add(m.ChangeSet(workspace_id=candidate.id, before_snapshot=workspace_content(db, candidate),
            resulting_content_revision=candidate.content_revision, actor_id=author_id, reason=value.reason, undone=True))
    selected_ids = {c['id'] for c in selected}
    row.changes = [{**c, 'workspace_id': candidate.id,
                    'submitted_session_ids': {sh['id']: aliases[sh['id']] for sh in c['after']}}
                   if c['id'] in selected_ids else c for c in row.changes]
    result = planning.lifecycle(db, actor, s.Command(kind='workspace.submit', target_id=candidate.id,
        expected_revision=candidate.revision, payload={'reason': value.reason}))
    check = candidate.validation
    review_organisation = planning._lock_review_check_inputs(db, actor, check)
    row.changes = [{**c, 'status': 'submitted', 'workspace_id': candidate.id,
                    'urgency': value.urgency, 'submitted_at': candidate.submitted_at.isoformat(),
                    'return_note': None}
                   if c['id'] in selected_ids else c for c in row.changes]
    row.revision += 1
    workspace.revision += 1
    history(db, actor, 'publication_change_lists', row, value.reason)
    manifest = check['input_manifest']
    hint = manifest['dirty_hint']
    # The transaction holds the input lock through this final change-list write.
    dirty = {**hint, 'input_revision': review_organisation.input_revision,
             'availability_revision': review_organisation.availability_revision}
    if 'department_revisions' in dirty:
        department = db.get(m.Department, candidate.department_id)
        dirty['department_revisions'] = [
            [ident, revision, department.catalogue_revision if ident == department.id else catalogue]
            for ident, revision, catalogue in dirty['department_revisions']]
    if dirty != hint:
        candidate.validation = {**check, 'input_manifest': {**manifest, 'dirty_hint': dirty}}
    return {**result, 'validation': candidate.validation,
            'changes': public_changes(row, candidate.validation, db=db, actor=actor)}


def on_reviewed(db, actor, workspace, reason):
    row = change_list(db, workspace.base_publication_id) if workspace.base_publication_id else None
    if row and any(c['workspace_id'] == workspace.id for c in row.changes):
        note = workspace.return_note if workspace.status == 'draft' else None
        row.changes = [{**c, 'status': workspace.status,
                        'return_note': note}
                       if c['workspace_id'] == workspace.id else c for c in row.changes]
        row.revision += 1
        history(db, actor, 'publication_change_lists', row, reason)
        if workspace.status == 'approved':
            # SW3-B-08: one notice per person per amendment naming each absence (one per changed duty read as duplicates).
            for person, spans in absences_by_person(row, workspace).items():
                outbox(db, actor, f"absence-approved:{workspace.id}:{person}", 'absence_approved',
                       {'person_id': person, 'workspace_id': workspace.id,
                        'start': spans[0]['start'], 'end': spans[-1]['end'], 'spans': spans})


def absences_by_person(row, workspace, status=None):
    """The amendment's staged absences per person, earliest first (start/end only: never a private reason)."""
    spans = {}
    for change in row.changes:
        if change['workspace_id'] == workspace.id and (status is None or change['status'] == status):
            for absence in change.get('absences', []):
                spans.setdefault(absence['person_id'], []).append({'start': str(absence['start']), 'end': str(absence['end'])})
    return {person: sorted(rows, key=lambda r: r['start']) for person, rows in spans.items()}


def materialize_absences(db, actor, workspace, row, reason):
    # Same transaction, after all publication gates and before ledgers read the approved facts.
    changed = False
    for change in row.changes if row else []:
        if change['workspace_id'] == workspace.id and change['status'] == 'approved':
            for staged in change.get('absences', []):
                absence = m.Availability(**{**staged, 'start': aware(staged['start']), 'end': aware(staged['end'])},
                    department_id=workspace.department_id, recorded_by=change['actor_id'])
                db.add(absence)
                changed = True
                history(db, actor, 'availability', absence, reason)
                bump_inputs(db, actor, availability=True)
    # SW3-B-08: one "recorded" notice per person per amendment, naming each absence.
    for person, spans in (absences_by_person(row, workspace, 'approved') if row else {}).items():
        outbox(db, actor, f"availability-published:{workspace.id}:{person}", 'availability', {'person_id': person, 'spans': spans})
    return changed


def draft_changes(row):
    """Unsent items carry forward into a successor draft when an urgent subset is published."""
    return [c for c in row.changes if c['status'] == 'draft'] if row else []


def on_published(db, actor, workspace, publication, row, reason):
    if not workspace.base_publication_id:
        return {}
    person_changes = personal_changes(db, db.get(m.Publication, workspace.base_publication_id), publication)
    if row:
        remaining = draft_changes(row)
        row.changes = [{**c, 'status': 'published'} if c['workspace_id'] == workspace.id else c for c in row.changes]
        row.revision += 1
        history(db, actor, 'publication_change_lists', row, reason)
        hidden = db.get(m.Workspace, row.workspace_id)
        if remaining:
            successor = new_workspace(db, actor, publication)
            rows = workspace_shifts(db, successor.id)
            changes = []
            previous = db.get(m.Publication, workspace.base_publication_id)
            issued, assignments = {}, {}
            for old, new in pairs(previous.snapshot['shifts'], publication.snapshot['shifts']):
                if old and new:
                    issued[old['id']] = new['id']
                    for person in old['participants']:
                        matched = [p for p in new['participants'] if p['person_id'] == person['person_id']
                                   and aware(p['start']) == aware(person['start']) and aware(p['end']) == aware(person['end'])]
                        if len(matched) == 1:
                            assignments[person['id']] = matched[0]['id']
            for change in remaining:
                before, after = [], []
                for sh in change['after']:
                    target = mapped_shift(rows, sh)
                    before.append(target)
                    after.append({**sh, 'id': target['id'], 'participants': [{**p, 'id': command_id(db), 'shift_id': target['id']} for p in sh['participants']]})
                restore_rows(db, actor, successor, after)
                rows = workspace_shifts(db, successor.id)
                rebound = {key: (issued if key in {'shift_id', 'target_shift_id'} else assignments).get(value, value)
                           for key, value in change.items() if key in {'shift_id', 'target_shift_id', 'assignment_id', 'target_assignment_id'}}
                changes.append({**change, **rebound, 'workspace_id': successor.id, 'before': before, 'after': after})
            db.add(m.PublicationChangeList(id=command_id(db), department_id=actor.department_id, publication_id=publication.id,
                                          workspace_id=successor.id, changes=changes))
            row.changes = [{**c, 'status': 'moved', 'moved_to_publication_id': publication.id}
                           if c['id'] in {r['id'] for r in remaining} else c for c in row.changes]
            row.revision += 1
            history(db, actor, 'publication_change_lists', row, reason)
            planning.revalidate(db, successor)
        hidden.archive_state = planning._archive_state(hidden, access_record(db, hidden))
        hidden.archived_at, hidden.archived_by = m.now(), actor.identity_id
        hidden.archive_reason = reason
        hidden.revision += 1
    continue_open_offers(db, actor, workspace.base_publication_id, publication)
    return person_changes


def staged_availability(db, workspace):
    """Pending absence facts belong only to their hidden list or submitted candidate."""
    if not workspace.base_publication_id or workspace.status in {'published', 'superseded'}:
        return []
    row = change_list(db, workspace.base_publication_id)
    return [{key: val for key, val in absence.items() if key != 'private_reason'} | {'counts_as_unavailable': True, 'retracted': False}
            for change in row.changes if change['status'] in {'draft', 'submitted', 'approved'}
            and (workspace.id == row.workspace_id or workspace.id == change['workspace_id'])
            for absence in change.get('absences', [])] if row else []


def personal_changes(db, previous, publication):
    """Own before/after duties, prepared once per publication for bounded notification delivery."""
    result = {}
    indices = {pub.id: {p['id']: (sh, p) for sh in pub.snapshot['shifts'] for p in sh['participants']}
               for pub in (previous, publication)}
    before, after = (schedule.in_force_snapshot(db, pub)['shifts'] for pub in (previous, publication))
    for change in delta(before, after):
        for side, key, pub in (('before', 'removed', previous), ('after', 'added', publication)):
            shift = change[side]
            if not shift or shift.get('cancelled') or shift.get('closed'):
                continue
            for person in shift['participants']:
                duty = swaps.summary(pub, person['id'], index=indices[pub.id])
                entry = {k: duty[k] for k in ('start', 'end', 'type', 'location', 'participation', 'overtime', 'segments')}
                entry.update(location_id=shift['location_id'], service_ids=sorted(shift['service_ids']),
                             services=[pub.snapshot['services'][sid]['name'] for sid in sorted(shift['service_ids'])])
                result.setdefault(person['person_id'], {'removed': [], 'added': []})[key].append(entry)
    return {pid: change for pid, change in result.items() if change['removed'] != change['added']}


def absence_attention(db, actor):
    if not actor.permits('coordinator'):
        return None
    R, P, A, S, Pub = m.RequestRecord, m.Person, m.Assignment, m.Shift, m.Publication
    zone = schedule.zone_of(db, actor.department_id)
    publications = schedule.current_publications(db, [actor.department_id], m.now().astimezone(ZoneInfo(zone)).date(), light=True)
    if not publications:
        return None
    from sqlalchemy import and_, or_
    query = select(R.id.label('request_id'), R.person_id, P.display_name.label('person_name'),
        R.start.label('start_at'), R.end.label('end_at'), R.status.label('request_status'),
        S.id.label('shift_id'), A.id.label('assignment_id'), Pub.id.label('publication_id')).join(P, P.id == R.person_id).join(
        A, and_(A.person_id == R.person_id, A.start == R.start, A.end == R.end)).join(S, S.id == A.shift_id).join(
        Pub, Pub.workspace_id == S.workspace_id).where(
        R.department_id == actor.department_id, R.kind == 'absence_report',
        R.status.in_(('submitted', 'approved')), R.end > m.now(), S.cancelled.is_(False),
        or_(*(schedule.in_force_clause(p, zone) for p in publications)))
    from sqlalchemy import func
    source = query.subquery()
    session_filter = select(source.c.shift_id).distinct()
    count = db.scalar(select(func.count()).select_from(session_filter.subquery()))
    ranked = select(source, func.row_number().over(
        partition_by=source.c.shift_id, order_by=(source.c.start_at, source.c.request_id)).label('sample_number')).subquery()
    sample = db.execute(select(ranked).where(ranked.c.sample_number == 1)
                        .order_by(ranked.c.start_at, ranked.c.request_id).limit(5)) if count else []
    return {'key': 'absence_reported', 'count': count, 'session_filter': session_filter, 'sample': [
        {'request_id': rid, 'person_id': pid, 'person': name, 'start': aware(start).isoformat(),
         'end': aware(end).isoformat(), 'status': status, 'replace_action': {
             'key': 'amend_publication', 'publication_id': pub, 'shift_id': sid, 'assignment_id': aid}}
        for rid, pid, name, start, end, status, sid, aid, pub, _ in sample]} if count else None


def continue_open_offers(db, actor, previous_id, publication):
    previous = db.get(m.Publication, previous_id)
    matching = {}
    for a, b in pairs(previous.snapshot['shifts'], publication.snapshot['shifts']):
        if a and b and swaps.semantic_content([a]) == swaps.semantic_content([b]):
            for p in a['participants']:
                candidates = [q for q in b['participants'] if q['person_id'] == p['person_id']
                              and aware(q['start']) == aware(p['start']) and aware(q['end']) == aware(p['end'])]
                if len(candidates) == 1:
                    matching[p['id']] = candidates[0]['id']
    offers = list(db.scalars(select(m.SwapRequest).where(m.SwapRequest.publication_id == previous_id,
        m.SwapRequest.open_offer.is_(True), m.SwapRequest.recipient_person_id.is_(None),
        m.SwapRequest.status == 'offered', m.SwapRequest.expires_at > m.now())))
    for offer in offers:
        # A still-in-force earlier slice needs no replacement offer.
        assignment = db.get(m.Assignment, offer.first_assignment_id)
        if previous.current and swaps.in_force(db, previous, assignment):
            continue
        offer.status, offer.revision = 'withdrawn', offer.revision + 1
        if offer.first_assignment_id in matching:
            replacement = m.SwapRequest(department_id=offer.department_id, publication_id=publication.id,
                proposer_id=offer.proposer_id, proposer_person_id=offer.proposer_person_id, kind=offer.kind,
                open_offer=True, first_assignment_id=matching[offer.first_assignment_id], expires_at=offer.expires_at,
                status='offered', reason=offer.reason, reopened_from_id=offer.id)
            db.add(replacement)
            db.flush()
            swaps.event(db, actor, replacement, 'reopened', 'Unchanged offered duty continued on the successor publication')
        else:
            outbox(db, actor, f'swap:{offer.id}:{offer.revision}:rota_changed', 'swap',
                {'swap_id': offer.id, 'person_ids': [offer.proposer_person_id], 'notice': 'rota_changed'})


def report_settings(db, actor):
    row = db.scalar(select(m.AbsenceReportSettings).where(m.AbsenceReportSettings.department_id == actor.department_id))
    return {'document_required': row.document_required if row else False, 'revision': row.revision if row else 1}


def save_report_settings(db, actor, command):
    require_role(actor, 'admin')
    value = s.AbsenceReportSettingsInput.model_validate(command.payload)
    row = db.scalar(select(m.AbsenceReportSettings).where(m.AbsenceReportSettings.department_id == actor.department_id))
    expected(row or SimpleNamespace(revision=1), command.expected_revision)
    if row is None:
        row = m.AbsenceReportSettings(department_id=actor.department_id, document_required=False, revision=1)
        db.add(row)
        db.flush()
        history(db, actor, 'absence_report_settings', row, value.reason)
    previous = row.document_required
    row.document_required, row.revision = value.document_required, row.revision + 1
    history(db, actor, 'absence_report_settings', row, value.reason)
    return {**report_settings(db, actor), 'undo': {'kind': 'absence_report.settings', 'expected_revision': row.revision,
            'payload': {'document_required': previous, 'reason': value.reason}}}


def own_absence_summary(row, actor):
    if row.kind != 'absence_report' or not actor.person_id or row.person_id != actor.person_id:
        return {}
    details = row.details or {}
    assignment_id = (details.get('replace_action') or {}).get('assignment_id')
    if not assignment_id:
        return {}
    from .leave_disclosure import canonical_absence_category, leave_wording
    wording = leave_wording(None, actor, row, own=True)
    category = details.get('reporter_category')
    return s.OwnAbsenceReportSummary(assignment_id=assignment_id, created_at=row.created_at,
        absence_category=canonical_absence_category(category),
        note=wording.get('note')).model_dump(mode='json', exclude_none=True)


def create_absence_report(db, actor, command):
    require_role(actor, 'staff')
    person_id = swaps.own_person(db, actor)
    value = s.AbsenceReportInput.model_validate(command.payload)
    publication = scoped(db, m.Publication, value.publication_id, actor)
    assignment = db.get(m.Assignment, value.assignment_id)
    shift = db.get(m.Shift, assignment.shift_id) if assignment else None
    require(assignment is not None and shift is not None and shift.workspace_id == publication.workspace_id
            and assignment.person_id == person_id, 'SWAP_ASSIGNMENT_NOT_FOUND', 'Choose your own published assignment', 404)
    require(publication.current and not shift.cancelled and aware(assignment.end) > m.now()
            and swaps.in_force(db, publication, shift), 'ABSENCE_DUTY_UNAVAILABLE', 'Choose a current duty that has not ended', 409)
    settings = report_settings(db, actor)
    require(not settings['document_required'] or bool((value.document_reference or '').strip()),
            'ABSENCE_DOCUMENT_REQUIRED', 'A supporting document reference is required')
    duplicate = db.scalar(select(m.RequestRecord.id).where(m.RequestRecord.department_id == actor.department_id,
        m.RequestRecord.person_id == person_id, m.RequestRecord.kind == 'absence_report',
        m.RequestRecord.start == assignment.start, m.RequestRecord.end == assignment.end,
        m.RequestRecord.status.in_(('submitted', 'approved'))))
    require(duplicate is None, 'REQUEST_DUPLICATE', 'This absence has already been reported', 409)
    action = {'key': 'amend_publication', 'publication_id': publication.id, 'shift_id': shift.id,
              'assignment_id': assignment.id}
    request = m.RequestRecord(department_id=actor.department_id, person_id=person_id, kind='absence_report',
        start=assignment.start, end=assignment.end, private_reason=value.reason, created_by=actor.identity_id,
        details={'replace_action': action, 'availability_kind': 'actual_absence', 'leave_type': '',
                 'note': value.note, 'document_reference': value.document_reference, 'document_policy': settings,
                  **({'reporter_category': value.category} if value.category is not None else {})})
    db.add(request)
    db.flush()
    outbox(db, actor, 'request:' + request.id, 'request',
           {'request_id': request.id, 'person_id': person_id, 'request_status': request.status, 'request_kind': request.kind})
    from .availability import absence_report_record
    return {'record': absence_report_record(request, actor, db), 'warnings': [],
            'message': 'Reported. An approver decides the absence; the coordinator arranges replacement cover.'}
