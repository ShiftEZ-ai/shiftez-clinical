"""One MIME message for downloadable drafts and automatic event/announcement delivery."""
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid, parseaddr
from html import escape
from urllib.parse import urlsplit

from .reports import event_calendar
from .time import now


def build_message(*, heading: str, details: str, acknowledge_url: str, recipients: list[str],
                  recipient_header="Bcc", sender=None, organiser=None, reply_to=None, event=None, events=None, draft=True) -> EmailMessage:
    if recipient_header not in {"Bcc", "To"}:
        raise ValueError("Choose Bcc or To for email recipients")
    link = urlsplit(acknowledge_url)
    if link.scheme not in {"https", "http"} or not link.netloc or any(ord(c) < 32 for c in acknowledge_url):
        raise ValueError("The acknowledgement link must be an absolute HTTP(S) URL")
    message = EmailMessage(policy=SMTP)
    message["Subject"] = heading
    message["Date"] = format_datetime(now())
    message["Message-ID"] = make_msgid(domain=parseaddr(sender or "")[1].rpartition("@")[2] or link.hostname)
    if sender:
        message["From"] = sender
    if reply_to:
        message["Reply-To"] = reply_to
    if recipients:
        message[recipient_header] = [Address(addr_spec=address) for address in dict.fromkeys(recipients)]
    if draft:
        message["X-Unsent"] = "1"
    message.set_content(f"{heading}\n\n{details}\n\nOpen in Rota to acknowledge: {acknowledge_url}\n")
    message.add_alternative("<!doctype html><html><body><h1>" + escape(heading) + "</h1><p>"
        + escape(details).replace("\n", "<br>\n") + '</p><p><a href="' + escape(acknowledge_url, quote=True)
        + '">Open in Rota to acknowledge</a></p></body></html>', subtype="html")
    invitations = events if events is not None else ([event] if event is not None else [])
    if invitations:
        # Hidden recipients must not leak through the calendar attachment's attendee list.
        attendees = recipients if recipient_header == "To" or len(recipients) == 1 else []
        calendar_organiser = organiser or (parseaddr(sender)[1] if sender else None)
        # iTIP permits only one UID per REQUEST; expanded occurrences each keep their feed identity.
        for index, invitation in enumerate(invitations, 1):
            method = "CANCEL" if invitation["cancelled"] else "REQUEST"
            calendar = event_calendar([invitation], method=method, organiser=calendar_organiser, attendees=attendees)
            filename = "event.ics" if len(invitations) == 1 else f"event-{index}.ics"
            message.add_attachment(calendar, subtype="calendar", params={"method": method}, filename=filename)
    return message
