"""Shared collection filters and ordering apply before API paging and exports."""
import csv
from datetime import timedelta
import io
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk_single_commands import relationship
from .test_requests_search import both  # noqa: F401


def test_audit_change_kind_nested_filter_and_multi_sort_share_csv_order(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        base = dict(department_id=h.seed['department_id'], actor_id=h.seed['identities']['coordinator'],
                    reason='Synthetic collection ordering', detail={}, correlation_id='rc17-collections')
        rows = [m.AuditEvent(**base, action='policy.create', created_at=m.now() + timedelta(seconds=n))
                for n in range(105)]
        last = m.AuditEvent(**base, action='policy.activate', created_at=m.now() - timedelta(days=1))
        db.add_all([*rows, last, m.AuditEvent(**base, action='workspace.create', created_at=m.now())])
        db.flush()
        expected = [row.id for row in rows] + [last.id]
    tree = {'conditions': [{'field': 'kind', 'values': ['policy']}, {'match': 'any', 'conditions': [
        {'conditions': [{'field': 'action', 'values': ['policy.create']}]},
        {'field': 'action', 'values': ['policy.activate']}]}]}
    query = {'filter': json.dumps({'conditions': tree, 'sort': ['kind', 'asc'],
                                  'then_sort': [['action', 'desc'], ['created_at', 'asc']]})}
    client = h.client('auditor')
    pages = [client.get('/api/audit', params={**query, 'offset': offset, 'people': False}) for offset in (0, 100)]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    events = [row for page in pages for row in page.json()['events']]
    assert [row['id'] for row in events] == expected
    assert all(row['kind'] == 'policy' for row in events)
    exported = client.get('/api/audit.csv', params=query)
    assert exported.status_code == 200, exported.text
    assert [row[2] for row in list(csv.reader(io.StringIO(exported.text)))[1:]] == [row['action'] for row in events]


def test_operations_multi_sort_orders_the_complete_filtered_collection(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Outbox(department_id=h.seed['department_id'], event_key=f'rc17-order-{n}',
                        kind='rc17', payload={}, state='queued', created_at=m.now() + timedelta(seconds=n))
                for n in range(105)]
        last = m.Outbox(department_id=h.seed['department_id'], event_key='rc17-order-last',
                        kind='rc17', payload={}, state='failed', created_at=m.now() + timedelta(days=1))
        db.add_all([*rows, last])
        db.flush()
        expected = [row.id for row in reversed(rows)] + [last.id]
    query = {'kind': 'rc17', 'filter': json.dumps({'sort': ['state', 'desc'],
                                                  'then_sort': [['created_at', 'desc']]})}
    pages = [h.client('operator').get('/api/operations', params={**query, 'offset': offset}) for offset in (0, 100)]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    assert [row['id'] for page in pages for row in page.json()['outbox']] == expected
    assert all(page.json()['total'] == 106 for page in pages)


@pytest.mark.parametrize('path,role', [('/api/audit', 'auditor'), ('/api/audit.csv', 'auditor'),
                                       ('/api/operations', 'operator')])
def test_server_collection_sort_rejects_unreadable_and_duplicate_keys(h, path, role):
    for value in ({'sort': ['private_reason', 'asc']},
                  {'sort': ['id', 'asc'], 'then_sort': [['id', 'desc']]}):
        response = h.client(role).get(path, params={'filter': json.dumps(value)})
        assert response.status_code == 422, response.text


@pytest.mark.parametrize('kind,collection,field,reference', [
    ('grant', 'grants', 'capability_id', 'capabilities'),
    ('membership', 'memberships', 'category_id', 'categories'),
])
def test_relationship_collection_identifying_columns_are_read_only_and_projected(both, kind, collection, field, reference):
    h = both
    row, _, _ = relationship(h, kind, ends_on=None)
    definition = s.BULK_COLLECTIONS[collection]
    assert definition['columns'][field]['read_only'] is True
    assert field not in definition['fields']
    assert definition['parent_of'][reference] == field
    selected = {'collection': collection, 'where': {'filters': {'id': [row['id']]}}}
    response = h.client('coordinator').post('/api/bulk/rows', json={'selection': selected})
    assert response.status_code == 200, response.text
    assert response.json()['rows'][0][field] == row[field]
