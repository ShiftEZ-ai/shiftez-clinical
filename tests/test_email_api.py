"""M4 public backend contracts and synthetic end-to-end provider acceptance."""
from email import message_from_bytes, policy
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import mailboxes
from tests.outbox_support import process_one_after_undo as process_one
from packages.contracts import schemas as s
from .governance_support import identity
from .test_email_delivery import smtp
from .test_events import event, event_type, input_of, occurrences
from .test_events import repeat


def settings(h, **values):
    current = h.get('admin', '/api/email/settings')
    return h.command('admin', 'email.settings', {'value': {**current['value'], **values}, 'reason': 'Synthetic email setup'},
                     h.seed['department_id'], current['revision'])


def notice(h, **values):
    return h.configure('department_content', {'name': 'Synthetic email heading', 'description': '<Agenda> & details',
        'starts_on': '2020-01-01', 'audience': {'roles': ['staff']}, **values}, role='coordinator')['record']


def staff_address(h, value='work@example.test'):
    with transaction(h.app.state.engine, write=True) as db:
        ident = db.get(m.Identity, h.seed['identities']['staff'])
        db.get(m.Person, ident.person_id).work_email = value
        return ident.person_id


def parsed(response):
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('message/rfc822')
    assert response.headers['cache-control'] == 'no-store'
    return message_from_bytes(response.content, policy=policy.default)


def test_provider_timeout_is_bounded_to_sixty_seconds():
    assert s.EmailSettings(timeout_seconds=1).timeout_seconds == 1
    assert s.EmailSettings(timeout_seconds=60).timeout_seconds == 60
    for timeout in (0, 61):
        with pytest.raises(ValidationError):
            s.EmailSettings(timeout_seconds=timeout)


def test_settings_defaults_audit_undo_revision_and_secrets(h, monkeypatch):
    monkeypatch.setenv('FMUC_SMTP_PASSWORD', 'synthetic-private-smtp-password')
    monkeypatch.setenv('FMUC_M365_CLIENT_SECRET', 'synthetic-private-client-secret')
    initial = h.get('admin', '/api/email/settings')
    assert not initial['value']['smtp_enabled'] and not initial['value']['m365_enabled']
    assert initial['value']['event_invites'] == 'without_calendar_link'
    changed = settings(h, announcement_default=True, recipient_header='To')
    assert changed['previous_value'] == initial['value']
    h.command('admin', 'email.settings', {'value': initial['value'], 'reason': 'Synthetic undo'},
              h.seed['department_id'], changed['revision'])
    h.command('admin', 'email.settings', {'value': initial['value'], 'reason': 'Stale revision'},
              h.seed['department_id'], initial['revision'], status=409)
    h.command('admin', 'email.settings', {'value': {**initial['value'], 'password': 'never-store-this'},
              'reason': 'Unsupported secret field'}, h.seed['department_id'], changed['revision'] + 1, status=422)
    output = str(h.get('admin', '/api/email/settings'))
    assert 'synthetic-private' not in output and 'never-store-this' not in output
    with transaction(h.app.state.engine) as db:
        audits = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'email.settings')))
        assert len(audits) == 2


def test_authenticated_smtp_settings_use_deployment_host_and_data_key_status_uses_loaded_settings(h, monkeypatch):
    saved = settings(h, smtp_enabled=True, smtp_username='synthetic-user', sender_address='sender@example.test')
    assert saved['value']['smtp_host'] == ''
    object.__setattr__(h.settings, 'fmuc_data_key', 'ab' * 32)
    monkeypatch.setenv('FMUC_DATA_KEY', 'invalid-after-startup')
    assert h.get('admin', '/api/email/settings')['data_key_configured'] is True
    object.__setattr__(h.settings, 'fmuc_data_key', '')
    assert h.get('admin', '/api/email/settings')['data_key_configured'] is False


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_editor_drafts_and_previews_and_admin_settings_permissions(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    row = notice(h)
    for suffix in ('email-preview', 'draft.eml'):
        response = h.client(role).get(f"/api/content/{row['id']}/{suffix}")
        assert response.status_code == (200 if role in {'admin', 'coordinator'} else 403)
    h.get(role, '/api/email/settings', status=200 if role == 'admin' else 403)


def test_draft_bcc_addresses_fallback_missing_and_to_setting(h):
    pid = staff_address(h)
    identity(h, 'missing_mail', ['staff'])
    row = notice(h)
    preview = h.get('coordinator', f"/api/content/{row['id']}/email-preview")
    assert preview['addresses'] == ['work@example.test']
    assert any(r['identity_id'] == h.seed['identities']['missing_mail'] for r in preview['missing_addresses'])
    mail = parsed(h.client('coordinator').get(f"/api/content/{row['id']}/draft.eml"))
    assert mail['Bcc'] == 'work@example.test' and mail['To'] is None and mail['X-Unsent'] == '1'
    assert mail['Subject'] == row['name']
    assert '&lt;Agenda&gt; &amp; details' in mail.get_body(preferencelist=('html',)).get_content()
    assert 'Open in Rota to acknowledge' in mail.get_body(preferencelist=('plain',)).get_content()
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, pid).work_email = ''
        db.add(m.Account(identity_id=h.seed['identities']['staff'], username='synthetic-email-fallback', email='account@example.test'))
    settings(h, recipient_header='To')
    changed = parsed(h.client('coordinator').get(f"/api/content/{row['id']}/draft.eml"))
    assert changed['To'] == 'account@example.test' and changed['Bcc'] is None


def test_event_draft_matches_feed_uid_and_override_details(h):
    staff_address(h)
    row = event(h)
    occurrence = occurrences(h)[0]
    path = h.command('staff', 'calendar_feed.create')['path']
    feed = h.client('staff').get(path).text
    mail = parsed(h.client('coordinator').get(f"/api/events/{row['id']}/draft.eml"))
    invite = next(mail.iter_attachments())
    uid = f"UID:event-{occurrence['id']}@rota.invalid"
    assert uid in feed and uid in invite.get_content()
    assert invite.get_param('method') == 'REQUEST'
    changed = h.command('coordinator', 'event.occurrence.update', {'value': input_of(row, heading='Changed occurrence'),
        'reason': 'Synthetic changed occurrence'}, occurrence['id'], occurrence['revision'])['occurrence']
    mail = parsed(h.client('coordinator').get(f"/api/events/{changed['id']}/draft.eml"))
    assert mail['Subject'] == 'Changed occurrence'


def test_microsoft_sender_defaults_are_pinned_and_occurrence_cannot_use_another_mailbox(h):
    settings(h, default_sender='microsoft', m365_enabled=True, m365_tenant_id='tenant.example', m365_client_id='client')
    row = event(h, organiser_ids=[], also_email=True)
    occurrence = occurrences(h)[0]
    assert row['email_sender_identity_id'] == h.seed['identities']['coordinator']
    assert occurrence['data']['email_sender_identity_id'] == row['email_sender_identity_id']
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(row,
        email_sender_identity_id=h.seed['identities']['admin']), 'reason': 'Cannot use someone else mailbox'},
        occurrence['id'], occurrence['revision'], status=403)
    preview = h.get('coordinator', f"/api/events/{row['id']}/email-preview")
    assert preview['sender_mode'] == 'microsoft' and 'draft' in preview['message']


def test_series_draft_refuses_to_combine_different_occurrence_audiences(h):
    row = event(h, repeat=repeat(), repeat_until='2026-10-12')
    first = occurrences(h)[0]
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(row, repeat=None, repeat_until=None,
        audience={'roles': ['coordinator']}, description='Restricted occurrence detail'), 'reason': 'Restrict this occurrence'},
        first['id'], first['revision'])
    reply = h.client('coordinator').get(f"/api/events/{row['id']}/draft.eml")
    assert reply.status_code == 409 and reply.json()['error']['code'] == 'EMAIL_AUDIENCES_DIFFER'
    assert 'Restricted occurrence detail' not in reply.text


def test_occurrence_preview_and_draft_use_its_sender_and_mixed_series_requires_separate_drafts(h):
    settings(h, smtp_enabled=True, smtp_host='127.0.0.1', sender_address='department@example.test',
             m365_enabled=True, m365_tenant_id='tenant.example', m365_client_id='client')
    row = event(h, repeat=repeat(), repeat_until='2026-10-12')
    occurrence = occurrences(h)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Account(identity_id=h.seed['identities']['coordinator'], username='coordinator-mail-contact',
                         email='organiser@example.test'))
    changed = h.command('coordinator', 'event.occurrence.update', {'value': input_of(row, repeat=None, repeat_until=None,
        email_sender='microsoft'), 'reason': 'Use my own mailbox for this occurrence'},
        occurrence['id'], occurrence['revision'])['occurrence']
    preview = h.get('coordinator', f"/api/events/{changed['id']}/email-preview")
    assert preview['sender_mode'] == 'microsoft'
    assert preview['sender_identity_id'] == h.seed['identities']['coordinator']
    mail = parsed(h.client('coordinator').get(f"/api/events/{changed['id']}/draft.eml"))
    assert 'organiser@example.test' in str(mail['From'])
    assert 'department@example.test' not in str(mail['From'])
    reply = h.client('coordinator').get(f"/api/events/{row['id']}/draft.eml")
    assert reply.status_code == 409 and reply.json()['error']['code'] == 'EMAIL_SENDERS_DIFFER'


def test_operations_manual_retry_respects_graph_cooldown_and_resets_recipient_status(h):
    with transaction(h.app.state.engine, write=True) as db:
        parent = m.Outbox(department_id=h.seed['department_id'], event_key=m.uid(), kind='event', payload={})
        child = m.Outbox(department_id=h.seed['department_id'], event_key=m.uid(), kind='email', payload={}, state='failed',
                         last_error='GRAPH_THROTTLED', next_attempt_at=m.now() + timedelta(seconds=60))
        db.add_all([parent, child])
        db.flush()
        delivery = m.EmailDelivery(department_id=h.seed['department_id'], outbox_id=child.id, source_outbox_id=parent.id,
            identity_id=h.seed['identities']['staff'], sender_mode='microsoft', address='work@example.test',
            state='failed', error_code='GRAPH_THROTTLED')
        db.add(delivery)
        child_id = child.id
    h.command('operator', 'outbox.retry', {'reason': 'Synthetic too early'}, child_id, 1, status=429)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Outbox, child_id).next_attempt_at = m.now()
    h.command('operator', 'outbox.retry', {'reason': 'Synthetic allowed retry'}, child_id, 1)
    with transaction(h.app.state.engine) as db:
        delivery = db.scalar(select(m.EmailDelivery).where(m.EmailDelivery.outbox_id == child_id))
        assert delivery.state == 'queued' and delivery.error_code is None


def test_cancellation_draft_keeps_previous_recipients_after_audience_change(h):
    staff_address(h)
    row = event(h)
    occurrence = occurrences(h)[0]
    process_one(h.app.state.engine, settings=h.settings)
    changed = h.command('coordinator', 'catalogue.update', {'catalogue': 'events', 'value': input_of(row,
        audience={'roles': ['coordinator']}, cancelled=True), 'reason': 'Synthetic cancellation'}, row['id'], row['revision'])['record']
    mail = parsed(h.client('coordinator').get(f"/api/events/{changed['id']}/draft.eml"))
    assert 'work@example.test' in str(mail['Bcc'])
    calendar = next(mail.iter_attachments())
    assert calendar.get_param('method') == 'CANCEL' and 'STATUS:CANCELLED' in calendar.get_content()
    assert f"UID:event-{occurrence['id']}@rota.invalid" in calendar.get_content()


def test_invite_default_suppresses_only_attachment_with_active_calendar_link(h, smtp):
    staff_address(h)
    settings(h, smtp_enabled=True, smtp_host='127.0.0.1', smtp_port=smtp.server_address[1], smtp_starttls=False,
             sender_address='invites@example.test')
    h.command('staff', 'calendar_feed.create')
    typ = event_type(h, also_email=True)
    row = event(h, typ)
    process_one(h.app.state.engine, settings=h.settings)
    assert len(smtp.messages) == 1
    mail = message_from_bytes(smtp.messages[0][1], policy=policy.default)
    assert not list(mail.iter_attachments())
    assert list(parsed(h.client('coordinator').get(f"/api/events/{row['id']}/draft.eml")).iter_attachments())
    h.command('staff', 'calendar_feed.revoke')
    row = h.configure('events', input_of(row, heading='Updated meeting'), row['id'], row['revision'], role='coordinator')['record']
    process_one(h.app.state.engine, settings=h.settings)
    assert len(smtp.messages) == 2
    assert list(message_from_bytes(smtp.messages[-1][1], policy=policy.default).iter_attachments())


@pytest.mark.postgres
def test_postgres_notified_announcement_flows_through_worker_smtp_and_receipts(pg, smtp):
    h = pg
    staff_address(h)
    settings(h, smtp_enabled=True, smtp_host='127.0.0.1', smtp_port=smtp.server_address[1], smtp_starttls=False,
             sender_address='invites@example.test', announcement_default=True)
    row = notice(h, importance='must_acknowledge', notify=True)
    process_one(h.app.state.engine, settings=h.settings)
    assert len(smtp.messages) == 1
    receipt = h.get('coordinator', f"/api/content/{row['id']}/receipts")['receipts'][0]
    assert receipt['email_deliveries'][0]['state'] == 'sent'
    assert receipt['email_deliveries'][0]['channel'] == 'email'
    assert receipt['delivered_at'] is not None and receipt['read_at'] is None and receipt['acknowledged_at'] is None
    assert len(h.get('staff', '/api/notifications')['notifications']) == 1
    operations = h.get('operator', '/api/operations?kind=email&state=sent')
    assert operations['total'] == 1 and operations['outbox'][0]['state'] == 'sent'
    assert operations['metrics']['outbox_states']['sent'] == 1
    assert all('payload' not in row for row in operations['outbox'])
    process_one(h.app.state.engine, settings=h.settings)
    assert len(smtp.messages) == 1
    checks = inspect(h.app.state.engine).get_check_constraints('events')
    assert any('end' in c['sqltext'] and 'start' in c['sqltext'] for c in checks)



def sender_update(h, scope, current, changes, *, omit=False, status=200):
    data = current['data'] if scope == 'occurrence' else current
    fields = s.DepartmentContentInput.model_fields if scope == 'announcement' else s.EventInput.model_fields
    value = {**{key: data[key] for key in fields if key in data}, **changes}
    if omit:
        for key in ('also_email', 'email_sender', 'email_sender_identity_id'):
            if key not in changes:
                value.pop(key, None)
    if scope == 'occurrence':
        reply = h.command('second_coordinator', 'event.occurrence.update',
            {'value': value, 'reason': 'Maintain the existing sender'}, current['id'], current['revision'], status=status)
        return reply['occurrence'] if status == 200 else reply
    catalogue = 'department_content' if scope == 'announcement' else 'events'
    reply = h.configure(catalogue, value, current['id'], current['revision'], role='second_coordinator', status=status)
    return reply['record'] if status == 200 else reply


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('scope', ['announcement', 'series', 'occurrence'])
def test_another_editor_can_maintain_preserved_sender_without_selecting_a_foreign_mailbox(request, database, scope, monkeypatch):
    h = request.getfixturevalue(database)
    settings(h, default_sender='microsoft', smtp_enabled=True, smtp_host='127.0.0.1',
        sender_address='department@example.test', m365_enabled=True, m365_tenant_id='tenant.example', m365_client_id='client')
    owner = h.seed['identities']['coordinator']
    editor = identity(h, 'second_coordinator', ['coordinator'])
    current = notice(h, also_email=True, email_sender='microsoft') if scope == 'announcement' else event(h,
        also_email=True, email_sender='microsoft', repeat=repeat(), repeat_until='2026-10-12')
    if scope == 'occurrence':
        current = occurrences(h)[0]
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender_identity_id'] == owner
    fields = s.DepartmentContentInput.model_fields if scope == 'announcement' else s.EventInput.model_fields
    creation = {key: data[key] for key in fields if key in data}
    rejected = h.configure('department_content' if scope == 'announcement' else 'events', creation,
        role='second_coordinator', status=403)
    assert rejected['error']['code'] == 'EMAIL_SENDER'
    rejected = sender_update(h, scope, current, {'email_sender_identity_id': h.seed['identities']['admin']}, status=403)
    assert rejected['error']['code'] == 'EMAIL_SENDER'
    current = sender_update(h, scope, current, {'description': 'Another editor maintained metadata'}, omit=True)
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender_identity_id'] == owner and data['also_email'] is True
    rejected = sender_update(h, scope, current, {'email_sender': 'smtp'}, status=403)
    assert rejected['error']['code'] == 'EMAIL_SENDER'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, owner).disabled = True
    current = sender_update(h, scope, current, {'description': 'Keep the disabled former sender for maintenance'})
    current = sender_update(h, scope, current, {'also_email': False}, omit=True)
    current = sender_update(h, scope, current, {'active': False} if scope == 'announcement' else {'cancelled': True})
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender_identity_id'] == owner and data['email_sender'] == 'microsoft' and data['also_email'] is False
    monkeypatch.setattr(mailboxes.httpx, 'post', lambda *args, **kwargs: pytest.fail('Disabled senders must not contact Microsoft'))
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as stopped:
            mailboxes.access_token(db, owner, h.seed['department_id'], h.settings)
        assert stopped.value.code == 'MAILBOX_UNAVAILABLE'
    current = sender_update(h, scope, current, {'email_sender_identity_id': editor})
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender_identity_id'] == editor


@pytest.mark.parametrize('scope', ['announcement', 'series', 'occurrence'])
def test_metadata_does_not_select_a_mailbox_when_department_default_changes(h, scope):
    current = notice(h, also_email=False) if scope == 'announcement' else event(h, also_email=False)
    if scope == 'occurrence':
        current = occurrences(h)[0]
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender'] is None and data['email_sender_identity_id'] is None
    settings(h, default_sender='microsoft', m365_enabled=True, m365_tenant_id='tenant.example', m365_client_id='client')
    identity(h, 'second_coordinator', ['coordinator'])
    current = sender_update(h, scope, current, {'description': 'Unrelated maintenance after default change'}, omit=True)
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender'] is None and data['email_sender_identity_id'] is None


@pytest.mark.parametrize('scope', ['series', 'occurrence'])
def test_maintained_event_sender_still_requires_organiser_and_configured_modes(h, scope):
    settings(h, default_sender='microsoft', smtp_enabled=True, smtp_host='127.0.0.1',
        sender_address='department@example.test', m365_enabled=True, m365_tenant_id='tenant.example', m365_client_id='client')
    owner = h.seed['identities']['coordinator']
    organiser, other = h.seed['people'][:2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, owner).person_id = organiser
    identity(h, 'second_coordinator', ['coordinator'])
    current = event(h, organiser_ids=[organiser, other], also_email=True, email_sender='microsoft')
    if scope == 'occurrence':
        current = occurrences(h)[0]
    current = sender_update(h, scope, current, {'description': 'Another editor maintained the organiser mailbox'}, omit=True)
    data = current['data'] if scope == 'occurrence' else current
    assert data['email_sender_identity_id'] == owner and data['organiser_ids'] == [organiser, other]
    rejected = sender_update(h, scope, current, {'organiser_ids': [other]}, status=422)
    assert rejected['error']['code'] == 'EMAIL_ORGANISER'
    settings(h, smtp_enabled=False)
    rejected = sender_update(h, scope, current, {'description': 'Modes must still permit this sender'}, status=409)
    assert rejected['error']['code'] == 'EMAIL_SENDER_MODE'
