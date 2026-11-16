"""Event calendar visibility and personal link preferences use canonical occurrences and audiences."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.time import aware
from .governance_support import identity
from .test_events import event, event_type, input_of, occurrences


def event_row(h, *, settings=None, **changes):
    """Canonical occurrence fixture; event editing itself is covered by the event API suites."""
    typ = s.EventTypeInput(name="Synthetic event type", starts_on="2020-01-01", **(settings or {}))
    value = s.EventInput.model_validate({"event_type_id": "00000000-0000-0000-0000-000000000001", "heading": "Clinical discussion", "place": "Teaching room",
        "start": "2026-10-04T09:00:00+03:00", "end": "2026-10-04T10:00:00+03:00",
        "audience": {"everyone": True}, **changes})
    with transaction(h.app.state.engine, write=True) as db:
        event_type = m.EventType(department_id=h.seed["department_id"], **typ.model_dump())
        db.add(event_type)
        db.flush()
        value.event_type_id = event_type.id
        event = m.Event(department_id=h.seed["department_id"], **value.model_dump(exclude={"tag_ids"}))
        db.add(event)
        db.flush()
        occurrence = m.EventOccurrence(department_id=event.department_id, event_id=event.id,
            original_start=value.start, start=value.start, end=value.end,
            data={**value.model_dump(mode="json"), "type": typ.model_dump(mode="json")})
        db.add(occurrence)
        db.flush()
        return {"event_id": event.id, "id": occurrence.id, "event_type_id": event_type.id}


def respond(h, occurrence, response, role="staff"):
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, h.seed["identities"][role])
        row = db.scalar(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.event_occurrence_id == occurrence["id"],
            m.AnnouncementReceipt.identity_id == identity.id))
        if row is None:
            row = m.AnnouncementReceipt(event_occurrence_id=occurrence["id"], identity_id=identity.id, person_id=identity.person_id)
            db.add(row)
        row.response, row.responded_at = response, m.now()


@pytest.mark.parametrize('role', ['admin', 'coordinator', 'approver', 'programme_lead', 'auditor'])
def test_planner_calendar_feed_stays_personal(h, role):
    identity(h, 'planner_attendee', [role], h.seed['people'][0])
    own = event_row(h, audience={'roles': [role]})
    outside = event_row(h, audience={'roles': ['staff']}, settings={'blocks_duties': True})
    schedule = h.get('planner_attendee', '/api/schedule/days?start=2026-10-01&end=2026-11-01')
    assert {row['id']: row['in_audience'] for row in schedule['events']} == {own['id']: True, outside['id']: False}
    path = h.command('planner_attendee', 'calendar_feed.create')['path']
    feed = h.client('planner_attendee').get(path)
    assert feed.status_code == 200
    assert f"UID:event-{own['id']}@rota.invalid" in feed.text
    assert f"UID:event-{outside['id']}@rota.invalid" not in feed.text


def test_feed_event_modes_audience_join_link_and_cancelled_stable_uid(h):
    everyone = event_row(h, format="hybrid", location_id=h.seed["locations"][0], url="https://example.org/join?room=1",
        description="Agenda; questions\nDiscussion")
    going = event_row(h, settings={"calendar": "going_only", "rsvp": True, "attendance": "optional"})
    event_row(h, settings={"calendar": "none"})
    event_row(h, audience={"roles": ["coordinator"]})
    path = h.command("staff", "calendar_feed.create")["path"]
    client = h.client("staff")
    initial = client.get(path).text
    assert initial.count("BEGIN:VEVENT") == 1
    assert "TRANSP:OPAQUE" in initial
    assert f"UID:event-{everyone['id']}@rota.invalid" in initial
    assert "URL:https://example.org/join?room=1" in initial
    assert r"Agenda\; questions\nDiscussion" in initial
    with transaction(h.app.state.engine) as db:
        place = db.get(m.Location, h.seed["locations"][0]).name
    assert f"LOCATION:{place}\\, Teaching room" in initial
    respond(h, going, "going")
    joined = client.get(path).text
    assert joined.count("BEGIN:VEVENT") == 2
    optional = next(item for item in joined.split("BEGIN:VEVENT") if f"UID:event-{going['id']}@rota.invalid" in item)
    assert "TRANSP:TRANSPARENT" in optional
    respond(h, going, "not_going")
    assert client.get(path).text.count("BEGIN:VEVENT") == 1
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.EventOccurrence, everyone["id"])
        row.cancelled, row.revision = True, row.revision + 1
    cancelled = client.get(path).text
    assert f"UID:event-{everyone['id']}@rota.invalid" in cancelled
    assert "STATUS:CANCELLED" in cancelled and "SEQUENCE:2" in cancelled


def test_event_feed_conditional_refresh_is_stable_until_an_occurrence_changes(h, monkeypatch):
    occurrence = event_row(h)
    path = h.command("staff", "calendar_feed.create")["path"]
    client = h.client("staff")
    first = client.get(path)
    assert first.status_code == 200
    with transaction(h.app.state.engine) as db:
        created_at = aware(db.get(m.EventOccurrence, occurrence["id"]).created_at)
    assert "DTSTAMP:" + created_at.strftime("%Y%m%dT%H%M%SZ") in first.text
    later = created_at + timedelta(hours=1)
    monkeypatch.setattr("apps.api.services.calendar_feeds.utc_now", lambda: later)
    monkeypatch.setattr("packages.domain.reports.clock_now", lambda: later)
    unchanged = client.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""
    assert client.get(path).content == first.content
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.EventOccurrence, occurrence["id"])
        row.cancelled, row.revision = True, row.revision + 1
    changed = client.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200 and changed.headers["etag"] != first.headers["etag"]
    assert f"UID:event-{occurrence['id']}@rota.invalid" in changed.text
    assert "STATUS:CANCELLED" in changed.text and "SEQUENCE:2" in changed.text
    assert client.get(path, headers={"If-None-Match": changed.headers["etag"]}).status_code == 304


def test_calendar_settings_are_revisioned_scoped_and_survive_link_rotation(h):
    row = event_row(h)
    h.command("staff", "calendar_feed.settings", {"hidden_event_type_ids": [row["event_type_id"]]}, revision=1, status=404)
    first = h.command("staff", "calendar_feed.create")
    h.command("staff", "calendar_feed.settings", {"hidden_event_type_ids": [row["event_type_id"]]}, status=428)
    saved = h.command("staff", "calendar_feed.settings", {"hidden_event_type_ids": [row["event_type_id"]]}, revision=1)
    assert saved["hidden_event_type_ids"] == [row["event_type_id"]] and saved["revision"] == 2
    h.command("staff", "calendar_feed.settings", {"hidden_event_type_ids": []}, revision=1, status=409)
    assert h.client("staff").get(first["path"]).text.count("BEGIN:VEVENT") == 0
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Another department")
        db.add(other)
        db.flush()
        foreign_type = m.EventType(department_id=other.id, name="Foreign synthetic type", starts_on=date(2020, 1, 1))
        db.add(foreign_type)
        db.flush()
        foreign_id = foreign_type.id
        foreign_feed = m.CalendarFeed(department_id=other.id, identity_id=h.seed["identities"]["staff"],
            person_id=db.get(m.Identity, h.seed["identities"]["staff"]).person_id, token_hash="other-department-link")
        db.add(foreign_feed)
        db.flush()
        foreign_feed_id = foreign_feed.id
    h.command("staff", "calendar_feed.settings", {"hidden_event_type_ids": [foreign_id]}, revision=2, status=404)
    rotated = h.command("staff", "calendar_feed.create")
    assert rotated["hidden_event_type_ids"] == [row["event_type_id"]]
    shown = h.get("staff", "/api/calendar-feed")
    assert shown["hidden_event_type_ids"] == rotated["hidden_event_type_ids"] and shown["revision"] == rotated["revision"]
    assert h.client("staff").get(first["path"]).status_code == 404
    assert h.client("staff").get(rotated["path"]).text.count("BEGIN:VEVENT") == 0
    h.command("staff", "calendar_feed.revoke")
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.CalendarFeed, foreign_feed_id).revoked


def test_feed_normalizes_offsets_and_keeps_uid_through_occurrence_edit_and_cancel(h):
    row = event(h, event_type(h, notify_inbox=False),
        start="2026-10-04T23:30:00-04:00", end="2026-10-05T00:30:00-04:00")
    occurrence = next(item for item in occurrences(h) if item["event_id"] == row["id"])
    uid = f"UID:event-{occurrence['id']}@rota.invalid"
    path = h.command("staff", "calendar_feed.create")["path"]
    client = h.client("staff")
    initial = client.get(path).text
    assert uid in initial and "SEQUENCE:1" in initial
    assert "DTSTART:20261005T033000Z" in initial and "DTEND:20261005T043000Z" in initial

    moved = h.command("coordinator", "event.occurrence.update", {"value": input_of(occurrence["data"],
        start="2026-10-06T09:00:00+05:30", end="2026-10-06T10:00:00+05:30"),
        "reason": "Move one event across timezones"}, occurrence["id"], occurrence["revision"])["occurrence"]
    changed = client.get(path).text
    assert changed.count("BEGIN:VEVENT") == 1 and uid in changed
    assert f"SEQUENCE:{moved['revision']}" in changed and moved["revision"] == 2
    assert "DTSTART:20261006T033000Z" in changed and "DTEND:20261006T043000Z" in changed
    assert "DTSTART:20261005T033000Z" not in changed

    cancelled = h.command("coordinator", "event.occurrence.update", {
        "value": input_of(moved["data"], cancelled=True), "reason": "Cancel the moved event"},
        moved["id"], moved["revision"])["occurrence"]
    final = client.get(path).text
    assert final.count("BEGIN:VEVENT") == 1 and uid in final and "STATUS:CANCELLED" in final
    assert f"SEQUENCE:{cancelled['revision']}" in final and cancelled["revision"] == 3


def test_feed_modes_capacity_and_hidden_types_follow_real_rsvp_commands(h):
    other = next(pid for pid in h.seed["people"] if pid != h.get("staff", "/api/me")["person_id"])
    identity(h, "other_attendee", ["staff"], other)
    everyone_type = event_type(h, rsvp=True, notify_inbox=False)
    everyone = event(h, everyone_type, heading="Whole audience")
    going = event(h, event_type(h, calendar="going_only", attendance="optional", rsvp=True,
        notify_inbox=False), heading="Reserved places", max_attendees=1)
    never = event(h, event_type(h, calendar="none", rsvp=True, notify_inbox=False), heading="No calendar")
    found = {item["event_id"]: item for item in occurrences(h)}

    def answer(row, role, response, status=200):
        occurrence = found[row["id"]]
        return h.command(role, "event.rsvp", {"response": response, "reason": "Synthetic calendar RSVP"},
            occurrence["id"], occurrence["revision"], status=status)

    path = h.command("staff", "calendar_feed.create")["path"]
    other_path = h.command("other_attendee", "calendar_feed.create")["path"]
    answer(everyone, "staff", "not_going")
    answer(never, "staff", "going")
    answer(going, "staff", "going")
    rejected = answer(going, "other_attendee", "going", status=409)
    assert rejected["error"]["code"] == "EVENT_FULL"
    staff_feed = h.client("staff").get(path).text
    assert "SUMMARY:Whole audience" in staff_feed and "SUMMARY:Reserved places" in staff_feed
    assert "SUMMARY:No calendar" not in staff_feed
    assert "SUMMARY:Reserved places" not in h.client("other_attendee").get(other_path).text

    h.command("staff", "calendar_feed.settings", {"hidden_event_type_ids": [everyone_type["id"]]}, revision=1)
    hidden = h.client("staff").get(path).text
    assert "SUMMARY:Whole audience" not in hidden and "SUMMARY:Reserved places" in hidden
    answer(going, "staff", "not_going")
    answer(going, "other_attendee", "going")
    assert "SUMMARY:Reserved places" not in h.client("staff").get(path).text
    assert "SUMMARY:Reserved places" in h.client("other_attendee").get(other_path).text


def test_feed_rechecks_tag_audience_without_losing_existing_going_receipt(h):
    me = h.get("staff", "/api/me")["person_id"]
    tag = h.configure("tags", {"name": "Calendar attendees", "labels": ["people"]})["record"]
    person = {"kind": "people", "id": me}
    tag = h.command("coordinator", "tag.members", {"add": [person], "reason": "Join event audience"},
        tag["id"], tag["revision"])["record"]
    row = event(h, event_type(h, calendar="going_only", rsvp=True, notify_inbox=False), audience={"where": {
        "match": "all", "conditions": [{"field": "person_ids", "op": "any", "values": [tag["id"]]}]}})
    occurrence = next(item for item in occurrences(h) if item["event_id"] == row["id"])
    path = h.command("staff", "calendar_feed.create")["path"]
    client = h.client("staff")
    assert "BEGIN:VEVENT" not in client.get(path).text
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AnnouncementReceipt.id).where(
            m.AnnouncementReceipt.event_occurrence_id == occurrence["id"])) is None
    receipt = h.command("staff", "event.rsvp", {"response": "going", "reason": "Reserve place"},
        occurrence["id"], occurrence["revision"])["receipt"]
    uid = f"UID:event-{occurrence['id']}@rota.invalid"
    assert uid in client.get(path).text
    tag = h.command("coordinator", "tag.members", {"remove": [person], "reason": "Leave event audience"},
        tag["id"], tag["revision"])["record"]
    assert uid not in client.get(path).text
    with transaction(h.app.state.engine) as db:
        assert db.get(m.AnnouncementReceipt, receipt["id"]).response == "going"
    h.command("coordinator", "tag.members", {"add": [person], "reason": "Restore event audience"},
        tag["id"], tag["revision"])
    assert uid in client.get(path).text


def test_feed_event_window_uses_local_days_half_open_overlap_and_configured_horizon(h, monkeypatch):
    from apps.api.services import calendar_feeds
    monkeypatch.setattr(calendar_feeds, "utc_now", lambda: aware("2026-10-03T23:30:00+00:00"))
    me = h.get("admin", "/api/me")
    org, dept = me["organisation"], me["department"]
    settings = {"department_name": dept["name"], "department_revision": dept["revision"],
        "organisation_name": org["name"], "timezone": org["timezone"], "week_start": org["week_start"],
        "event_horizon_days": 7, "reason": "Configure calendar event horizon"}
    saved = h.command("admin", "department.settings", settings, dept["id"], org["revision"])
    today = aware("2026-10-04T00:00:00+03:00")
    lower, upper = today - timedelta(days=60), today + timedelta(days=7)
    typ = event_type(h, notify_inbox=False)
    for heading, start, end in (
        ("Ends at lower bound", lower - timedelta(hours=1), lower),
        ("Overlaps lower bound", lower - timedelta(hours=1), lower + timedelta(seconds=1)),
        ("Starts at lower bound", lower, lower + timedelta(hours=1)),
        ("Overlaps upper bound", upper - timedelta(seconds=1), upper + timedelta(hours=1)),
        ("Starts at upper bound", upper, upper + timedelta(hours=1)),
    ):
        event(h, typ, heading=heading, start=start.isoformat(), end=end.isoformat())
    path = h.command("staff", "calendar_feed.create")["path"]
    client = h.client("staff")
    feed = client.get(path).text
    assert {line for line in feed.splitlines() if line.startswith("SUMMARY:")} == {
        "SUMMARY:Overlaps lower bound", "SUMMARY:Starts at lower bound", "SUMMARY:Overlaps upper bound"}
    h.command("admin", "department.settings", {**settings,
        "department_revision": saved["department"]["revision"], "event_horizon_days": 8},
        dept["id"], saved["organisation"]["revision"])
    extended = client.get(path).text
    assert "SUMMARY:Starts at upper bound" in extended and "SUMMARY:Ends at lower bound" not in extended
