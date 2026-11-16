"""Workflow status choices do not silently narrow a collection's permission scope."""
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_request_decide_many import pattern_row
from .test_scheduling_facts_api import workload


def read(h, role, selection):
    found = h.client(role).post('/api/bulk/resolve', json=selection)
    assert found.status_code == 200, found.text
    payload = {'selection': selection, 'columns': ['status'], 'limit': 1, 'group': 'status'}
    pages = [h.client(role).post('/api/bulk/rows', json={**payload, 'offset': offset})
             for offset in range(found.json()['count'] + 1)]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    assert all(page.json()['count'] == found.json()['count'] for page in pages)
    rows = [row for page in pages for row in page.json()['rows']]
    assert {row['id'] for row in rows} == {row['id'] for row in found.json()['sample']}
    assert sum(group['count'] for group in pages[0].json()['groups']) == len(rows)
    assert h.client(role).post('/api/bulk/rows', json=payload).json() == pages[0].json()
    return rows


@pytest.mark.parametrize('path', ['flat', 'tree'])
def test_every_availability_pattern_status_is_readable_and_defaults_are_only_filters(h, path):
    states = [option['value'] for option in s.BULK_COLLECTIONS['availability_patterns']['status']['options']]
    expected = {}
    for status in states:
        row = pattern_row(h)
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.AvailabilityPattern, row['id']).status = status
        expected[status] = row['id']
    # The other person's ended pattern must remain outside the staff read even with explicit ticks.
    other = h.command('coordinator', 'availability_pattern.propose', {
        'person_id': h.seed['people'][1], 'weekdays': [1], 'start_time': '13:00',
        'end_time': '17:00', 'starts_on': '2026-10-01'})['pattern']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.AvailabilityPattern, other['id']).status = 'ended'
    for status in states:
        where = {'filters': {'status': [status]}} if path == 'flat' else {'conditions': {
            'conditions': [{'field': 'status', 'values': [status]}]}}
        selected = {'collection': 'availability_patterns', 'where': where, 'include_ids': [other['id']]}
        assert [row['id'] for row in read(h, 'staff', selected)] == [expected[status]]
    defaults = s.BULK_COLLECTIONS['availability_patterns']['status']['defaults']
    assert defaults == ['active', 'proposed']
    selected = {'collection': 'availability_patterns', 'where': {'filters': {'status': defaults}}}
    assert {row['id'] for row in read(h, 'staff', selected)} == {expected[key] for key in defaults}
    assert {row['id'] for row in read(h, 'staff', {'collection': 'availability_patterns'})} == set(expected.values())


@pytest.mark.parametrize('status', ['proposed', 'active', 'retired', 'declined'])
def test_programme_fact_status_filters_keep_all_states_inside_existing_person_scope(h, status):
    lead, inside, outside = h.seed['people'][:3]
    identity(h, 'status_lead', ['programme_lead'], person=lead)
    programme = h.configure('programmes', {'name': 'Status scope programme', 'starts_on': '2020-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=person,
            programme_id=programme['id'], starts_on=date(2020, 1, 1), ends_on=date(2027, 1, 1))
            for person in (lead, inside))
    records = [h.command('coordinator', 'person.workload.propose', workload(h, person_id=person))['fact']
               for person in (inside, outside)]
    with transaction(h.app.state.engine, write=True) as db:
        for row in records:
            db.get(m.SchedulingFact, row['id']).status = status
    for tree in (False, True):
        where = {'conditions': {'conditions': [{'field': 'status', 'values': [status]}]}} if tree else {
            'filters': {'status': [status]}}
        selected = {'collection': 'scheduling_facts', 'where': where, 'include_ids': [records[1]['id']]}
        assert [row['id'] for row in read(h, 'status_lead', selected)] == [records[0]['id']]
    history = h.get('status_lead', '/api/requests/all')
    assert [row['id'] for row in history['items']] == [records[0]['id']]
    pending = h.get('status_lead', '/api/requests/all?status=waiting')
    assert [row['id'] for row in pending['items']] == ([records[0]['id']] if status == 'proposed' else [])
    assert history['counts']['stages']['waiting'] == int(status == 'proposed')
    assert history['counts']['to_decide'] == 0  # Person facts remain an approver's decision.
    assert not history['items'][0]['can_decide']
