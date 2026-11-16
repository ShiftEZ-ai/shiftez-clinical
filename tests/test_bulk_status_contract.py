"""Collection workflow and dated statuses share one advertised vocabulary on every read path."""
from copy import deepcopy
from datetime import date
import json
import re

import pytest
from sqlalchemy import CheckConstraint

from apps.api import models as m
from apps.api.commands import command_roles
from apps.api.db import transaction
from apps.api.services import bulk_selection as bulk
from apps.api.services.bulk_selection import MODELS, collection_readers
from packages.contracts import schemas as s
from packages.domain.worker_contract import OUTBOX_STATES
from scripts.generate_contracts import render
from .test_bulk_single_commands import relationship
from .test_bulk_selection import actor
from .test_rule_catalogue_api import line, person, propose
from .test_scheduling_fact_approvals import propose as propose_fact


def post(h, endpoint, payload):
    collection = payload.get('collection', payload.get('selection', {}).get('collection'))
    role = 'operator' if collection == 'outbox' else 'auditor' if collection == 'history' else 'admin'
    response = h.client(role).post('/api/bulk/' + endpoint, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_registry_status_metadata_matches_workflow_constraints_and_generated_contract():
    source, _ = render()
    exported = next(line for line in source.splitlines() if line.startswith('export const BULK_COLLECTIONS = '))
    exported = json.loads(exported.removeprefix('export const BULK_COLLECTIONS = ').removesuffix(' as const;'))
    assert exported == json.loads(json.dumps(s.bulk_collection_registry(command_roles, collection_readers)))
    for key, collection in s.BULK_COLLECTIONS.items():
        status = collection['status']
        choices = {option['value'] for option in status['options']}
        assert set(status['defaults']) <= choices
        assert all(option['label'] and '_' not in option['label'] for option in status['options'])
        for constraint in MODELS[key].__table__.constraints:
            if isinstance(constraint, CheckConstraint):
                match = re.fullmatch(r"status IN \(([^)]+)\)", str(constraint.sqltext))
                if match:
                    assert choices == set(re.findall(r"'([^']+)'", match[1])), key
        if hasattr(MODELS[key], 'status'):
            assert 'current' not in choices and 'future' not in choices, key
    assert s.BULK_COLLECTIONS['history']['status'] == {'options': [], 'defaults': []}
    for key in ('policies', 'policy_lines', 'scheduling_facts'):
        assert s.BULK_COLLECTIONS[key]['status']['defaults'] == ['active', 'proposed']
    assert s.BULK_COLLECTIONS['exceptions']['status']['defaults'] == ['approved', 'proposed']
    assert s.BULK_COLLECTIONS['eligibilities']['status']['defaults'] == ['current', 'future']
    assert [option['value'] for option in s.BULK_COLLECTIONS['outbox']['status']['options']] == list(OUTBOX_STATES)


def test_registry_planner_actions_only_ask_for_inputs_not_derived_from_selected_rows():
    for collection in ('sessions', 'issues'):
        for action in s.BULK_COLLECTIONS[collection]['actions'].values():
            assert not {'workspace_id', 'shift_id', 'shift_ids', 'keys'} & action['fields'].keys()
    assert 'offset_days' in s.BULK_COLLECTIONS['sessions']['actions']['copy']['fields']
    assert 'action' in s.BULK_COLLECTIONS['issues']['actions']['resolve']['fields']


def test_every_collection_projects_its_own_advertised_statuses_on_all_read_paths(h):
    with transaction(h.app.state.engine) as db:
        for key, descriptor in s.BULK_COLLECTIONS.items():
            role = next(role for role in ('admin', 'coordinator', 'operator', 'auditor', 'credentialer', 'staff')
                        if role in collection_readers(key))
            current = actor(db, h, role)
            selection = {'collection': key, 'where': {'as_of': '2026-10-04'}}
            resolved = bulk.resolve(db, current, selection)
            expected = {row['id']: row['status'] for row in resolved['sample']}
            choices = [option['value'] for option in descriptor['status']['options']]
            assert set(expected.values()) <= set(choices) | {None}, key
            for path in ('unfiltered', 'flat', 'tree') if choices else ('unfiltered',):
                selected = deepcopy(selection)
                if path == 'flat':
                    selected['where']['filters'] = {'status': choices}
                elif path == 'tree':
                    selected['where']['conditions'] = {'conditions': [{'field': 'status', 'values': choices}]}
                page = bulk.rows(db, current, {'selection': selected, 'columns': ['status'], 'limit': 100})
                found = bulk.resolve(db, current, selected)
                assert page['count'] == found['count'] == resolved['count'], (key, path)
                assert {row['id']: row['status'] for row in page['rows']} == expected, (key, path)


@pytest.mark.parametrize('collection', ['policy_lines', 'scheduling_facts', 'exceptions'])
def test_dated_workflow_children_resolve_filter_count_and_page_with_registry_defaults(h, collection):
    who = h.seed['people'][0]
    if collection == 'policy_lines':
        first = propose(h, 'duties_per_week', [line(who=person(who), numbers={'max': {'value': 3}})])['record']
        second = propose(h, 'duties_per_month', [line(who=person(who), numbers={'max': {'value': 8}})])['record']
        target_ids = [row['definition']['rule']['lines'][0]['id'] for row in (first, second)]
    elif collection == 'scheduling_facts':
        first = propose_fact(h, person=who)
        second = propose_fact(h, person=who, kind='overtime_offer')
        target_ids = [first['id'], second['id']]
    else:
        policy = h.policy({'kind': 'max_hours', 'hours': 40})
        target_ids = []
        with transaction(h.app.state.engine, write=True) as db:
            for status in ('proposed', 'approved'):
                row = m.PolicyException(department_id=h.seed['department_id'], policy_id=policy['id'],
                    policy_revision=policy['revision'], person_id=who, starts_on=date(2020, 1, 1),
                    ends_on=date(2021, 1, 1), reason='Synthetic historical exception',
                    created_by=h.seed['identities']['coordinator'], status=status)
                db.add(row)
                db.flush()
                target_ids.append(row.id)
    base = {'collection': collection, 'parent': {'collection': 'people', 'selection': {
        'collection': 'people', 'include_ids': [who]}}, 'where': {'as_of': '2026-10-04',
        'filters': {'id': target_ids}}}
    if collection == 'policy_lines':
        base['where']['filters']['policy_id'] = [first['id'], second['id']]
    expected = {row['id']: row['status'] for row in post(h, 'resolve', base)['sample']}
    assert set(expected) == set(target_ids)
    assert all(value in s.BULK_COLLECTIONS[collection]['status']['defaults'] for value in expected.values())
    for tree in (False, True):
        selected = deepcopy(base)
        values = s.BULK_COLLECTIONS[collection]['status']['defaults']
        if tree:
            selected['where']['conditions'] = {'match': 'any', 'conditions': [
                {'field': 'status', 'values': values}]}
        else:
            selected['where']['filters']['status'] = values
        resolved = post(h, 'resolve', selected)
        assert resolved['count'] == len(expected)
        assert {row['id']: row['status'] for row in resolved['sample']} == expected
        payload = {'selection': selected, 'columns': ['status'], 'sort': [{'field': 'status'}],
                   'group': 'status', 'limit': 1}
        pages = [post(h, 'rows', {**payload, 'offset': offset}) for offset in range(len(expected))]
        assert all(page['count'] == len(expected) for page in pages)
        assert {row['id']: row['status'] for page in pages for row in page['rows']} == expected
        assert sum(group['count'] for group in pages[0]['groups']) == len(expected)
        assert post(h, 'rows', payload) == pages[0]


@pytest.mark.parametrize('collection', ['grants', 'identities', 'inbox', 'outbox', 'sessions'])
def test_computed_status_is_identical_in_unfiltered_resolve_flat_tree_and_cached_rows(h, collection):
    if collection == 'grants':
        record, _, _ = relationship(h, 'grant', ends_on=None)
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.CapabilityGrant, record['id']).revoked = True
        expected = 'ended'
    elif collection == 'identities':
        record = {'id': h.seed['identities']['staff']}
        expected = 'disabled'
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Identity, record['id']).disabled = True
    elif collection == 'sessions':
        made = h.shift(h.workspace())
        record = {'id': made['shift_id']}
        expected = 'cancelled'
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Shift, record['id']).cancelled = True
    else:
        expected = 'read' if collection == 'inbox' else 'failed'
        with transaction(h.app.state.engine, write=True) as db:
            event = m.Outbox(department_id=h.seed['department_id'], event_key='synthetic-status',
                kind='request', payload={}, state='failed')
            db.add(event)
            db.flush()
            record = {'id': event.id}
            if collection == 'inbox':
                notification = m.Notification(outbox_id=event.id, identity_id=h.seed['identities']['admin'],
                    title='Synthetic read message', message='Synthetic status test', read_at=m.now())
                db.add(notification)
                db.flush()
                record = {'id': notification.id}
    assert expected in {choice['value'] for choice in s.BULK_COLLECTIONS[collection]['status']['options']}
    base = {'collection': collection, 'where': {'as_of': '2026-10-04', 'filters': {'id': [record['id']]}}}
    for path in ('unfiltered', 'flat', 'tree'):
        selected = deepcopy(base)
        if path == 'flat':
            selected['where']['filters']['status'] = [expected]
        elif path == 'tree':
            selected['where']['conditions'] = {'conditions': [{'field': 'status', 'values': [expected]}]}
        resolved = post(h, 'resolve', selected)
        payload = {'selection': selected, 'columns': ['status'], 'limit': 1}
        page = post(h, 'rows', payload)
        assert resolved['count'] == page['count'] == 1
        assert resolved['sample'][0]['status'] == page['rows'][0]['status'] == expected
        assert post(h, 'rows', payload) == page


@pytest.mark.parametrize('collection', ['policy_lines', 'scheduling_facts', 'exceptions', 'history'])
@pytest.mark.parametrize('path', ['flat', 'tree'])
def test_workflow_and_unavailable_statuses_reject_temporal_vocabulary(h, collection, path):
    where = {'filters': {'status': ['current']}} if path == 'flat' else {'conditions': {
        'conditions': [{'field': 'status', 'values': ['current']}]}}
    for endpoint in ('resolve', 'rows'):
        selection = {'collection': collection, 'where': where}
        payload = selection if endpoint == 'resolve' else {'selection': selection}
        response = h.client('auditor' if collection == 'history' else 'admin').post('/api/bulk/' + endpoint, json=payload)
        assert response.status_code == 422, response.text
        error = response.json()['error']
        if collection == 'history' and path == 'tree':
            assert error['code'] == 'SCHEMA_INVALID'
            assert any('Unknown condition field: status' in detail['message'] for detail in error['details'])
        else:
            assert error['code'] == 'INVALID_FILTER'
