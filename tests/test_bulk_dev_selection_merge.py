"""New catalogue records retain their canonical read scope in bulk lists and caches."""
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_events import event, event_type
from .test_requests_search import request_row


@pytest.fixture
def programme_rows(h):
    lead = identity(h, 'scoped_lead', ['programme_lead'], person=h.seed['people'][0])
    with transaction(h.app.state.engine, write=True) as db:
        programmes = [m.Programme(department_id=h.seed['department_id'], name=name,
            owner_identity_id=lead if index == 0 else None, starts_on=date(2020, 1, 1))
            for index, name in enumerate(('Owned programme', 'Current membership programme', 'Other programme'))]
        db.add_all(programmes)
        db.flush()
        db.add_all([m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            programme_id=programmes[index].id, starts_on=date(2026, 1, 1),
            ends_on=date(2027, 1, 1) if index == 1 else date(2026, 9, 30)) for index in (1, 2)])
        cohorts = [m.Cohort(department_id=h.seed['department_id'], programme_id=p.id, name=p.name,
            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)) for p in programmes]
        rotation_type = m.RotationType(department_id=h.seed['department_id'], name='Synthetic rotation',
            starts_on=date(2020, 1, 1))
        db.add_all([*cohorts, rotation_type])
        db.flush()
        targets = {'programmes': programmes, 'cohorts': cohorts}
        for kind, cls, values in (
            ('programme_memberships', m.ProgrammeMembership, {'person_id': h.seed['people'][1]}),
            ('programme_calendar_entries', m.ProgrammeCalendarEntry, {'name': 'Teaching', 'kind': 'academic_block'}),
            ('mentorships', m.Mentorship, {'person_id': h.seed['people'][1], 'mentor_id': h.seed['people'][0]}),
            ('rotation_templates', m.RotationTemplate, {'name': 'Template',
                'blocks': [{'service_id': h.seed['services'][0], 'length': 1, 'unit': 'months'}]}),
        ):
            dates = {} if kind == 'rotation_templates' else {'starts_on': date(2026, 1, 1), 'ends_on': date(2027, 1, 1)}
            targets[kind] = [cls(department_id=h.seed['department_id'], programme_id=p.id, **values, **dates)
                for p in programmes]
            db.add_all(targets[kind])
        targets['rotations'] = [m.Rotation(department_id=h.seed['department_id'], person_id=h.seed['people'][1],
            cohort_id=cohort.id, rotation_type_id=rotation_type.id,
            starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1)) for cohort in cohorts]
        shared = m.RotationTemplate(department_id=h.seed['department_id'], name='Shared template',
            blocks=[{'service_id': h.seed['services'][0], 'length': 1, 'unit': 'months'}])
        db.add_all([*targets['rotations'], shared])
        db.flush()
        return {kind: [row.id for row in values] for kind, values in targets.items()}, shared.id


@pytest.mark.parametrize('collection', ['programmes', 'cohorts', 'programme_memberships', 'programme_calendar_entries',
    'mentorships', 'rotation_templates', 'rotations'])
def test_bulk_programme_catalogues_share_ownership_membership_scope_before_counting_and_paging(h, programme_rows, collection):
    targets, shared = programme_rows
    ids = targets[collection] + ([shared] if collection == 'rotation_templates' else [])
    expected = set(ids) - {targets[collection][2]}
    listed = h.get('scoped_lead', '/api/catalogues')[collection]
    assert {row['id'] for row in listed} & set(ids) == expected
    assert h.get('scoped_lead', '/api/bulk/collections')['collections'][collection]['count'] == len(listed)
    client = h.client('scoped_lead')
    for selection in ({'collection': collection, 'include_ids': ids},
                      {'collection': collection, 'where': {'filters': {'id': ids}, 'as_of': '2026-08-01'}}):
        resolved = client.post('/api/bulk/resolve', json=selection)
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()['count'] == len(expected)
        assert {row['id'] for row in resolved.json()['sample']} == expected
        pages = [client.post('/api/bulk/rows', json={'selection': selection, 'columns': ['id'],
            'offset': offset, 'limit': 1}) for offset in range(len(expected) + 1)]
        assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
        assert all(page.json()['count'] == len(expected) for page in pages)
        assert {row['id'] for page in pages for row in page.json()['rows']} == expected
    if collection == 'programmes':
        hidden = client.post('/api/bulk/resolve', json={'collection': collection,
            'names': ['Other programme'], 'include_ids': [targets[collection][2]]})
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()['count'] == 0 and hidden.json()['unmatched'] == ['Other programme']
        assert hidden.json()['ambiguous'] == [] and hidden.json()['matches'] == [{'line': 'Other programme', 'ids': []}]


@pytest.mark.parametrize('authority', ['owner', 'membership'])
def test_bulk_rows_cache_rechecks_current_programme_authority(h, programme_rows, authority):
    targets, _ = programme_rows
    index = 0 if authority == 'owner' else 1
    selection = {'collection': 'rotation_templates', 'include_ids': [targets['rotation_templates'][index]]}
    client = h.client('scoped_lead')
    request = {'selection': selection, 'columns': ['name']}
    assert client.post('/api/bulk/rows', json=request).json()['count'] == 1
    with transaction(h.app.state.engine, write=True) as db:
        if authority == 'owner':
            row = db.get(m.Programme, targets['programmes'][index])
            row.owner_identity_id = None
        else:
            row = db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == h.seed['people'][0],
                m.ProgrammeMembership.programme_id == targets['programmes'][index]))
            row.ends_on = date(2026, 9, 30)
        row.revision += 1
    page = client.post('/api/bulk/rows', json=request)
    assert page.status_code == 200, page.text
    assert page.json()['count'] == 0 and page.json()['rows'] == []
    resolved = client.post('/api/bulk/resolve', json=selection)
    assert resolved.status_code == 200 and resolved.json()['count'] == 0


@pytest.mark.parametrize('role', ['approver', 'credentialer', 'auditor', 'programme_lead', 'staff'])
def test_bulk_event_definitions_use_the_same_manager_only_read_guard(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    row = event(h, heading='Restricted synthetic event definition')
    h.get(role, f"/api/events/definitions/{row['id']}", status=403)
    client = h.client(role)
    selection = {'collection': 'events', 'include_ids': [row['id']]}
    for path, payload in (('/api/bulk/resolve', selection),
                          ('/api/bulk/rows', {'selection': selection, 'columns': ['heading', 'audience']})):
        response = client.post(path, json=payload)
        assert response.status_code == 403, response.text
        assert response.json()['error']['code'] == 'FORBIDDEN'
    assert 'events' not in h.get(role, '/api/bulk/collections')['collections']


@pytest.mark.parametrize('collection', ['event_types', 'events', 'rotation_templates'])
def test_new_bulk_catalogues_accept_every_advertised_column_on_populated_rows(h, collection):
    if collection == 'events':
        event(h)
    elif collection == 'event_types':
        event_type(h)
    else:
        h.configure(collection, {'name': 'Shared plan',
            'blocks': [{'service_id': h.seed['services'][0], 'length': 1, 'unit': 'months'}]})
    registry = s.BULK_COLLECTIONS[collection]
    columns = list(dict.fromkeys([*registry['fields'], *registry.get('columns', {}), *registry['related_columns']]))
    response = h.client('admin').post('/api/bulk/rows', json={'selection': {'collection': collection}, 'columns': columns})
    assert response.status_code == 200, response.text
    assert response.json()['rows']
    assert all(set(columns) <= row.keys() for row in response.json()['rows'])


@pytest.fixture
def lead_requests(h, programme_rows):
    targets, _ = programme_rows
    outside = h.configure('people', {'display_name': 'Foreign programme trainee'})['record']['id']
    workspace = h.workspace('Private draft linked to requests')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][1]).display_name = 'Owned programme trainee'
        db.get(m.Person, h.seed['people'][2]).display_name = 'Current programme trainee'
        # Isolate ownership authority: the catalogue fixture otherwise enrolls this trainee in every programme.
        for row in db.scalars(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == h.seed['people'][1],
            m.ProgrammeMembership.programme_id.in_(targets['programmes'][1:]))):
            db.delete(row)
        db.add_all([m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=person,
            programme_id=targets['programmes'][index], starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
            for person, index in ((h.seed['people'][2], 1), (outside, 2))])
        rows = {
            'own': request_row(h, h.seed['people'][0], 'Own private note'),
            'own_other': request_row(h, h.seed['people'][0], 'Own other request', kind='clinic_change'),
            'owned': request_row(h, h.seed['people'][1], 'Owned private note'),
            'current': request_row(h, h.seed['people'][2], 'Current private note'),
            'foreign': request_row(h, outside, 'Foreign private note'),
            'other_kind': request_row(h, h.seed['people'][1], 'Other request kind', kind='clinic_change'),
            'outside_dates': request_row(h, h.seed['people'][1], 'Outside trainee dates', day=731),
            'rotation': request_row(h, h.seed['people'][1], 'Owned rotation change', kind='rotation_change'),
            'foreign_rotation': request_row(h, h.seed['people'][1], 'Foreign rotation change', kind='rotation_change'),
        }
        rows['rotation'].rotation_id = targets['rotations'][0]
        rows['foreign_rotation'].rotation_id = targets['rotations'][2]
        rows['own'].workspace_id = rows['owned'].workspace_id = workspace['id']
        db.add_all(rows.values())
        db.flush()
        return targets, {name: {'id': row.id, 'revision': row.revision, 'workspace_id': row.workspace_id}
            for name, row in rows.items()}


def test_programme_lead_bulk_requests_share_own_and_trainee_scope_for_names_conditions_counts_and_pages(h, lead_requests):
    _, records = lead_requests
    expected = {records[name]['id'] for name in ('own', 'own_other', 'owned', 'current', 'rotation')}
    assert {row['id'] for row in h.get('scoped_lead', '/api/requests')['requests']} == expected
    assert {row['id'] for row in h.get('scoped_lead', '/api/requests/all')['items']} == expected
    assert h.get('scoped_lead', '/api/bulk/collections')['collections']['requests']['count'] == len(expected)
    client = h.client('scoped_lead')
    for day in ('2020-01-01', '2026-10-03', '2030-01-01'):
        selection = {'collection': 'requests', 'where': {'as_of': day, 'conditions': {'match': 'any', 'conditions': [
            {'field': 'status', 'op': 'none', 'values': ['approved']},
            {'field': 'person_ids', 'values': [h.seed['people'][1]]}]}}}
        resolved = client.post('/api/bulk/resolve', json=selection)
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()['count'] == len(expected)
        assert {row['id'] for row in resolved.json()['sample']} == expected
        assert all('private_reason' not in row and 'workspace_id' not in row for row in resolved.json()['sample'])
        pages = [client.post('/api/bulk/rows', json={'selection': selection, 'columns': ['person_id'],
            'offset': offset, 'limit': 2}) for offset in (0, 2, 4)]
        assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
        assert all(page.json()['count'] == len(expected) for page in pages)
        assert {row['id'] for page in pages for row in page.json()['rows']} == expected
    for selector in ({'include_ids': [row['id'] for row in records.values()]},
                     {'where': {'filters': {'id': [row['id'] for row in records.values()]}}}):
        resolved = client.post('/api/bulk/resolve', json={'collection': 'requests', **selector})
        assert resolved.status_code == 200 and resolved.json()['count'] == len(expected), resolved.text
    names = ['Owned programme trainee', 'Current programme trainee', 'Foreign programme trainee']
    resolved = client.post('/api/bulk/resolve', json={'collection': 'requests', 'names': names})
    assert resolved.status_code == 200, resolved.text
    matches = {row['line']: set(row['ids']) for row in resolved.json()['matches']}
    assert matches == {names[0]: {records[name]['id'] for name in ('owned', 'rotation')},
        names[1]: {records['current']['id']}, names[2]: set()}
    assert resolved.json()['unmatched'] == [names[2]] and resolved.json()['ambiguous'] == []


@pytest.mark.parametrize('authority', ['owner', 'membership', 'trainee_membership'])
def test_programme_lead_request_cache_rechecks_current_authority_and_trainee_dates(h, lead_requests, authority):
    targets, records = lead_requests
    kind = 'owned' if authority == 'owner' else 'current'
    request = {'selection': {'collection': 'requests', 'include_ids': [records[kind]['id']],
        'where': {'as_of': '2026-08-01'}}, 'columns': ['person_id']}
    client = h.client('scoped_lead')
    assert client.post('/api/bulk/rows', json=request).json()['count'] == 1
    with transaction(h.app.state.engine, write=True) as db:
        if authority == 'owner':
            row = db.get(m.Programme, targets['programmes'][0])
            row.owner_identity_id = None
        else:
            person = h.seed['people'][0 if authority == 'membership' else 2]
            row = db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == person,
                m.ProgrammeMembership.programme_id == targets['programmes'][1]))
            row.ends_on = date(2026, 9, 30)
        row.revision += 1
    assert records[kind]['id'] not in {row['id'] for row in h.get('scoped_lead', '/api/requests')['requests']}
    page = client.post('/api/bulk/rows', json=request)
    assert page.status_code == 200, page.text
    assert page.json()['count'] == 0 and page.json()['rows'] == []


def test_programme_lead_bulk_request_decisions_recheck_each_record_and_independence(h, lead_requests):
    _, records = lead_requests
    item = lambda name: {'target_id': records[name]['id'], 'expected_revision': records[name]['revision'],
        'payload': {'decision': 'reject'}}
    payload = {'item_kind': 'request.decide', 'items': [item('owned'), item('foreign')],
        'reason': 'Independent programme review'}
    preview = h.client('scoped_lead').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    assert [row['result'] for row in preview.json()['items']] == ['ready', 'problem']
    assert preview.json()['items'][1]['issues'][0]['code'] == 'FORBIDDEN'
    refused = h.command('scoped_lead', 'bulk.apply', payload, status=403)
    assert refused['error']['details']['failed_index'] == 1
    own = h.command('scoped_lead', 'bulk.apply', {**payload, 'items': [item('own')]}, status=403)
    assert own['error']['code'] == 'SELF_APPROVAL'
    with transaction(h.app.state.engine) as db:
        assert all(db.get(m.RequestRecord, row['id']).status == 'submitted' for row in records.values())
    command = {'kind': 'bulk.apply', 'payload': {'item_kind': 'request.decide',
        'selection': {'collection': 'requests', 'include_ids': [records['owned']['id']]},
        'common': {'decision': 'reject'}, 'reason': 'Independent programme review'}}
    client = h.client('scoped_lead')
    preview = client.post('/api/commands/preview', json=command)
    assert preview.status_code == 200 and preview.json()['counts']['ready'] == 1, preview.text
    applied = client.post('/api/commands', headers={'idempotency-key': str(uuid4())},
        json={**command, 'expected_fingerprint': preview.json()['source_fingerprint']})
    assert applied.status_code == 200, applied.text
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, records['owned']['id']).status == 'rejected'
        assert db.get(m.RequestRecord, records['foreign']['id']).status == 'submitted'


@pytest.mark.parametrize('role', ['staff', 'scoped_lead', 'coordinator'])
def test_bulk_request_workspace_identifiers_follow_ordinary_request_field_privacy(h, lead_requests, role):
    _, records = lead_requests
    workspace_id = records['own']['workspace_id']
    selection = {'collection': 'requests', 'include_ids': [records['own']['id'], records['owned']['id']]}
    client = h.client(role)
    resolved = client.post('/api/bulk/resolve', json=selection)
    assert resolved.status_code == 200, resolved.text
    page = client.post('/api/bulk/rows', json={'selection': selection})
    assert page.status_code == 200, page.text
    for row in [*resolved.json()['sample'], *page.json()['rows']]:
        if role == 'coordinator':
            assert row['workspace_id'] == workspace_id
        else:
            assert 'workspace_id' not in row
    column = client.post('/api/bulk/rows', json={'selection': selection, 'columns': ['workspace_id']})
    assert column.status_code == (200 if role == 'coordinator' else 422), column.text
    if role == 'coordinator':
        assert all(row['workspace_id'] == workspace_id for row in column.json()['rows'])
        return
    for value in (workspace_id, 'not-a-workspace'):
        for where in ({'filters': {'workspace_id': [value]}},
                      {'conditions': {'conditions': [{'field': 'workspace_id', 'values': [value]}]}}):
            refused = client.post('/api/bulk/resolve', json={'collection': 'requests', 'where': where})
            assert refused.status_code == 422, refused.text
        searched = client.post('/api/bulk/resolve', json={'collection': 'requests', 'where': {'q': value}})
        assert searched.status_code == 200 and searched.json()['count'] == 0, searched.text
    for option in ({'sort': [{'field': 'workspace_id'}]}, {'group': 'workspace_id'}):
        refused = client.post('/api/bulk/rows', json={'selection': selection, **option})
        assert refused.status_code == 422, refused.text
