"""Published readers and Planner share current external-rotation flags and privacy."""
import copy
from datetime import date
import json

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from tests.test_leave_disclosure import setting
from tests.test_rotation_leave_policy import resident, START, END


WINDOW = '/api/schedule/sessions?start=2026-10-04&end=2026-10-05'


def flags(rows):
    return {row['id']: row.get('double_booking_conflicts', []) for row in rows}


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('mode', ['none', 'details'])
def test_current_external_rotations_match_planner_schedule_and_publication_without_changing_evidence(
        request, database, mode):
    h = request.getfixturevalue(database)
    own, paired, other = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Synthetic paired rotation clinic', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[
        {'person_id': own, 'start': START, 'end': END, 'participation': 'trainee', 'supervisor_person_id': paired},
        {'person_id': paired, 'start': START, 'end': END, 'participation': 'supervisor'}, other])['workspace']
    publication = h.publish(workspace)['publication_id']
    publication_path = '/api/publications/' + publication
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication)
        evidence = copy.deepcopy((issued.snapshot, issued.validation))
    client = h.client('coordinator')
    original = client.get(WINDOW)
    assert original.status_code == 200 and not any(flags(original.json()['shifts']).values())

    _, value, _ = resident(h, 'allowed', create=False)
    with transaction(h.app.state.engine, write=True) as db:
        value = {**value, 'starts_on': date(2026, 10, 4), 'ends_on': date(2026, 10, 5)}
        rotations = [m.Rotation(department_id=h.seed['department_id'], **{**value, 'person_id': pid},
                                department_available=False) for pid in (own, paired, other)]
        db.add_all(rotations)
        db.add_all(m.Rotation(department_id=h.seed['department_id'], **{**value,
            'starts_on': start, 'ends_on': end}, department_available=available) for start, end, available in (
                (date(2026, 10, 3), date(2026, 10, 4), False),
                (date(2026, 10, 5), date(2026, 10, 6), False),
                (date(2026, 10, 4), date(2026, 10, 5), True)))
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        db.flush()
        rotation_ids = {row.person_id: row.id for row in rotations}
    setting(h, mode)

    planner = h.detail(workspace)['shifts']
    expected = flags(planner)
    assert len(expected) == 1
    assert sum(len(rows) for rows in expected.values()) == len(rotation_ids)
    assert {flag['person_id']: flag['availability_id'] for rows in expected.values() for flag in rows} == rotation_ids
    for rows in expected.values():
        assert all(flag['code'] == 'EXTERNAL_ROTATION' and flag['label'] == 'Unavailable'
                   and not flag.get('leave') for flag in rows)

    for path in (WINDOW, publication_path):
        statements = []

        def queried(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith('SELECT') and 'FROM rotations' in statement:
                statements.append(statement)

        event.listen(h.app.state.engine, 'before_cursor_execute', queried)
        try:
            response = client.get(path, headers={'if-none-match': original.headers['etag']} if path == WINDOW else {})
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', queried)
        assert response.status_code == 200, response.text
        body = response.json()
        rows = body['snapshot']['shifts'] if path == publication_path else body['shifts']
        assert flags(rows) == expected
        assert len(statements) == 1, 'Current rotations must be batch loaded across all participating people'

        staff = h.get('staff', path)
        own_rows = staff['snapshot']['shifts'] if path == publication_path else staff['shifts']
        assert {person['person_id'] for row in own_rows for person in row['participants']} == {own, paired}
        service_name = staff.get('snapshot', staff)['services'][h.seed['services'][0]]['name']
        assert flags(own_rows) == {sid: [{**flag, 'session_name': service_name} for flag in rows if flag['person_id'] == own]
                                   for sid, rows in expected.items()}
        assert rotation_ids[paired] not in json.dumps(staff) and rotation_ids[other] not in json.dumps(staff)
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication)
        assert (issued.snapshot, issued.validation) == evidence
