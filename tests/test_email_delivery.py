"""Synthetic provider acceptance, private failures and per-recipient retry evidence."""
import base64
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.utils import format_datetime
import socketserver
import smtplib
import ssl
import threading

import httpx
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.data_crypto import encrypt_secret
from apps.api.errors import Problem
from apps.api.services.email_audiences import extra_people
from apps.worker import email_delivery as email
from apps.worker import main as worker
from tests.outbox_support import dispatch_batch_after_undo
from packages.domain.time import aware
from .clock_support import freeze_clock
from .governance_support import identity
from .test_events import definition, event, event_type, input_of, occurrences, repeat


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 Synthetic SMTP\r\n")
        recipients = []
        while line := self.rfile.readline():
            command = line.upper()
            if command.startswith((b"EHLO", b"HELO")):
                self.wfile.write(b"250 Synthetic SMTP\r\n")
            elif command.startswith(b"RCPT TO:"):
                recipients.append(line.decode().strip()[8:])
                self.wfile.write(b"250 Recipient accepted\r\n")
            elif command.startswith(b"DATA"):
                self.wfile.write(b"354 Send message\r\n")
                data = []
                while (part := self.rfile.readline()) not in (b".\r\n", b""):
                    data.append(part[1:] if part.startswith(b"..") else part)
                self.server.messages.append((recipients, b"".join(data)))
                recipients = []
                self.wfile.write(b"250 Message accepted\r\n")
            elif command.startswith(b"QUIT"):
                self.wfile.write(b"221 Goodbye\r\n")
                return
            else:
                self.wfile.write(b"250 Accepted\r\n")


@pytest.fixture
def smtp(request):
    store = request.getfixturevalue("pg" if "pg" in request.fixturenames else "h")
    store.settings = replace(store.settings, smtp_private_host_allowlist="127.0.0.1")
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTPHandler) as server:
        server.messages = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def configuration(h, **changes):
    value = {"smtp_enabled": True, "smtp_host": "127.0.0.1", "smtp_starttls": False,
             "sender_address": "invites@example.test", "sender_name": "Synthetic Rota", **changes}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).email_settings = value
    return value


def source(h, **changes):
    with transaction(h.app.state.engine, write=True) as db:
        recipient = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, recipient.person_id).work_email = "synthetic-work@example.test"
        row = m.DepartmentContent(**{"department_id": h.seed["department_id"], "name": "Synthetic announcement",
            "description": "Synthetic agenda", "kind": "announcement", "starts_on": date(2020, 1, 1),
            "audience": {"roles": ["staff"]}, "also_email": True,
            "email_sender_identity_id": h.seed["identities"]["coordinator"], **changes})
        db.add(row)
        db.flush()
        parent = m.Outbox(department_id=row.department_id, event_key=m.uid(), kind="announcement",
            payload={"content_id": row.id, "revision": row.revision,
                     "actor_id": h.seed["identities"]["coordinator"]})
        db.add(parent)
        db.flush()
        return row.id, parent.id


def queued(h, parent_id):
    with transaction(h.app.state.engine, write=True) as db:
        return email.queue(db, db.get(m.Outbox, parent_id))


def dispatch(h):
    with transaction(h.app.state.engine, write=True) as db:
        return email.dispatch(db, h.settings)


def evidence(h):
    with transaction(h.app.state.engine) as db:
        return [(row.state, row.error_code, db.get(m.Outbox, row.outbox_id).state,
                 db.get(m.Outbox, row.outbox_id).attempts) for row in db.scalars(select(m.EmailDelivery))]


def mock_graph(monkeypatch, responder):
    client = httpx.Client
    monkeypatch.setattr(email.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(responder), **kwargs))
    monkeypatch.setattr(email.mailboxes, "access_token", lambda *args: "synthetic-access-token-private")


def no_account_person(h, *, email_address="no-account@example.test", member=True, active=True, ends_on=None):
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Synthetic person without account",
                          work_email=email_address, active=active)
        db.add(person)
        db.flush()
        if member:
            db.add(m.Membership(department_id=h.seed["department_id"], person_id=person.id,
                category_id=h.seed["category"], starts_on=date(2020, 1, 1), ends_on=ends_on))
        return person.id


def test_smtp_fake_server_accepts_one_private_recipient_once_without_read_or_ack(h, smtp, monkeypatch):
    # A provisioned password must not be used for an unauthenticated in-app relay.
    monkeypatch.setenv('FMUC_SMTP_PASSWORD', 'synthetic-unused-password')
    monkeypatch.setattr(email.smtplib.SMTP, 'login', lambda *args: pytest.fail('Unauthenticated relay must never receive a password'))
    configuration(h, smtp_port=smtp.server_address[1])
    _, parent_id = source(h, importance="must_acknowledge")
    assert queued(h, parent_id) == 1
    assert queued(h, parent_id) == 0
    assert dispatch(h) == {h.seed["department_id"]}
    assert dispatch(h) == set()
    assert evidence(h) == [("sent", None, "sent", 1)]
    assert len(smtp.messages) == 1
    recipients, raw = smtp.messages[0]
    assert recipients == ["<synthetic-work@example.test>"]
    message = message_from_bytes(raw, policy=policy.default)
    assert message["Bcc"] is None and message["X-Unsent"] is None
    assert message["Subject"] == "Synthetic announcement"
    assert "Open in Rota to acknowledge" in message.get_body(preferencelist=("plain",)).get_content()
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.AnnouncementReceipt))
        assert receipt.read_at is None and receipt.acknowledged_at is None


def test_disabled_channels_create_no_email_children_or_provider_calls(h, monkeypatch):
    configuration(h, smtp_enabled=False, m365_enabled=False)
    _, parent_id = source(h)
    monkeypatch.setattr(email, "send", lambda *args: pytest.fail("Disabled email must not contact a provider"))
    assert queued(h, parent_id) == 0
    assert dispatch(h) == set()
    assert evidence(h) == []


def test_missing_address_records_failed_recipient_without_provider_call(h, monkeypatch):
    configuration(h)
    _, parent_id = source(h)
    with transaction(h.app.state.engine, write=True) as db:
        recipient = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, recipient.person_id).work_email = ""
        account = db.scalar(select(m.Account).where(m.Account.identity_id == recipient.id))
        if account is not None:
            account.email = ""
    monkeypatch.setattr(email, "send", lambda *args: pytest.fail("No recipient address"))
    assert queued(h, parent_id) == 1
    assert dispatch(h) == set()
    assert evidence(h) == [("failed", "EMAIL_ADDRESS_MISSING", "failed", 0)]


@pytest.mark.parametrize('cloud, graph', [
    ('global', 'https://graph.microsoft.com/v1.0'),
    ('us_gov', 'https://graph.microsoft.us/v1.0'),
    ('china', 'https://microsoftgraph.chinacloudapi.cn/v1.0'),
])
def test_graph_sends_base64_mime_as_connected_organiser_and_keeps_provider_body_private(h, monkeypatch, caplog, cloud, graph):
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft", m365_cloud=cloud)
    _, parent_id = source(h)
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url == graph + "/me/sendMail"
        assert request.headers["authorization"] == "Bearer synthetic-access-token-private"
        assert request.headers["content-type"] == "text/plain"
        message = message_from_bytes(base64.b64decode(request.content), policy=policy.default)
        assert message["Subject"] == "Synthetic announcement"
        assert message["Bcc"] == "synthetic-work@example.test"
        return httpx.Response(202)
    mock_graph(monkeypatch, respond)
    assert queued(h, parent_id) == 1
    dispatch(h)
    assert len(requests) == 1 and evidence(h) == [("sent", None, "sent", 1)]
    assert "synthetic-access-token-private" not in caplog.text


def test_graph_429_respects_provider_delay_then_retries_only_unsent_recipient(h, monkeypatch, caplog):
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft", retry_base_seconds=1, retry_max_seconds=2)
    _, parent_id = source(h)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "7200"}, text="private-provider-error-token") if len(calls) == 1 else httpx.Response(202)
    mock_graph(monkeypatch, respond)
    queued(h, parent_id)
    current = [m.now()]
    before = freeze_clock(monkeypatch, lambda: current[0])
    dispatch(h)
    with transaction(h.app.state.engine) as db:
        child = db.scalar(select(m.Outbox).where(m.Outbox.kind == "email"))
        assert aware(child.next_attempt_at) >= before + timedelta(seconds=7200)
    assert evidence(h) == [("queued", "GRAPH_THROTTLED", "retry", 1)]
    assert dispatch(h) == set() and len(calls) == 1
    current[0] += timedelta(seconds=7200)
    dispatch(h)
    assert evidence(h) == [("sent", None, "sent", 2)]
    assert dispatch(h) == set() and len(calls) == 2
    assert "private-provider-error-token" not in caplog.text


def test_retry_after_accepts_http_date_and_invalid_value(monkeypatch):
    freeze_clock(monkeypatch)
    assert 119 <= email.retry_after(format_datetime(m.now() + timedelta(seconds=120), usegmt=True)) <= 120
    assert email.retry_after("invalid") == email.retry_after(None) == email.retry_after("-1") == 0


def test_graph_throttle_defers_other_mailbox_recipients_and_retry_exhaustion_is_bounded(h, monkeypatch):
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft", max_attempts=2, retry_base_seconds=1, retry_max_seconds=1)
    other = identity(h, "another_email_recipient", ["staff"])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Account(identity_id=other, username="another_email_recipient", email="other@example.test"))
    _, parent_id = source(h)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})
    mock_graph(monkeypatch, respond)
    assert queued(h, parent_id) == 2
    current = [m.now()]
    freeze_clock(monkeypatch, lambda: current[0])
    dispatch(h)
    assert len(calls) == 1
    assert set(evidence(h)) == {("queued", "GRAPH_THROTTLED", "retry", 1), ("queued", None, "queued", 0)}
    current[0] += timedelta(seconds=30)
    retried_at = current[0]
    dispatch(h)
    assert any(item == ("failed", "GRAPH_THROTTLED", "failed", 2) for item in evidence(h))
    assert len(calls) == 2
    with transaction(h.app.state.engine) as db:
        children = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "email")))
        assert all(aware(child.next_attempt_at) == retried_at + timedelta(seconds=30) for child in children)
    assert dispatch(h) == set() and len(calls) == 2
    current[0] += timedelta(seconds=30)
    dispatch(h)
    assert len(calls) == 3
    assert set(evidence(h)) == {("failed", "GRAPH_THROTTLED", "failed", 2),
                               ("queued", "GRAPH_THROTTLED", "retry", 1)}


def test_unconnected_microsoft_never_falls_back_to_smtp(h, monkeypatch, caplog):
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft")
    _, parent_id = source(h)
    def unavailable(*args):
        raise Problem(422, "MAILBOX_UNAVAILABLE", "private-refresh-token-from-provider")
    monkeypatch.setattr(email.mailboxes, "access_token", unavailable)
    monkeypatch.setattr(email, "send_smtp", lambda *args: pytest.fail("Never silently switch mailbox"))
    queued(h, parent_id)
    dispatch(h)
    assert evidence(h) == [("draft", "MAILBOX_UNAVAILABLE", "failed", 1)]
    assert "private-refresh-token-from-provider" not in caplog.text


def test_smtp_password_is_environment_only_and_provider_error_text_is_private(h, monkeypatch, caplog):
    configuration(h, smtp_username="synthetic-sender")
    _, parent_id = source(h)
    monkeypatch.setenv("FMUC_SMTP_PASSWORD", "synthetic-private-smtp-password")
    monkeypatch.setenv("FMUC_SMTP_HOST", "relay.example.test")
    class RejectedSMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, *, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        def login(self, username, password):
            assert username == "synthetic-sender" and password == "synthetic-private-smtp-password"
            raise smtplib.SMTPAuthenticationError(535, b"synthetic-private-smtp-password")
    monkeypatch.setattr(email, "RestrictedSMTP", RejectedSMTP)
    queued(h, parent_id)
    dispatch(h)
    assert evidence(h) == [("draft", "SMTP_AUTH_FAILED", "failed", 1)]
    assert "synthetic-private-smtp-password" not in caplog.text


@pytest.mark.parametrize('tls_failure', [None, 'unsupported', 'certificate'])
def test_authenticated_smtp_ignores_admin_destination_and_requires_verified_tls(h, monkeypatch, tls_failure):
    configuration(h, smtp_username='synthetic-sender')
    _, parent_id = source(h)
    queued(h, parent_id)
    current = h.get('admin', '/api/email/settings')
    h.command('admin', 'email.settings', {'value': {**current['value'], 'smtp_host': 'capture.example.test',
        'smtp_port': 2525, 'smtp_starttls': False}, 'reason': 'Synthetic admin relay change'},
        h.seed['department_id'], current['revision'])
    monkeypatch.setenv('FMUC_SMTP_HOST', 'approved-relay.example.test')
    monkeypatch.setenv('FMUC_SMTP_PORT', '5870')
    monkeypatch.setenv('FMUC_SMTP_PASSWORD', 'synthetic-private-password')
    steps = []
    class Relay:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ('approved-relay.example.test', 5870)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, *, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            if tls_failure == 'unsupported':
                raise smtplib.SMTPNotSupportedError('No STARTTLS')
            if tls_failure == 'certificate':
                raise ssl.SSLCertVerificationError('Untrusted certificate')
            steps.append('tls')
        def login(self, username, password):
            assert steps == ['tls']
            assert (username, password) == ('synthetic-sender', 'synthetic-private-password')
            steps.append('login')
        def send_message(self, *args, **kwargs):
            assert steps == ['tls', 'login']
            steps.append('sent')
    monkeypatch.setattr(email, 'RestrictedSMTP', Relay)
    dispatch(h)
    if tls_failure:
        assert steps == []
        code = 'SMTP_TLS_FAILED' if tls_failure == 'certificate' else 'SMTP_UNCONFIGURED'
        assert evidence(h) == [('draft', code, 'failed', 1)]
    else:
        assert steps == ['tls', 'login', 'sent']
        assert evidence(h) == [('sent', None, 'sent', 1)]


@pytest.mark.parametrize('host, port', [('', '587'), ('approved.example.test', 'bad'),
                                     ('approved.example.test', '0'), ('approved.example.test', '65536')])
def test_authenticated_smtp_never_falls_back_to_admin_host_when_environment_is_missing_or_invalid(h, monkeypatch, host, port):
    configuration(h, smtp_username='synthetic-sender')
    _, parent_id = source(h)
    queued(h, parent_id)
    monkeypatch.setenv('FMUC_SMTP_PASSWORD', 'synthetic-private-password')
    monkeypatch.setenv('FMUC_SMTP_HOST', host)
    monkeypatch.setenv('FMUC_SMTP_PORT', port)
    monkeypatch.setattr(email, 'RestrictedSMTP', lambda *args, **kwargs: pytest.fail('Invalid deployment must not connect'))
    dispatch(h)
    assert evidence(h) == [('draft', 'SMTP_UNCONFIGURED', 'failed', 1)]


@pytest.mark.parametrize("change, code", [("stale", "STALE_EMAIL_SOURCE"), ("access", "EMAIL_ACCESS_ENDED"),
                                         ("disabled", "EMAIL_DISABLED"), ("read", "EMAIL_NO_LONGER_PENDING")])
def test_pending_send_revalidates_source_access_settings_and_reminder(h, monkeypatch, change, code):
    configuration(h)
    content_id, parent_id = source(h)
    with transaction(h.app.state.engine, write=True) as db:
        if change == "read":
            parent = db.get(m.Outbox, parent_id)
            parent.payload = {**parent.payload, "reminder": True}
    queued(h, parent_id)
    with transaction(h.app.state.engine, write=True) as db:
        if change == "stale":
            db.get(m.DepartmentContent, content_id).revision += 1
        elif change == "access":
            db.get(m.Identity, h.seed["identities"]["staff"]).disabled = True
        elif change == "disabled":
            department = db.get(m.Department, h.seed["department_id"])
            department.email_settings = {**department.email_settings, "smtp_enabled": False}
        else:
            db.scalar(select(m.AnnouncementReceipt)).read_at = m.now()
    monkeypatch.setattr(email, "send", lambda *args: pytest.fail("Revalidation must prevent sending"))
    dispatch(h)
    state = "draft" if change == "disabled" else "failed"
    assert evidence(h) == [(state, code, "available_in_app", 0)]


def test_inbox_disabled_event_still_queues_only_changed_occurrence_and_reminders_only_pending(h):
    configuration(h)
    typ = event_type(h, notify_inbox=False, also_email=True)
    row = event(h, typ)
    with transaction(h.app.state.engine, write=True) as db:
        occurrences = list(db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == row["id"])))
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event"))
        parent.payload = {**parent.payload, "occurrence_ids": [occurrences[0].id]}
        assert email.queue(db, parent) == 1
        delivery = db.scalar(select(m.EmailDelivery))
        assert db.get(m.AnnouncementReceipt, delivery.receipt_id).event_occurrence_id == occurrences[0].id
        assert db.scalar(select(func.count()).select_from(m.Notification)) == 0


def test_microsoft_event_mime_keeps_calendar_organiser_without_claiming_mailbox_from(h, monkeypatch):
    h.client("coordinator")
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft")
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, staff.person_id).work_email = "synthetic-work@example.test"
        organiser = db.get(m.Identity, h.seed["identities"]["coordinator"])
        account = db.scalar(select(m.Account).where(m.Account.identity_id == organiser.id))
        if account is None:
            db.add(m.Account(identity_id=organiser.id, username="synthetic_organiser_mailbox", email="organiser@example.test"))
        else:
            account.email = "organiser@example.test"
    typ = event_type(h, notify_inbox=False, also_email=True)
    row = event(h, typ)
    requests = []
    def respond(request):
        requests.append(request)
        message = message_from_bytes(base64.b64decode(request.content), policy=policy.default)
        assert message["From"] is None  # /me sends as the consenting mailbox, whose address can differ.
        calendar = next(part for part in message.walk() if part.get_content_type() == "text/calendar")
        assert "ORGANIZER:mailto:organiser@example.test" in calendar.get_content()
        assert "METHOD:REQUEST" in calendar.get_content()
        return httpx.Response(202)
    mock_graph(monkeypatch, respond)
    with transaction(h.app.state.engine, write=True) as db:
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event",
            m.Outbox.payload["event_id"].as_string() == row["id"]))
        assert email.queue(db, parent) == 1
        email.dispatch(db, h.settings, limit=1)
    assert len(requests) == 1 and evidence(h) == [("sent", None, "sent", 1)]


@pytest.mark.postgres
def test_locked_child_is_skipped_by_another_dispatcher_without_global_write_lock(pg, monkeypatch):
    configuration(pg)
    _, parent_id = source(pg)
    assert queued(pg, parent_id) == 1
    sends = []
    monkeypatch.setattr(email, "send", lambda db, delivery, *args: sends.append(delivery.id))
    with transaction(pg.app.state.engine) as owner:
        child = owner.scalar(select(m.Outbox).where(m.Outbox.kind == "email").with_for_update())
        with transaction(pg.app.state.engine) as other:
            assert email.dispatch(other, pg.settings, limit=1) == set()
        assert child.attempts == 0 and sends == []
    with transaction(pg.app.state.engine) as other:
        assert email.dispatch(other, pg.settings, limit=1) == {pg.seed["department_id"]}
    assert len(sends) == 1 and evidence(pg) == [("sent", None, "sent", 1)]


@pytest.mark.parametrize("audience_kind", ["named", "everyone", "rank", "where"])
def test_smtp_reaches_no_account_department_people_without_creating_app_receipts(h, smtp, audience_kind):
    configuration(h, smtp_port=smtp.server_address[1])
    person_id = no_account_person(h)
    audiences = {"named": {"person_ids": [person_id]}, "everyone": {"everyone": True},
                 "rank": {"category_ids": [h.seed["category"]]},
                 "where": {"where": {"match": "all", "conditions": [{"field": "person_ids", "values": [person_id]}]}}}
    _, parent_id = source(h, audience=audiences[audience_kind])
    queued(h, parent_id)
    assert queued(h, parent_id) == 0
    dispatch(h)
    assert any(recipients == ["<no-account@example.test>"] for recipients, _ in smtp.messages)
    with transaction(h.app.state.engine) as db:
        delivery = db.scalar(select(m.EmailDelivery).where(m.EmailDelivery.person_id == person_id))
        assert delivery.identity_id is None and delivery.receipt_id is None and delivery.state == "sent"
        assert db.scalar(select(m.AnnouncementReceipt.id).where(m.AnnouncementReceipt.person_id == person_id)) is None


def test_no_account_missing_work_email_keeps_failed_recipient_evidence(h):
    configuration(h)
    person_id = no_account_person(h, email_address="")
    _, parent_id = source(h, audience={"person_ids": [person_id]})
    assert queued(h, parent_id) == 1
    assert dispatch(h) == set()
    assert evidence(h) == [("failed", "EMAIL_ADDRESS_MISSING", "failed", 0)]


@pytest.mark.parametrize("edit", ["definition", "occurrence"])
@pytest.mark.parametrize("former_account", ["existing", "none", "new"])
def test_cancellation_after_audience_change_keeps_updated_email_details_private(h, smtp, edit, former_account):
    configuration(h, smtp_port=smtp.server_address[1])
    h.client("coordinator")
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, staff.person_id).work_email = "former@example.test"
        former_person = staff.person_id
        db.add(m.Account(identity_id=h.seed["identities"]["coordinator"], username="current_email_recipient",
                         email="current@example.test"))
    former_person = (former_person if former_account == "existing" else
                     no_account_person(h, email_address="former@example.test"))
    row = event(h, event_type(h, also_email=True), audience={"person_ids": [former_person]})
    occurrence = occurrences(h)[0]
    with transaction(h.app.state.engine, write=True) as db:
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event"))
        assert email.queue(db, parent) == 1
    dispatch(h)
    if former_account == "new":
        identity(h, "new_former_draft_recipient", ["auditor"], person=former_person)
    heading, agenda = "Coordinators-only revised heading", "Coordinators-only revised agenda"
    place, link = "Coordinators-only revised room", "https://example.test/private-meeting"
    changes = {"audience": {"roles": ["coordinator"]}, "heading": heading, "description": agenda,
               "place": place, "url": link, "format": "hybrid", "cancelled": True}
    if edit == "definition":
        h.configure("events", input_of(definition(h, row), **changes), row["id"], row["revision"], role="coordinator")
    else:
        h.command("coordinator", "event.occurrence.update", {"value": input_of(occurrence["data"], **changes),
            "reason": "Restrict updated cancellation details"}, occurrence["id"], occurrence["revision"])
    with transaction(h.app.state.engine, write=True) as db:
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event",
            m.Outbox.payload["revision"].as_integer() == 2))
        assert email.queue(db, parent) == 2
    smtp.messages.clear()
    dispatch(h)
    messages = {recipients[0]: message_from_bytes(raw, policy=policy.default) for recipients, raw in smtp.messages}
    assert set(messages) == {"<former@example.test>", "<current@example.test>"}
    former, current = messages["<former@example.test>"], messages["<current@example.test>"]
    assert former["Subject"] == "Cancelled: Event" and current["Subject"] == heading
    for part in former.walk():
        if part.get_content_maintype() == "text":
            assert heading not in part.get_content() and agenda not in part.get_content()
    for content_type in ("text/plain", "text/html", "text/calendar"):
        body = next(part.get_content() for part in current.walk() if part.get_content_type() == content_type)
        assert heading in body and agenda in body
    cancellation = next(part.get_content() for part in former.walk() if part.get_content_type() == "text/calendar")
    assert "METHOD:CANCEL" in cancellation and f"UID:event-{occurrence['id']}@rota.invalid" in cancellation
    for source_id in (occurrence["id"], row["id"]):
        url = h.get("coordinator", f"/api/events/{source_id}/email-preview")["draft_url"]
        drafts = {}
        for group in (None, "current", "former"):
            response = h.client("coordinator").get(url, params={"group": group} if group else {})
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("message/rfc822")
            drafts[group] = message_from_bytes(response.content, policy=policy.default)
        assert drafts[None]["Subject"] == drafts["current"]["Subject"] == heading
        assert drafts[None]["Bcc"] == drafts["current"]["Bcc"] == "current@example.test"
        assert drafts["former"]["Bcc"] == "former@example.test"
        assert drafts["former"]["Subject"] == "Cancelled: Event"
        for content_type in ("text/plain", "text/html", "text/calendar"):
            body = next(part.get_content() for part in drafts["current"].walk() if part.get_content_type() == content_type)
            assert all(value in body for value in (heading, agenda, place, link))
            body = next(part.get_content() for part in drafts["former"].walk() if part.get_content_type() == content_type)
            assert "An event previously shared with you has been cancelled." in body
            assert all(value not in body for value in (heading, agenda, place, link))
        for draft in drafts.values():
            calendar = next(part for part in draft.walk() if part.get_content_type() == "text/calendar")
            assert calendar.get_param("method") == "CANCEL"
            body = calendar.get_content()
            assert "METHOD:CANCEL" in body and f"UID:event-{occurrence['id']}@rota.invalid" in body
            assert "SEQUENCE:2" in body and "STATUS:CANCELLED" in body


@pytest.mark.parametrize("group", ["current", "former", "shared_address"])
@pytest.mark.parametrize("recipient_header", ["Bcc", "To"])
def test_single_group_cancellation_draft_keeps_existing_details_and_recipients(h, group, recipient_header):
    configuration(h, recipient_header=recipient_header)
    h.client("coordinator")
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, staff.person_id).work_email = "recipient@example.test"
        if group == "shared_address":
            db.add(m.Account(identity_id=h.seed["identities"]["coordinator"], username="shared_draft_address",
                             email="recipient@example.test"))
    row = event(h, event_type(h, also_email=True))
    occurrence = occurrences(h)[0]
    with transaction(h.app.state.engine, write=True) as db:
        assert email.queue(db, db.scalar(select(m.Outbox).where(m.Outbox.kind == "event"))) == 1
    heading, agenda = "Revised cancellation heading", "Revised cancellation agenda"
    changes = {"cancelled": True, "heading": heading, "description": agenda,
               "audience": {"roles": ["staff" if group == "current" else "coordinator"]}}
    h.command("coordinator", "event.occurrence.update", {"value": input_of(occurrence["data"], **changes),
        "reason": "Synthetic single draft group"}, occurrence["id"], occurrence["revision"])
    url = f"/api/events/{occurrence['id']}/draft.eml"
    response = h.client("coordinator").get(url)
    assert response.status_code == 200, response.text
    draft = message_from_bytes(response.content, policy=policy.default)
    assert draft[recipient_header] == "recipient@example.test" and draft["X-Unsent"] == "1"
    assert draft["Subject"] == ("Cancelled: Event" if group == "former" else heading)
    for part in draft.walk():
        if part.get_content_maintype() == "text":
            if group == "former":
                assert heading not in part.get_content() and agenda not in part.get_content()
                assert "Synthetic meeting room" not in part.get_content()
            else:
                assert heading in part.get_content() and agenda in part.get_content()
    calendar = next(part.get_content() for part in draft.walk() if part.get_content_type() == "text/calendar")
    assert "METHOD:CANCEL" in calendar and f"UID:event-{occurrence['id']}@rota.invalid" in calendar
    assert "SEQUENCE:2" in calendar
    selected = "former" if group == "former" else "current"
    assert h.client("coordinator").get(url, params={"group": selected}).status_code == 200
    absent = "current" if selected == "former" else "former"
    response = h.client("coordinator").get(url, params={"group": absent})
    assert response.status_code == 404 and response.json()["error"]["code"] == "EMAIL_DRAFT_GROUP_EMPTY"
    assert h.client("staff").get(url, params={"group": selected}).status_code == 403
    assert h.client("coordinator").get(url, params={"group": "invalid"}).status_code == 422


@pytest.mark.parametrize("same_current", [False, True])
def test_series_cancellation_drafts_respect_current_and_former_membership(h, same_current):
    configuration(h)
    h.client("coordinator")
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, staff.person_id).work_email = "staff@example.test"
        db.add(m.Account(identity_id=h.seed["identities"]["coordinator"], username="series_draft_recipient",
                         email="coordinator@example.test"))
    row = event(h, event_type(h, also_email=True), audience={"roles": ["staff", "coordinator"]},
                repeat=repeat(), repeat_until="2026-10-12")
    series = occurrences(h)
    assert len(series) == 2
    with transaction(h.app.state.engine, write=True) as db:
        assert email.queue(db, db.scalar(select(m.Outbox).where(m.Outbox.kind == "event"))) == 4
    for index, occurrence in enumerate(series):
        role = "coordinator" if index == 0 or same_current else "staff"
        h.command("coordinator", "event.occurrence.update", {"value": input_of(occurrence["data"],
            cancelled=True, audience={"roles": [role]}, heading=f"Restricted occurrence {index}"),
            "reason": "Synthetic series cancellation groups"}, occurrence["id"], occurrence["revision"])
    for group in (None, "current", "former"):
        response = h.client("coordinator").get(f"/api/events/{row['id']}/draft.eml",
                                                params={"group": group} if group else {})
        if not same_current:
            assert response.status_code == 409 and response.json()["error"]["code"] == "EMAIL_AUDIENCES_DIFFER"
            assert set(response.json()["error"]["details"]["draft_urls"]) == {
                f"/api/events/{r['id']}/draft.eml" for r in series}
            assert "Restricted occurrence" not in response.text
            continue
        assert response.status_code == 200, response.text
        draft = message_from_bytes(response.content, policy=policy.default)
        assert draft["Bcc"] == ("staff@example.test" if group == "former" else "coordinator@example.test")
        calendars = list(draft.iter_attachments())
        assert len(calendars) == 2
        for index, calendar in enumerate(calendars):
            body = calendar.get_content()
            assert "METHOD:CANCEL" in body and f"UID:event-{series[index]['id']}@rota.invalid" in body
            assert "SEQUENCE:2" in body
            assert (f"Restricted occurrence {index}" in body) == (group != "former")


@pytest.mark.parametrize("new_account", [False, True])
def test_no_account_event_retains_cancellation_recipient_after_audience_changes(h, smtp, new_account):
    configuration(h, smtp_port=smtp.server_address[1])
    person_id = no_account_person(h)
    row = event(h, event_type(h, notify_inbox=False, also_email=True), audience={"person_ids": [person_id]})
    with transaction(h.app.state.engine, write=True) as db:
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event"))
        assert email.queue(db, parent) == 1
    dispatch(h)
    if new_account:
        identity(h, "newly_linked_email_person", ["auditor"], person=person_id)
    with transaction(h.app.state.engine, write=True) as db:
        master = db.get(m.Event, row["id"])
        master.revision += 1
        occurrence = db.scalar(select(m.EventOccurrence).where(m.EventOccurrence.event_id == master.id))
        occurrence.cancelled = True
        occurrence.data = {**occurrence.data, "audience": {"roles": ["staff"]},
                           "heading": "Private revised heading", "description": "Private revised agenda"}
        parent = m.Outbox(department_id=h.seed["department_id"], kind="event", event_key=m.uid(),
            payload={"event_id": master.id, "revision": master.revision, "occurrence_ids": [occurrence.id],
                     "actor_id": h.seed["identities"]["coordinator"]})
        db.add(parent)
        db.flush()
        email.queue(db, parent)
    dispatch(h)
    messages = [message_from_bytes(raw, policy=policy.default) for recipients, raw in smtp.messages
                if recipients == ["<no-account@example.test>"]]
    assert len(messages) == 2
    calendars = [next(part for part in message.walk() if part.get_content_type() == "text/calendar").get_content()
                 for message in messages]
    assert "METHOD:REQUEST" in calendars[0] and "METHOD:CANCEL" in calendars[1]
    assert "STATUS:CANCELLED" in calendars[1]
    assert messages[1]["Subject"] == "Cancelled: Event"
    for part in messages[1].walk():
        if part.get_content_maintype() == "text":
            assert "Private revised heading" not in part.get_content()
            assert "Private revised agenda" not in part.get_content()
    with transaction(h.app.state.engine) as db:
        deliveries = list(db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.person_id == person_id)))
        assert len(deliveries) == 2 and all(delivery.state == "sent" for delivery in deliveries)


def test_microsoft_event_without_organiser_address_is_a_draft_without_provider_fallback(h, monkeypatch):
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft")
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, staff.person_id).work_email = "synthetic-work@example.test"
    row = event(h, event_type(h, also_email=True))
    monkeypatch.setattr(email.mailboxes, "access_token", lambda *args: pytest.fail("Missing organiser must not contact Microsoft"))
    monkeypatch.setattr(email, "send_smtp", lambda *args: pytest.fail("Never fall back to another provider"))
    with transaction(h.app.state.engine, write=True) as db:
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event",
            m.Outbox.payload["event_id"].as_string() == row["id"]))
        assert email.queue(db, parent) == 1
    dispatch(h)
    assert evidence(h) == [("draft", "EMAIL_ORGANISER_ADDRESS_MISSING", "failed", 1)]


def test_legacy_smtp_event_cannot_borrow_another_organisers_connected_microsoft_mailbox(h, monkeypatch):
    configuration(h)
    person_id = no_account_person(h, email_address="another-organiser@example.test")
    other_id = identity(h, "another_connected_organiser", ["coordinator"], person=person_id)
    row = event(h, event_type(h, also_email=True), organiser_ids=[person_id])
    assert row["email_sender_identity_id"] is None
    configuration(h, m365_enabled=True, m365_tenant_id="synthetic-tenant", m365_client_id="synthetic-client",
                  default_sender="microsoft")
    object.__setattr__(h.settings, 'fmuc_data_key', base64.b64encode(b"0123456789abcdef0123456789abcdef").decode())
    with transaction(h.app.state.engine, write=True) as db:
        staff = db.get(m.Identity, h.seed["identities"]["staff"])
        db.get(m.Person, staff.person_id).work_email = "synthetic-work@example.test"
        _, _, signature = email.mailboxes._configuration(db, h.seed["department_id"], h.settings)
        db.add(m.MailboxConnection(department_id=h.seed["department_id"], identity_id=other_id,
            encrypted_refresh_token=encrypt_secret("synthetic-refresh-only", email.mailboxes._context("mailbox", other_id, h.seed["department_id"]), h.settings),
            configuration_hash=signature))
        db.flush()
        assert email.mailboxes.connected(db, other_id, h.seed["department_id"], h.settings)
        parent = db.scalar(select(m.Outbox).where(m.Outbox.kind == "event",
            m.Outbox.payload["event_id"].as_string() == row["id"]))
        assert email.queue(db, parent) == 1
        delivery = db.scalar(select(m.EmailDelivery))
        assert delivery.sender_mode == "microsoft" and delivery.sender_identity_id is None
    monkeypatch.setattr(email.mailboxes, "access_token", lambda *args: pytest.fail("An unpinned event cannot borrow a mailbox"))
    monkeypatch.setattr(email, "send_smtp", lambda *args: pytest.fail("Never silently use the department sender"))
    dispatch(h)
    assert evidence(h) == [("draft", "EMAIL_ORGANISER_ADDRESS_MISSING", "failed", 1)]


def test_no_account_scope_excludes_unaffiliated_inactive_expired_and_any_disabled_identity(h):
    member = no_account_person(h)
    unaffiliated = no_account_person(h, member=False)
    inactive = no_account_person(h, active=False)
    expired = no_account_person(h, ends_on=date(2026, 10, 1))
    disabled_person = no_account_person(h)
    disabled_identity = identity(h, "disabled_email_identity", ["staff"], person=disabled_person)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, disabled_identity).disabled = True
        ids = {person.id for person in extra_people(db, h.seed["department_id"], {"everyone": True}, date(2026, 10, 3))}
        assert member in ids and not ids & {unaffiliated, inactive, expired, disabled_person}
        assert {person.id for person in extra_people(db, h.seed["department_id"],
            {"person_ids": [unaffiliated, inactive, disabled_person]}, date(2026, 10, 3))} == {unaffiliated}
        assert extra_people(db, h.seed["department_id"], {"roles": ["staff"]}, date(2026, 10, 3)) == []
        assert extra_people(db, h.seed["department_id"], {"where": {"match": "all", "conditions": [
            {"field": "roles", "op": "none", "values": ["staff"]}]}}, date(2026, 10, 3)) == []


@pytest.mark.postgres
def test_sender_lock_serializes_distinct_children_for_same_provider_mailbox(pg, monkeypatch):
    configuration(pg)
    _, parent_id = source(pg)
    queued(pg, parent_id)
    sends = []
    monkeypatch.setattr(email, "send", lambda db, delivery, *args: sends.append(delivery.id))
    with transaction(pg.app.state.engine) as owner:
        delivery = owner.scalar(select(m.EmailDelivery))
        assert email._sender_available(owner, delivery)
        with transaction(pg.app.state.engine) as other:
            assert email.dispatch(other, pg.settings, limit=1) == set()
        assert sends == []
    with transaction(pg.app.state.engine) as other:
        assert email.dispatch(other, pg.settings, limit=1) == {pg.seed["department_id"]}
    assert len(sends) == 1


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('pending_state', ['queued', 'retry'])
@pytest.mark.parametrize('account', [True, False], ids=['accounts', 'accountless'])
def test_editing_one_occurrence_preserves_other_pending_recipients_without_resending_accepted_email(request, backend, pending_state, account, monkeypatch):
    h = request.getfixturevalue(backend)
    configuration(h)
    if account:
        people = h.seed['people'][:2]
        identity(h, 'another_pending_event_recipient', ['staff'], person=people[1])
        with transaction(h.app.state.engine, write=True) as db:
            for index, person_id in enumerate(people):
                db.get(m.Person, person_id).work_email = f'pending-event-{index}@example.test'
    else:
        people = [no_account_person(h, email_address=f'pending-event-{index}@example.test') for index in range(2)]
    row = event(h, event_type(h, notify_inbox=False, also_email=True), audience={'person_ids': people},
        repeat=repeat(), repeat_until='2026-10-12')
    first, second = occurrences(h)
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    sent = []

    def send(db, delivery, message, *args):
        calendar = next(part.get_content() for part in message.walk() if part.get_content_type() == 'text/calendar')
        sent.append((delivery.event_occurrence_id, delivery.person_id, message['Subject'], calendar))

    monkeypatch.setattr(email, 'send', send)
    with transaction(h.app.state.engine, write=True) as db:
        parents = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'event')))
        assert len(parents) == 1 and parents[0].state == 'available_in_app'
        deliveries = list(db.scalars(select(m.EmailDelivery)))
        assert len(deliveries) == 4
        accepted = next(item for item in deliveries if item.event_occurrence_id == first['id'] and item.person_id == people[0])
        accepted_id = accepted.id
        for delivery in deliveries:
            child = db.get(m.Outbox, delivery.outbox_id)
            assert child.payload['occurrence_revision'] == 1
            if delivery.id != accepted_id:
                child.next_attempt_at = m.now() + timedelta(days=1)
    assert dispatch(h) == {h.seed['department_id']}
    assert len(sent) == 1 and sent[0][:2] == (first['id'], people[0])
    with transaction(h.app.state.engine, write=True) as db:
        for delivery in db.scalars(select(m.EmailDelivery)):
            child = db.get(m.Outbox, delivery.outbox_id)
            if delivery.id != accepted_id:
                child.next_attempt_at = m.now()
                child.state = pending_state
                child.attempts = int(pending_state == 'retry')
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(second['data'], heading='Only the second occurrence changed'),
        'reason': 'Edit another occurrence while invitations are pending'}, second['id'], second['revision'])
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    dispatch(h)
    assert Counter((oid, pid, subject) for oid, pid, subject, _ in sent) == Counter({
        (oid, person_id, row['heading'] if oid == first['id'] else 'Only the second occurrence changed'): 1
        for oid in (first['id'], second['id']) for person_id in people})
    assert all(('SEQUENCE:1' if oid == first['id'] else 'SEQUENCE:2') in calendar for oid, _, _, calendar in sent)
    with transaction(h.app.state.engine) as db:
        accepted = db.get(m.EmailDelivery, accepted_id)
        assert accepted.state == 'sent' and db.get(m.Outbox, accepted.outbox_id).attempts == 1
        stale = list(db.scalars(select(m.EmailDelivery).where(m.EmailDelivery.event_occurrence_id == second['id'],
            m.EmailDelivery.error_code == 'STALE_EMAIL_SOURCE')))
        assert len(stale) == 2
        if not account:
            assert not list(db.scalars(select(m.AnnouncementReceipt.id).where(
                m.AnnouncementReceipt.event_occurrence_id.in_([first['id'], second['id']]))))


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('account', [True, False], ids=['account', 'accountless'])
@pytest.mark.parametrize('stale', [False, True], ids=['current-master', 'stale-master'])
def test_legacy_pending_event_child_without_occurrence_pin_keeps_strict_master_revision_checks(request, backend, account, stale, monkeypatch):
    h = request.getfixturevalue(backend)
    configuration(h)
    if account:
        person_id = h.seed['people'][0]
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Person, person_id).work_email = 'legacy-event@example.test'
    else:
        person_id = no_account_person(h, email_address='legacy-event@example.test')
    row = event(h, event_type(h, notify_inbox=False, also_email=True), audience={'person_ids': [person_id]})
    retry_write(h.app.state.engine, dispatch_batch_after_undo)
    with transaction(h.app.state.engine, write=True) as db:
        delivery = db.scalar(select(m.EmailDelivery))
        child = db.get(m.Outbox, delivery.outbox_id)
        child.payload = {'source_outbox_id': delivery.source_outbox_id}
        if stale:
            db.get(m.Event, row['id']).revision += 1
    sent = []
    monkeypatch.setattr(email, 'send', lambda *args: sent.append(True))
    dispatch(h)
    assert len(sent) == int(not stale)
    assert evidence(h) == ([('failed', 'STALE_EMAIL_SOURCE', 'available_in_app', 0)] if stale
        else [('sent', None, 'sent', 1)])
