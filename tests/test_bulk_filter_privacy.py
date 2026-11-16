"""Filtering and count diagnostics never widen the source's visibility scope."""
from datetime import date
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_requests_search import both, request_row  # noqa: F401
from .test_scheduling_facts_api import workload


@pytest.mark.parametrize('role', ['programme_lead', 'auditor'])
def test_private_request_names_never_become_filter_or_count_oracles(both, role):
    h = both
    identity(h, 'scoped_reader', [role], person=h.seed['people'][0])
    person = h.seed['people'][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person).display_name = 'Hidden request owner'
        db.add(request_row(h, person, 'Hidden request note'))
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'start': '2026-11-01T00:00:00+03:00', 'end': '2026-11-02T00:00:00+03:00',
        'private_reason': 'Hidden leave note'})
    h.command('coordinator', 'availability_pattern.propose', {'person_id': person, 'weekdays': [1],
        'start_time': '13:00', 'end_time': '17:00', 'starts_on': '2026-10-01', 'note': 'Hidden pattern note'})
    h.command('coordinator', 'wish.create', {'person_id': person, 'first_day': '2026-11-10',
        'last_day': '2026-11-10', 'kind': 'prefer_off', 'points': 2, 'note': 'Hidden preference note'})
    trees = [
        {'conditions': [{'field': 'person', 'values': ['Hidden request owner']}]},
        {'conditions': [{'field': 'person_id', 'values': [person]}]},
        {'conditions': [{'field': 'person', 'op': 'none', 'values': ['Nobody']}]},
        {'match': 'any', 'conditions': [{'field': 'person_id', 'values': [person]},
                                       {'field': 'status', 'op': 'none', 'values': ['declined']}]},
    ]
    for tree in trees:
        response = h.client('scoped_reader').get('/api/requests/all', params={
            'filter': json.dumps({'conditions': tree, 'as_of': '2026-10-04'})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page['items'] == [] and page['total'] == 0 and not page['has_more']
        assert page['counts']['to_decide'] == sum(page['counts']['stages'].values()) == 0


@pytest.mark.parametrize('role', ['programme_lead', 'auditor'])
def test_bulk_filters_and_names_keep_forbidden_and_self_only_sources_private(both, role):
    h = both
    identity(h, 'scoped_reader', [role], person=h.seed['people'][0])
    person = h.seed['people'][1]
    pattern = h.command('coordinator', 'availability_pattern.propose', {'person_id': person,
        'weekdays': [1], 'start_time': '13:00', 'end_time': '17:00', 'starts_on': '2026-10-01',
        'note': 'Private colleague pattern'})['pattern']
    tree = {'match': 'any', 'conditions': [{'field': 'person_ids', 'values': [person]},
                                         {'field': 'person_ids', 'op': 'none', 'values': [h.seed['people'][0]]}]}
    client = h.client('scoped_reader')
    for collection in ('requests', 'availability', 'swaps', 'availability_patterns'):
        selection = {'collection': collection, 'where': {'conditions': tree}, 'include_ids': [pattern['id']]}
        for endpoint, body in (('/api/bulk/resolve', selection),
                               ('/api/bulk/rows', {'selection': selection, 'limit': 1})):
            response = client.post(endpoint, json=body)
            if collection != 'availability_patterns' and not (collection == 'requests' and role == 'programme_lead'):
                assert response.status_code == 403, response.text
            else:
                assert response.status_code == 200, response.text
                assert response.json()['count'] == 0
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Person(organisation_id=h.seed['organisation_id'], display_name='Hidden foreign clinician',
                           staff_number='FOREIGN-PRIVATE')
        db.add(foreign); db.flush()
        foreign_id = foreign.id
    resolved = client.post('/api/bulk/resolve', json={'collection': 'people',
        'names': ['FOREIGN-PRIVATE'], 'include_ids': [foreign_id]})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()['count'] == 0 and resolved.json()['unmatched'] == ['FOREIGN-PRIVATE']
    assert resolved.json()['ambiguous'] == [] and resolved.json()['matches'] == [{'line': 'FOREIGN-PRIVATE', 'ids': []}]


def test_programme_fact_scope_precedes_condition_filter_and_historical_as_of(both):
    h = both
    lead, inside, outside = h.seed['people'][:3]
    identity(h, 'scoped_reader', ['programme_lead'], person=lead)
    programme = h.configure('programmes', {'name': 'Visible synthetic programme', 'starts_on': '2020-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=person,
            programme_id=programme['id'], starts_on=date(2020, 1, 1), ends_on=date(2027, 1, 1))
            for person in (lead, inside))
    visible = h.command('coordinator', 'person.workload.propose', workload(h, person_id=inside))['fact']
    hidden = h.command('coordinator', 'person.workload.propose', workload(h, person_id=outside))['fact']
    for day in ('2020-01-01', '2026-10-04', '2030-01-01'):
        for field in ('person_id', 'person'):
            value = outside if field == 'person_id' else 'Hidden fact owner'
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Person, outside).display_name = 'Hidden fact owner'
            response = h.client('scoped_reader').get('/api/requests/all', params={'filter': json.dumps({
                'conditions': {'conditions': [{'field': field, 'values': [value]}]}, 'as_of': day})})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page['items'] == [] and page['total'] == sum(page['counts']['stages'].values()) == 0
        selection = {'collection': 'scheduling_facts', 'where': {'as_of': day,
            'conditions': {'conditions': [{'field': 'person_ids', 'values': [outside]}]}},
            'include_ids': [hidden['id']]}
        for endpoint, body in (('/api/bulk/resolve', selection), ('/api/bulk/rows', {'selection': selection})):
            response = h.client('scoped_reader').post(endpoint, json=body)
            assert response.status_code == 200 and response.json()['count'] == 0, response.text
    response = h.client('scoped_reader').get('/api/requests/all')
    assert [row['id'] for row in response.json()['items']] == [visible['id']]
