"""Condition trees filter authorized history before SQL counts and page limits."""
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import literal, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.collection_filters import predicate
from packages.contracts import schemas as s
from packages.domain.conditions import evaluate
from .test_bulk_single_commands import relationship
from .test_requests_search import both, request_row  # noqa: F401


def read(h, path, tree, role='operator', **params):
    response = h.client(role).get(path, params={
        'filter': json.dumps({'conditions': tree, 'as_of': '2026-10-04'}), **params})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('tree', [
    {'conditions': [{'field': 'state', 'op': 'none', 'values': ['queued']}]},
    {'match': 'any', 'conditions': [{'field': 'state', 'values': ['failed']},
                                  {'field': 'kind', 'values': ['absent']}]},
])
def test_outbox_negative_and_or_filters_find_older_event_beyond_first_page(both, tree):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        older = m.Outbox(department_id=h.seed['department_id'], event_key='old-failed', kind='notification',
                         payload={}, state='failed', created_at=m.now() - timedelta(days=2))
        db.add(older)
        db.add_all(m.Outbox(department_id=h.seed['department_id'], event_key=f'new-queued-{n}',
                           kind='notification', payload={}, state='queued', created_at=m.now()) for n in range(100))
        db.flush()
        expected = older.id
    page = read(h, '/api/operations', tree)
    assert page['total'] == 1 and not page['has_more']
    assert [r['id'] for r in page['outbox']] == [expected]


def test_requests_condition_tree_filters_complete_sources_and_stage_counts(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(request_row(h, h.seed['people'][0], 'Synthetic pending', day=n) for n in range(105))
        old = request_row(h, h.seed['people'][0], 'Synthetic decided', status='rejected', kind='meeting')
        db.add(old)
        db.flush()
        expected = old.id
    tree = {'match': 'all', 'conditions': [{'field': 'status', 'op': 'none', 'values': ['waiting']},
        {'match': 'any', 'conditions': [{'field': 'type', 'values': ['other']},
                                      {'field': 'person_id', 'values': [h.seed['people'][1]]}]}]}
    page = read(h, '/api/requests/all', tree, 'approver')
    assert page['total'] == 1 and [r['id'] for r in page['items']] == [expected]
    assert page['counts']['stages']['declined'] == 1 and sum(page['counts']['stages'].values()) == 1


def test_audit_condition_tree_and_csv_apply_before_paging(h):
    with transaction(h.app.state.engine, write=True) as db:
        base = dict(department_id=h.seed['department_id'], actor_id=h.seed['identities']['coordinator'],
                    reason='Synthetic history filter', detail={}, correlation_id='collection-filter-test')
        old = m.AuditEvent(**base, action='catalogue.create', created_at=m.now() - timedelta(days=2))
        db.add(old)
        db.add_all(m.AuditEvent(**base, action='workspace.update', created_at=m.now()) for _ in range(100))
        db.flush()
        expected = old.id
    tree = {'conditions': [{'field': 'action', 'op': 'none', 'values': ['workspace.update', 'auth.sign_in']}]}
    page = read(h, '/api/audit', tree)
    assert [r['id'] for r in page['events']] == [expected] and not page['has_more']
    response = h.client('operator').get('/api/audit.csv', params={'filter': json.dumps({'conditions': tree})})
    assert response.status_code == 200 and 'catalogue.create' in response.text
    assert 'workspace.update' not in response.text


@pytest.mark.parametrize('path,role', [('/api/operations', 'operator'), ('/api/requests/all', 'approver'),
                                       ('/api/audit', 'operator'), ('/api/audit.csv', 'operator')])
def test_paged_condition_filters_reject_unreadable_fields_and_invalid_trees(h, path, role):
    client = h.client(role)
    for supplied in ('{invalid', json.dumps({'conditions': {'conditions': [
            {'field': 'private_reason', 'op': 'none', 'values': ['hidden']}]}})):
        response = client.get(path, params={'filter': supplied})
        assert response.status_code == 422, response.text
    response = h.client('staff').get(path, params={'filter': json.dumps({'conditions': {
        'conditions': [{'field': 'id', 'op': 'none', 'values': ['absent']}]}})})
    assert response.status_code == (200 if path == '/api/requests/all' else 403), response.text


@pytest.mark.parametrize('op', ['any', 'all', 'none'])
@pytest.mark.parametrize('held', [('queued', None), ('queued', 'failed'), (None, None)])
def test_sql_leaves_and_shared_condition_evaluator_agree_for_null_and_multiple_values(h, op, held):
    tree = s.CollectionConditions.model_validate({'match': 'any', 'conditions': [
        {'match': 'all', 'conditions': [{'field': 'state', 'op': op, 'values': ['queued', 'failed']}]},
        {'field': 'kind', 'values': ['missing']} ]})
    values = {'state': {v for v in held if v is not None}, 'kind': set()}
    expected = evaluate(tree.model_dump(mode='json'), values.__getitem__)
    with transaction(h.app.state.engine) as db:
        actual = db.scalar(select(predicate(tree, {'state': tuple(literal(v) for v in held), 'kind': literal(None)})))
    assert actual is expected


def test_generic_collection_contract_keeps_tree_limits_and_shared_view_privacy():
    tree = {'conditions': [{'field': 'state', 'op': 'none', 'values': ['queued']}]}
    saved = s.TableViewDefinition.model_validate({'conditions': tree, 'as_of': '2026-10-04'})
    assert saved.conditions.fields() == {'state'} and saved.as_of.isoformat() == '2026-10-04'
    with pytest.raises(ValidationError):
        s.Scope.model_validate({'conditions': tree})  # Clinical scopes retain their domain field allow-list.
    assert s.CollectionConditions.model_validate({'conditions': [{'conditions': [tree]}]})
    for _ in range(6):
        tree = {'conditions': [tree]}
    with pytest.raises(ValidationError, match='five levels'):
        s.CollectionConditions.model_validate(tree)
    for field in ('person', 'person_ids', 'person.staff_number', 'identity.display_name'):
        personal = s.CollectionConditions.model_validate({'conditions': [{'field': field, 'values': ['someone']}]})
        with pytest.raises(ValueError, match='cannot filter by person'):
            s.department_view_filters(None, conditions=personal)


def test_bulk_child_status_tree_uses_as_of_before_resolve_count_and_cached_paging(h):
    revoked, payload, role = relationship(h, 'grant', ends_on=None)
    other = h.command(role, 'grant.create', {**payload, 'person_id': h.seed['people'][1]})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.CapabilityGrant, revoked['id']).revoked = True
    selection = {'collection': 'grants', 'where': {'as_of': '2026-10-04',
        'filters': {'id': [revoked['id'], other['id']]}, 'conditions': {
            'conditions': [{'field': 'status', 'op': 'none', 'values': ['current']}]}}}
    for _ in range(2):
        found = h.client('coordinator').post('/api/bulk/resolve', json=selection)
        rows = h.client('coordinator').post('/api/bulk/rows', json={'selection': selection, 'limit': 1})
        assert found.status_code == rows.status_code == 200, found.text + rows.text
        assert found.json()['count'] == rows.json()['count'] == 1
        assert rows.json()['rows'][0]['id'] == revoked['id'] and rows.json()['rows'][0]['status'] == 'ended'
    selection['where']['as_of'] = '2026-09-30'
    assert h.client('coordinator').post('/api/bulk/resolve', json=selection).json()['count'] == 2


def test_request_condition_tree_never_widens_self_scope(h):
    with transaction(h.app.state.engine, write=True) as db:
        mine = request_row(h, h.seed['people'][0], 'Private own reason', status='rejected')
        db.add_all([mine, request_row(h, h.seed['people'][1], 'Private colleague reason', status='rejected')])
        db.flush()
        expected = mine.id
    page = read(h, '/api/requests/all', {'conditions': [
        {'field': 'status', 'op': 'none', 'values': ['waiting']}]}, 'staff')
    assert page['total'] == 1 and [row['id'] for row in page['items']] == [expected]


@pytest.mark.parametrize('collection', ['sessions', 'recurring_series'])
def test_participant_conditions_keep_row_facets_in_the_same_tree(h, collection):
    person = h.seed['people'][0]
    if collection == 'sessions':
        workspace = h.shift(h.workspace())['workspace']
        row = h.detail(workspace)['shifts'][0]
    else:
        row = h.configure('recurring_series', {'name': 'Synthetic participants', 'starts_on': '2020-01-01',
            'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0],
            'definition': {'anchor': '2026-10-04', 'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
                           'service_ids': [h.seed['services'][0]], 'default_person_ids': [person]}})['record']
    selection = {'collection': collection, 'where': {'as_of': '2026-10-04', 'filters': {'id': [row['id']]},
        'conditions': {'conditions': [{'field': 'person_ids', 'values': [person]},
                                      {'field': 'status', 'values': ['current']}]}}}
    for op, count in [('any', 1), ('none', 0)]:
        selection['where']['conditions']['conditions'][1]['op'] = op
        for path, body in [('/api/bulk/resolve', selection), ('/api/bulk/rows', {'selection': selection})]:
            response = h.client('coordinator').post(path, json=body)
            assert response.status_code == 200, response.text
            assert response.json()['count'] == count
