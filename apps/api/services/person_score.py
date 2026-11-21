"""Issued score attribution and current, non-mutating staffing opportunities for one person."""

from packages.domain.recurrence import inactive
from sqlalchemy import select

from apps.api import models as m
from apps.api.errors import require
from apps.api.snapshots import domain_snapshot
from packages.domain.input_manifest import digest, toolchain
from apps.api.settings import quota_policy_limit
from packages.domain.conditions import counting_key
from packages.domain.generation import COVERAGE_CODES, duty_labels, coverage_findings, citation_finding_key
from packages.domain.rota_summary import normalized_check
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate, finding_observations, index_person_rows
from packages.contracts import schemas as s
from .common import scoped
from .fairness import READERS, sees_everyone, viewer
from .person_record import require_person
from .staffing import MAX_SCOPE_SHIFTS, _new_candidate_findings, candidate_status, check_people, people_index
from apps.api.services.common import neutral_rota_name


def _gap_key(row):
    detail = row.get('detail') or {}
    return tuple(row.get(key) for key in ('code', 'severity', 'rule_id', 'shift_id', 'person_id', 'start', 'end')) + tuple(
        detail.get(key) for key in ('line', 'slot', 'rule_severity', 'rule_strength', 'capability_id')) + ('maximum' in detail, counting_key(detail))


def _progressed(before, after):
    if after is None:
        return True
    old, new = before.get('detail') or {}, after.get('detail') or {}
    if before['code'] == 'SUPERVISION_GAP':
        return (new.get('qualified_supervisors', 0) > old.get('qualified_supervisors', 0)
                and new.get('trainees', 0) <= old.get('trainees', 0))
    return new.get('available', 0) > old.get('available', 0)


def _worsened(before, after):
    old, new = before.get('detail') or {}, after.get('detail') or {}
    if before['code'] == 'SUPERVISION_GAP':
        return (new.get('qualified_supervisors', 0) < old.get('qualified_supervisors', 0)
                or new.get('trainees', 0) > old.get('trainees', 0))
    return (new.get('available', 0) < old.get('available', 0)
            or new.get('required', 0) > old.get('required', 0)
            or new.get('scheduled', 0) > old.get('scheduled', 0))


def contributions(db, actor, person_id, workspace_id=None, open_limit=20, open_offset=0):
    viewer(actor)
    require_person(db, actor, person_id, READERS)
    draft = None
    if workspace_id:
        require(sees_everyone(actor), 'FORBIDDEN', 'Drafts are visible to planners only', 403)
        draft = scoped(db, m.Workspace, workspace_id, actor)
        require(draft.status == 'draft' and draft.archived_at is None,
                'WORKFLOW_STATE', 'Choose a current draft', 409)
    elif sees_everyone(actor):
        draft = db.scalar(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
            m.Workspace.status == 'draft', m.Workspace.archived_at.is_(None))
            .order_by(m.Workspace.starts_on.desc(), m.Workspace.created_at.desc(), m.Workspace.seq.desc()).limit(1))
    publication = db.scalar(select(m.Publication).where(m.Publication.department_id == actor.department_id,
        m.Publication.current.is_(True)).order_by(m.Publication.starts_on.desc(), m.Publication.sequence.desc(),
                                                 m.Publication.id).limit(1))
    published = db.get(m.Workspace, publication.workspace_id) if publication else None
    return {'person_id': person_id,
            'draft': _record(db, actor, person_id, draft, None, open_limit, open_offset) if draft else None,
            'published': _record(db, actor, person_id, published, publication, open_limit, open_offset)
                         if published else None}


def _record(db, actor, person_id, workspace, publication, limit, offset):
    check = normalized_check(publication.validation if publication else workspace.validation) or {}
    issued = check.get('score') or {}
    complete = 'contributions' in issued
    lines = issued.get('contributions', issued.get('why', []))
    assigned = ({shift['id'] for shift in publication.snapshot['shifts']
                 if any(p['person_id'] == person_id for p in shift['participants'])} if publication else
                set(db.scalars(select(m.Assignment.shift_id).join(m.Shift).where(
                    m.Shift.workspace_id == workspace.id, m.Assignment.person_id == person_id))))
    def involves(row):
        return row.get('person_id') == person_id or (not row.get('person_id') and row.get('shift_id') in assigned)
    contribution_fields = s.PersonScoreContribution.model_fields
    own = [{**{key: row[key] for key in contribution_fields if key in row},
            'score_delta': -row['points'],
            'component_weight': (issued.get('settings') or {}).get('weights', {}).get(row['component'])}
           for row in lines if involves(row)]
    copy_fields = ('line', 'slot', 'rule_severity', 'rule_strength')
    issued_weights = issued.get('rule_weights', [])
    by_copy = {(row['rule_id'], row.get('line'), row.get('slot'), row['severity'], row['strength']): row
               for row in issued_weights}
    findings = []
    for row in check.get('findings', []):
        if not involves(row):
            continue
        source = {key: (row.get('detail') or {}).get(key) for key in copy_fields}
        weight = by_copy.get((row.get('rule_id'), *(source[key] for key in copy_fields)))
        findings.append({**{key: row.get(key) for key in ('code', 'severity', 'message', 'rule_id', 'shift_id',
                                                         'person_id', 'start', 'end')}, **source,
                         'rule_weight': weight['weight'] if weight else None})
    related = {}
    for row in own + findings:
        if row.get('rule_id'):
            related.setdefault(row['rule_id'], []).append(row)
    weights = [weight for weight in issued_weights if any(all(
        row.get(key) is None or row[key] == weight.get(stored) for key, stored in (
            ('line', 'line'), ('slot', 'slot'), ('rule_severity', 'severity'), ('rule_strength', 'strength')))
        for row in related.get(weight['rule_id'], ()))]
    manifest = check.get('input_manifest') or {}
    dirty = manifest.get('dirty_hint') or {}
    org = db.get(m.Organisation, actor.organisation_id)
    fresh = bool(check) and (bool(publication) or (
        dirty.get('content_revision') == workspace.content_revision
        and manifest.get('components', {}).get('toolchain') == digest(toolchain(quota_policy_limit()))))
    current_data = None
    person = db.get(m.Person, person_id)
    person_facts = ((manifest.get('sources') or {}).get(f'people:{person_id}') or {}).get('facts') or {}
    if not person.active or person_facts and person_facts.get('active') != person.active:
        fresh = False
    if check and not publication and limit:
        current_data = domain_snapshot(db, workspace, include_fairness=False,
                                       include_objectives=False, include_fairness_history=False)
    places, total, opportunity_score = [], 0, None
    gap_ids = {row['shift_id'] for row in check.get('coverage', []) if row.get('status') == 'gap'}
    for row in coverage_findings(check):
        if row['code'] in COVERAGE_CODES:
            if row.get('shift_id'):
                gap_ids.add(row['shift_id'])
            gap_ids.update((row.get('detail') or {}).get('affected_shift_ids', []))
    if gap_ids and fresh and limit:
        data = current_data or domain_snapshot(db, workspace)
        # Trials change participants only; dated person/scope facts stay fixed for this read.
        data.update(_person_rows=index_person_rows(data), _person_dates_cache={},
                    _scope_interval_cache={}, _line_index_cache={})
        require(len(data['shifts']) <= MAX_SCOPE_SHIFTS * 2, 'STAFFING_SCOPE_LIMIT',
                'Narrow the planning period to read staffing opportunities', 409)
        focus = set(data['focus_ids'])
        shifts = sorted((row for row in data['shifts'] if row['id'] in gap_ids and row['id'] in focus
                         and not inactive(row) and not row['locked']), key=lambda row: (row['start'], row['id']))
        total = min(max(0, len(shifts) - offset), limit)
        index = people_index(data)
        options = dict(data['department'].get('generation_defaults') or {})
        if publication and issued.get('settings'):
            options['scoring'] = issued['settings']
        baseline_check, session_cache = None, {}
        has_eligibility = db.scalar(select(m.Eligibility.id).where(
            m.Eligibility.department_id == actor.department_id,
            m.Eligibility.person_id == person_id).limit(1)) is not None
        for shift in shifts[offset:offset + limit]:
            if not has_eligibility:
                continue
            # The complete trial below re-adds session-level safety findings
            # before this opportunity is scored or exposed.
            candidates, slot = check_people(data, shift, None, {person_id}, index=index, session_safety=False)
            if (not candidates or candidates[0]['status'] == 'blocked'
                    or candidates[0].get('supervision') and candidates[0]['supervision']['proposal'] is None):
                continue
            if baseline_check is None:
                baseline_check = evaluate(data, session_cache=session_cache)
                baseline = schedule_score(data, baseline_check, options, limit=0)
                cover_before = {_gap_key(row): row for row in coverage_findings(baseline_check)}
                mandatory_before = {citation_finding_key(row) for row in finding_observations(baseline_check)
                                    if row['severity'] == 'error' and row['code'] not in COVERAGE_CODES}
                opportunity_score = baseline['score']
            lead = candidates[0]['can_lead'] and any(row['code'] == 'LEAD_GAP' and row.get('shift_id') == shift['id']
                and (row.get('detail') or {}).get('required', 0) > (row.get('detail') or {}).get('available', 0)
                for row in baseline_check['findings'])
            seat = {**slot, 'id': 'person-record-candidate', 'shift_id': shift['id'], 'person_id': person_id,
                    'fixed': False, 'team_lead': lead,
                    'supervisor_person_id': ((candidates[0].get('supervision') or {}).get('proposal') or {}).get('supervisor_person_id')}
            trial = {**data, 'shifts': [dict(row, participants=[*row['participants'], seat])
                                       if row['id'] == shift['id'] else row for row in data['shifts']]}
            # Each trial copies only its changed session; reuse the other sessions' checks.
            after_check = evaluate(trial, session_cache=session_cache)
            new_findings = _new_candidate_findings(baseline_check, after_check)
            unsafe_trial = (any(row['severity'] == 'error' and row['code'] not in COVERAGE_CODES
                                and citation_finding_key(row) not in mandatory_before for row in finding_observations(after_check))
                            or any(row['severity'] == 'error' and (_gap_key(row) not in cover_before
                                   or _worsened(cover_before[_gap_key(row)], row)) for row in coverage_findings(after_check)))
            if unsafe_trial:
                continue
            trial_status = candidate_status(new_findings)
            if trial_status == 'blocked':
                continue
            after = schedule_score(trial, after_check, options, limit=0)
            before_cover, after_cover = baseline['coverage_fraction'], after['coverage_fraction']
            # Scoped headcounts and capability/supervision gaps are findings, not headcount coverage rows.
            # Compare the shared validator's issued counts, including partial fills with a zero score delta.
            remaining = {_gap_key(row): row for row in coverage_findings(after_check)}
            progressed = False
            for row in coverage_findings(baseline_check):
                if row.get('shift_id') != shift['id'] and shift['id'] not in (row.get('detail') or {}).get('affected_shift_ids', []):
                    continue
                if _progressed(row, remaining.get(_gap_key(row))):
                    progressed = True
                    break
            if not progressed and (before_cover is None or after_cover is None or after_cover <= before_cover):
                continue
            places.append({'shift_id': shift['id'], 'start': slot['start'], 'end': slot['end'],
                           **duty_labels(data, shift),
                           'status': trial_status, 'team_lead': lead,
                           'score_delta': round(after['score'] - opportunity_score, 1),
                           **({'supervision': candidates[0]['supervision']} if candidates[0].get('supervision') else {})})
    return {'workspace_id': workspace.id, 'publication_id': publication.id if publication else None,
            'name': neutral_rota_name(workspace.name), 'starts_on': workspace.starts_on.isoformat(), 'ends_on': workspace.ends_on.isoformat(),
            'basis': 'issued_publication' if publication else 'stored_draft_check', 'fresh': fresh,
            'score': issued.get('score') if sees_everyone(actor) else None,
            'person_score': (issued.get('people') or {}).get(person_id),
            'contributions': own, 'findings': findings, 'rule_weights': weights,
            'contributions_complete': complete, 'open_places': places,
            'open_places_total': total if sees_everyone(actor) and limit else None,
            'opportunity_score': opportunity_score if sees_everyone(actor) else None,
            'opportunity_basis': 'current_recorded_inputs',
            'open_limit': limit, 'open_offset': offset,
            'open_has_more': offset + limit < total if sees_everyone(actor) and limit else None}
