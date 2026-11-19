"""Direct record history follows the affected person, including exact bulk creation and Undo receipts."""
import csv
import io
import json
from uuid import uuid4

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ROLE_NAMES
from .governance_support import identity


def history(h, person, query='', role='auditor'):
    return h.get(role, f'/api/audit?person_id={person}&people=false{query}')


def request(h, person, day, role='coordinator'):
    return h.command(role, 'request.create', {'person_id': person, 'kind': 'meeting',
        'start': f'2026-10-{day:02d}T09:00:00+03:00', 'end': f'2026-10-{day:02d}T10:00:00+03:00',
        'private_reason': 'PRIVATE request detail'})


def subject_journey(h):
    own, other = h.seed['people'][:2]
    identity(h, 'subject_author', ['coordinator'], person=own)
    request(h, own, 25)
    request(h, other, 26, role='subject_author')
    request(h, own, 27)
    # A department-local history must not pick up a foreign department's direct event for this same person.
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        foreign = m.Department(organisation_id=department.organisation_id, name='Other department')
        db.add(foreign); db.flush()
        db.add(m.AuditEvent(department_id=foreign.id, actor_id=h.seed['identities']['coordinator'],
            action='request.create', target_id=own, reason='Foreign event', detail={}, correlation_id=str(uuid4())))
    found = history(h, own, '&action=request')
    assert len(found['events']) == 2 and not found['has_more'] and found['person_id'] == own and found['people'] == []
    assert {e['actor_id'] for e in found['events']} == {h.seed['identities']['coordinator']}
    assert all(e['target_id'] is None for e in found['events'])  # IDs are recovered from the actual create receipt.
    assert history(h, own, '&actor_id=' + h.seed['identities']['subject_author'])['events'] == []
    first = history(h, own, '&action=request&limit=1')
    second = history(h, own, '&action=request&limit=1&offset=1')
    assert first['has_more'] and not second['has_more']
    assert first['events'] + second['events'] == found['events']
    response = h.client('auditor').get(f'/api/audit.csv?person_id={own}&action=request')
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2 and {r['reason'] for r in rows} == {e['reason'] for e in found['events']}
    assert 'PRIVATE' not in json.dumps(found) + response.text
    assert len(history(h, other, '&action=request')['events']) == 1


def test_subject_history_is_not_actor_history_and_filters_before_pagination_and_csv(h):
    subject_journey(h)


@pytest.mark.postgres
def test_postgres_subject_history_is_not_actor_history_and_filters_before_pagination_and_csv(pg):
    subject_journey(pg)


@pytest.mark.parametrize('role', [role for role in ROLE_NAMES if role not in {'operator', 'auditor'}])
def test_person_filter_does_not_widen_audit_permission(h, role):
    identity(h, 'history_' + role, [role], person=h.seed['people'][0])
    query = '?person_id=' + h.seed['people'][0]
    h.get('history_' + role, '/api/audit' + query, status=403)
    assert h.client('history_' + role).get('/api/audit.csv' + query).status_code == 403


def test_foreign_or_unknown_subject_is_not_read(h):
    with transaction(h.app.state.engine, write=True) as db:
        org = m.Organisation(name='Foreign organisation')
        db.add(org); db.flush()
        person = m.Person(organisation_id=org.id, display_name='Foreign person')
        db.add(person); db.flush()
        foreign = person.id
    for person in (foreign, str(uuid4())):
        h.get('auditor', '/api/audit?person_id=' + person, status=404)
        assert h.client('auditor').get('/api/audit.csv?person_id=' + person).status_code == 404


def bulk_undo_journey(h, kind):
    people = h.seed['people'][:2]
    catalogue, field, cls = ('services', 'service_ids', m.Eligibility) if kind == 'eligibility' else ('capabilities', 'capability_ids', m.CapabilityGrant)
    item = h.configure(catalogue, {'name': 'History fixture ' + kind, 'starts_on': '2020-01-01'})['record']
    payload = {'person_ids': people, field: [item['id']], 'starts_on': '2026-10-01', 'reason': 'Dated facts'}
    if kind == 'grant':
        payload['evidence'] = 'PRIVATE verification evidence'
    created = h.command('credentialer', kind + '.bulk_create', payload)['created']
    assert len(created) == 2
    with transaction(h.app.state.engine, write=True) as db:
        rows = [db.get(cls, r['id']) for r in created]
        removed = next(r for r in rows if r.person_id == people[0])
        kept = next(r for r in rows if r.person_id == people[1])
        removed_id, kept_id = removed.id, kept.id
        kept.revision += 1  # A later edit makes this record ineligible for creation Undo.
    result = h.command('credentialer', kind + '.undo_create', {'items': created, 'reason': 'Undo created facts'})
    assert result['removed'] == 1 and result['kept'] == 1 and result['removed_ids'] == [removed_id]
    assert kept_id not in result['removed_ids']
    changed = history(h, people[0], '&action=' + kind)['events']
    kept_events = history(h, people[1], '&action=' + kind)['events']
    assert {e['action'] for e in changed} == {kind + '.bulk_create', kind + '.undo_create'}
    assert [e['action'] for e in kept_events] == [kind + '.bulk_create']
    assert history(h, h.seed['people'][2], '&action=' + kind)['events'] == []
    assert 'PRIVATE' not in json.dumps(changed + kept_events)


@pytest.mark.parametrize('kind', ['eligibility', 'grant'])
def test_bulk_undo_history_names_only_the_removed_person_not_kept_or_unrelated(h, kind):
    bulk_undo_journey(h, kind)


@pytest.mark.postgres
def test_postgres_bulk_undo_history_names_only_the_removed_person(pg):
    bulk_undo_journey(pg, 'eligibility')


def programme_journey(h):
    programme = h.configure('programmes', {'name': 'History programme', 'starts_on': '2026-01-01'})['record']
    people = h.seed['people'][:2]
    for person in people:
        h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    clinic = h.configure('services', {'name': 'History programme clinic', 'starts_on': '2026-01-01'})['record']
    payload = {'programme_id': programme['id'], 'person_ids': people, 'service_ids': [clinic['id']],
               'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Reviewed roster'}
    created = h.command('credentialer', 'eligibility.programme', payload)
    again = h.command('credentialer', 'eligibility.programme', payload)
    assert created['created_count'] == len(created['created_ids']) == 2
    assert again['created_count'] == 0 and again['created_ids'] == [] and again['already_current_count'] == 2
    actual = [r for r in h.get('credentialer', '/api/catalogues')['eligibilities'] if r['service_id'] == clinic['id']]
    assert set(created['created_ids']) == {r['id'] for r in actual}
    for person in people:
        assert [e['action'] for e in history(h, person, '&action=eligibility')['events']] == ['eligibility.programme']
    assert history(h, h.seed['people'][2], '&action=eligibility')['events'] == []


def test_programme_permission_receipt_attributes_only_actual_new_records(h):
    programme_journey(h)


@pytest.mark.postgres
def test_postgres_programme_permission_receipt_attributes_only_actual_new_records(pg):
    programme_journey(pg)


def test_bulk_people_updates_and_both_sides_of_an_attachment_are_subject_history(h):
    own, other, third = h.seed['people']
    people = {r['id']: r for r in h.get('coordinator', '/api/catalogues')['people']}
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': pid, 'revision': people[pid]['revision']} for pid in (own, other)],
        'set': {'title': 'History fixture'}, 'reason': 'Selected people'})
    for person in (own, other):
        assert len(history(h, person, '&action=catalogue')['events']) == 1
    assert history(h, third, '&action=catalogue')['events'] == []
    attachment = h.configure('mentorships', {'person_id': own, 'mentor_id': other,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')['record']
    for person in (own, other):
        assert attachment['id'] in {e['target_id'] for e in history(h, person)['events']}
    assert history(h, third)['events'] == []
# Include this file in hosted supervision and team-lead qualification.
