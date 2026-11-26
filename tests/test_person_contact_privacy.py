"""Contact projections share one role boundary without rewriting historical evidence."""
import csv
import io
import json

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import CATALOGUE_READ_ROLES, effective_roles
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.people import CONTACT_FIELDS
from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES
from .governance_support import identity

CONTACT = {'work_email': 'private@example.test', 'work_phone': '+966 11 555 0199',
           'work_extension': '777-secret', 'pager': '888-secret'}


@pytest.mark.postgres
@pytest.mark.parametrize('role', ROLE_NAMES)
def test_person_contact_visibility_across_readers(pg, role):
    h = pg
    person = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])
    person = h.configure('people', {'display_name': 'Synthetic contact', 'staff_number': 'PRIVATE-EMP', **CONTACT},
                         person['id'], person['revision'])['record']
    other = h.configure('people', {'display_name': 'Synthetic other contact', **CONTACT})['record']
    reader = 'contact_' + role
    identity(h, reader, [role], person['id'])
    configured = h.get(reader, '/api/contact-types')['contact_types']
    roles = effective_roles({role})

    def expected_for(person_id):
        if person_id == person['id']:
            return CONTACT
        visible = [typ for typ in configured if typ['visibility']['mode'] == 'everyone' or
                   typ['visibility']['mode'] == 'roles' and roles.intersection(typ['visibility']['roles']) or
                   typ['visibility']['mode'] == 'self_and_admins' and 'admin' in roles]
        return {typ['legacy_field']: CONTACT[typ['legacy_field']] for typ in visible}

    directory = h.get(reader, '/api/directory')['people']
    for person_id in (person['id'], other['id']):
        entry = next(row for row in directory if row['id'] == person_id)
        expected = expected_for(person_id)
        assert {field: entry[field] for field in CONTACT_FIELDS & entry.keys()} == expected
        assert {c['legacy_field']: c['value'] for c in entry['contacts']} == expected
    if role in CATALOGUE_READ_ROLES:
        catalogue = h.get(reader, '/api/catalogues?include=people')['people']
        current = next(row for row in catalogue if row['id'] == person['id'])
        history = h.get(reader, f"/api/catalogues/people/{person['id']}/history")['versions']
        assert history and all(set(row['snapshot']) <= set(current) for row in history)
        assert all(not CONTACT_FIELDS.intersection(row['snapshot']) for row in history)
        assert ('staff_number' in history[0]['snapshot']) == (role in {'admin', 'coordinator'})
    else:
        h.get(reader, f"/api/catalogues/people/{person['id']}/history", status=403)

    workspace = h.shift(h.workspace(), people=[person['id']])['workspace']
    duty, = h.detail(workspace)['shifts']
    pub = h.publish(workspace)['publication_id']
    client = h.client(reader)
    can_read_schedule = bool(roles & {'staff', *PERMISSIONS['schedule.read_team']})
    response = client.get('/api/schedule/sessions?start=2026-10-01&end=2026-11-01')
    assert response.status_code == (200 if can_read_schedule else 403)
    assert not any(value in response.text for field, value in CONTACT.items()
                   if field != 'pager' or not can_read_schedule)
    if can_read_schedule:
        data = response.json()
        shift = next(row for row in data['shifts'] if row['id'] == duty['id'])
        assert [p['person_id'] for p in shift['participants']] == [person['id']]
        entry = data['people'][person['id']]
        assert {field: entry[field] for field in CONTACT_FIELDS & entry.keys()} == {'pager': CONTACT['pager']}
        assert 'contacts' not in entry

    can_read_publication = bool(roles & {'staff', *PERMISSIONS['publications.read_team']})
    publication_status = 200 if can_read_publication else 403
    response = client.get(f'/api/publications/{pub}')
    assert response.status_code == publication_status
    assert not any(value in response.text for value in CONTACT.values())
    if can_read_publication:
        data = response.json()
        assert data['publication']['id'] == pub
        shift = next(row for row in data['snapshot']['shifts'] if row['id'] == duty['id'])
        assert [p['person_id'] for p in shift['participants']] == [person['id']]

    reports = {}
    for query in ('assignments', 'print', f'ics&person_id={person["id"]}'):
        response = client.get(f'/api/reports/{pub}?format={query}')
        assert response.status_code == publication_status, query
        assert not any(value in response.text for value in CONTACT.values())
        reports[query.split('&', 1)[0]] = response.text
    if can_read_publication:
        row, = csv.DictReader(io.StringIO(reports['assignments']))
        assert (row['publication_id'], row['shift_id'], row['person']) == (pub, duty['id'], person['display_name'])
        assert f"{person['display_name']} (" in reports['print']
        calendar = reports['ics'].replace('\r\n ', '')
        assert f"\r\nUID:{pub}-{duty['id']}-{person['id']}" in calendar


@pytest.mark.postgres
def test_published_contacts_reauthorize_conditional_reads(pg):
    from dataclasses import replace
    from datetime import date
    from apps.api.auth import Actor
    from apps.api.services.schedule_view import sessions

    person = next(p for p in pg.get('admin', '/api/catalogues')['people'] if p['id'] == pg.seed['people'][0])
    pg.configure('people', {'display_name': person['display_name'], **CONTACT}, person['id'], person['revision'])
    pg.publish(pg.shift(pg.workspace())['workspace'])
    actor = Actor(pg.seed['identities']['coordinator'], pg.seed['department_id'], pg.seed['organisation_id'],
                  'Synthetic reader', frozenset({'coordinator'}), None, m.uid())
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    with transaction(pg.app.state.engine) as db:
        tag, data = sessions(db, actor, start, end)
        assert data['people'][person['id']]['pager'] == CONTACT['pager']
        assert sessions(db, actor, start, end, if_none_match=tag) == (tag, None)
        restricted_tag, restricted = sessions(db, replace(actor, roles=frozenset({'auditor'})),
                                               start, end, if_none_match=tag)
        assert restricted_tag != tag and restricted is not None
        assert restricted['people'][person['id']]['pager'] == CONTACT['pager']


@pytest.mark.postgres
@pytest.mark.parametrize('role', ROLE_NAMES)
def test_leaver_history_is_filtered_without_rewriting_evidence(pg, role):
    person = pg.configure('people', {'display_name': 'Synthetic leaver', 'active': False, **CONTACT})['record']
    identity(pg, 'history_' + role, [role])
    with transaction(pg.app.state.engine) as db:
        versions = [record(row) for row in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'people', m.DefinitionHistory.object_id == person['id']))]
        events = [record(row) for row in db.scalars(select(m.AuditEvent))]
    assert all(value in json.dumps(versions) for value in CONTACT.values())
    pg.command('admin', 'person.anonymise', {'reason': 'Synthetic leaver privacy check'}, person['id'], person['revision'])
    if role in CATALOGUE_READ_ROLES:
        history = pg.get('history_' + role, f"/api/catalogues/people/{person['id']}/history")['versions']
        assert history
        assert all(not CONTACT_FIELDS.intersection(version['snapshot']) for version in history)
        assert not any(value in json.dumps(history) for value in CONTACT.values())
    else:
        pg.get('history_' + role, f"/api/catalogues/people/{person['id']}/history", status=403)
    with transaction(pg.app.state.engine) as db:
        assert [record(db.get(m.DefinitionHistory, row['id'])) for row in versions] == versions
        assert [record(db.get(m.AuditEvent, row['id'])) for row in events] == events
