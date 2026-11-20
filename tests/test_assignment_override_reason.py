"""Manual absence overrides require an audited reason and retain their blocking findings."""
from datetime import date
import json
import time

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from packages.contracts import schemas as s
from packages.domain.time import aware
from tests.test_event_calendar import event_row
from tests.test_rotation_leave_policy import resident
from tests.test_rule_catalogue_api import approve, line, person, propose
from tests.test_scheduler_performance import _budget

START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
REASON = 'Reviewed absence conflict; arrange cover before submission'


AUDITED_REASONS = [(s.ShiftCreate, 'override_reason'), (s.ShiftEdit, 'override_reason'), (s.SelfApprovalInput, 'reason'),
                   (s.RuleAdjustmentInput, 'reason'), (s.TargetOverrideInput, 'reason'), (s.ExceptionInput, 'reason'),
                   (s.ExceptionBatch, 'reason'), (s.Decision, 'reason'), (s.SeriesOccurrenceCancel, 'reason'),
                   (s.GapAcknowledgement, 'reason'), (s.RotaItemDecision, 'reason')]


@pytest.mark.parametrize('model, field', AUDITED_REASONS, ids=lambda x: getattr(x, '__name__', x))
def test_every_audited_reason_is_the_one_stripped_reason_type(model, field):
    """One contract type: outer spaces are trimmed and at least 3 real characters are required."""
    from typing import Annotated
    from pydantic import TypeAdapter, ValidationError
    info = model.model_fields[field]
    reason = TypeAdapter(Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation)
    assert reason.validate_python('  Covered by the on-call lead  ') == 'Covered by the on-call lead'
    for bad in ('   ', ' ab ', '\t\n'):
        with pytest.raises(ValidationError):
            reason.validate_python(bad)


@pytest.mark.parametrize('starts_on, ends_on, blocked', [
    ('2026-10-05', '2026-10-07', True),   # starts at local midnight inside the session (UTC date is still 4 Oct)
    ('2026-10-01', '2026-10-05', True),   # ends_on is exclusive: away through 4 Oct, the session's first local day
    ('2026-10-01', '2026-10-04', False),  # away through 3 Oct only
])
def test_rotation_admission_matches_the_validator_across_local_midnight(starts_on, ends_on, blocked):
    """A session 22:00-02:00 department-local, sent in UTC, is judged on local dates like the validator."""
    from packages.domain.assignment_override import assignment_reason_allowed
    from packages.domain.validation import rotation_blocks
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'rotations': [
        {'person_id': 'p1', 'department_available': False, 'starts_on': starts_on, 'ends_on': ends_on}]}
    start, end = '2026-10-04T19:00:00+00:00', '2026-10-04T23:00:00+00:00'
    finding = {'code': 'EXTERNAL_ROTATION', 'person_id': 'p1', 'start': start, 'end': end}
    assert assignment_reason_allowed(data, finding) is rotation_blocks(data, 'p1', aware(start), aware(end)) is blocked


def payload(h, workspace, shift=None, *, start=START, end=END, people=None, **extra):
    return {'workspace_id': workspace['id'], 'assignment_type_id': h.seed['type'],
            'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
            'start': start, 'end': end, 'participants': [
                {'person_id': pid, 'start': start, 'end': end}
                for pid in (h.seed['people'][:1] if people is None else people)],
            **({'shift_id': shift['id']} if shift else {}), **extra}


def absence(h, kind):
    pid = h.seed['people'][0]
    if kind in {'zero_limit', 'zero_quota', 'zero_hours'}:
        entry = {'zero_limit': 'duties_per_week', 'zero_quota': 'duties_per_month', 'zero_hours': 'hours_per_week'}[kind]
        approve(h, propose(h, entry, [line(who=person(pid),
            numbers={'max': {'value': 0, 'level': 'mandatory'}})])['record'])
        return {'zero_limit': 'MAX_DUTIES', 'zero_quota': 'QUOTA_ABOVE_MAXIMUM', 'zero_hours': 'MAX_HOURS'}[kind]
    if kind == 'event':
        event_row(h, settings={'blocks_duties': True}, audience={'person_ids': [pid]})
        return 'RESTRICTION'
    if kind == 'rotation':
        _, value, _ = resident(h, 'allowed', create=False)
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.Rotation(department_id=h.seed['department_id'], **{**value,
                'starts_on': date(2026, 10, 4), 'ends_on': date(2026, 10, 5)}, department_available=False))
            db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        return 'EXTERNAL_ROTATION'
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Availability(person_id=pid, department_id=h.seed['department_id'],
            start=aware(START), end=aware(END), kind=kind, private_reason='Never disclose this private reason',
            recorded_by=h.seed['identities']['coordinator']))
        db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
    return 'UNAVAILABLE'


def availability(h, person_id, kind, start=START, end=END):
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Availability(person_id=person_id, department_id=h.seed['department_id'],
            start=aware(start), end=aware(end), kind=kind, private_reason='Never disclose this private reason',
            recorded_by=h.seed['identities']['coordinator'])
        db.add(row)
        db.flush()
        db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
        return row.id


def changes(h, workspace):
    with transaction(h.app.state.engine) as db:
        return [(row.id, row.reason) for row in db.scalars(select(m.ChangeSet).where(
            m.ChangeSet.workspace_id == workspace['id']).order_by(m.ChangeSet.created_at, m.ChangeSet.id))]


def test_override_reason_is_context_not_an_editable_session_property():
    assert 'override_reason' not in s.BULK_COLLECTIONS['sessions']['actions']['update']['fields']


@pytest.mark.parametrize('kind,allowed', [('approved_leave', True), ('protected_time', True)])
def test_fill_options_marks_fixed_list_blockers_as_override_allowed(h, kind, allowed):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    absence(h, kind)
    response = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': workspace['id'], 'shift_id': shift['id'], 'expected_revision': workspace['revision']})
    assert response.status_code == 200, response.text
    candidate = next(row for row in response.json()['candidates'] if row['person_id'] == h.seed['people'][0])
    assert candidate['override_allowed'] is allowed, candidate


@pytest.mark.parametrize('kind,expected', [
    ('approved_leave', True), ('protected_time', True), ('rotation', True),
    ('zero_limit', True), ('event', False),
])
def test_fill_override_allowed_matches_shift_update_reason_admission(h, kind, expected):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    absence(h, kind)
    response = h.client('coordinator').post('/api/planner/fill-options', json={
        'workspace_id': workspace['id'], 'shift_id': shift['id'], 'expected_revision': workspace['revision']})
    assert response.status_code == 200, response.text
    candidate = next(row for row in response.json()['candidates'] if row['person_id'] == h.seed['people'][0])
    result = h.command('coordinator', 'shift.update', {**payload(h, workspace, shift), 'override_reason': REASON},
                       revision=workspace['revision'], status=200 if expected else 422)
    assert candidate['override_allowed'] is expected
    if expected:
        assert result['shift_id'] == shift['id']


@pytest.mark.parametrize('kind', ['approved_leave', 'recorded_leave', 'protected_time', 'rotation', 'event', 'zero_limit', 'zero_quota', 'zero_hours'])
@pytest.mark.parametrize('update', [False, True])
def test_new_absent_assignment_requires_reason_and_keeps_blocking_findings(h, kind, update):
    workspace = h.workspace()
    shift = None
    if update:
        workspace = h.shift(workspace, people=[])['workspace']
        shift = h.detail(workspace)['shifts'][0]
    code = absence(h, kind)
    before, history = h.detail(workspace), changes(h, workspace)
    command = 'shift.update' if update else 'shift.create'
    value = payload(h, workspace, shift)
    allowed = kind in {'approved_leave', 'recorded_leave', 'protected_time', 'rotation',
                       'zero_limit', 'zero_quota', 'zero_hours'}
    refused = h.command('coordinator', command, value, revision=workspace['revision'], status=422)
    assert refused['error']['code'] == ('EDIT_INVALID' if kind == 'event' else 'PERSON_UNAVAILABLE')
    if update and code == 'UNAVAILABLE':  # the fill panel has the reason field: say what blocks and ask for it there
        assert refused['error']['message'] == ('Known unavailability overlaps this duty. '
                                               'Enter a reason to assign them anyway; the conflict still blocks submission')
    assert h.detail(workspace) == before and changes(h, workspace) == history
    whitespace = h.command('coordinator', command, {**value, 'override_reason': '   '},
                           revision=workspace['revision'], status=422)
    assert whitespace['error']['code'] == 'SCHEMA_INVALID'
    assert [row['field'] for row in whitespace['error']['details']] == ['override_reason'], whitespace
    if not allowed:
        refused = h.command('coordinator', command, {**value, 'override_reason': REASON},
                            revision=workspace['revision'], status=422)
        assert refused['error']['code'] == 'EDIT_INVALID'
        assert any(row['code'] == code for row in refused['error']['details']['findings'])
        assert h.detail(workspace) == before and changes(h, workspace) == history
        return
    saved = h.command('coordinator', command, {**value, 'override_reason': '  ' + REASON + '  '},
                      revision=workspace['revision'])
    assert (set(changes(h, workspace)) - set(history)).pop()[1] == REASON
    participant = h.detail(saved['workspace'])['shifts'][0]['participants'][0]
    assert participant['override_reason'] == REASON
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Assignment, participant['id']).override_reason == REASON
        event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == command).order_by(
            m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc())).first()
        assert event.reason == REASON
        assert REASON not in json.dumps(event.detail)
    assert any(f['code'] == code and f['severity'] == 'error' and f['person_id'] == h.seed['people'][0]
               for f in saved['validation']['findings'])
    assert h.lifecycle(saved['workspace'], 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


def test_existing_absent_assignment_can_change_details_or_be_removed_without_another_override(h):
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    absence(h, 'approved_leave')
    value = payload(h, workspace, shift)
    value['participants'][0]['team_lead'] = True
    saved = h.command('coordinator', 'shift.update', value, revision=workspace['revision'])
    assert any(f['code'] == 'UNAVAILABLE' for f in saved['validation']['findings'])
    assert changes(h, workspace)[-1][1] == 'Edit session'
    removed = h.command('coordinator', 'shift.update', payload(h, saved['workspace'], shift, people=[]),
                        revision=saved['workspace']['revision'])
    assert not any(f['code'] == 'UNAVAILABLE' for f in removed['validation']['findings'])


def test_one_reason_covers_one_blocked_person_and_never_saves_several(h):
    first, second = h.seed['people'][:2]
    workspace = h.workspace()
    availability(h, first, 'approved_leave')
    availability(h, second, 'protected_time')
    refused = h.command('coordinator', 'shift.create',
                        payload(h, workspace, people=[first, second], override_reason=REASON),
                        revision=workspace['revision'], status=422)['error']
    assert refused['code'] == 'PERSON_UNAVAILABLE'
    assert set(refused['details']['people']) == {first, second}
    assert h.detail(workspace)['shifts'] == []

    saved = h.command('coordinator', 'shift.create',
                      payload(h, workspace, people=[first], override_reason=REASON),
                      revision=workspace['revision'])
    saved = h.command('coordinator', 'shift.create',
                      payload(h, saved['workspace'], people=[second], override_reason=REASON),
                      revision=saved['workspace']['revision'])
    assert len(h.detail(saved['workspace'])['shifts']) == 2


def test_reason_must_admit_every_blocker_on_create(h):
    person = h.seed['people'][0]
    workspace = h.workspace()
    external_id = availability(h, person, 'external_commitment', '2026-10-04T08:00:00+03:00',
                                '2026-10-04T09:00:00+03:00')
    availability(h, person, 'protected_time', '2026-10-04T11:00:00+03:00',
                 '2026-10-04T12:00:00+03:00')
    refused = h.command('coordinator', 'shift.create',
                        payload(h, workspace, start=START, end=END, override_reason=REASON),
                        revision=workspace['revision'], status=422)['error']
    assert refused['code'] == 'EDIT_INVALID'
    assert any(row['code'] == 'UNAVAILABLE' and row['person_id'] == person
               for row in refused['details']['findings'])
    assert h.detail(workspace)['shifts'] == []

    with transaction(h.app.state.engine, write=True) as db:
        db.delete(db.get(m.Availability, external_id))
        db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
    workspace = h.workspace()
    saved = h.command('coordinator', 'shift.create',
                      payload(h, workspace, start=START, end=END, override_reason=REASON),
                      revision=workspace['revision'])
    assert saved['shift_id']


@pytest.mark.parametrize('update', [False, True])
def test_overlapping_admissible_record_never_admits_a_blocking_one(h, update):
    """External commitment 08-10 overlapping protected time 09-11: the reason cannot admit the commitment."""
    person = h.seed['people'][0]
    workspace, shift = h.workspace(), None
    if update:
        workspace = h.shift(workspace, people=[])['workspace']
        shift = h.detail(workspace)['shifts'][0]
    availability(h, person, 'external_commitment', '2026-10-04T08:00:00+03:00', '2026-10-04T10:00:00+03:00')
    availability(h, person, 'protected_time', '2026-10-04T09:00:00+03:00', '2026-10-04T11:00:00+03:00')
    before = h.detail(workspace)
    refused = h.command('coordinator', 'shift.update' if update else 'shift.create',
                        payload(h, workspace, shift, override_reason=REASON),
                        revision=workspace['revision'], status=422)['error']
    assert refused['code'] == 'EDIT_INVALID', refused
    assert refused['message'] == ('Known unavailability overlaps this duty. '
                                  'This edit introduces a conflict that a manual reason cannot override')
    assert h.detail(workspace)['shifts'] == before['shifts']


@pytest.mark.parametrize('rule, code', [
    ({'kind': 'min_rest', 'hours': 24}, 'MIN_REST'),
    ({'kind': 'forbidden_transition', 'from_type_ids': ['{type}'], 'to_type_ids': ['{type}']}, 'FORBIDDEN_TRANSITION'),
])
def test_manual_create_saves_rest_and_next_day_findings_without_a_reason(h, rule, code):
    """Like a double booking (maintenance ruling a, dev behaviour): a manual create saves with the safety finding,
    which then blocks submission; no reason is asked for or able to admit it."""
    person = h.seed['people'][0]
    h.policy({k: [h.seed['type']] if v == ['{type}'] else v for k, v in rule.items()})
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    saved = h.command('coordinator', 'shift.create',
                      payload(h, workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00'),
                      revision=workspace['revision'])
    assert any(f['code'] == code and f['severity'] == 'error' for f in saved['validation']['findings']), saved
    assert h.lifecycle(saved['workspace'], 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


def test_new_reason_does_not_replace_retained_person_reason(h):
    first, second = h.seed['people'][:2]
    availability(h, first, 'approved_leave')
    availability(h, second, 'protected_time')
    original_reason = 'Original reviewed leave admission'
    new_reason = 'New reviewed protected-time admission'
    workspace = h.workspace()
    saved = h.command('coordinator', 'shift.create',
                      payload(h, workspace, people=[first], override_reason=original_reason),
                      revision=workspace['revision'])
    shift = h.detail(saved['workspace'])['shifts'][0]
    updated = h.command('coordinator', 'shift.update',
                        payload(h, saved['workspace'], shift, people=[first, second], override_reason=new_reason),
                        revision=saved['workspace']['revision'])
    reasons = {row['person_id']: row['override_reason']
               for row in h.detail(updated['workspace'])['shifts'][0]['participants']}
    assert reasons == {first: original_reason, second: new_reason}


def test_override_audit_names_blocker_without_private_reason(h):
    person = h.seed['people'][0]
    workspace = h.workspace()
    availability_id = availability(h, person, 'approved_leave')
    h.command('coordinator', 'shift.create',
              payload(h, workspace, override_reason=REASON), revision=workspace['revision'])
    with transaction(h.app.state.engine) as db:
        event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'shift.create').order_by(
            m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc())).first()
        assert event.reason == REASON  # the typed reason is audited; the blocker ids are in detail
        assert REASON not in json.dumps(event.detail)
        assert {'code': 'UNAVAILABLE', 'person_id': person, 'availability_id': availability_id} in \
            event.detail['assignment_override_blockers']


def test_moving_existing_assignment_into_leave_requires_reason(h):
    workspace = h.shift(h.workspace(), start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00')['workspace']
    shift = h.detail(workspace)['shifts'][0]
    absence(h, 'approved_leave')
    refused = h.command('coordinator', 'shift.update', payload(h, workspace, shift),
                        revision=workspace['revision'], status=422)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'


@pytest.mark.parametrize('kind', ['approved_leave', 'protected_time', 'rotation', 'zero_limit', 'zero_quota', 'zero_hours'])
@pytest.mark.parametrize('engine', ['cp_sat', 'greedy'])
def test_automatic_scheduling_never_uses_manual_override(h, kind, engine):
    """No luck: the control proves person 0 is the pick without the absence, so a PASS proves it is read.

    The earlier form (any addition, none of them person 0) also passed on a tree that ignored leave, when the
    solver happened to break a tie towards someone else.
    """
    from packages.domain.cp_sat import generate as cp_generate
    from packages.domain.generation import generate as greedy_generate
    generate = cp_generate if engine == 'cp_sat' else greedy_generate
    pid = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[])['workspace']

    def only_person_0(data):
        data['people'] = {pid: data['people'][pid]}
        return data

    control = generate(only_person_0(h.snapshot(workspace)), time_budget_seconds=2)
    assert [item['participant']['person_id'] for item in control['additions']] == [pid], control
    absence(h, kind)
    workspace = h.refresh_draft_baseline(workspace)
    alone = generate(only_person_0(h.snapshot(workspace)), time_budget_seconds=2)
    assert all(item['participant']['person_id'] != pid for item in alone['additions']), alone
    result = generate(h.snapshot(workspace), time_budget_seconds=2)
    assert result['additions'], result
    assert all(item['participant']['person_id'] != pid for item in result['additions'])


@pytest.mark.parametrize('field,value,rule', [
    ('team_lead', True, {'metric': 'lead_turns'}),
    ('overtime', True, {'metric': 'duties', 'overtime': 'only'}),
    ('participation', 'trainee', {'metric': 'duties', 'participation': ['trainee']}),
])
def test_changing_existing_participation_into_a_zero_maximum_needs_reason(h, field, value, rule):
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
              'comparison': 'maximum', 'amount': 0, **rule}, {'person_id': h.seed['people'][0]})
    workspace = h.workspace()
    workspace = h.command('coordinator', 'shift.create', payload(h, workspace), revision=workspace['revision'])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    changed = payload(h, workspace, shift)
    changed['participants'][0][field] = value
    refused = h.command('coordinator', 'shift.update', changed, revision=workspace['revision'], status=422)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'
    saved = h.command('coordinator', 'shift.update', {**changed, 'override_reason': REASON},
                      revision=workspace['revision'])
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in saved['validation']['findings'])


@pytest.mark.parametrize('relaxation', ['advisory', 'approved_exception'])
def test_canonical_zero_maximum_relaxations_do_not_need_a_manual_override(h, relaxation):
    policy = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'metric': 'duties', 'comparison': 'maximum', 'amount': 0}, {'person_id': h.seed['people'][0]},
        severity='advisory' if relaxation == 'advisory' else 'mandatory', exception=True)
    if relaxation == 'approved_exception':
        exception = h.command('coordinator', 'exception.create', {'policy_id': policy['id'],
            'person_id': h.seed['people'][0], 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
            'reason': 'Reviewed full-block exception'})['record']
        h.command('approver', 'exception.approve', {'reason': 'Independent full-block approval'},
                  exception['id'], exception['revision'])
    workspace = h.workspace()
    saved = h.command('coordinator', 'shift.create', payload(h, workspace), revision=workspace['revision'])
    assert saved['validation']['valid']
    assert h.detail(saved['workspace'])['shifts'][0]['participants'][0]['override_reason'] is None


def test_bulk_session_create_keeps_visible_leave_conflict_without_interactive_override(h):
    from tests.test_bulk_draft_session_creation import creation
    workspace = h.workspace()
    absence(h, 'approved_leave')
    item = creation(h, workspace)
    item['payload']['participants'] = payload(h, workspace)['participants']
    saved = h.command('coordinator', 'bulk.apply', {'items': [item], 'reason': 'Create reviewed sessions'})
    assert saved['counts']['changed'] == 1
    detail = h.detail(workspace)
    assert detail['shifts'][0]['participants'][0]['override_reason'] is None
    assert any(f['code'] == 'UNAVAILABLE' and f['severity'] == 'error'
               for f in detail['workspace']['validation']['findings'])
    assert h.lifecycle(detail['workspace'], 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.detail['batch_id'].as_string() == saved['batch_id'])))
        assert {event.action: event.reason for event in events} == {
            'shift.create': 'Create reviewed sessions', 'bulk.apply': 'Create reviewed sessions'}


def test_override_reason_survives_unrelated_edits_undo_and_publication_without_staff_disclosure(h):
    workspace = h.workspace()
    absence(h, 'approved_leave')
    saved = h.command('coordinator', 'shift.create', payload(h, workspace, override_reason=REASON),
                      revision=workspace['revision'])
    workspace = saved['workspace']
    shift = h.detail(workspace)['shifts'][0]
    edited = h.command('coordinator', 'shift.update', {**payload(h, workspace, shift),
        'location_id': h.seed['locations'][1], 'override_reason': REASON}, revision=workspace['revision'])
    assert h.detail(edited['workspace'])['shifts'][0]['participants'][0]['override_reason'] == REASON
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore original reviewed assignment'},
                       workspace['id'], edited['workspace']['revision'])['workspace']
    assert h.detail(undone)['shifts'][0]['participants'][0]['override_reason'] == REASON
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.Availability).where(m.Availability.person_id == h.seed['people'][0])).retracted = True
        db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
    checked = h.command('coordinator', 'workspace.validate', {'reason': 'Recheck live leave inputs'},
                        undone['id'], undone['revision'])['workspace']
    submitted = h.lifecycle(checked, 'submit')['workspace']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    publication = h.lifecycle(approved, 'publish')
    planner = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']
    assert planner['shifts'][0]['participants'][0]['override_reason'] == REASON
    staff = h.get('staff', '/api/publications/' + publication['publication_id'])['snapshot']
    assert REASON not in str(staff)
    amendment = h.workspace(base=publication['publication_id'])
    assert h.detail(amendment)['shifts'][0]['participants'][0]['override_reason'] == REASON


def test_staff_projection_hides_own_and_paired_override_reasons():
    from apps.api.services.schedule_view import own_and_paired
    people = [{'person_id': 'trainee', 'supervisor_person_id': 'supervisor', 'override_reason': REASON},
              {'person_id': 'supervisor', 'override_reason': 'Private supervisory context'}]
    for person_id in ('trainee', 'supervisor'):
        projected = own_and_paired(people, person_id)
        assert {p['person_id'] for p in projected} == {'trainee', 'supervisor'}
        assert all('override_reason' not in p for p in projected)
    assert all(p['override_reason'] for p in people)


@pytest.fixture
def normal_assignment(h, monkeypatch):
    workspace = h.workspace()
    calls = []
    real = planning.evaluate

    def checked(data, **kwargs):
        calls.append(data)
        return real(data, **kwargs)

    monkeypatch.setattr(planning, 'evaluate', checked)
    started = time.process_time()
    saved = h.command('coordinator', 'shift.create', payload(h, workspace), revision=workspace['revision'])
    elapsed = time.process_time() - started
    return saved, calls, elapsed


def test_normal_assignment_needs_no_override_or_duplicate_evaluation(normal_assignment):
    saved, calls, _ = normal_assignment
    assert saved['validation']['valid'] and len(calls) == 1


@pytest.mark.cpu_budget
def test_normal_assignment_cpu_budget(normal_assignment):
    _, _, elapsed = normal_assignment
    assert elapsed < _budget(2), elapsed


@pytest.mark.parametrize('schema', [s.ShiftCreate, s.ShiftEdit])
@pytest.mark.parametrize('reason', ['', 'ab', 'x' * 501])
def test_override_reason_contract_is_bounded(schema, reason):
    from pydantic import ValidationError
    import uuid
    key = str(uuid.uuid4())
    with pytest.raises(ValidationError):
        schema.model_validate({'workspace_id': key, **({'shift_id': key} if schema is s.ShiftEdit else {}),
            'assignment_type_id': key, 'location_id': key, 'service_ids': [],
            'start': START, 'end': END, 'override_reason': reason})
