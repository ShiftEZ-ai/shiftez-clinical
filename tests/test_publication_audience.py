"""Publication notification audience contract, preview and worker equivalence."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import authenticate, department_roles
from apps.api.db import transaction
from apps.api.services import people
from apps.worker import main as worker
from packages.contracts import schemas as s
from packages.contracts.notification_audiences import audience_definition
from .governance_support import identity
from .outbox_support import dispatch_batch_after_undo
from .test_pwa_push import ready, subscribe


def pytest_generate_tests(metafunc):
    if 'h' in metafunc.fixturenames:
        metafunc.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


def approved_with_audience(h):
    """Create an approved candidate whose audience includes a rota person and a wish-only person."""
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['admin']).person_id = h.seed['people'][0]
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = h.seed['people'][1]
        db.get(m.Identity, h.seed['identities']['approver']).person_id = h.seed['people'][2]
    workspace = h.workspace('Publication audience preview')
    workspace = h.shift(workspace, people=[h.seed['people'][0]])['workspace']
    h.command('coordinator', 'wish.create', {
        'person_id': h.seed['people'][2], 'first_day': '2026-10-05', 'last_day': '2026-10-05',
        'kind': 'prefer_off', 'points': 1,
    })
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    return h.lifecycle(workspace, 'approve', 'approver')['workspace']


def test_publication_description_is_the_shared_audience_definition():
    expected = audience_definition('publication')
    event = next(item for item in s.PWA_NOTIFICATION_EVENTS if item['id'] == 'publication')
    assert event['audience'] == expected.description
    assert all(group in event['audience'] for group in ('rota', 'coordinators', 'approvers', 'wish'))


def test_preview_recipient_identities_equal_worker_delivery(ready):
    h = ready
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).email_settings = s.EmailSettings(
            smtp_enabled=True, sender_address='rota@example.org', smtp_host='relay.example.org').model_dump(mode='json')
    h.command('admin', 'notification.policy.update', {
        'department_id': h.seed['department_id'], 'push_enabled': True,
        'event_defaults': {kind: {'email': 'on'} for kind in ('publication', 'duty_changed')},
        'reason': 'Exercise all publication delivery channels',
    }, revision=2)
    workspace = approved_with_audience(h)
    for role in ('staff', 'admin', 'approver', 'coordinator'):
        subscribe(h, role, endpoint='https://fcm.googleapis.com/audience-' + role, hint='audience-' + role)
    publisher = h.seed['identities']['coordinator']
    preview = h.get('coordinator', f"/api/workspaces/{workspace['id']}/publication-audience")
    assert preview['delivery'] == 'in_app'
    assert {row['identity_id'] for row in preview['recipients']} == {
        h.seed['identities']['staff'], h.seed['identities']['admin'], h.seed['identities']['approver']}
    assert [group['reason'] for group in preview['groups']] == list(s.PUBLICATION_AUDIENCE_REASON_LABELS)
    assert publisher not in {row['identity_id'] for row in preview['recipients']}
    assert all(set(group) == {'reason', 'count', 'visible_count', 'hidden_count'} for group in preview['groups'])

    published = h.lifecycle(workspace, 'publish')
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == 'publication:' + published['publication_id']))
        publication = db.get(m.Publication, published['publication_id'])
        assert publication.published_by == event.payload['actor_id'] == publisher
        assert worker.recipients_of(db, event, publication, department_roles(db, event.department_id)) == {
            row['identity_id'] for row in preview['recipients']}
        worker.dispatch_batch(db)
        assert event.state == 'available_in_app'
        delivered = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)))
        push = set(db.scalars(select(m.PushDelivery.identity_id).where(m.PushDelivery.source_outbox_id == event.id)))
        email = set(db.scalars(select(m.EmailDelivery.identity_id).where(m.EmailDelivery.source_outbox_id == event.id)))
    assert publisher not in delivered
    assert push == email == delivered
    assert delivered == {row['identity_id'] for row in preview['recipients']}


def test_department_publication_push_off_keeps_in_app_preview_and_delivery(h):
    workspace = approved_with_audience(h)
    h.command('admin', 'notification.policy.update', {
        'department_id': h.seed['department_id'], 'event_defaults': {'publication': {'push': 'off'}},
        'reason': 'Turn publication notifications off for this test',
    }, revision=1)
    preview = h.get('coordinator', f"/api/workspaces/{workspace['id']}/publication-audience")
    assert preview['delivery'] == 'in_app'
    expected = {h.seed['identities']['staff'], h.seed['identities']['admin'], h.seed['identities']['approver']}
    assert preview['count'] == len(expected)
    assert {row['identity_id'] for row in preview['recipients']} == expected
    assert all(row['channels'] == ['in_app'] for row in preview['recipients'])
    published = h.lifecycle(workspace, 'publish')
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == 'publication:' + published['publication_id']))
        worker.dispatch_batch(db)
        delivered = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)))
    assert delivered == expected


def test_publication_audience_requires_publish_permission(h):
    workspace = approved_with_audience(h)
    h.get('staff', f"/api/workspaces/{workspace['id']}/publication-audience", status=403)


def test_hidden_publication_recipients_are_counted_without_names(h, monkeypatch):
    workspace = h.workspace('Hidden publication audience')
    workspace = h.shift(workspace, people=[h.seed['people'][0]])['workspace']
    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    h.get('coordinator', '/api/access', status=403)
    visible_names = people.visible_identity_names
    visible = {h.seed['identities']['staff'], h.seed['identities']['coordinator']}
    monkeypatch.setattr(people, 'visible_identity_names', lambda db, actor, ids: {
        key: name for key, name in visible_names(db, actor, ids).items() if key in visible})
    preview = h.get('coordinator', f"/api/workspaces/{workspace['id']}/publication-audience")
    assert preview['count'] == 3 and preview['visible_count'] == 1 and preview['hidden_count'] == 2
    assert {row['identity_id'] for row in preview['recipients']} == visible - {h.seed['identities']['coordinator']}
    for group in preview['groups']:
        assert set(group) == {'reason', 'count', 'visible_count', 'hidden_count'}
        assert group['count'] == group['visible_count'] + group['hidden_count']
    assert h.seed['identities']['admin'] not in str(preview)
    assert h.seed['identities']['approver'] not in str(preview)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_preview_privacy_for_every_role(h, role):
    workspace = approved_with_audience(h)
    reader = 'audience_' + role
    identity(h, reader, [role])
    client = h.client(reader)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        permitted = actor.permits(*s.PERMISSIONS['workspace.publish'])
        names = people.visible_identity_names(db, actor, h.seed['identities'].values())
    response = client.get(f"/api/workspaces/{workspace['id']}/publication-audience")
    assert response.status_code == (200 if permitted else 403), response.text
    if not permitted:
        assert all(identity_id not in response.text for identity_id in h.seed['identities'].values())
        return
    preview = response.json()
    for group in preview['groups']:
        assert set(group) == {'reason', 'count', 'visible_count', 'hidden_count'}
    for row in preview['recipients']:
        assert set(row) == {'identity_id', 'display_name', 'reasons', 'channels'}
        assert row['identity_id'] in names
        assert row['display_name'] == names[row['identity_id']]
    assert actor.identity_id not in {row['identity_id'] for row in preview['recipients']}


def test_preview_has_no_incomplete_conditional_cache(h):
    workspace = approved_with_audience(h)
    path = f"/api/workspaces/{workspace['id']}/publication-audience"
    client = h.client('coordinator')
    first = client.get(path)
    assert first.status_code == 200
    assert 'etag' not in first.headers and first.headers['cache-control'] == 'no-store'
    added = identity(h, 'new_coordinator', ['coordinator'])
    second = client.get(path, headers={'if-none-match': '*'})
    assert second.status_code == 200 and 'etag' not in second.headers
    assert {row['identity_id'] for row in second.json()['recipients']} == {
        row['identity_id'] for row in first.json()['recipients']} | {added}


def test_amendment_preview_equals_delivered_for_added_removed_and_moved_people(h):
    removed, moved, added = h.seed['people'][:3]
    removed_id = h.seed['identities']['staff']
    moved_id = identity(h, 'moved_staff', ['staff'], moved)
    added_id = identity(h, 'added_staff', ['staff'], added)
    unchanged = h.configure('people', {'display_name': 'Unchanged staff', 'active': True})['record']['id']
    h.command('coordinator', 'membership.create', {
        'person_id': unchanged, 'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
    h.command('credentialer', 'eligibility.create', {
        'person_id': unchanged, 'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    unchanged_id = identity(h, 'unchanged_staff', ['staff'], unchanged)
    workspace = h.shift(h.workspace(), people=[removed])['workspace']
    workspace = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
                        people=[moved])['workspace']
    workspace = h.shift(workspace, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00',
                        people=[unchanged])['workspace']
    issued = h.publish(workspace)
    amendment = h.workspace(base=issued['publication_id'])
    shifts = h.detail(amendment)['shifts']
    for old_person, new_person in ((removed, added), (moved, None)):
        shift = next(row for row in shifts if any(p['person_id'] == old_person for p in row['participants']))
        start, end = (('2026-10-07T08:00:00+03:00', '2026-10-07T12:00:00+03:00') if new_person is None
                      else (shift['start'], shift['end']))
        amendment = h.command('coordinator', 'shift.update', {
            'workspace_id': amendment['id'], 'shift_id': shift['id'],
            'assignment_type_id': shift['assignment_type_id'], 'location_id': shift['location_id'],
            'service_ids': shift['service_ids'], 'start': start, 'end': end,
            'participants': [{'person_id': new_person or old_person, 'start': start, 'end': end}],
            'override_reason': 'Synthetic publication audience regression',
        }, revision=amendment['revision'])['workspace']
    amendment = h.lifecycle(amendment, 'submit')['workspace']
    amendment = h.lifecycle(amendment, 'approve', 'approver')['workspace']
    preview = h.get('coordinator', f"/api/workspaces/{amendment['id']}/publication-audience")
    publisher = h.seed['identities']['coordinator']
    expected = {removed_id, moved_id, added_id,
                h.seed['identities']['approver'], h.seed['identities']['admin']}
    assert {row['identity_id'] for row in preview['recipients']} == expected
    assert publisher not in expected
    assert unchanged_id not in expected
    published = h.lifecycle(amendment, 'publish')
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == 'publication:' + published['publication_id']))
        publication = db.get(m.Publication, published['publication_id'])
        assert publication.published_by == event.payload['actor_id'] == publisher
        assert set(event.payload['person_changes']) == {removed, moved, added}
        assert worker.recipients_of(db, event, publication, department_roles(db, event.department_id)) == expected
        worker.dispatch_batch(db)
        assert event.state == 'available_in_app'
        delivered = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)))
    assert publisher not in delivered
    assert delivered == {row['identity_id'] for row in preview['recipients']}


def test_feedback_still_notifies_its_reporter_for_their_own_status_change(h):
    from .test_feedback_api import detail, submit, update
    report = detail(h, submit(h, role='operator'))
    changed = update(h, report, status='triaged')['report']
    reporter = h.seed['identities']['operator']
    with transaction(h.app.state.engine, write=True) as db:
        dispatch_batch_after_undo(db)
        event = db.scalar(select(m.Outbox).where(
            m.Outbox.event_key == f"feedback-status:{report['id']}:{changed['revision']}"))
        assert event.payload['actor_id'] == event.payload['reporter_identity_id'] == reporter
        assert event.state == 'available_in_app'
        delivered = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)))
    assert delivered == {reporter}
