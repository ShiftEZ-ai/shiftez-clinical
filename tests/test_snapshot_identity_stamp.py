"""Only identity links/enabled state and dated grants invalidate protected-time inputs."""
from datetime import date

import pytest
from sqlalchemy import insert, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from tests.governance_support import identity
from tests.test_accounts import PASSWORD, accepted, sign_in, with_users_file
from tests.test_bulk import people_change, preview
from tests.test_events import event, event_type
from tests.test_fix_list_performance import backend
from tests.test_snapshot_non_input_stamp import input_state


def all_stamps(h):
    with transaction(h.app.state.engine) as db:
        return {row.id: (row.input_revision, row.availability_revision) for row in db.scalars(select(m.Organisation))}


def extra_organisations(h):
    with transaction(h.app.state.engine, write=True) as db:
        other, untouched = [m.Organisation(id=m.uid(), name=name) for name in ('Other synthetic org', 'Untouched synthetic org')]
        db.add_all([other, untouched])
        db.flush()
        person = m.Person(id=m.uid(), organisation_id=other.id, display_name='Synthetic linked person')
        department = m.Department(id=m.uid(), organisation_id=other.id, name='Other synthetic department')
        db.add_all([person, department])
    return other.id, untouched.id, person.id, department.id


def blocking_audience_events(h, other_department):
    meeting = event(h, event_type(h, blocks_duties=True))
    with transaction(h.app.state.engine, write=True) as db:
        source = db.get(m.Event, meeting['id'])
        typ = m.EventType(department_id=other_department, name='Synthetic audience event type',
                          starts_on=date(2020, 1, 1), blocks_duties=True)
        db.add(typ)
        db.flush()
        db.add(m.Event(department_id=other_department, event_type_id=typ.id,
                       heading='Synthetic audience event', start=source.start, end=source.end,
                       occurrence_data=source.occurrence_data))


@pytest.mark.parametrize('writer', ['flush', 'statement'])
def test_extra_role_without_blocking_events_keeps_checked_draft_off_home(backend, writer):
    h = backend
    workspace = h.workspace('Checked empty draft')
    h.lifecycle(workspace, 'validate')
    before = all_stamps(h)
    inputs_before = input_state(h, workspace)
    if writer == 'flush':
        identity(h, 'linked-role-account', ['staff', 'coordinator'], h.seed['people'][0])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(insert(m.RoleBinding), [{
                'department_id': h.seed['department_id'], 'identity_id': h.seed['identities']['staff'],
                'role': 'coordinator', 'starts_on': date(2020, 1, 1),
            }])
    assert all_stamps(h) == before
    assert input_state(h, workspace) == inputs_before
    assert not [item for item in h.get('coordinator', '/api/home')['attention']
                if item.get('rota_id') == workspace['id']]


def test_extra_role_that_changes_blocking_attendance_advances_draft_inputs(backend):
    h = backend
    event(h, event_type(h, blocks_duties=True), audience={'roles': ['coordinator']})
    workspace = h.shift(h.workspace())['workspace']
    before_fingerprint, before_hint = input_state(h, workspace)

    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'],
        'role': 'coordinator', 'starts_on': '2026-10-01', 'reason': 'Synthetic additional role'})

    fingerprint, hint = input_state(h, workspace)
    assert fingerprint != before_fingerprint
    assert hint['availability_revision'] == before_hint['availability_revision'] + 1


def test_identity_exclusions_are_exactly_the_metadata_not_read_by_protected_facts():
    spec = next(spec for spec in snapshots.snapshot_input_specs() if spec[1] is m.Identity)
    # person_role_context selects the join key and person link, filtered by enabled state.
    assert set(m.Identity.__table__.columns.keys()) - spec[-1] == {'id', 'person_id', 'disabled'}


def test_sign_in_password_and_preferences_do_not_stamp_any_organisation(backend, tmp_path):
    h = with_users_file(backend, tmp_path)
    extra_organisations(h)
    identity_id = accepted(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, identity_id).person_id = h.seed['people'][0]
    before = all_stamps(h)
    client, status = sign_in(h, 'new.person', PASSWORD)
    assert status == 200 and all_stamps(h) == before
    h.clients['metadata_account'] = client
    response = client.post('/api/account/password', json={
        'current_password': PASSWORD, 'new_password': 'synthetic new passphrase 456'})
    assert response.status_code == 200
    assert all_stamps(h) == before
    h.command('metadata_account', 'notification.preferences.update', {'department_id': h.seed['department_id'],
        'event_overrides': {'swap': {'push': 'off'}}, 'reason': 'Synthetic preference change'}, revision=1)
    assert all_stamps(h) == before


@pytest.mark.parametrize('action', ['link', 'unlink', 'relink', 'disable', 'enable', 'unlinked_disable', 'unlinked_enable'])
def test_identity_changes_stamp_only_old_and_new_person_organisations(backend, action):
    h = backend
    other_id, untouched_id, other_person, other_department = extra_organisations(h)
    blocking_audience_events(h, other_department)
    target_id = h.seed['identities']['staff']
    if action == 'link' or action.startswith('unlinked_'):
        with transaction(h.app.state.engine, write=True) as db:
            identity = m.Identity(id=m.uid(), issuer='local-development', subject='stamp-unlinked', display_name='Synthetic unlinked identity')
            db.add(identity)
            db.add(m.RoleBinding(department_id=h.seed['department_id'], identity_id=identity.id,
                                 role='staff', starts_on=date(2020, 1, 1)))
        target_id = identity.id
    if action.endswith('enable'):
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Identity, target_id).disabled = True
    payload = people_change(h)
    check = preview(h, 'coordinator', payload)
    before = all_stamps(h)
    if action in {'disable', 'enable', 'unlinked_disable', 'unlinked_enable'}:
        with transaction(h.app.state.engine) as db:
            revision = db.get(m.Identity, target_id).revision
        h.command('admin', 'identity.' + action.rsplit('_', 1)[-1], {'reason': 'Synthetic enabled-state change'}, target_id, revision)
    else:
        # Existing link creation uses account.invite/identity.bind; relink/unlink have no public endpoint.
        with transaction(h.app.state.engine, write=True) as db:
            identity = db.get(m.Identity, target_id)
            identity.person_id = (other_person if action == 'relink' else h.seed['people'][0] if action == 'link' else None)
            identity.revision += 1
            db.flush()
            db.flush()
    affected = set() if action.startswith('unlinked_') else {h.seed['organisation_id']}
    if action == 'relink':
        affected.add(other_id)
    after = all_stamps(h)
    assert after == {ident: (general + (ident in affected), availability + (ident in affected))
                     for ident, (general, availability) in before.items()}
    assert after[untouched_id] == before[untouched_id]
    fresh = preview(h, 'coordinator', payload)
    if affected:
        assert fresh['fingerprint'] != check['fingerprint']
        assert h.command('coordinator', 'bulk.apply', payload, expected_fingerprint=check['fingerprint'], status=409)['error']['code'] == 'STALE_PREVIEW'
    else:
        assert fresh['fingerprint'] == check['fingerprint']


@pytest.mark.parametrize('action', ['create', 'date', 'end', 'restore', 'scope'])
def test_role_binding_writers_stamp_the_binding_scope_once(backend, action):
    h = backend
    other_id, untouched_id, _, other_department = extra_organisations(h)
    blocking_audience_events(h, other_department)
    with transaction(h.app.state.engine, write=True) as db:
        identity = m.Identity(id=m.uid(), issuer='local-development', subject='role-stamp-' + m.uid(),
                              display_name='Synthetic role target')
        db.add(identity)
        db.flush()
        identity_id = identity.id
    value = {'identity_id': identity_id, 'role': 'staff',
             'starts_on': '2026-10-04', 'reason': 'Synthetic dated grant'}
    row = None if action == 'create' else h.command('admin', 'role.grant', value)['record']
    if action == 'restore':
        row = h.command('admin', 'role.revoke', {'reason': 'Synthetic end'}, row['id'], row['revision'])['record']
    payload = people_change(h)
    check = preview(h, 'coordinator', payload)
    before = all_stamps(h)
    if action == 'create':
        h.command('admin', 'role.grant', value)
    elif action in {'end', 'restore'}:
        h.command('admin', 'role.restore' if action == 'restore' else 'role.revoke',
                  {'reason': 'Synthetic dated-grant transition'}, row['id'], row['revision'])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            binding = db.get(m.RoleBinding, row['id'])
            if action == 'date':
                binding.starts_on = date(2026, 10, 5)
                binding.ends_on = date(2026, 10, 12)
            else:
                binding.department_id = other_department
            binding.revision += 1
            db.flush()
            db.flush()
    affected = {h.seed['organisation_id'], *([other_id] if action == 'scope' else [])}
    assert all_stamps(h) == {ident: (general + (ident in affected), availability + (ident in affected))
                            for ident, (general, availability) in before.items()}
    assert all_stamps(h)[untouched_id] == before[untouched_id]
    assert preview(h, 'coordinator', payload)['fingerprint'] != check['fingerprint']
