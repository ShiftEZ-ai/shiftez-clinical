"""Active rule lines have the same read authority as exceptions and proposals."""
from inspect import getclosurevars
import json
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from apps.api import live, models as m
from apps.api.auth import effective_roles
from apps.api.db import transaction
from apps.api.services import audit_log
from apps.api.services.common import CATALOGUES
from packages.contracts import schemas as s
from .governance_support import identity
from .test_rule_catalogue_api import approve, line, person, propose


NOTE = 'Synthetic confidential active line note'
DATABASES = ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)]


@pytest.fixture
def active_line(h):
    for role in s.ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
    return approve(h, propose(h, 'duties_per_week', [line(
        who=person(h.seed['people'][0]), numbers={'max': {'value': 5}}, note=NOTE)])['record'])


def test_every_catalogue_kind_declares_its_read_permission(h):
    route = next(route for route in h.app.routes if getattr(route, 'path', None) == '/api/catalogues')
    lists = getclosurevars(route.endpoint).nonlocals['LISTS']
    assert (set(lists) | set(CATALOGUES)) <= s.CATALOGUE_READS.keys()
    assert set(s.CATALOGUE_READS) == set(lists) | {'policy_lines'}
    assert all(entry['permission'] in s.PERMISSIONS for entry in s.CATALOGUE_READS.values())


def test_frontend_rules_capability_uses_contract_permission():
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/dom.ts').read_text(encoding='utf-8')
    capability = re.search(r"\bRULES_READ\s*:\s*'([^']+)'", source)
    assert capability is not None
    assert capability.group(1) == 'rules.read'
    assert s.CAPABILITIES[capability.group(1)] == s.PERMISSIONS['rules.read']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_every_rule_action_holder_can_read_rules(role):
    actions = {permission: roles for permission, roles in s.PERMISSIONS.items()
               if permission.partition('.')[0] in {'rule', 'policy', 'exception'}}
    assert actions
    held = effective_roles({role})
    owned = {permission for permission, roles in actions.items() if held.intersection(roles)}
    assert not owned or held.intersection(s.PERMISSIONS['rules.read']), (role, owned)


@pytest.mark.parametrize('h', DATABASES, indirect=True)
@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_active_person_line_is_private_on_every_surface(h, active_line, role):
    client = h.client(role)
    roles = set(client.get('/api/me').json()['roles'])
    reads = bool(roles.intersection(s.PERMISSIONS['rules.read']))
    catalogue = bool(roles.intersection(s.PERMISSIONS['catalogue.read']))
    policy_id = active_line['id']
    rule_line = active_line['definition']['rule']['lines'][0]
    subject = rule_line['who']['conditions'][0]['values'][0]
    privileged = h.client('coordinator').get('/api/catalogues?include=policies')
    for params in ({}, {'include': 'policies'}, {'boot': 'true'},
                   {'include': 'policies', 'known': ','.join(privileged.json()['_versions'].values())}):
        response = client.get('/api/catalogues', params=params)
        assert response.status_code == (200 if catalogue else 403), response.text
        if catalogue and not reads:
            assert response.json().get('policies', []) == []
            assert NOTE not in response.text and policy_id not in response.text
        if reads and not params:
            assert NOTE in response.text and subject in response.text
    response = client.get('/api/catalogues?counts_only=true&include=policies')
    assert response.status_code == (200 if catalogue else 403), response.text
    if catalogue:
        body = response.json()
        assert body['rules_in_force'] > 0
        assert body['rules_in_force'] == h.get('coordinator', '/api/catalogues?counts_only=true')['rules_in_force']
        assert body['counts']['policies'] > 0
        if not reads:
            assert body['counts']['policies'] == body['rules_in_force']
        assert all(value not in response.text for value in (NOTE, policy_id, subject, rule_line['id']))

    paths = [
        '/api/policies?status=active&effective=true',
        '/api/policies/applies?person_id=' + subject + '&day=2026-10-04',
        '/api/policies/reach?day=2026-10-04',
        '/api/policies/capabilities', '/api/policies/groups',
        '/api/policies/' + policy_id + '/review', '/api/policies/' + policy_id + '/impact',
        '/api/rules/catalogue?version=1', '/api/rules/catalogue?version=2',
        '/api/rules/catalogue/duties_per_week/detail',
        '/api/rules/catalogue/duties_per_week/history',
        '/api/search/references?collection=policy_lines&kind=people&q=Synthetic',
        '/api/search/references?collection=policies&kind=people&q=Synthetic',
    ]
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        for path in paths:
            response = client.get(path)
            if not reads:
                assert response.status_code == 403, (path, response.text)
                assert all(value not in response.text for value in (NOTE, subject, policy_id, rule_line['id']))
            elif path.startswith('/api/policies?'):
                assert response.status_code == 200, response.text
                assert NOTE in response.text and subject in response.text
                assert policy_id in response.json()['effective']
            elif path.startswith('/api/rules/catalogue?'):
                assert response.status_code == 200, response.text
                if path.endswith('version=1'):
                    assert NOTE in response.text and subject in response.text
        for collection in ('policies', 'policy_lines'):
            for method, path, payload in (
                ('post', '/api/bulk/rows', {'selection': {'collection': collection}}),
                ('post', '/api/bulk/rows', {'selection': {'collection': collection, 'where': {'q': NOTE}}}),
                ('post', '/api/bulk/resolve', {'collection': collection, 'include_ids': [rule_line['id'] if collection == 'policy_lines' else policy_id]}),
            ):
                response = getattr(client, method)(path, json=payload)
                assert response.status_code == (200 if reads else 403), response.text
                if not reads:
                    assert all(value not in response.text for value in (NOTE, subject, policy_id))
                elif path == '/api/bulk/rows' and 'where' not in payload['selection']:
                    assert NOTE in response.text and subject in response.text
        if not reads:
            response = client.post('/api/policies/check', json={'definition': active_line['definition']})
            assert response.status_code == 403 and NOTE not in response.text
            for suffix, value in (
                ('relax', {'workspace_id': str(uuid4()), 'person_id': subject,
                           'binding_line_id': rule_line['id'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}),
                ('promote-adjustment', {'workspace_id': str(uuid4()), 'adjustment_id': str(uuid4())}),
            ):
                response = client.post('/api/policies/' + policy_id + '/' + suffix, json=value)
                assert response.status_code == 403 and NOTE not in response.text
            payload = {'selection': {'collection': 'policy_lines', 'include_ids': [rule_line['id']]},
                       'changes': [{'field': 'note', 'value': 'Synthetic replacement note'}],
                       'reason': 'Synthetic privacy preview'}
            response = client.post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
            assert response.status_code == 403, response.text
            h.command(role, 'bulk.apply', payload, status=403)
            assert not any('from policies' in statement or 'from policy_exceptions' in statement
                           for statement in statements), statements
        elif roles.intersection(s.PERMISSIONS['rule.propose']):
            response = client.post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': {
                'selection': {'collection': 'policy_lines', 'include_ids': [rule_line['id']]},
                'changes': [{'field': 'note', 'value': NOTE + ' revised'}],
                'reason': 'Synthetic authorized rule preview'}})
            assert response.status_code == 200, response.text
            assert NOTE in response.text and subject in response.text
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    for kind in ('policy.activate', 'rule.propose', 'exception.create'):
        changes = live.command_changes(kind, {'person_id': subject, 'note': NOTE, 'rule_id': policy_id},
                                       {'record': active_line, 'rule': active_line}, target_id=policy_id)
        assert changes == [{'scope': 'rules', 'id': None}]
        assert all(value not in json.dumps(changes) for value in (NOTE, subject, policy_id, rule_line['id']))
    if not reads:
        response = client.post('/api/planner/why', json={'workspace_id': str(uuid4()),
            'expected_revision': 1, 'shift_ids': [str(uuid4())]})
        assert response.status_code == 403 and NOTE not in response.text


@pytest.mark.parametrize('h', DATABASES, indirect=True)
def test_audit_rule_privacy_filters_json_envelopes_before_reading(h):
    cases = [
        ('policy.activate', {}, False), ('rule.propose', {}, False), ('exception.create', {}, False),
        ('bulk.apply', {'item_kinds': ['shift.update', 'rule.propose']}, False),
        ('bulk.apply', {'item_kinds': ['exception.end']}, False),
        ('bulk.apply', {'item_kinds': ['policy.retire']}, False),
        ('shift.update', {}, True), ('bulk.apply', {'item_kinds': ['shift.update']}, True),
        ('bulk.apply', {'item_kinds': []}, True), ('bulk.apply', {'item_kinds': None}, True),
        ('bulk.apply', {'note': 'rule.propose'}, True),
    ]
    with transaction(h.app.state.engine, write=True) as db:
        events = [m.AuditEvent(department_id=h.seed['department_id'],
            actor_id=h.seed['identities']['coordinator'], action=action, reason=NOTE,
            detail=detail, correlation_id='synthetic-policy-privacy') for action, detail, _ in cases]
        db.add_all(events)
        db.flush()
        expected = {row.id for row, (_, _, visible) in zip(events, cases) if visible}
        all_ids = {row.id for row in events}
        for role, visible in (('operator', expected), ('auditor', all_ids)):
            actor = SimpleNamespace(permits=lambda *allowed: bool(effective_roles({role}).intersection(allowed)))
            assert set(db.scalars(select(m.AuditEvent.id).where(
                m.AuditEvent.id.in_(all_ids), audit_log.read_scope(actor)))) == visible
