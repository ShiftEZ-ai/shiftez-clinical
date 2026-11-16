"""GET /api/directory: catalogue field visibility for every signed-in role (staff cannot read /api/catalogues)."""
import json

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import CATALOGUE_READ_ROLES, effective_roles
from packages.contracts.schemas import DEFAULT_CONTACT_TYPES, ROLE_NAMES
from .governance_support import identity


def test_anonymous_cannot_read_the_directory(h):
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert client.get('/api/directory').status_code == 401


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_every_role_reads_contacts_only_of_active_people(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    values = {'work_phone': '+966 11 000 0000', 'work_extension': '1234 / 5678',
              'pager': '4417', 'work_email': 'synthetic-directory@example.test'}
    active = h.configure('people', {'display_name': 'Fictional Directory Contact', 'title': 'Registrar',
        **values, 'staff_number': 'PRIVATE-001', 'directory_section': 'Urgent care', 'directory_order': 1,
        'tags': ['private-tag'], 'short_code': 'FDC'})['record']
    gone = h.configure('people', {'display_name': 'Fictional Former Colleague', 'work_phone': '999', 'active': False})['record']
    configured = h.get(role, '/api/contact-types')['contact_types']
    roles = effective_roles({role})
    # Read each type's saved policy; the new person is not the reader's own record.
    permitted = [typ for typ in configured if typ['visibility']['mode'] == 'everyone'
                 or typ['visibility']['mode'] == 'roles' and roles.intersection(typ['visibility']['roles'])
                 or typ['visibility']['mode'] == 'self_and_admins' and 'admin' in roles]
    contact_fields = {typ['legacy_field'] for typ in permitted}
    assert {'work_phone', 'pager'} <= contact_fields  # D68 defaults, for every ROLE_NAMES role.
    people = h.get(role, '/api/directory')['people']
    fields = {'id', 'display_name', 'given_name', 'family_name', 'title', 'directory_order', 'directory_section'}
    # Empty contacts are omitted; populated values may appear only under a permitted type or its legacy mirror.
    assert people and all(fields <= set(p) <= fields | contact_fields | {'contacts'} for p in people)
    mine = next(p for p in people if p['id'] == active['id'])
    assert set(mine) == fields | contact_fields | {'contacts'}
    assert {c['contact_type_id']: c['value'] for c in mine['contacts']} == {
        typ['id']: values[typ['legacy_field']] for typ in permitted}
    assert {key: mine[key] for key in contact_fields} == {key: values[key] for key in contact_fields}
    assert mine['directory_section'] == 'Urgent care'
    body = json.dumps(people)
    assert all(value not in body for key, value in values.items() if key not in contact_fields)
    assert all(not {'staff_number', 'tags'}.intersection(p) for p in people)
    assert 'PRIVATE-001' not in body and 'private-tag' not in body
    assert gone['id'] not in {p['id'] for p in people}
    # Directory order first, then name.
    assert people[0]['id'] == active['id']


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_directory_and_people_catalogue_have_role_safe_projections(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    active = h.configure('people', {
        'display_name': 'Fictional Projection Target', 'given_name': 'Fictional', 'family_name': 'Target',
        'title': 'Registrar', 'directory_section': 'Urgent care', 'directory_order': 2,
        'work_phone': '+966 11 000 0000', 'work_extension': '1234', 'work_email': 'projection@example.test',
        'pager': '4417', 'staff_number': 'PRIVATE-002', 'short_code': 'FPT', 'rota_name': 'Private rota',
        'rota_aliases': ['Private alias'], 'tags': ['private-tag'], 'colour': 'blue'})['record']

    directory = h.get(role, '/api/directory')['people']
    public = {'id', 'display_name', 'given_name', 'family_name', 'title', 'directory_section', 'directory_order'}
    contact_fields = {typ['legacy_field'] for typ in DEFAULT_CONTACT_TYPES if typ['legacy_field']}
    allowed = public | contact_fields | {'contacts'}
    forbidden = {'tags', 'short_code', 'colour', 'rota_name', 'rota_aliases', 'aliases', 'staff_number',
                 'display_title', 'revision', 'active', 'employee_number_redacted'}
    assert directory
    for person in directory:
        assert set(person) <= allowed
        assert not forbidden.intersection(person)
        assert not any(key.startswith('current_') or key.startswith('employee_number') for key in person)
    target = next(person for person in directory if person['id'] == active['id'])
    assert set(target) <= allowed and {'id', 'display_name', 'directory_section', 'directory_order'} <= set(target)

    if role not in CATALOGUE_READ_ROLES:
        return
    catalogue = h.get(role, '/api/catalogues?include=people')['people']
    row = next(person for person in catalogue if person['id'] == active['id'])
    if role in {'admin', 'coordinator'}:
        assert row['directory_section'] == 'Urgent care'
        return
    non_editor = {'id', 'revision', 'display_name', 'given_name', 'family_name', 'active', 'title', 'tags',
                  'display_title', 'current_category_ids', 'current_programme_ids', 'current_rotation_type_ids',
                  'current_cohort_ids', 'colour', 'short_code', 'rota_name', 'rota_aliases', 'aliases',
                  'employee_number_redacted', 'directory_order', 'contacts'}
    configured = h.get(role, '/api/contact-types')['contact_types']
    roles = effective_roles({role})
    permitted = {typ['legacy_field'] for typ in configured if typ['legacy_field'] and (
        typ['visibility']['mode'] == 'everyone'
        or typ['visibility']['mode'] == 'roles' and roles.intersection(typ['visibility']['roles'])
        or typ['visibility']['mode'] == 'self_and_admins' and 'admin' in roles)}
    expected = non_editor | permitted
    if role == 'approver':
        expected.add('directory_section')
    assert set(row) == expected
    assert ('directory_section' in row) == (role == 'approver')


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_linked_person_sees_all_contact_types_and_others_use_saved_policy(h, role):
    values = {'work_phone': '+966 11 111 1111', 'work_extension': '4321',
              'pager': '5522', 'work_email': 'self@example.test'}
    own = h.configure('people', {'display_name': 'Linked Directory Contact', **values})['record']
    other = h.configure('people', {'display_name': 'Other Directory Contact', **values})['record']
    reader = f'linked_directory_reader_{role}'
    identity(h, reader, [role], own['id'])
    configured = h.get(reader, '/api/contact-types')['contact_types']
    roles = effective_roles({role})
    directory = {row['id']: row for row in h.get(reader, '/api/directory')['people']}

    def expected(person_id):
        visible = [typ for typ in configured if person_id == own['id'] or
                   typ['visibility']['mode'] == 'everyone' or
                   typ['visibility']['mode'] == 'roles' and roles.intersection(typ['visibility']['roles']) or
                   typ['visibility']['mode'] == 'self_and_admins' and 'admin' in roles]
        return {typ['legacy_field']: values[typ['legacy_field']] for typ in visible}

    assert {c['legacy_field']: c['value'] for c in directory[own['id']]['contacts']} == expected(own['id'])
    assert {c['legacy_field']: c['value'] for c in directory[other['id']]['contacts']} == expected(other['id'])
