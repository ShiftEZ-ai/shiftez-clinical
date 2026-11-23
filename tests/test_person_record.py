"""A catalogue person belongs to the department before ranks or sign-ins exist."""
from datetime import date

import pytest

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services.catalogue import write_catalogue
from packages.contracts import schemas as s
from tests.governance_support import identity


def test_partial_people_update_preserves_omissions_and_undo_restores_preimage(pg):
    before = pg.configure('people', {
        'display_name': 'Synthetic person', 'active': False, 'inactive_from': '2026-10-01',
        'title': 'Synthetic title', 'staff_number': 'T42', 'directory_order': 37,
        'work_extension': '4321', 'short_code': 'P42', 'tags': ['Synthetic tag'],
    })['record']
    value = {'display_name': 'Updated synthetic person', 'title': '', 'inactive_from': None, 'tags': []}
    payload = {'catalogue': 'people', 'value': value, 'reason': 'Edit synthetic person'}
    command = {'kind': 'catalogue.update', 'target_id': before['id'],
               'expected_revision': before['revision'], 'payload': payload}
    original = {key: before[key] for key in s.PersonInput.model_fields}
    wanted = {**original, **value}
    preview = pg.client('admin').post('/api/catalogue/preview', json=command)
    assert preview.status_code == 200, preview.text
    trial = preview.json()
    outcome = pg.command('admin', command['kind'], payload, before['id'], before['revision'],
                         expected_fingerprint=trial['source_fingerprint'])
    after = outcome['record']
    assert {key: after[key] for key in wanted} == wanted
    assert {key: trial['proposed'][key] for key in wanted} == wanted
    assert outcome['previous_value'] == original
    undone = pg.configure('people', outcome['previous_value'], after['id'], after['revision'])['record']
    assert {key: undone[key] for key in original} == original
    with transaction(pg.app.state.engine) as db:
        history = db.query(m.DefinitionHistory).filter_by(kind='people', object_id=before['id']).order_by(
            m.DefinitionHistory.object_revision).all()
        assert [{key: row.snapshot[key] for key in original} for row in history] == [original, wanted, original]
        assert db.query(m.AuditEvent).filter_by(action='catalogue.update', target_id=before['id']).count() == 2


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_new_department_person_returns_empty_records_for_authorized_roles(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    person = h.configure('people', {'display_name': 'New department person (test)'})['record']['id']
    with transaction(h.app.state.engine) as db:
        assert not db.query(m.Membership).filter_by(person_id=person).count()
        assert not db.query(m.Identity).filter_by(person_id=person).count()
    readers = {'admin', 'coordinator', 'approver', 'programme_lead', 'auditor'}
    for area in ('fairness', 'score', 'workload', 'availability', 'accounts', 'sessions'):
        allowed = role in (readers if area in {'fairness', 'score', 'workload'} else
                           {'admin', 'coordinator', 'approver'} if area == 'availability' else {'admin'})
        query = '?start=2026-10-01&end=2026-11-01' if area == 'availability' else ''
        result = h.get(role, f'/api/people/{person}/{area}{query}', status=200 if allowed else 403)
        if not allowed:
            continue
        assert result['person_id'] == person
        if area == 'fairness':
            assert result['goals'] == []
        elif area == 'score':
            assert result['draft'] is None and result['published'] is None
        elif area == 'workload':
            assert all(p['totals']['duties'] == p['totals']['hours'] == 0 for p in result['periods'])
        elif area == 'availability':
            assert result['availability'] == result['requests'] == result['patterns'] == []
        else:
            assert result[area] == []
    for area in ('swaps', 'wishes'):
        allowed = role in (readers if area == 'wishes' else {'admin', 'coordinator', 'approver'})
        result = h.get(role, f'/api/{area}?person_id={person}', status=200 if allowed else 403)
        if allowed:
            assert result[area] == []


def test_catalogue_ownership_does_not_admit_other_department_people(h):
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other department (test)')
        db.add(other)
        db.flush()
        actor = Actor(h.seed['identities']['admin'], other.id, h.seed['organisation_id'],
                      'Synthetic administrator', frozenset({'admin'}), None, 'synthetic-session')
        command = s.Command(kind='catalogue.create', payload={
            'catalogue': 'people', 'value': {'display_name': 'Other department only (test)'},
            'reason': 'Synthetic other department record'})
        person = write_catalogue(db, actor, command)['record']['id']
        db.add(m.Availability(department_id=other.id, person_id=person, kind='recorded_leave',
                              private_reason='Synthetic other department leave',
                              start=m.now(), end=m.now().replace(year=m.now().year + 1),
                              recorded_by=actor.identity_id))
    for area in ('fairness', 'score', 'workload', 'availability', 'accounts', 'sessions'):
        query = '?start=2026-10-01&end=2026-11-01' if area == 'availability' else ''
        h.get('admin', f'/api/people/{person}/{area}{query}', status=404)
    for area in ('swaps', 'wishes'):
        h.get('admin', f'/api/{area}?person_id={person}', status=404)


def test_night_named_duty_does_not_invent_a_department_night_definition(h):
    duty = h.configure('assignment_types', {
        'name': 'Night duty (test)', 'starts_on': '2020-01-01',
        'definition': {'schema_version': 1}})['record']
    w = h.shift(h.workspace(), start='2026-10-04T22:00:00+03:00', end='2026-10-05T02:00:00+03:00',
                typ=duty['id'])['workspace']
    h.publish(w)
    result = h.get('staff', f"/api/people/{h.seed['people'][0]}/workload?on={date(2026, 10, 5)}")
    assert result['unsupported_metrics'] == ['nights']
    assert all(p['totals']['nights'] is None for p in result['periods'])
    month = next(p for p in result['periods'] if p['key'] == 'this_month')
    assert month['totals']['duties'] == 1 and month['totals']['hours'] == 4
