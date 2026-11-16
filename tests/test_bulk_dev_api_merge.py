"""Bulk settings and inbox links coexist with the admitted events and preference features."""
from datetime import timedelta
import uuid

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk_department_settings_contract import settings_command
from .test_bulk_staged import staged_change


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('trainee', [False, True])
def test_me_contract_preserves_training_status_and_the_owned_staged_count(request, database, trainee):
    h = request.getfixturevalue(database)
    staged_change(h)
    person = h.configure('people', {'display_name': 'Synthetic signed-in trainee'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = person['id']
        if trainee:
            programme = m.Programme(department_id=h.seed['department_id'], name='Synthetic training programme',
                                    starts_on=m.now().date())
            db.add(programme)
            db.flush()
            db.add(m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=person['id'],
                programme_id=programme.id, starts_on=m.now().date(), ends_on=m.now().date() + timedelta(days=30)))
    body = h.get('coordinator', '/api/me')
    value = s.Me.model_validate(body)
    assert value.permissions == body['permissions'] == sorted(
        name for name, roles in s.PERMISSIONS.items() if 'coordinator' in roles)
    assert body['trainee'] is value.trainee is trainee
    assert value.bulk_staged_count == body['bulk_staged_count'] == 1
    assert value.catalogue_version == body['catalogue_version'] == 2
    assert s.Me.model_validate({key: item for key, item in body.items() if key != 'trainee'}).trainee is False


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_one_department_settings_write_and_undo_preserve_bulk_preferences_and_event_controls(request, database):
    h = request.getfixturevalue(database)
    original = h.get('admin', '/api/me')['department']
    effective_bulk = h.get('admin', '/api/bulk/settings')['settings']
    values = {'bulk_max_items': 120, 'bulk_direct_max': 12, 'bulk_staged_days': 9,
              'rate_bulk_preview_per_minute': 17}
    department, organisation, payload = settings_command(h, values)
    controls = {'wish_settings': {'types': {'prefer_off': {'default_points': 3}}},
                'event_horizon_days': 91, 'event_receipt_window_days': 9,
                'swap_offer_window_days': 21, 'swap_open_offer_cap': 4,
                'announcement_send_time': '10:15', 'announcement_auto_reminder_days': 3,
                'announcement_reminder_interval_hours': 12}
    result = h.command('admin', 'department.settings', {**payload, **controls},
                       department['id'], organisation['revision'])
    expected = {**controls, 'wish_settings': s.WishSettings.model_validate(controls['wish_settings']).model_dump()}
    assert {key: result['department'][key] for key in expected} == expected
    assert result['department']['bulk_settings'] == values
    assert result['previous']['bulk_settings'] == effective_bulk
    restored = h.command('admin', 'department.settings', {**result['previous'], 'reason': 'Undo combined settings'},
                         department['id'], result['organisation']['revision'])
    assert {key: restored['department'][key] for key in controls} == {key: original[key] for key in controls}
    assert h.get('admin', '/api/bulk/settings')['settings'] == effective_bulk


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_inbox_keeps_bulk_staged_and_event_links_with_receipt_joins(request, database):
    h = request.getfixturevalue(database)
    staged_id, event_id = str(uuid.uuid4()), str(uuid.uuid4())
    with transaction(h.app.state.engine, write=True) as db:
        for kind, link in [('bulk.staged.expired', {'staged_id': staged_id}), ('event.reminder', {'event_id': event_id})]:
            event = m.Outbox(department_id=h.seed['department_id'], event_key=str(uuid.uuid4()),
                             kind=kind, payload=link, state='sent')
            db.add(event)
            db.flush()
            db.add(m.Notification(outbox_id=event.id, identity_id=h.seed['identities']['coordinator'],
                                  title='Synthetic integration notice', message='No private payload'))
    notices = h.get('coordinator', '/api/notifications')['notifications']
    links = {key: value for notice in notices for key, value in notice['link'].items()}
    assert links['staged_id'] == staged_id and links['event_id'] == event_id
    assert all(not notice['acknowledgement_required'] for notice in notices)
