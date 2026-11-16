"""Drafts and senders share safe MIME; invitations preserve the subscribed event identity."""
from email import policy
from email.parser import BytesParser

import pytest

from packages.domain.email import build_message
from packages.domain.reports import calendar_feed, event_calendar


def occurrence(**changes):
    return {"id": "canonical-occurrence", "revision": 3, "created_at": "2026-10-01T09:00:00+03:00", "heading": "Clinical discussion",
        "description": "Agenda; questions\nDiscussion", "place": "Teaching room", "attendance": "optional",
        "cancelled": False, "online_url": "https://example.test/join", "start": "2026-10-04T09:00:00+03:00",
        "end": "2026-10-04T10:00:00+03:00", **changes}


def draft(**changes):
    return build_message(**{"heading": "Discussion <&>", "details": '<script>alert("x")</script>\nAgenda',
        "acknowledge_url": "https://rota.example.test/?content=1&view=receipts", "recipients": ["one@example.test", "two@example.test"],
        "sender": "Organiser <organiser@example.test>", **changes})


def test_draft_has_bcc_unsent_rfc5322_and_escaped_text_html_link():
    wire = draft().as_bytes()
    assert b"\r\n" in wire and b"\n" not in wire.replace(b"\r\n", b"")
    message = BytesParser(policy=policy.default).parsebytes(wire)
    assert not message.defects and message["X-Unsent"] == "1" and message["Subject"] == "Discussion <&>"
    assert message["To"] is None and str(message["Bcc"]) == "one@example.test, two@example.test"
    assert message["Date"] and message["Message-ID"] and message["From"].addresses[0].addr_spec == "organiser@example.test"
    assert '<script>alert("x")</script>' in message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "<script>" not in html and "&lt;script&gt;" in html and "Discussion &lt;&amp;&gt;" in html
    assert 'href="https://rota.example.test/?content=1&amp;view=receipts"' in html
    assert "Open in Rota to acknowledge" in html


@pytest.mark.parametrize("sender, domain", [("Organiser <organiser@example.test>", "example.test"),
    (None, "rota.example.test")])
def test_message_id_uses_configured_domain_without_dns(monkeypatch, sender, domain):
    def no_dns(*args, **kwargs):
        pytest.fail("Message construction must not resolve the machine hostname")
    monkeypatch.setattr("socket.getfqdn", no_dns)
    first, second = draft(sender=sender), draft(sender=sender)
    assert str(first["Message-ID"]).endswith("@" + domain + ">")
    assert first["Message-ID"] != second["Message-ID"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_event_invitation_matches_feed_uid_revision_times_and_cancellation(cancelled):
    event = occurrence(cancelled=cancelled, rrule="FREQ=WEEKLY;COUNT=4", exdates=["2026-10-11T09:00:00+03:00"])
    message = draft(event=event)
    attachment, = message.iter_attachments()
    method = "CANCEL" if cancelled else "REQUEST"
    assert attachment.get_content_type() == "text/calendar" and attachment.get_param("method") == method
    invitation = attachment.get_content().replace("\r\n ", "")
    feed = calendar_feed([], "person", "Schedule", events=[event]).replace("\r\n ", "")
    assert "DTSTAMP:20261001T060000Z" in feed
    for prop in ["UID:event-canonical-occurrence@rota.invalid", "SEQUENCE:3", "DTSTART:20261004T060000Z",
                 "DTEND:20261004T070000Z", "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE:20261011T060000Z",
                 "STATUS:" + ("CANCELLED" if cancelled else "CONFIRMED")]:
        assert prop in invitation and prop in feed
    assert f"METHOD:{method}" in invitation and "ORGANIZER:mailto:organiser@example.test" in invitation
    assert "one@example.test" not in invitation and "two@example.test" not in invitation


def test_expanded_recurrence_uses_each_feed_uid_and_single_recipient_attendee():
    events = [occurrence(), occurrence(id="next-occurrence", start="2026-10-11T09:00:00+03:00", end="2026-10-11T10:00:00+03:00")]
    message = draft(events=events, recipients=["one@example.test"], draft=False)
    assert message["X-Unsent"] is None
    invitations = [part.get_content() for part in message.iter_attachments()]
    assert len(invitations) == 2
    for invitation, event in zip(invitations, events):
        assert invitation.count("BEGIN:VEVENT") == 1 and f"UID:event-{event['id']}@rota.invalid" in invitation
        assert "ATTENDEE;RSVP=FALSE:mailto:one@example.test" in invitation and "RRULE:" not in invitation
    with pytest.raises(ValueError, match="only one UID"):
        event_calendar(events)


def test_explicit_to_and_reply_to_are_preserved_without_an_invitation():
    message = draft(recipient_header="To", reply_to="reply@example.test")
    assert message["Bcc"] is None and message["To"].addresses[1].addr_spec == "two@example.test"
    assert str(message["Reply-To"]) == "reply@example.test" and list(message.iter_attachments()) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_calendar_organiser_is_independent_of_provider_assigned_from(cancelled):
    message = draft(sender=None, organiser="mailbox@example.test", event=occurrence(cancelled=cancelled),
                    recipients=["one@example.test"], draft=False)
    assert message["From"] is None
    calendar = next(message.iter_attachments()).get_content()
    assert "ORGANIZER:mailto:mailbox@example.test" in calendar
    assert "ATTENDEE;RSVP=FALSE:mailto:one@example.test" in calendar


@pytest.mark.parametrize("changes", [{"recipient_header": "Cc"}, {"heading": "Title\r\nBcc: hidden@example.test"},
    {"recipients": ["one@example.test\r\nCc: hidden@example.test"]}, {"acknowledge_url": "javascript:alert(1)"},
    {"acknowledge_url": "https://rota.example.test/\nunsafe"}])
def test_draft_rejects_header_injection_and_unsafe_acknowledgement_links(changes):
    with pytest.raises(ValueError):
        draft(**changes)


def test_recurrence_cannot_inject_calendar_properties():
    with pytest.raises(ValueError):
        event_calendar([occurrence(rrule="FREQ=DAILY\r\nATTENDEE:mailto:other@example.test")])
