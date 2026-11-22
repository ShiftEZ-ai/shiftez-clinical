"""Shared overlap flags on draft sessions and published schedule projections."""
import copy
import json
import time
from datetime import date, timedelta
from itertools import permutations
from random import Random
import uuid

import pytest

from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import schedule_view
from apps.api.services.schedule_view import staff_shift
from packages.contracts.schemas import MAX_DUTY_DURATION_MINUTES
from packages.domain.time import aware
from tests.test_schedule_window import _big_month
from tests.test_source_parity_series import series
from tests.test_series_reconciliation import materialise


DAY = ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')
REASON = 'Cancel one synthetic overlapping occurrence'


def test_empty_overlap_focus_does_not_build_reservations_or_events():
    from packages.domain.overlap import evaluate_overlaps

    class Unreadable:
        def __iter__(self):
            pytest.fail('An empty focus needs no reservation or event scan')

    assert evaluate_overlaps({'focus_ids': [], 'shifts': Unreadable()}) == []
    assert evaluate_overlaps({'shifts': Unreadable()}, focus_ids=[], commitments=Unreadable()) == []


def test_overlap_witness_matches_actual_flags_and_discards_incompatible_inputs(h):
    from packages.domain.overlap import per_session_overlaps
    from packages.domain.validation import evaluate

    own, other = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own])['workspace']
    workspace = h.shift(workspace, *DAY, people=[own, other], service=1)['workspace']
    source, witness = h.snapshot(workspace), {}
    for case in ('actual', 'absence', 'missing_fixed', 'rotation', 'manual_override', 'subjects', 'partial', 'quota', 'skip_cover'):
        data = copy.deepcopy(source)
        first = next(row for row in data['shifts'] if len(row['participants']) == 1)
        if case == 'absence':
            data['availability'] = [{'id': str(uuid.uuid4()), 'person_id': own,
                'kind': 'approved_leave', 'start': DAY[0], 'end': DAY[1]}]
        elif case in {'missing_fixed', 'manual_override'}:
            first['series_snapshot'] = {'definition': {'usual_assignees_fixed': True,
                'default_person_ids': [other if case == 'missing_fixed' else own]}}
            first['series_modified'] = case == 'manual_override'
            if case == 'missing_fixed':
                first['participants'] = [row for row in first['participants'] if row['person_id'] != other]
        elif case == 'rotation':
            data['rotations'] = [{'id': str(uuid.uuid4()), 'person_id': own,
                'department_id': data['workspace']['department_id'], 'starts_on': '2026-10-04',
                'ends_on': '2026-10-11', 'department_available': False}]
        elif case == 'subjects':
            data['person_rule_subjects'] = {own}
        elif case == 'partial':
            data['_partial'] = {'people': {own}, 'shifts': set(data['focus_ids'])}
        elif case == 'skip_cover':
            data['skip_coverage'] = True
        kwargs = {'quota_only': case == 'quota'}
        expected = evaluate(data, **kwargs)
        checked = evaluate({**data, '_overlap_witness': witness}, **kwargs)
        assert checked == expected and '_overlap_witness' not in checked
        assert ('conflicts' in witness) is (case in {'actual', 'manual_override'})
        assert per_session_overlaps(data, conflicts=witness.get('conflicts')) == per_session_overlaps(data)
        if not witness:
            witness['conflicts'] = ['A previous evaluation must not survive the next check']
    edited = copy.deepcopy(source)
    shifted = edited['shifts'][-1]
    shifted.update(start='2026-10-05T13:00:00+03:00', end='2026-10-05T17:00:00+03:00')
    for participant in shifted['participants']:
        participant.update(start=shifted['start'], end=shifted['end'])
    assert evaluate({**edited, '_overlap_witness': witness}) == evaluate(edited)
    assert witness['conflicts'] == []


def test_first_session_creation_reuses_one_overlap_sweep_and_skips_empty_previous_focus(h, monkeypatch):
    from packages.domain import overlap, validation

    workspace = h.workspace()
    calls, original = [], overlap.evaluate_overlaps

    def counted(data, **kwargs):
        calls.append(kwargs)
        return original(data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(overlap, 'evaluate_overlaps', counted)
        patch.setattr(validation, 'evaluate_overlaps', counted)
        response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
            'kind': 'shift.create', 'expected_revision': workspace['revision'], 'payload': {
                'workspace_id': workspace['id'], 'assignment_type_id': h.seed['type'],
                'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
                'start': DAY[0], 'end': DAY[1], 'participants': [{'person_id': h.seed['people'][0],
                    'start': DAY[0], 'end': DAY[1]}]}})
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0]['commitments'] is not None
    assert calls[0]['include_unavailability'] is False
    assert not response.json()['shifts'][0].get('double_booking_conflicts')
    published = h.publish(response.json()['workspace'])
    assert '_overlap_witness' not in h.get('coordinator', f"/api/publications/{published['publication_id']}")['snapshot']


def test_overlap_witness_preserves_mixed_own_external_order_for_simultaneous_duties():
    from packages.domain.overlap import per_session_overlaps
    from packages.domain.validation import evaluate
    from tests.test_schedule_score_scale import department

    source = department()
    source['policies'], source['availability'] = [], []
    people = list(source['people'])[:3]
    source['people'] = {pid: source['people'][pid] for pid in people}
    rows = []
    for sid, audience in [('z-own', people[:2]), ('a-external', people[1:2]), ('m-own', [people[0], people[2]])]:
        row = copy.deepcopy(source['shifts'][0])
        row['id'] = sid
        row['participants'] = [{**row['participants'][0], 'id': sid + pid, 'person_id': pid} for pid in audience]
        rows.append(row)
    source['focus_ids'] = ['z-own', 'm-own']
    for ordered in permutations(rows):
        data = {**source, 'shifts': list(ordered)}
        witness = {}
        assert evaluate({**data, '_overlap_witness': witness}) == evaluate(data)
        assert per_session_overlaps(data, conflicts=witness['conflicts']) == per_session_overlaps(data)


def flags(h, workspace):
    return {row['id']: [{key: value for key, value in flag.items() if key != 'cancel_actions'}
                       for flag in row.get('double_booking_conflicts', [])] for row in h.detail(workspace)['shifts']}


def test_cancel_actions_bounds_permissions_and_keeps_each_action_independent(h):
    from types import SimpleNamespace
    from apps.api.services.series_conflicts import cancel_actions

    workspace = h.shift(h.workspace(), *DAY, people=[h.seed['people'][0]])['workspace']
    created = h.shift(workspace, *DAY, service=1)
    conflicts = [flag for row in created['shifts'] for flag in row.get('double_booking_conflicts', [])]
    assert len(conflicts) == 2
    for allowed in (True, False):
        calls = []

        def permits(role):
            calls.append(role)
            return allowed and role == 'coordinator'

        actor = SimpleNamespace(department_id=h.seed['department_id'], permits=permits)
        with transaction(h.app.state.engine) as db:
            result = cancel_actions(db, actor, conflicts * 50)
        assert calls == ['coordinator', 'admin']
        expected = conflicts if allowed else [{**row, 'cancel_actions': []} for row in conflicts]
        assert result == expected * 50
        if allowed:
            result[0]['cancel_actions'][0]['payload']['shift_ids'].append('independent')
            assert result[2] == conflicts[0]


def test_planner_marks_both_occurrences_and_cancel_undo_restore_flags_and_audit(h):
    recurring = series(h, usual_assignees_fixed=True)
    workspace = materialise(h, recurring)
    first = h.detail(workspace)['shifts'][0]['id']
    created = h.shift(workspace, *DAY, service=1)
    workspace, second = created['workspace'], created['shift_id']
    before = flags(h, workspace)
    assert {row['id'] for row in created['shifts'] if row.get('double_booking_conflicts')} == {first, second}
    assert [(row['person_id'], row['other_shift_id']) for row in before[first]] == [(h.seed['people'][0], second)]
    assert [(row['person_id'], row['other_shift_id']) for row in before[second]] == [(h.seed['people'][0], first)]
    assert before[first][0]['other_clinic_ids'] == [h.seed['services'][1]]
    assert before[second][0]['other_series_id'] == recurring['id']
    checked = h.detail(workspace)['workspace']['validation']
    finding = next(row for row in checked['findings'] if row['code'] == 'TIME_OVERLAP')
    assert recurring['name'] in finding['message'] and 'cancel one occurrence' in finding['message']
    overlaps = [finding['detail']['overlap']]
    for row in h.detail(workspace)['shifts']:
        overlaps.extend(row.get('double_booking_conflicts', []))
    assert {row['shift_id'] for row in overlaps} == {first, second}
    for overlap in overlaps:
        actions = {action['side']: action for action in overlap['cancel_actions']}
        assert set(actions) == {'this', 'other'}
        assert actions['this']['payload']['shift_ids'] == [overlap['shift_id']]
        assert actions['other']['payload']['shift_ids'] == [overlap['other_shift_id']]
    cancellation = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [first], 'action': 'cancel', 'reason': REASON}, revision=workspace['revision'])
    assert {row['id'] for row in cancellation['shifts']} == {first, second}
    assert all(row['double_booking_conflicts'] == [] for row in cancellation['shifts'])
    cancelled = cancellation['workspace']
    assert all(not rows for rows in flags(h, cancelled).values())
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore synthetic cancellation'},
        workspace['id'], cancelled['revision'])['workspace']
    assert flags(h, restored) == before
    current = h.detail(restored)
    # Cancels resolve the current workspace revision at execution, keeping detail stable across Undo.
    assert all(action.get('expected_revision') is None for row in current['shifts']
               for conflict in row['double_booking_conflicts'] for action in conflict['cancel_actions'])
    with transaction(h.app.state.engine) as db:
        actions = list(db.scalars(select(m.AuditEvent.action).where(
            m.AuditEvent.action.in_(['shift.action', 'workspace.undo'])).order_by(m.AuditEvent.created_at)))
    assert actions == ['shift.action', 'workspace.undo']


def test_staff_projection_keeps_only_own_overlap_and_preserves_source():
    shift = {'participants': [{'person_id': 'mine', 'supervisor_person_id': 'paired'}, {'person_id': 'paired'}],
             'attributes': {}, 'type_snapshot': {'definition': {}},
             'double_booking_conflicts': [{'person_id': 'mine', 'other_shift_id': 'other-own-duty'},
                                           {'person_id': 'paired', 'other_shift_id': 'private-supervisor-duty'}]}
    before = copy.deepcopy(shift)
    assert staff_shift(shift, 'mine')['double_booking_conflicts'] == shift['double_booking_conflicts'][:1]
    assert shift == before


def publish_synthetic_rows(h, workspace, sequence=1):
    """Exercise reads of imported overlap evidence without bypassing the publication content guard."""
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        row.status = 'published'
        publication = m.Publication(workspace_id=row.id, department_id=row.department_id, sequence=sequence,
            starts_on=row.starts_on, ends_on=row.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], snapshot={}, validation=row.validation,
            fingerprint='0' * 64)
        db.add(publication)
        db.flush()
        people = set(db.scalars(select(m.Assignment.person_id).join(m.Shift).where(m.Shift.workspace_id == row.id)))
        db.add_all(m.PublicationPerson(publication_id=publication.id, person_id=pid) for pid in people)


def test_published_schedule_flags_both_sessions_and_obeys_staff_identity_scope(h):
    own, other = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own, other])['workspace']
    workspace = h.shift(workspace, *DAY, people=[own, other], service=1)['workspace']
    publish_synthetic_rows(h, workspace)
    path = '/api/schedule/sessions?start=2026-10-05&end=2026-10-06'
    planner = h.get('coordinator', path)
    assert len(planner['shifts']) == 2
    ids = {row['id'] for row in planner['shifts']}
    for row in planner['shifts']:
        assert {flag['person_id'] for flag in row['double_booking_conflicts']} == {own, other}
        assert {flag['other_shift_id'] for flag in row['double_booking_conflicts']} == ids - {row['id']}
        assert all(flag['other_clinic_names'] for flag in row['double_booking_conflicts'])
    staff = h.get('staff', path)
    assert len(staff['shifts']) == 2
    assert all({flag['person_id'] for flag in row['double_booking_conflicts']} == {own}
               for row in staff['shifts'])
    assert other not in staff['people']


@pytest.fixture
def department_schedule_overlaps(h, monkeypatch):
    own = h.seed['people'][0]
    preceding = h.shift(h.workspace('Synthetic preceding night', '2026-09-29', '2026-10-02'),
        '2026-09-29T22:00:00+03:00', '2026-09-30T06:00:00+03:00', people=[own], service=1)
    with transaction(h.app.state.engine, write=True) as db:
        duty = db.get(m.Shift, preceding['shift_id'])
        duty.end = aware(duty.start) + timedelta(minutes=MAX_DUTY_DURATION_MINUTES)
        for participant in db.scalars(select(m.Assignment).where(m.Assignment.shift_id == duty.id)):
            participant.end = duty.end
    publish_synthetic_rows(h, preceding['workspace'])
    with transaction(h.app.state.engine, write=True) as db:
        publication = db.scalar(select(m.Publication).where(m.Publication.workspace_id == preceding['workspace']['id']))
        publication.effective_until = date(2026, 9, 30)
    workspace = h.workspace('Synthetic overlap month', '2026-10-01', '2026-11-01')
    boundary = h.shift(workspace, '2026-10-01T02:00:00+03:00', '2026-10-01T05:00:00+03:00', people=[own])
    workspace = boundary['workspace']
    contracts = h.lifecycle(workspace, 'validate')['validation']['contracts']
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic overlap person {n}')
                 for n in range(147)]
        db.add_all(extra)
        db.flush()
        people = list(h.seed['people']) + [row.id for row in extra]
        typ = db.get(m.AssignmentType, h.seed['type'])
        row = db.get(m.Workspace, workspace['id'])
        row.validation = {**_big_month(db, row.id, people, typ, h.seed['locations'], h.seed['services'],
                                     date(2026, 10, 1), Random(21), '2026-09-30T00:00:00+00:00'),
                          'contracts': contracts}
    publish_synthetic_rows(h, workspace, sequence=2)
    client = h.client('coordinator')
    queries, windows, overlap_cpu, hydrated = [], [], [], []
    records = schedule_view.shift_records
    original, original_flags = schedule_view.published_shifts, schedule_view.session_overlap_flags

    def counted_records(db, rows):
        rows = list(rows)
        hydrated.extend(row.id for row in rows)
        return records(db, rows)

    def bounded_peers(db, publications, zone, start, end, **kwargs):
        assert (end - start).days <= 34, 'A month read must not scan historical publication duties'
        windows.append((start, end))
        began = time.process_time()
        try:
            return original(db, publications, zone, start, end, **kwargs)
        finally:
            overlap_cpu.append(time.process_time() - began)

    def measured_flags(*args, **kwargs):
        began = time.process_time()
        try:
            return original_flags(*args, **kwargs)
        finally:
            overlap_cpu.append(time.process_time() - began)

    monkeypatch.setattr(schedule_view, 'published_shifts', bounded_peers)
    monkeypatch.setattr(schedule_view, 'session_overlap_flags', measured_flags)
    monkeypatch.setattr(schedule_view, 'shift_records', counted_records)

    def queried(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        response = client.get('/api/schedule/sessions?start=2026-10-01&end=2026-11-01')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert response.status_code == 200, response.text
    return response.json()['shifts'], queries, windows, boundary['shift_id'], preceding['shift_id'], hydrated, sum(overlap_cpu)


def test_department_scale_schedule_flags_use_batched_queries(department_schedule_overlaps):
    shifts, queries, windows, boundary, preceding, hydrated, _ = department_schedule_overlaps
    assert len(shifts) == 497 and len(queries) < 50
    assert len(hydrated) <= len(shifts) + 5, len(hydrated)
    assert windows
    visible = next(row for row in shifts if row['id'] == boundary)
    assert {flag['other_shift_id'] for flag in visible['double_booking_conflicts']} == {preceding}
    assert any(row.get('double_booking_conflicts') for row in shifts)
    assert all(flag['other_shift_id'] != row['id'] for row in shifts for flag in row.get('double_booking_conflicts', []))


@pytest.mark.cpu_budget
def test_department_scale_schedule_overlap_cpu_budget(department_schedule_overlaps):
    from tests.test_scheduler_performance import _budget
    assert department_schedule_overlaps[-1] < _budget(2), department_schedule_overlaps[-1]


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_filtered_published_schedule_keeps_hidden_peer_overlap_flags(h, role):
    own, other = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own, other])['workspace']
    workspace = h.shift(workspace, *DAY, people=[own, other], service=1)['workspace']
    publish_synthetic_rows(h, workspace)
    path = '/api/schedule/sessions?start=2026-10-05&end=2026-10-06'
    before = h.get(role, path)
    selected = next(row for row in before['shifts'] if row['service_ids'] == [h.seed['services'][0]])
    for services, expected in (([h.seed['services'][0]], [selected]), ([], [])):
        response = h.client(role).get('/api/schedule/sessions', params={'start': '2026-10-05', 'end': '2026-10-06', 'filter': json.dumps({'conditions': {'conditions': [
            {'field': 'service_ids', 'op': 'any', 'values': services or [str(uuid.uuid4())]}]}})})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['shifts'] == expected
        assert set(result['services']) == set(services)
        if role == 'staff':
            assert other not in result['people']
            assert all(flag['person_id'] == own for row in result['shifts']
                       for flag in row['double_booking_conflicts'])


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
@pytest.mark.parametrize(('separate_publications', 'read_day'), [
    (False, '2026-10-05'), (True, '2026-10-05'), (True, '2026-10-06')])
def test_day_limited_schedule_flags_overnight_peers_without_returning_them(h, role, separate_publications, read_day):
    own, other = h.seed['people'][:2]
    created = h.shift(h.workspace('Synthetic overnight', '2026-10-05', '2026-10-07'),
        '2026-10-05T22:00:00+03:00', '2026-10-06T06:00:00+03:00', people=[own, other])
    workspace, visible_id = created['workspace'], created['shift_id']
    first_workspace = workspace
    if separate_publications:
        workspace = h.workspace('Synthetic next day', '2026-10-06', '2026-10-07')
    created = h.shift(workspace, '2026-10-06T02:00:00+03:00', '2026-10-06T05:00:00+03:00',
                      people=[own, other], service=1)
    peer_id = created['shift_id']
    if separate_publications:
        publish_synthetic_rows(h, first_workspace)
        with transaction(h.app.state.engine, write=True) as db:
            publication = db.scalar(select(m.Publication).where(m.Publication.workspace_id == first_workspace['id']))
            publication.effective_until = date(2026, 10, 6)
    publish_synthetic_rows(h, created['workspace'], sequence=2 if separate_publications else 1)
    if read_day == '2026-10-06':
        visible_id, peer_id = peer_id, visible_id
    read_end = '2026-10-06' if read_day == '2026-10-05' else '2026-10-07'
    path = f'/api/schedule/sessions?start={read_day}&end={read_end}'
    result = h.get(role, path)
    assert [row['id'] for row in result['shifts']] == [visible_id]
    flags = result['shifts'][0]['double_booking_conflicts']
    assert {flag['other_shift_id'] for flag in flags} == {peer_id}
    assert {flag['person_id'] for flag in flags} == ({own, other} if role == 'coordinator' else {own})
    assert all(flag['other_clinic_names'] for flag in flags)
    if separate_publications:
        assert len(result['sources']) == 1
        peer_workspace = created['workspace'] if read_day == '2026-10-05' else first_workspace
        assert h.get(role, path + '&exclude=' + peer_workspace['id'])['shifts'][0].get(
            'double_booking_conflicts', []) == []
    if role == 'staff':
        assert other not in result['people']
        assert [participant['person_id'] for participant in result['shifts'][0]['participants']] == [own]
