"""A staff duty read requires a linked person."""
from datetime import datetime, timezone
import json

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import person_card, schedule, schedule_view
from apps.api.services.conditional_reads import revision_etag
from packages.domain.supervision_teams import own_and_paired

from .clock_support import freeze_clock
from .governance_support import identity
from .test_home_team_payload import supervision_type


def test_pairing_with_no_person_returns_no_participants():
    participants = [{"person_id": "staff", "supervisor_person_id": None},
                    {"person_id": "trainee", "supervisor_person_id": "staff"}]
    assert own_and_paired(participants, None) == []
    assert own_and_paired(participants, "staff") == participants


def test_staff_duty_reads_respect_the_linked_person(h, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    staff, supervisor, other = h.seed["people"]
    identity(h, "unlinked_staff", ["staff"])
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    participants = [
        {"person_id": staff, "start": start, "end": end, "participation": "trainee", "supervisor_person_id": supervisor},
        {"person_id": supervisor, "start": start, "end": end, "participation": "supervisor"},
        {"person_id": other, "start": start, "end": end},
    ]
    first = h.shift(h.workspace(), start, end, people=participants, typ=supervision_type(h))
    second = h.shift(first["workspace"], "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[other])
    publication_id = h.publish(second["workspace"])["publication_id"]
    pager = "5550141"
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, supervisor)
        person.pager = pager
        supervisor_name = person.display_name

    client = h.client("unlinked_staff")
    linked = h.client("staff")
    window = {"start": "2026-10-04", "end": "2026-10-07"}
    sessions = client.get("/api/schedule/sessions", params=window)
    assert sessions.status_code == 200
    assert sessions.json()["shifts"] == [] and sessions.json()["people"] == {}
    assert supervisor_name not in sessions.text and pager not in sessions.text
    assert client.get("/api/schedule/sessions", params=window,
                      headers={"If-None-Match": sessions.headers["etag"]}).status_code == 304
    filtered = {**window, "filter": json.dumps({"conditions": {"conditions": [
        {"field": "person_ids", "values": [other]},
    ]}})}
    for params in (window, filtered):
        days = client.get("/api/schedule/days", params=params)
        assert days.status_code == 200
        assert days.json()["sources"] == [] and days.json()["days"] == {}
        assert supervisor_name not in days.text and pager not in days.text
        assert client.get("/api/schedule/days", params=params,
                          headers={"If-None-Match": days.headers["etag"]}).status_code == 304

    for path, params, status in (("/api/calendar", window, 403), (f"/api/publications/{publication_id}", {}, 404)):
        visible = linked.get(path, params=params)
        assert visible.status_code == 200
        refused = client.get(path, params=params, headers={"If-None-Match": visible.headers["etag"]})
        assert refused.status_code == status
        assert supervisor_name not in refused.text and pager not in refused.text

    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies["rota_session"], settings=h.settings)
        linked_actor = authenticate(db, linked.cookies["rota_session"], settings=h.settings)
        person = db.get(m.Person, staff)
        with monkeypatch.context() as previous:
            previous.setattr(person_card, "revision_etag", lambda _kind, *parts: revision_etag("person-card", *parts))
            previous_unlinked_tag = person_card.card_etag(db, actor, person)
            previous_linked_tag = person_card.card_etag(db, linked_actor, person)
        publication = db.get(m.Publication, publication_id)
        zone = schedule.zone_of(db, actor.department_id)
        shifts = publication.snapshot["shifts"]
        assert shifts
        assert schedule_view.publication_session_flags(db, actor, publication, shifts, zone) == []
        assert schedule_view.session_overlap_flags(db, actor, shifts, zone, team=False) == []

    card_path = f"/api/people/{staff}/card"
    card = client.get(card_path, headers={"If-None-Match": previous_unlinked_tag})
    assert card.status_code == 200 and card.headers["etag"] != previous_unlinked_tag
    assert card.json()["today"] == [] and card.json()["coming_up"] == []
    assert supervisor_name not in card.text and pager not in card.text
    assert client.get(card_path, headers={"If-None-Match": card.headers["etag"]}).status_code == 304

    session, = linked.get("/api/schedule/sessions", params=window).json()["shifts"]
    assert session["id"] == first["shift_id"]
    assert {row["person_id"] for row in session["participants"]} == {staff, supervisor}
    linked_card = linked.get(card_path)
    assert linked_card.status_code == 200 and linked_card.headers["etag"] == previous_linked_tag
    assert linked.get(card_path, headers={"If-None-Match": previous_linked_tag}).status_code == 304
    upcoming, = linked_card.json()["coming_up"]
    assert upcoming["id"] == first["shift_id"]
    assert {row["person_id"] for row in upcoming["team"]} == {staff, supervisor}
