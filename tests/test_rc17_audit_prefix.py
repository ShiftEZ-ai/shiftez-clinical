"""Audit action groups use literal prefixes in the page and its CSV query."""
import csv
import inspect
import io

import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_requests_search import both  # noqa: F401


def seed_actions(h, prefix):
    with transaction(h.app.state.engine, write=True) as db:
        base = dict(department_id=h.seed['department_id'], actor_id=h.seed['identities']['coordinator'],
                    reason='Synthetic literal-prefix check', detail={}, correlation_id='audit-prefix-test')
        match = m.AuditEvent(**base, action=prefix + '.create')
        decoy = prefix.replace('_', 'X').replace('%', 'X').replace('\\', '').replace('/', '')
        db.add_all([match, *(m.AuditEvent(**base, action=value) for value in (
            decoy + '.create', prefix, prefix + 'more.create', 'before' + prefix + '.create'))])
        db.flush()
        return match.id


@pytest.mark.parametrize('prefix', ['availability_pattern', 'change%kind', r'change\kind', r'change\%_kind', 'change/kind'])
def test_audit_query_treats_all_pattern_characters_literally(both, prefix):
    h = both
    expected = seed_actions(h, prefix)
    # These internal prefixes bypass the stricter public action-name validation deliberately.
    endpoint = next(route.endpoint for route in h.app.routes if getattr(route, 'path', None) == '/api/audit')
    audit_query = inspect.getclosurevars(endpoint).nonlocals['audit_query']
    token = h.client('auditor').cookies.get('rota_session')
    filters = dict(start=None, end=None, actor_id=None, action=[prefix], self_approved=False,
                   q='', person_id=None, target_id=None, collection_filter=s.CollectionFilter())
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, token, settings=h.settings)
        query, _ = audit_query(db, actor, filters)
        assert {event.id for event, _ in db.execute(query)} == {expected}


def test_audit_page_and_csv_share_literal_underscore_prefix_filter(both):
    h = both
    expected = seed_actions(h, 'availability_pattern')
    client = h.client('auditor')
    page = client.get('/api/audit', params={'action': 'availability_pattern'})
    assert page.status_code == 200, page.text
    assert [event['id'] for event in page.json()['events']] == [expected]
    exported = client.get('/api/audit.csv', params={'action': 'availability_pattern'})
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert [row['action'] for row in rows] == ['availability_pattern.create']


def test_audit_page_and_csv_keep_public_action_name_validation(both):
    client = both.client('auditor')
    for action in ('change%kind', r'change\kind'):
        for path in ('/api/audit', '/api/audit.csv'):
            response = client.get(path, params={'action': action})
            assert response.status_code == 422, response.text
            assert response.json()['error']['code'] == 'INVALID_ACTION'
