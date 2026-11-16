"""Bounded read projections and deterministic query/payload budgets."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import json
import time

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import DEFAULT_CONTACT_TYPES
from tests.test_performance_budget import python_budget
from .governance_support import identity


def test_home_strip_projects_large_workspace_and_pages(h):
    w = h.workspace()
    h.workspace('Second')
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        row.accepted_issues = [{'key': str(i), 'reason': 'Private review evidence ' * 20} for i in range(1500)]
        row.validation = {'valid': False, 'coverage': [
            {'shift_id': str(i), 'required': 2, 'available_eligible': 1, 'status': 'gap'} for i in range(500)],
            'findings': []}
    full = h.client('coordinator').get('/api/workspaces').content
    path = '/api/home/rotas?start=2026-09-01&end=2027-01-01'
    response = h.client('coordinator').get(path)
    assert response.status_code == 200
    assert len(full) > 500_000 and len(response.content) < 4_000
    rows = response.json()['workspaces']
    assert set(rows[0]) == {'id', 'name', 'status', 'starts_on', 'ends_on', 'base_publication_id', 'health'}
    assert next(r for r in rows if r['id'] == w['id'])['health']['summary']['places_open'] == 500
    page = h.get('coordinator', path + '&limit=1')
    last = h.get('coordinator', path + '&limit=1&offset=1')
    assert page['has_more'] and not last['has_more']
    assert page['workspaces'][0]['id'] != last['workspaces'][0]['id']
    h.get('staff', path, status=403)
    h.get('coordinator', '/api/home/rotas?start=2026-01-01&end=2028-01-01', status=422)


def test_catalogue_stamps_single_query_and_collection_projection(h):
    h.get('coordinator', '/api/catalogues?boot=true')
    sql = []
    def collect(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.upper())
    event.listen(h.app.state.engine, 'before_cursor_execute', collect)
    try:
        value = h.get('coordinator', '/api/catalogues?include=people&include=categories')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', collect)
    assert set(value) == {'people', 'categories', '_versions'}
    assert sum(statement.startswith('SELECT DEPARTMENTS.CATALOGUE_REVISION,') for statement in sql) == 1
    assert not any('SUM(' in statement or 'COUNT(' in statement for statement in sql)
    h.get('coordinator', '/api/catalogues?include=unknown', status=422)
    rule = h.command('coordinator', 'policy.create', {'name': 'Pending cache rule', 'owner': 'Synthetic',
        'source': 'Synthetic', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'maximum_participants', 'maximum': 3}, 'scope': {}, 'severity': 'mandatory'}})['record']
    assert rule['id'] in {r['id'] for r in h.get('coordinator', '/api/catalogues?include=policies')['policies']}


def test_home_strip_reference_queries_are_constant_for_checked_rotas(h):
    for index in range(10):
        workspace = h.workspace(f'Checked {index}')
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Workspace, workspace['id']).validation = {'valid': True, 'coverage': [], 'findings': []}
    h.client('coordinator')
    sql = []
    def collect(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.upper())
    event.listen(h.app.state.engine, 'before_cursor_execute', collect)
    try:
        value = h.get('coordinator', '/api/home/rotas?start=2026-10-01&end=2026-11-01')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', collect)
    assert len(value['workspaces']) == 10
    # Constant, never per rota: the actor's department, the strip's, and the shared inputs' department ids (maintenance).
    assert sum(statement.startswith('SELECT DEPARTMENTS.') for statement in sql) <= 3
    assert sum(statement.startswith('SELECT ORGANISATIONS.') for statement in sql) <= 2


def test_people_projection_cannot_reuse_manager_contacts(h):
    identity(h, 'projection_programme_lead', ['programme_lead'])
    seeded = next(p for p in h.get('coordinator', '/api/catalogues?include=people')['people']
                  if p['id'] == h.seed['people'][0])
    person = h.configure('people', {'display_name': 'Projection test', 'staff_number': 'staff-secret',
        'work_phone': '5551234', 'work_email': 'synthetic@example.test', 'pager': '123',
        'directory_section': 'Consultants'},
        seeded['id'], seeded['revision'])['record']
    missing = h.configure('people', {'display_name': 'No employee number', 'staff_number': ''})['record']
    def read(role, target=person):
        return next(p for p in h.get(role, '/api/catalogues?include=people')['people'] if p['id'] == target['id'])
    assert read('coordinator')['staff_number'] == 'staff-secret'
    assert read('credentialer')['current_category_ids'] == [h.seed['category']]
    assert any(r['person_id'] == person['id'] for r in
               h.get('credentialer', '/api/catalogues?include=eligibilities')['eligibilities'])
    assert read('admin')['employee_number_redacted'] is False
    assert read('coordinator')['employee_number_redacted'] is False
    assert read('coordinator', missing)['staff_number'] == ''
    assert read('coordinator', missing)['employee_number_redacted'] is False
    approver = read('approver')
    assert approver['work_phone'] == '5551234' and 'staff_number' not in approver
    assert approver['directory_section'] == 'Consultants'
    assert approver['employee_number_redacted'] is True
    for role in ('auditor', 'credentialer', 'projection_programme_lead'):
        row = read(role)
        # Contact fields come only from the contact reader, as each type's visibility allows (SSOT: DEFAULT_CONTACT_TYPES).
        open_to_all = {t['legacy_field'] for t in DEFAULT_CONTACT_TYPES if t['visibility']['mode'] == 'everyone'}
        assert not ({'staff_number', 'work_phone', 'work_email', 'pager', 'work_extension'} - open_to_all) & row.keys()
        assert {'id', 'display_name', 'current_category_ids', 'tags'} <= row.keys()
        assert 'directory_section' not in row
        assert row['employee_number_redacted'] is True
        assert read(role, missing)['employee_number_redacted'] is True
    assert read('coordinator')['staff_number'] == 'staff-secret'


def test_audit_keyset_paging_and_actor_picker_use_bounded_queries(h):
    h.workspace()
    h.workspace('More history')
    first = h.get('auditor', '/api/audit?limit=1')
    cursor = first['next_cursor']
    assert cursor and first['people'] and not first['people_has_more']
    next_page = h.get('auditor', '/api/audit?limit=1&' + urlencode(cursor))
    assert first['events'][0]['id'] != next_page['events'][0]['id']
    assert next_page['events'] == h.get('auditor', '/api/audit?offset=1&limit=1')['events']
    offset_cursor = {**cursor, 'before_at': datetime.fromisoformat(cursor['before_at']).astimezone(
        timezone(timedelta(hours=3))).isoformat()}
    assert h.get('auditor', '/api/audit?limit=1&' + urlencode(offset_cursor))['events'] == next_page['events']
    h.get('auditor', '/api/audit?before_id=' + first['events'][0]['id'], status=422)


def test_jobs_list_pages_without_loading_proposals(h):
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', input_fingerprint='0' * 64,
            input_snapshot={'generation_engine': 'legacy', 'large_snapshot': ['secret'] * 1000},
            status='succeeded', result={'status': 'candidate_feasible', 'additions': ['large'] * 1000}) for _ in range(3))
    first = h.get('coordinator', '/api/jobs?limit=2')
    last = h.get('coordinator', '/api/jobs?limit=2&offset=2')
    assert first['has_more'] and not last['has_more']
    assert len({row['id'] for row in first['jobs'] + last['jobs']}) == 3
    assert len(json.dumps(first)) < 10_000


@pytest.mark.cpu_budget
def test_warm_projected_catalogue_cpu_budget(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Person {i}') for i in range(150))
    client = h.client('coordinator')
    client.get('/api/catalogues?include=people')
    started = time.process_time()
    response = client.get('/api/catalogues?include=people')
    assert response.status_code == 200
    assert time.process_time() - started < python_budget(0.5)
