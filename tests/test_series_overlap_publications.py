"""Publication links and Schedule share current clash flags without rewriting issued evidence."""
import copy
import json
from datetime import date, timedelta
from random import Random

import pytest
from sqlalchemy import event, func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import publication_snapshot
from packages.contracts.schemas import MAX_DUTY_DURATION_MINUTES, PERMISSIONS, ROLE_NAMES
from packages.domain.time import aware
from tests.governance_support import identity
from tests.test_leave_disclosure import setting
from tests.test_schedule_window import _big_month
from tests.test_series_overlap_leave_flags import absence, leave_values, NOTE
from tests.test_series_overlap_schedule import DAY
from tests.clock_support import freeze_clock


WINDOW = '/api/schedule/sessions?start=2026-10-05&end=2026-10-06'


def imported_publication(h, workspace):
    """A complete synthetic legacy publication, including conflicts ordinary publication rejects."""
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        snapshot = publication_snapshot(db, row, evaluation=row.validation)
        row.status = 'published'
        sequence = (db.scalar(select(func.max(m.Publication.sequence)).where(
            m.Publication.department_id == row.department_id)) or 0) + 1
        publication = m.Publication(workspace_id=row.id, department_id=row.department_id, sequence=sequence,
            starts_on=row.starts_on, ends_on=row.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], snapshot=snapshot, validation=row.validation,
            fingerprint='0' * 64)
        db.add(publication)
        db.flush()
        people = {p['person_id'] for shift in snapshot['shifts'] for p in shift['participants']}
        db.add_all(m.PublicationPerson(publication_id=publication.id, person_id=pid) for pid in people)
        return publication.id


def flags(rows):
    return {row['id']: row.get('double_booking_conflicts', []) for row in rows}


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_publication_leave_flags_obey_the_reader_and_display_setting_without_changing_evidence(h, mode, monkeypatch):
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    own, other = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own, other])['workspace']
    publication = h.publish(workspace)['publication_id']
    path = '/api/publications/' + publication
    before = h.get('coordinator', path)
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication)
        evidence = copy.deepcopy((issued.snapshot, issued.validation))
    recorded = [absence(h, pid) for pid in (own, other)]
    setting(h, mode)
    identity(h, 'programme_lead', ['programme_lead'])
    identity(h, 'staff_lead', ['staff', 'programme_lead'], own)
    for role in (*ROLE_NAMES, 'staff_lead'):
        roles = set(h.get(role, '/api/me')['roles'])
        team = bool(roles.intersection(PERMISSIONS['publications.read_team']))
        allowed = team or 'staff' in roles
        body = h.get(role, path, status=200 if allowed else 403)
        if not allowed:
            continue
        conflicts = body['snapshot']['shifts'][0]['double_booking_conflicts']
        assert {row['person_id'] for row in conflicts} == ({own, other} if team else {own})
        for row in conflicts:
            assert row['code'] == 'UNAVAILABLE'
            if not team or mode == 'none':
                assert row['label'] == 'Unavailable' and 'leave' not in row
            else:
                assert row['label'] == ('Annual: ' + NOTE if mode == 'details' else 'Annual')
                assert row['leave'].get('note') == (NOTE if mode == 'details' else None)
        if not team or mode != 'details':
            assert NOTE not in json.dumps(body)
        values = list(leave_values(body))
        if not team or mode == 'none':
            assert not values
        elif mode == 'type':
            assert values and all(set(value) == {'type', 'starts_on', 'ends_on'} for value in values)
        else:
            assert values and all(value.get('note') == NOTE for value in values)
        if role != 'staff_lead':
            assert flags(body['snapshot']['shifts']) == flags(h.get(role, WINDOW)['shifts'])
    for row in recorded:
        h.command('coordinator', 'availability.retract', {'reason': 'Synthetic leave cancelled'}, row['id'], row['revision'])
    assert h.get('coordinator', path) == before
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication)
        assert (issued.snapshot, issued.validation) == evidence


def test_publication_flags_both_sessions_match_schedule_and_leave_issued_data_unchanged(h):
    own, other = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own, other])['workspace']
    workspace = h.shift(workspace, *DAY, people=[own, other], service=1)['workspace']
    publication = imported_publication(h, workspace)
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication)
        evidence = copy.deepcopy((issued.snapshot, issued.validation))
    for role in ('coordinator', 'staff'):
        body = h.get(role, '/api/publications/' + publication)
        projected = body['snapshot']['shifts']
        assert len(projected) == 2
        assert flags(projected) == flags(h.get(role, WINDOW)['shifts'])
        assert all(row['code'] == 'TIME_OVERLAP' for shift in projected for row in shift['double_booking_conflicts'])
        assert all({row['person_id'] for row in shift['double_booking_conflicts']} ==
                   ({own} if role == 'staff' else {own, other}) for shift in projected)
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication)
        assert (issued.snapshot, issued.validation) == evidence


def test_publication_link_includes_neighbouring_published_overnight_clash(h):
    previous = h.shift(h.workspace(start='2026-10-04', end='2026-10-06'),
        '2026-10-04T22:00:00+03:00', '2026-10-05T02:00:00+03:00')['workspace']
    first = h.publish(previous)['publication_id']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Publication, first).effective_until = date(2026, 10, 5)
    following = h.shift(h.workspace(start='2026-10-05', end='2026-10-06'),
        '2026-10-05T01:00:00+03:00', '2026-10-05T05:00:00+03:00')['workspace']
    second = imported_publication(h, following)
    window = '/api/schedule/sessions?start=2026-10-04&end=2026-10-06'
    for role in ('coordinator', 'staff'):
        expected = flags(h.get(role, window)['shifts'])
        for pid in (first, second):
            actual = flags(h.get(role, '/api/publications/' + pid)['snapshot']['shifts'])
            assert actual and all(actual[sid] == expected[sid] and actual[sid] for sid in actual)


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_maximum_duration_peer_survives_a_publication_replaced_two_dates_before_read(h, role):
    created = h.shift(h.workspace(start='2026-10-04', end='2026-10-07'),
        '2026-10-04T22:00:00+03:00', '2026-10-05T06:00:00+03:00')
    with transaction(h.app.state.engine, write=True) as db:
        duty = db.get(m.Shift, created['shift_id'])
        duty.end = aware(duty.start) + timedelta(minutes=MAX_DUTY_DURATION_MINUTES)
        for participant in db.scalars(select(m.Assignment).where(m.Assignment.shift_id == duty.id)):
            participant.end = duty.end
    first = imported_publication(h, created['workspace'])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Publication, first).effective_until = date(2026, 10, 5)
    following = h.shift(h.workspace(start='2026-10-05', end='2026-10-07'),
        '2026-10-06T02:00:00+03:00', '2026-10-06T05:00:00+03:00')
    second = imported_publication(h, following['workspace'])
    expected = flags(h.get(role, '/api/schedule/sessions?start=2026-10-06&end=2026-10-07')['shifts'])
    assert set(expected) == {following['shift_id']}
    assert {row['other_shift_id'] for row in expected[following['shift_id']]} == {created['shift_id']}
    actual = flags(h.get(role, '/api/publications/' + second)['snapshot']['shifts'])
    assert actual == expected


def test_superseded_publication_does_not_clash_with_its_replacement(h):
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    original = h.publish(workspace)['publication_id']
    replacement = h.publish(h.workspace(base=original))['publication_id']
    for pid in (original, replacement):
        rows = h.get('coordinator', '/api/publications/' + pid)['snapshot']['shifts']
        assert rows and not any(row.get('double_booking_conflicts') for row in rows)


def test_publication_overlap_flags_are_batched_at_department_scale(h):
    workspace = h.workspace('Synthetic published overlap month', '2026-10-01', '2026-11-01')
    contracts = h.detail(workspace)['workspace']['validation']['contracts']
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic person {n}') for n in range(147)]
        db.add_all(extra)
        db.flush()
        people = list(h.seed['people']) + [row.id for row in extra]
        db.get(m.Workspace, workspace['id']).validation = {**_big_month(db, workspace['id'], people,
            db.get(m.AssignmentType, h.seed['type']), h.seed['locations'], h.seed['services'],
            date(2026, 10, 1), Random(24), '2026-09-30T00:00:00Z'), 'contracts': contracts}
    publication = imported_publication(h, workspace)
    client, statements = h.client('coordinator'), []

    def queried(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        response = client.get('/api/publications/' + publication)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert response.status_code == 200, response.text
    rows = response.json()['snapshot']['shifts']
    assert len(rows) == 496 and any(row.get('double_booking_conflicts') for row in rows)
    assert len(statements) < 40, len(statements)
