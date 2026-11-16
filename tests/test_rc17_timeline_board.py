"""Rotation board data remains scoped when read through the shared timeline."""
from datetime import date
import json
import time

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import timeline, training
from packages.domain.time import aware
from packages.domain.validation import rotation_leave_findings
from tests.governance_support import identity
from tests.test_timeline_data import START, END, actor_for, timeline_records
from tests.test_timeline_layers import stored_publication


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def board_h(request):
    return request.getfixturevalue(request.param)


def read(h, role="coordinator", **params):
    response = h.client(role).get("/api/timeline", params={
        "start": START.isoformat(), "end": END.isoformat(), **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_trainees_filter_uses_requested_window_before_paging_and_preserves_membership_details(board_h):
    h = board_h
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        dep = h.seed["department_id"]
        member = db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == ids["person"]))
        member.external, member.home_programme = True, "Synthetic home programme"
        db.add_all([
            m.ProgrammeMembership(department_id=dep, person_id=h.seed["people"][1], programme_id=ids["programme"],
                starts_on=END, ends_on=date(2026, 12, 1)),
            m.ProgrammeMembership(department_id=dep, person_id=h.seed["people"][2], programme_id=ids["programme"],
                starts_on=date(2026, 9, 1), ends_on=START),
        ])
        db.get(m.Identity, h.seed["identities"]["staff"]).person_id = ids["person"]
    result = read(h, trainees_only=True, limit=1)
    assert result["total"] == 1 and not result["has_more"]
    assert [person["id"] for person in result["people"]] == [ids["person"]]
    membership = next(item for item in result["people"][0]["items"] if item["kind"] == "programme")
    assert membership["details"] == {"programme_id": ids["programme"], "external": True,
                                     "home_programme": "Synthetic home programme"}
    assert read(h, trainees_only=True, offset=1)["people"] == []
    assert read(h, trainees_only=True, q="missing synthetic person")["total"] == 0
    assert read(h, trainees_only=True, person_ids=h.seed["people"][1])["total"] == 0
    assert read(h)["total"] == 3
    assert [p["id"] for p in read(h, "staff", trainees_only=True)["people"]] == [ids["person"]]
    with transaction(h.app.state.engine) as db:
        direct = timeline.list_data(db, actor_for(h, db), START, END, trainees_only=True)
    assert direct["total"] == 1 and direct["people"][0]["id"] == ids["person"]


def test_programme_only_external_trainee_appears_without_rank_or_department_identity(board_h):
    h = board_h
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Synthetic external trainee")
        db.add(person)
        db.flush()
        person_id = person.id
    h.configure("programme_memberships", {"person_id": person_id, "programme_id": ids["programme"],
        "starts_on": START.isoformat(), "ends_on": END.isoformat(),
        "external": True, "home_programme": "Synthetic home programme"})
    for trainees_only in (False, True):
        result = read(h, trainees_only=trainees_only, person_ids=person_id)
        assert result["total"] == 1 and result["people"][0]["id"] == person_id
        membership = next(item for item in result["people"][0]["items"] if item["kind"] == "programme")
        assert membership["details"]["external"]
    board = h.get("coordinator", "/api/rotation-board?start=2026-10-01&end=2026-11-01")
    assert person_id in {person["id"] for person in board["people"]}


def test_timeline_form_choices_match_board_and_keep_historical_names(board_h):
    h = board_h
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        dep = h.seed["department_id"]
        db.get(m.RotationType, ids["type"]).active = False
        active_type = m.RotationType(department_id=dep, name="Unused active rotation", starts_on=START)
        active_service = m.Service(department_id=dep, name="Unused active service", starts_on=START)
        inactive_type = m.RotationType(department_id=dep, name="Inactive rotation", starts_on=START, active=False)
        inactive_service = m.Service(department_id=dep, name="Inactive service", starts_on=START, active=False)
        foreign = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic foreign department")
        db.add_all([active_type, active_service, inactive_type, inactive_service, foreign]); db.flush()
        foreign_type = m.RotationType(department_id=foreign.id, name="Foreign active rotation", starts_on=START)
        foreign_service = m.Service(department_id=foreign.id, name="Foreign active service", starts_on=START)
        db.add_all([foreign_type, foreign_service]); db.flush()
        choices = {"rotation_types": (active_type.id, inactive_type.id, foreign_type.id),
                   "services": (active_service.id, inactive_service.id, foreign_service.id)}
    result = read(h, trainees_only=True)
    board = h.get("coordinator", "/api/rotation-board?start=2026-10-01&end=2026-11-01")
    assert result["choices"] == board["choices"]
    for kind, (active, inactive, foreign) in choices.items():
        assert active in result["choices"][kind]
        assert {inactive, foreign}.isdisjoint(result["choices"][kind])
    assert ids["type"] in result["names"]["rotation_types"]
    assert ids["type"] not in result["choices"]["rotation_types"]


def test_timeline_notes_match_board_authority_without_disclosing_notes_or_self_counts(board_h):
    h = board_h
    ids = timeline_records(h)
    own, other, myself = h.seed["people"]
    lead_id = identity(h, "timeline_note_lead", ["programme_lead"], person=myself)
    identity(h, "timeline_note_reader", ["programme_lead", "auditor"], person=myself)
    with transaction(h.app.state.engine, write=True) as db:
        dep = h.seed["department_id"]
        db.get(m.Programme, ids["programme"]).owner_identity_id = lead_id
        # The second lead reads the department, but note access remains limited to current led trainees.
        db.add_all([
            m.ProgrammeMembership(department_id=dep, person_id=myself, programme_id=ids["programme"], starts_on=START, ends_on=END),
            m.ProgrammeMembership(department_id=dep, person_id=other, programme_id=ids["programme"], starts_on=END,
                                  ends_on=date(2026, 12, 1)),
        ])
        for pid, retracted in ((own, False), (own, True), (other, False), (myself, False)):
            db.add(m.TraineeNote(department_id=dep, person_id=pid, body="SENSITIVE SYNTHETIC NOTE",
                                 author_id=lead_id, retracted=retracted))
    for role in ("timeline_note_lead", "timeline_note_reader"):
        result = read(h, role)
        board = h.get(role, "/api/rotation-board?start=2026-10-01&end=2026-11-01")
        assert result["can_note"] and result["note_person_ids"] == board["note_person_ids"] == [own]
        counts = {person["id"]: person["notes"] for person in result["people"]}
        assert counts[own] == 1 and all(count == 0 for pid, count in counts.items() if pid != own)
        assert "SENSITIVE" not in json.dumps(result)
        h.get(role, f"/api/people/{myself}/trainee-notes", status=403)
    admin = read(h, "admin", trainees_only=True)
    admin_board = h.get("admin", "/api/rotation-board?start=2026-10-01&end=2026-11-01")
    assert admin["can_note"] and admin["note_person_ids"] == admin_board["note_person_ids"]
    assert {p["id"]: p["notes"] for p in admin["people"]} == {p["id"]: p["notes"] for p in admin_board["people"]}
    for role in ("coordinator", "staff", "auditor"):
        username = "timeline_without_notes_" + role
        identity(h, username, [role], person=own)
        result = read(h, username)
        assert not result["can_note"] and result["note_person_ids"] == []
        assert all(person["notes"] == 0 for person in result["people"])


@pytest.mark.parametrize("policy", ["not_allowed", "needs_approval", "allowed"])
def test_timeline_stored_rotation_leave_conflicts_match_shared_domain_evaluator(board_h, policy, monkeypatch):
    h = board_h
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RotationType, ids["type"]).leave_policy = policy
    with transaction(h.app.state.engine) as db:
        actor = actor_for(h, db)
        data = timeline._check_data(db, actor, START, END)
        pending = [record(row) for row in db.scalars(select(m.RequestRecord).where(
            m.RequestRecord.person_id == ids["person"], m.RequestRecord.kind == "leave"))]
    expected = rotation_leave_findings({**data, "rotation_leave_availability": data["rotation_leave_availability"] + pending})
    stored_publication(h, findings=expected)
    def forbidden(*args, **kwargs):
        pytest.fail("Timeline reads must use stored rotation/leave findings without a live check")
    monkeypatch.setattr(timeline, "_check_data", forbidden)
    monkeypatch.setattr(timeline, "evaluate", forbidden)
    result = read(h, trainees_only=True)["people"][0]
    actual = [finding for finding in result["conflicts"] if finding["code"].startswith("ROTATION_LEAVE_")]

    def signature(finding):
        return (finding["code"], finding["severity"], finding["person_id"], aware(finding["start"]), aware(finding["end"]),
                json.dumps(finding["detail"], sort_keys=True))

    assert sorted(map(signature, actual)) == sorted(map(signature, expected))
    assert len(actual) == (0 if policy == "allowed" else 3)


@pytest.mark.cpu_budget
def test_timeline_note_summary_has_constant_queries_and_ci_aware_budget(h):
    import coverage
    ids = timeline_records(h)
    lead_id = identity(h, "timeline_scale_lead", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        dep = h.seed["department_id"]
        db.get(m.Programme, ids["programme"]).owner_identity_id = lead_id
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic trainee {n}") for n in range(150)]
        db.add_all(people); db.flush()
        person_ids = [person.id for person in people]
        db.add_all(m.ProgrammeMembership(department_id=dep, person_id=pid, programme_id=ids["programme"],
            starts_on=START, ends_on=END) for pid in person_ids)
        db.add_all(m.TraineeNote(department_id=dep, person_id=pid, body="Synthetic scale note", author_id=lead_id)
                   for pid in person_ids)
    statements = []

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(h.app.state.engine, "before_cursor_execute", count_sql)
    try:
        with transaction(h.app.state.engine) as db:
            actor = actor_for(h, db, "timeline_scale_lead")
            counts = []
            for selected in (person_ids[:1], person_ids):
                statements.clear()
                started = time.process_time()
                notes, allowed = training.note_summary(db, actor, selected)
                elapsed = time.process_time() - started
                counts.append(len(statements))
                assert notes == dict.fromkeys(selected, 1) and allowed == set(selected)
            assert counts[1] <= counts[0] and counts[1] <= 6, counts
            assert elapsed < 2 * (3 if coverage.Coverage.current() else 1), elapsed
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", count_sql)
