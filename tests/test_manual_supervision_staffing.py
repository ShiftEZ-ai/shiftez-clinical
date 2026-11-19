"""Manual plans name the exact supervisor the ordinary write will validate."""
import copy
import uuid

import pytest
from sqlalchemy import event, select

from apps.api.db import transaction
from apps.api import models as m
from apps.api.services import staffing, issues
from packages.contracts import schemas as s
from packages.domain.validation import evaluate
from packages.domain.time import aware
from .test_fill_options import options
from .test_planner_why import why
from .governance_support import identity

START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


@pytest.fixture(autouse=True)
def no_native_model(monkeypatch):
    from ortools.sat.python import cp_model
    def forbidden(*args, **kwargs):
        pytest.fail('Manual staffing LIGHT tests must never construct a native model or solver')
    monkeypatch.setattr(cp_model, 'CpModel', forbidden)
    monkeypatch.setattr(cp_model, 'CpSolver', forbidden)


def world(h):
    trainee, supervisor, other = h.seed['people']
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Manual supervised clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 2, 'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[supervisor])['workspace']
    data = h.snapshot(w)
    return w, data, data['shifts'][0], trainee, supervisor, other


def checked(data, shift, trainee, **kwargs):
    before = copy.deepcopy(data)
    rows, _ = staffing.check_people(data, shift, None, {trainee}, **kwargs)
    assert data == before, 'Decision support never changes any roster or recorded association'
    return rows[0]


def trial(data, shift, proposal):
    return {**data, 'shifts': [dict(row, participants=[*row['participants'], {
        **proposal, 'id': 'trial', 'shift_id': shift['id']}]) if row['id'] == shift['id'] else row for row in data['shifts']]}


def command_for(w, shift, proposal):
    participants = [{k: p[k] for k in s.ParticipantInput.model_fields if k in p} for p in shift['participants']]
    return {'kind': 'shift.update', 'expected_revision': w['revision'], 'payload': {
        **{k: shift[k] for k in ('assignment_type_id', 'location_id', 'service_ids', 'attributes', 'start', 'end')},
        'workspace_id': w['id'], 'shift_id': shift['id'], 'participants': [*participants, proposal]}}


def test_missing_association_blocks_but_manual_plan_and_exact_write_pass(h):
    w, data, shift, trainee, supervisor, _ = world(h)
    missing = {'person_id': trainee, 'start': START, 'end': END, 'participation': 'staff', 'overtime': False}
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in evaluate(trial(data, shift, missing))['findings']}
    row = checked(data, shift, trainee)
    assert row['status'] == 'ok' and row['fills']
    proposal = row['supervision']['proposal']
    assert proposal['supervisor_person_id'] == supervisor
    assert evaluate(trial(data, shift, proposal))['valid']
    assert s.StaffingSupervisionPlan.model_validate(row['supervision'])
    original = h.detail(w)
    offered = next(r for r in options(h, w, shift['id'])['candidates'] if r['person_id'] == trainee)
    assert offered['supervision'] == row['supervision'] and h.detail(w) == original
    command = command_for(w, shift, proposal)
    preview = h.client('coordinator').post('/api/planner/preview', json=command)
    assert preview.status_code == 200 and preview.json()['validation']['valid'], preview.text
    done = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={**command, 'expected_fingerprint': preview.json()['source_fingerprint']})
    assert done.status_code == 200 and done.json()['validation']['valid'], done.text
    saved = h.detail(done.json()['workspace'])['shifts'][0]['participants']
    assert next(p for p in saved if p['person_id'] == trainee)['supervisor_person_id'] == supervisor
    assert next(p for p in saved if p['person_id'] == supervisor) == original['shifts'][0]['participants'][0]


@pytest.mark.parametrize('case', ['none', 'partial', 'break', 'absent', 'wrong-capability', 'expired-grant',
                                 'wrong-service-grant', 'capacity-full', 'dated-cap', 'rank-limit', 'commitment', 'lead-only'])
def test_no_qualified_whole_attendance_spare_place_remains_blocked(h, case):
    _, data, shift, trainee, supervisor, other = world(h)
    if case == 'none':
        shift['participants'] = []
    if case == 'partial':
        shift['participants'][0]['end'] = '2026-10-04T10:00:00+03:00'
    if case == 'break':
        shift['participants'][0]['segments'] = [{'start': START, 'end': '2026-10-04T09:00:00+03:00'},
            {'start': '2026-10-04T10:00:00+03:00', 'end': END}]
    if case == 'absent':
        data['availability'].append({'id': 'supervisor-absence', 'person_id': supervisor, 'start': '2026-10-04T09:00:00+03:00',
            'end': '2026-10-04T10:00:00+03:00', 'kind': 'actual_absence'})
    grants = [g for g in data['grants'] if g['person_id'] == supervisor and g['capability_id'] == h.seed['supervisor_capability']]
    if case in {'wrong-capability', 'lead-only'}:
        for grant in grants:
            data['grants'].remove(grant)
        shift['participants'][0]['team_lead'] = True
    if case == 'expired-grant':
        for grant in grants:
            grant['ends_on'] = '2026-10-04'
    if case == 'wrong-service-grant':
        for grant in grants:
            grant['service_id'] = h.seed['services'][1]
    if case == 'capacity-full':
        shift['type_snapshot']['definition']['supervision']['trainees_per_supervisor'] = 1
        shift['participants'].append({'person_id': other, 'start': START, 'end': END, 'participation': 'trainee',
            'supervisor_person_id': supervisor, 'id': 'kept-trainee', 'shift_id': shift['id'], 'overtime': False})
    if case in {'dated-cap', 'rank-limit'}:
        rule = ({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0} if case == 'dated-cap' else
                {'kind': 'supervision_limit', 'mode': 'allowed_ranks', 'trainee_category_ids': [h.configure('categories', {'name': 'Other qualification', 'starts_on': '2020-01-01'})['record']['id']]})
        policy = h.policy(rule, {'person_id': supervisor, 'start_time': '09:00', 'end_time': '10:00'})
        data['policies'] = [policy]
    if case == 'commitment':
        other_shift = copy.deepcopy(shift)
        other_shift['id'] = 'other-hidden-duty'
        data['shifts'].append(other_shift)  # Outside focus still occupies the supervisor.
    row = checked(data, shift, trainee)
    assert row['status'] == 'blocked' and not row['fills']
    assert not row['supervision']['choices'] and row['supervision']['proposal'] is None
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in row['findings']}


def test_multiple_supervisors_require_explicit_choice_and_reject_wrong_selection(h):
    w, data, shift, trainee, supervisor, other = world(h)
    shift['participants'].append({**shift['participants'][0], 'person_id': other, 'id': 'second'})
    row = checked(data, shift, trainee)
    assert {c['supervisor_person_id'] for c in row['supervision']['choices']} == {supervisor, other}
    assert row['status'] == 'warn' and not row['fills'] and row['supervision']['choice_required']
    assert row['supervision']['proposal'] is None
    for pid in (supervisor, other):
        selected = checked(data, shift, trainee, supervisor_person_id=pid)
        assert selected['status'] == 'ok' and selected['fills']
        assert selected['supervision']['proposal']['supervisor_person_id'] == pid
        assert evaluate(trial(data, shift, selected['supervision']['proposal']))['valid']
    invalid = checked(data, shift, trainee, supervisor_person_id=trainee)
    assert invalid['status'] == 'blocked' and invalid['supervision']['proposal'] is None
    assert 'SUPERVISOR_SELECTION_INVALID' in {f['code'] for f in invalid['findings']}
    # Request schema/API selection also reaches the identical trial.
    result = options(h, w, shift['id'], supervisor_person_id=supervisor)
    assert next(r for r in result['candidates'] if r['person_id'] == trainee)['supervision']['proposal']['supervisor_person_id'] == supervisor


def test_shared_supervisor_spare_capacity_does_not_rewrite_existing_pair(h):
    _, data, shift, trainee, supervisor, other = world(h)
    shift['participants'].append({'person_id': other, 'start': START, 'end': END, 'participation': 'trainee',
        'supervisor_person_id': supervisor, 'id': 'kept-trainee', 'shift_id': shift['id'], 'fixed': True, 'overtime': False})
    row = checked(data, shift, trainee)
    assert row['status'] == 'ok' and row['fills']
    assert row['supervision']['proposal']['supervisor_person_id'] == supervisor
    assert evaluate(trial(data, shift, row['supervision']['proposal']))['valid']


def test_independent_blocker_stays_factual_with_named_plan(h):
    _, data, shift, trainee, supervisor, _ = world(h)
    data['availability'].append({'id': 'trainee-absence', 'person_id': trainee, 'start': START, 'end': END, 'kind': 'actual_absence'})
    row = checked(data, shift, trainee)
    assert row['supervision']['proposal']['supervisor_person_id'] == supervisor
    assert row['status'] == 'blocked' and not row['fills']
    assert 'UNAVAILABLE' in {f['code'] for f in row['findings']}
    assert 'SUPERVISION_PAIRING' not in {f['code'] for f in row['findings']}


def test_required_attachment_limits_choices_and_retain_why_preserves_actual_pair(h):
    w, data, shift, trainee, supervisor, other = world(h)
    shift['participants'].append({**shift['participants'][0], 'person_id': other, 'id': 'second'})
    data['mentorships'].append({'person_id': trainee, 'mentor_id': supervisor, 'strength': 'required',
        'starts_on': '2020-01-01', 'ends_on': None})
    assert checked(data, shift, trainee)['supervision']['proposal']['supervisor_person_id'] == supervisor
    original = dict(shift['participants'][0], person_id=other, supervisor_person_id=supervisor, participation='trainee', fixed=True)
    shift['participants'] = [shift['participants'][0], original]
    [row], _ = staffing.check_people(data, shift, original, {trainee}, retain_assignment=True)
    assert row['supervision']['proposal']['supervisor_person_id'] == supervisor
    assert row['supervision']['choices'] == []
    actual = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[{
        'person_id': trainee, 'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00',
        'supervisor_person_id': supervisor}, supervisor], typ=shift['assignment_type_id'])
    result = why(h, actual['workspace'], [actual['shift_id']], person_id=trainee)
    assert result['sessions'][0]['rows'][0]['supervisor_person_id'] == supervisor


def test_replacement_and_best_plan_keep_association_hidden_roster_and_attendance(h):
    _, data, shift, trainee, supervisor, other = world(h)
    shift['participants'].append({**shift['participants'][0], 'person_id': other, 'id': 'replace',
        'participation': 'trainee', 'supervisor_person_id': supervisor, 'fixed': True})
    original = copy.deepcopy(data)
    [row], slot = staffing.check_people(data, shift, shift['participants'][1], {trainee})
    assert row['supervision']['proposal']['supervisor_person_id'] == supervisor
    assert aware(row['supervision']['proposal']['start']) == aware(slot['start'])
    plan = issues.best_replacements(data, {(shift['id'], other): ['test']}, {trainee})
    assert plan[0]['replacement_id'] == trainee and plan[0]['supervisor_person_id'] == supervisor
    assert data == original


def test_dated_grant_change_rejects_preview_and_old_client_never_bypasses_check(h):
    # D65 (docs/user-guide.md:49-55): a new or worsened blocker refuses the edit, even with a reason.
    w, data, shift, trainee, supervisor, _ = world(h)
    original = h.detail(w)
    proposal = checked(data, shift, trainee)['supervision']['proposal']
    command = command_for(w, shift, proposal)
    preview = h.client('coordinator').post('/api/planner/preview', json=command)
    assert preview.status_code == 200 and preview.json()['validation']['valid']
    with transaction(h.app.state.engine, write=True) as db:
        grant = db.scalar(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == supervisor,
            m.CapabilityGrant.capability_id == h.seed['supervisor_capability']))
        grant.revoked = True
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    stale = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={**command, 'expected_fingerprint': preview.json()['source_fingerprint']})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'STALE_PREVIEW'
    fresh = h.client('coordinator').post('/api/planner/preview', json=command)
    assert fresh.status_code == 422 and fresh.json()['error']['code'] == 'EDIT_INVALID'
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in fresh.json()['error']['details']['findings']}
    saved = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json=command)
    assert saved.status_code == 422 and saved.json()['error']['code'] == 'EDIT_INVALID'
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in saved.json()['error']['details']['findings']}
    legacy = command_for(w, shift, {**proposal, 'supervisor_person_id': None})
    checked_legacy = h.client('coordinator').post('/api/planner/preview', json=legacy)
    assert checked_legacy.status_code == 422 and checked_legacy.json()['error']['code'] == 'EDIT_INVALID'
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in checked_legacy.json()['error']['details']['findings']}
    assert h.detail(w)['shifts'] == original['shifts']
    assert h.detail(w)['workspace'] == original['workspace']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_permission_and_privacy_use_canonical_roles(h, role):
    w, _, shift, trainee, _, _ = world(h)
    if role not in h.seed['identities']:
        identity(h, role, [role])
    h.command('coordinator', 'availability.record', {'person_id': trainee, 'kind': 'actual_absence',
        'start': START, 'end': END, 'private_reason': 'SECRET_PRIVATE_ABSENCE'})
    body = {'workspace_id': w['id'], 'shift_id': shift['id'], 'expected_revision': w['revision']}
    reply = h.client(role).post('/api/planner/fill-options', json=body)
    assert reply.status_code == (200 if role in {'admin', 'coordinator'} else 403)
    assert 'SECRET_PRIVATE_ABSENCE' not in reply.text


def test_no_per_candidate_sql_and_why_score_trials_use_same_proposal(h, monkeypatch):
    w, data, shift, trainee, supervisor, _ = world(h)
    calls = []
    def sql(*args):
        calls.append(1)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        h.client('coordinator')
        calls.clear()
        options(h, w, shift['id'])
        first = len(calls)
        # Candidate evaluation is pure: one snapshot, no SQL inside any validator trial.
        real = staffing.evaluate
        def checked_evaluate(*args, **kwargs):
            count = len(calls)
            result = real(*args, **kwargs)
            assert len(calls) == count
            return result
        monkeypatch.setattr(staffing, 'evaluate', checked_evaluate)
        calls.clear()
        options(h, w, shift['id'])
        assert len(calls) == first
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    result = why(h, w, [shift['id']], person_id=trainee)
    row = result['sessions'][0]['rows'][0]
    assert row['supervision']['proposal']['supervisor_person_id'] == supervisor
    from packages.domain.score import schedule_score, score_effect
    exact = trial(data, shift, row['supervision']['proposal'])
    expected = score_effect(schedule_score(exact, evaluate(exact), {}, limit=0), schedule_score(data, evaluate(data), {}, limit=0))
    assert {k: row['effect'][k] for k in expected} == expected


def test_legacy_suggestions_trial_the_exact_explicit_trainee_slot(h):
    w, data, shift, _, supervisor, other = world(h)
    # This person is not in the trainee rank: explicit participation still needs a named plan.
    response = h.client('coordinator').post('/api/planner/staffing-suggestions', json={
        'workspace_id': w['id'], 'shift_id': shift['id'], 'expected_revision': w['revision'],
        'slot': {'start': START, 'end': END, 'participation': 'trainee', 'overtime': True, 'fixed': True}})
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()['candidates'] if r['person_id'] == other)
    assert row['whole_candidate_valid']
    proposal = row['supervision']['proposal']
    assert proposal['supervisor_person_id'] == supervisor
    assert proposal['overtime'] and proposal['fixed'] and proposal['participation'] == 'trainee'
    assert evaluate(trial(data, shift, proposal))['valid']


def test_dated_qualification_must_last_through_midnight_and_attendance_breaks(h):
    _, data, shift, trainee, supervisor, _ = world(h)
    shift['start'], shift['end'] = '2026-10-04T22:00:00+03:00', '2026-10-05T02:00:00+03:00'
    shift['participants'][0].update(start=shift['start'], end=shift['end'])
    for grant in data['grants']:
        if grant['person_id'] == supervisor and grant['capability_id'] == h.seed['supervisor_capability']:
            grant['ends_on'] = '2026-10-05'
    row = checked(data, shift, trainee)
    assert row['status'] == 'blocked' and row['supervision']['proposal'] is None


def test_existing_whole_duty_lead_requirement_is_not_satisfied_by_pairing(h):
    _, data, shift, trainee, supervisor, _ = world(h)
    # A current governed-rule snapshot; no legacy type setting or rule write is needed.
    data['policies'].append({'id': str(uuid.uuid4()), 'revision': 1, 'name': 'Whole shift named lead',
        'status': 'active', 'active': True, 'department_id': data['workspace']['department_id'],
        'starts_on': '2020-01-01', 'ends_on': None, 'definition': s.PolicyDefinition.model_validate({
            'severity': 'mandatory', 'rule': {'kind': 'team_lead_requirement', 'minimum': 1,
            'continuity': 'whole_duty'}, 'scope': {}}).model_dump(mode='json')})
    row = checked(data, shift, trainee)
    proposal = row['supervision']['proposal']
    assert proposal and proposal['supervisor_person_id'] == supervisor and not proposal['team_lead']
    exact = evaluate(trial(data, shift, proposal))
    assert 'LEAD_GAP' in {f['code'] for f in exact['findings']}
    assert row['status'] == 'warn' and not row['fills']
    assert 'SESSION_FINDINGS_REMAIN' in {f['code'] for f in row['findings']}


def test_bulk_replacement_records_the_previewed_named_pair_and_keeps_other_rows(h):
    w, _, shift, trainee, supervisor, other = world(h)
    h.command('coordinator', 'membership.create', {'person_id': other,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    filled = h.command('coordinator', 'shift.update', command_for(w, shift, {
        'person_id': other, 'start': START, 'end': END, 'supervisor_person_id': supervisor})['payload'], revision=w['revision'])
    w = filled['workspace']
    h.command('coordinator', 'availability.record', {'person_id': other, 'kind': 'actual_absence', 'start': START, 'end': END,
        'private_reason': 'Fictional absence for replacement validation'})
    w = h.lifecycle(w, 'validate')['workspace']
    from .test_resolve_issues import preview, resolve, issues as findings
    keys = [issues.issue_key(f) for f in findings(h, w, 'UNAVAILABLE')]
    assert keys
    before = h.detail(w)['shifts'][0]['participants']
    offered = preview(h, w, keys)
    assert offered['plan'][0]['replacement_id'] == trainee
    assert offered['plan'][0]['supervisor_person_id'] == supervisor
    done = resolve(h, w, 'replace', keys, offered['plan_hash'])
    assert done['validation']['valid']
    after = h.detail(done['workspace'])['shifts'][0]['participants']
    assert next(p for p in after if p['person_id'] == trainee)['supervisor_person_id'] == supervisor
    assert next(p for p in after if p['person_id'] == supervisor) == next(p for p in before if p['person_id'] == supervisor)


def test_supervision_trial_work_counts(h, monkeypatch):
    """One full baseline plus one full placement per candidate, independent of supervisor count."""
    import json
    import os
    from pathlib import Path
    from collections import Counter
    _, data, shift, trainee, supervisor, other = world(h)
    second = str(uuid.uuid4())
    data['people'][second] = {**data['people'][trainee], 'id': second, 'display_name': 'Second trainee'}
    for name in ('memberships', 'eligibilities', 'grants'):
        data[name] += [{**r, 'id': str(uuid.uuid4()), 'person_id': second}
                       for r in list(data[name]) if r['person_id'] == trainee]
    shift['participants'].append({**shift['participants'][0], 'id': 'second-supervisor', 'person_id': other})
    counts = Counter()
    real = staffing.evaluate
    def counted(source, **kwargs):
        counts['partial' if source.get('_partial') else 'full'] += 1
        counts['full_session_work' if not source.get('_partial') else 'partial_session_work'] += len(
            source['focus_ids'] if not source.get('_partial') else source['_partial']['shifts'])
        return real(source, **kwargs)
    monkeypatch.setattr(staffing, 'evaluate', counted)
    candidates = {trainee, second}
    index = staffing.people_index(data)
    rows, _ = staffing.check_people(data, shift, None, candidates, index=index)
    assert len(index['supervision_pool']) == 1
    assert all(r['supervision']['choice_required'] and len(r['supervision']['choices']) == 2 for r in rows)
    if path := os.getenv('ROTA_MANUAL_WORK_REPORT'):
        Path(path).write_text(json.dumps({'candidates': len(candidates), 'supervisors': 2, 'canonical_checks': dict(counts)}, indent=2)+'\n')
    assert counts['full'] <= 1 + len(candidates)
    assert counts['partial'] > 0
# Include this file in hosted supervision and team-lead qualification.
