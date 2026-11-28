"""Timeline projection, shared findings and read boundaries, using synthetic records only."""
from datetime import date, datetime
import json

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import Actor, current_roles
from apps.api.db import transaction
from apps.api.services.timeline import LAYER_KEYS, list_data
from packages.contracts import schemas as s


START, END = date(2026, 10, 1), date(2026, 11, 1)


def actor_for(h, db, role="coordinator"):
    identity = db.get(m.Identity, h.seed["identities"][role])
    return Actor(identity.id, h.seed["department_id"], h.seed["organisation_id"], identity.display_name,
                 current_roles(db, identity.id, h.seed["department_id"]), identity.person_id, "timeline-test")


def timeline_records(h):
    with transaction(h.app.state.engine, write=True) as db:
        dep, pid = h.seed["department_id"], h.seed["people"][0]
        author = h.seed["identities"]["coordinator"]
        programme = m.Programme(department_id=dep, name="Synthetic training", starts_on=date(2026, 1, 1))
        typ = m.RotationType(department_id=dep, name="Synthetic away rotation", starts_on=date(2026, 1, 1), leave_policy="not_allowed")
        db.add_all([programme, typ]); db.flush()
        cohort = m.Cohort(department_id=dep, programme_id=programme.id, name="Synthetic cohort", starts_on=START, ends_on=END)
        member = m.ProgrammeMembership(department_id=dep, person_id=pid, programme_id=programme.id, starts_on=START, ends_on=END)
        mentor = m.Mentorship(department_id=dep, person_id=pid, mentor_id=h.seed["people"][1], programme_id=programme.id,
                             starts_on=START, ends_on=END)
        academic = m.ProgrammeCalendarEntry(department_id=dep, programme_id=programme.id, name="Synthetic academic block",
                                           kind="academic_block", starts_on=START, ends_on=END)
        db.add_all([cohort, member, mentor, academic]); db.flush()
        rotation = m.Rotation(department_id=dep, person_id=pid, cohort_id=cohort.id, rotation_type_id=typ.id,
                              starts_on=START, ends_on=END, department_available=False)
        db.add(rotation); db.flush()
        db.add(m.RotationAttachment(rotation_id=rotation.id, mentorship_id=mentor.id, synced_revision=1))
        a, b = datetime.fromisoformat("2026-10-04T22:00:00+00:00"), datetime.fromisoformat("2026-10-05T02:00:00+00:00")
        leave = m.Availability(department_id=dep, person_id=pid, start=a, end=b, kind="recorded_leave",
            leave_type="Synthetic private leave type", private_reason="SENSITIVE SYNTHETIC REASON", recorded_by=author,
            counts_as_unavailable=False)
        away = m.Availability(department_id=dep, person_id=pid, start=a, end=b, kind="external_commitment",
            private_reason="SENSITIVE TIME AWAY", recorded_by=author)
        db.add_all([leave, away]); db.flush()
        for kind in ("protected_time", "overtime_offer"):
            line = {"id": m.uid(), "who": {"conditions": [{"field": "person_ids", "values": [pid]}]},
                    "weekdays": [0], "start_time": "08:00", "end_time": "12:00",
                    "numbers": {"apply": {"value": None, "level": "mandatory" if kind == "protected_time" else "medium"}},
                    "starts_on": START.isoformat(), "ends_on": END.isoformat()}
            db.add(m.SchedulingFact(department_id=dep, person_id=pid, calendar_entry_id=academic.id if kind == "protected_time" else None,
                kind=kind, status="active", on=True, starts_on=START, ends_on=END, proposed_by=author,
                approved_by=h.seed["identities"]["approver"], reason="Synthetic approved fact",
                data={"catalogue_id": "protected_time" if kind == "protected_time" else "overtime_offers", "catalogue_version": 1,
                      "line": line}))
        db.add(m.RequestRecord(department_id=dep, person_id=pid, kind="leave", start=a, end=b,
            private_reason="SENSITIVE PENDING REASON", created_by=author))
        db.add(m.Eligibility(department_id=dep, person_id=pid, service_id=h.seed["services"][0], starts_on=START, ends_on=END))
        capability = db.scalar(select(m.Capability).where(m.Capability.department_id == dep))
        db.add(m.CapabilityGrant(department_id=dep, person_id=pid, capability_id=capability.id, starts_on=START, ends_on=END,
                                granted_by=author, evidence="SENSITIVE SKILL EVIDENCE"))
        db.get(m.Department, dep).leave_reason_display = "none"
        ids = {"person": pid, "programme": programme.id, "type": typ.id, "cohort": cohort.id,
               "rotation": rotation.id, "leave": leave.id, "away": away.id}
    return ids


def read(h, role="coordinator", **kwargs):
    with transaction(h.app.state.engine) as db:
        return list_data(db, actor_for(h, db, role), START, END, **kwargs)


def rota_eligibility_fixture(h):
    first, second = h.seed["people"][:2]
    category = h.configure("categories", {"name": "Only this rota's rank", "starts_on": "2020-01-01"})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Membership(department_id=h.seed["department_id"], person_id=first, category_id=category["id"],
                            starts_on=date(2020, 1, 1)))
    typ = h.configure("assignment_types", {"name": "Rank-scoped rota duty", "starts_on": "2020-01-01",
        "definition": {"allowed_category_ids": [category["id"]]}})["record"]
    workspace = h.workspace("Rota eligibility truth table")
    h.shift(workspace, typ=typ["id"], people=[first])
    return workspace, first, second


def test_timeline_rota_eligibility_truth_table(h):
    workspace, first, second = rota_eligibility_fixture(h)
    result = h.get("coordinator", "/api/timeline?start=2026-10-01&end=2026-11-01"
                   f"&person_ids={first}&person_ids={second}&eligible_workspace_id={workspace['id']}")
    people = {person["id"]: person for person in result["people"]}
    assert result["rota_eligibility"] == {"workspace_id": workspace["id"], "sessions": 1, "basis": "qualified"}
    assert people[first]["rota_eligible"] is True
    assert people[second]["rota_eligible"] is False


def test_timeline_rota_eligibility_is_null_for_an_empty_rota(h):
    workspace = h.workspace("Empty rota eligibility")
    person_id = h.seed["people"][0]
    result = h.get("coordinator", "/api/timeline?start=2026-10-01&end=2026-11-01"
                   f"&person_ids={person_id}&eligible_workspace_id={workspace['id']}")
    assert result["rota_eligibility"] == {"workspace_id": workspace["id"], "sessions": 0, "basis": "qualified"}
    assert result["people"][0]["rota_eligible"] is None


def test_timeline_all_layers_share_the_contract_and_leave_rule(h):
    ids = timeline_records(h)
    result = read(h, person_ids=[ids["person"]], layers=LAYER_KEYS)
    s.TimelineResponse.model_validate(result)
    person = result["people"][0]
    by_kind = {item["kind"]: item for item in person["items"]}
    assert {"rotation", "attachment", "leave", "time_away", "academic", "protected_time", "overtime_offer",
            "eligibility", "grant", "programme", "rank", "pending"} <= by_kind.keys()
    assert by_kind["rotation"]["details"]["mentor_id"] == h.seed["people"][1]
    assert by_kind["leave"]["starts_on"] == "2026-10-05"
    assert by_kind["leave"]["label"] == "On leave or away"
    assert by_kind["academic"]["source"] == "programme_calendar_entries"
    assert by_kind["overtime_offer"]["details"]["line"]["weekdays"] == [0]
    assert "SENSITIVE" not in json.dumps(result) and "Synthetic private leave type" not in json.dumps(result)
    assert person["conflicts"] == []  # Reads never run a new rule check.
    layer_counts = {layer["kind"]: layer["count"] for layer in result["layers"]}
    assert layer_counts["conflict"] == len(person["conflicts"]) and layer_counts["rotation"] == 1
    assert all(layer["source"] and layer["style_key"] for layer in result["layers"])


def test_timeline_filter_group_sort_and_page_before_loading_person_layers(h):
    ids = timeline_records(h)
    filt = s.CollectionFilter.model_validate({"conditions": {"conditions": [
        {"field": "person_ids", "values": [ids["person"]]}]}}, context={"table_key": "people"})
    result = read(h, collection_filter=filt, group_by="programme", sort_by="code", limit=1)
    assert result["total"] == 1 and not result["has_more"]
    assert result["people"][0]["id"] == ids["person"]
    assert result["people"][0]["group"] == "Synthetic training"
    assert read(h, q="no matching synthetic person")["people"] == []
    first, second = read(h, limit=1), read(h, offset=1, limit=1)
    assert first["has_more"] and first["total"] == second["total"]
    assert first["people"][0]["id"] != second["people"][0]["id"]


def test_timeline_staff_self_only_and_operator_forbidden(h):
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["staff"]).person_id = ids["person"]
    result = read(h, "staff")
    assert [p["id"] for p in result["people"]] == [ids["person"]]
    assert not result["can_plan"]
    assert next(i for i in result["people"][0]["items"] if i["kind"] == "leave")["label"] == "Synthetic private leave type"
    from apps.api.errors import Problem
    with pytest.raises(Problem, match="own timeline"):
        read(h, "staff", person_ids=[h.seed["people"][1]])
    with pytest.raises(Problem):
        read(h, "operator")


def test_timeline_programme_lead_is_scoped_before_counts_and_paging(h):
    ids = timeline_records(h)
    from tests.governance_support import identity
    identity(h, "timeline_lead", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, ids["programme"]).owner_identity_id = h.seed["identities"]["timeline_lead"]
        for attachment in db.scalars(select(m.Mentorship).where(m.Mentorship.person_id == ids["person"])):
            attachment.programme_id = None  # Rotation-linked attachments use no separate programme scope.
        other = m.Programme(department_id=h.seed["department_id"], name="Private other programme", starts_on=START)
        db.add(other); db.flush()
        db.add(m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=h.seed["people"][1], programme_id=other.id,
                                     starts_on=START, ends_on=END))
    result = read(h, "timeline_lead", limit=1)
    assert result["total"] == 1 and [p["id"] for p in result["people"]] == [ids["person"]]
    assert any(row["kind"] == "attachment" for row in result["people"][0]["items"])
    assert "Private other programme" not in json.dumps(result)
    assert read(h, "timeline_lead", person_ids=[h.seed["people"][1]])["total"] == 0


def test_timeline_never_discloses_private_notes_even_when_planning_findings_allow_details(h):
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).leave_reason_display = "details"
    result = read(h, person_ids=[ids["person"]])
    assert "SENSITIVE" not in json.dumps(result)
    assert "Synthetic private leave type" in json.dumps(result)
    assert all("note" not in item["details"] for item in result["people"][0]["items"] if item["source"] == "availability")


def test_timeline_endpoint_family_uses_published_local_start_day_without_live_checks(h):
    workspace = h.shift(h.workspace(), start="2026-10-04T22:00:00+03:00", end="2026-10-05T02:00:00+03:00")["workspace"]
    h.publish(workspace)
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Availability, ids["away"]).retracted = True
    query = "?start=2026-10-01&end=2026-11-01"
    board = h.get("coordinator", "/api/timeline" + query + "&person_ids=" + ids["person"])
    person = h.get("coordinator", f"/api/people/{ids['person']}/timeline" + query)
    assert board["people"] == person["people"]
    assert person["people"][0]["duty_density"] == [{"day": "2026-10-04", "count": 1}]
    assert person["people"][0]["conflicts"] == []  # Inputs changed after the stored publication check.
    h.get("staff", f"/api/people/{h.seed['people'][1]}/timeline" + query, status=403)


def test_timeline_includes_event_protection_and_clips_calendar_protection_to_membership(h):
    ids = timeline_records(h)
    from tests.test_event_calendar import event_row
    meeting = event_row(h, settings={"blocks_duties": True})
    with transaction(h.app.state.engine, write=True) as db:
        member = db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == ids["person"],
                                                               m.ProgrammeMembership.programme_id == ids["programme"]))
        member.starts_on = date(2026, 10, 15)
        fact = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.kind == "protected_time"))
        fact.person_id = None
        fact.data = {**fact.data, "programme_id": ids["programme"]}
    result = read(h, person_ids=[ids["person"]])
    protected = [row for row in result["people"][0]["items"] if row["kind"] == "protected_time"]
    assert any(row["source"] == "events" and row["details"]["event_occurrence_id"] == meeting["id"] for row in protected)
    assert [row["starts_on"] for row in protected if row["source"] == "scheduling_facts"] == ["2026-10-15"]
    assert all(row["starts_on"] >= "2026-10-15" for row in result["people"][0]["items"] if row["kind"] == "academic")


def test_timeline_lead_reads_future_members_facts_by_the_requested_window(h):
    ids = timeline_records(h)
    from tests.governance_support import identity
    identity(h, "future_timeline_lead", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, ids["programme"]).owner_identity_id = h.seed["identities"]["future_timeline_lead"]
        for cls in (m.ProgrammeMembership, m.Rotation, m.Mentorship):
            for row in db.scalars(select(cls).where(cls.department_id == h.seed["department_id"])):
                row.starts_on, row.ends_on = date(2027, 10, 1), date(2027, 11, 1)
        for row in db.scalars(select(m.SchedulingFact)):
            row.starts_on, row.ends_on = date(2027, 10, 1), date(2027, 11, 1)
        entry = db.scalar(select(m.ProgrammeCalendarEntry))
        entry.starts_on, entry.ends_on = date(2027, 10, 1), date(2027, 11, 1)
    with transaction(h.app.state.engine) as db:
        result = list_data(db, actor_for(h, db, "future_timeline_lead"), date(2027, 10, 1), date(2027, 11, 1))
    assert [p["id"] for p in result["people"]] == [ids["person"]]
    assert {"overtime_offer", "protected_time"} <= {row["kind"] for row in result["people"][0]["items"]}


def test_timeline_foreign_absence_is_not_disclosed_or_checked_on_read(h):
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=h.seed["organisation_id"], name="Foreign timeline department")
        db.add(foreign); db.flush()
        absence = db.get(m.Availability, ids["leave"])
        absence.department_id = foreign.id
    result = read(h, person_ids=[ids["person"]])
    assert ids["leave"] not in json.dumps(result)
    assert result["people"][0]["conflicts"] == []


def test_timeline_programme_lead_clips_personal_facts_to_their_membership_authority(h):
    ids = timeline_records(h)
    from tests.governance_support import identity
    identity(h, "bounded_timeline_lead", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, ids["programme"]).owner_identity_id = h.seed["identities"]["bounded_timeline_lead"]
        for fact in db.scalars(select(m.SchedulingFact)):
            fact.calendar_entry_id = None
            fact.starts_on, fact.ends_on = date(2026, 9, 1), None
    with transaction(h.app.state.engine) as db:
        result = list_data(db, actor_for(h, db, "bounded_timeline_lead"), date(2026, 9, 1), date(2026, 12, 1))
    facts = [item for item in result["people"][0]["items"] if item["source"] == "scheduling_facts"]
    assert {item["kind"] for item in facts} == {"protected_time", "overtime_offer"}
    assert all((item["starts_on"], item["ends_on"]) == ("2026-10-01", "2026-11-01") for item in facts)
    assert all((item["details"]["line"]["starts_on"], item["details"]["line"]["ends_on"]) ==
               ("2026-10-01", "2026-11-01") for item in facts)


def test_timeline_filters_do_not_recheck_stale_department_balance_findings(h, monkeypatch):
    _check_publication_timeline_findings(h, monkeypatch, "original")


@pytest.mark.parametrize(
    "h,publication_kind",
    [
        ("sqlite", "amendment"),
        ("sqlite", "replacement"),
        ("sqlite", "absence"),
        pytest.param("postgresql", "original", marks=pytest.mark.postgres),
        pytest.param("postgresql", "amendment", marks=pytest.mark.postgres),
        pytest.param("postgresql", "replacement", marks=pytest.mark.postgres),
        pytest.param("postgresql", "absence", marks=pytest.mark.postgres),
    ],
    indirect=["h"],
)
def test_timeline_finding_currency_across_publication_variants(h, monkeypatch, publication_kind):
    _check_publication_timeline_findings(h, monkeypatch, publication_kind)


def _check_publication_timeline_findings(h, monkeypatch, publication_kind):
    from apps.api.services import timeline

    a, b, c = h.seed["people"]
    goal = h.policy({"kind": "balance_goal", "metric": "duties", "aim": "equal_share", "period": "rota",
                     "tolerance_percent": 0, "tolerance_units": 0}, severity="advisory")
    workspace = h.workspace()
    for day, person in ((4, a), (5, a), (6, a), (7, a), (8, b)):
        workspace = h.shift(workspace, start=f"2026-10-{day:02d}T08:00:00+03:00",
                            end=f"2026-10-{day:02d}T12:00:00+03:00", people=[person])["workspace"]
    published = h.publish(workspace)
    if publication_kind == "amendment":
        amendment = h.workspace(base=published["publication_id"])
        amendment = h.shift(amendment, start="2026-10-09T08:00:00+03:00",
                            end="2026-10-09T12:00:00+03:00", people=[a])["workspace"]
        published = h.publish(amendment)
    elif publication_kind == "replacement":
        replacement = h.command("coordinator", "workspace.create", {
            "name": "Replace part of the issued rota", "starts_on": "2026-10-06", "ends_on": "2026-10-11",
            "replaces_publication_id": published["publication_id"]})["workspace"]
        for day, person in ((6, a), (7, a), (8, a), (9, a), (10, b)):
            replacement = h.shift(replacement, start=f"2026-10-{day:02d}T08:00:00+03:00",
                                  end=f"2026-10-{day:02d}T12:00:00+03:00", people=[person])["workspace"]
        published = h.publish(replacement)
    elif publication_kind == "absence":
        publication_id = published["publication_id"]
        snapshot = h.get("coordinator", f"/api/publications/{publication_id}")["snapshot"]
        duty = next(shift for shift in snapshot["shifts"] if shift["participants"][0]["person_id"] == a)
        change = h.command("coordinator", "publication.change", {
            "kind": "replace", "shift_id": duty["id"], "assignment_id": duty["participants"][0]["id"],
            "person_id": c, "reason": "Arrange cover", "absence_reason": "Sick"}, target=publication_id)
        submitted = h.command("coordinator", "publication.changes.submit", {"reason": "Review cover"},
                              target=publication_id, revision=change["workspace"]["revision"])
        approved = h.lifecycle(submitted["workspace"], "approve", "approver")
        published = h.lifecycle(approved["workspace"], "publish")
        with transaction(h.app.state.engine) as db:
            absence = db.scalar(select(m.Availability).where(m.Availability.person_id == a,
                                                             m.Availability.kind == "actual_absence"))
            assert absence is not None

    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, published["publication_id"])
        organisation = db.get(m.Organisation, h.seed["organisation_id"])
        hint = publication.validation["input_manifest"]["dirty_hint"]
        assert hint["input_revision"] == organisation.input_revision
        assert hint["availability_revision"] == organisation.availability_revision
        if publication_kind == "amendment":
            assert any(f["person_id"] == c and f["rule_id"] == goal["id"]
                       for f in publication.validation["findings"])
    monkeypatch.setattr(timeline, "evaluate", lambda *a, **kw: pytest.fail("timeline ran validation"))
    current = read(h, person_ids=[a])["people"][0]["conflicts"]
    assert any(finding["rule_id"] == goal["id"] and finding["code"] == "BALANCE_GOAL" for finding in current)
    if publication_kind == "amendment":
        inherited = read(h, person_ids=[c])["people"][0]["conflicts"]
        assert not any(f["rule_id"] == goal["id"] for f in inherited)

    h.command("approver", "policy.retire", {"reason": "Retire the old balance goal"}, goal["id"], goal["revision"])
    full = next(person for person in read(h)["people"] if person["id"] == a)["conflicts"]
    filtered = read(h, person_ids=[a])["people"][0]["conflicts"]
    assert full == filtered == []


def test_timeline_staff_fact_projection_hides_source_evidence_and_peer_amounts(h):
    ids = timeline_records(h)
    own, other = ids["person"], h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["staff"]).person_id = own
        fact = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.kind == "overtime_offer"))
        line = {**fact.data["line"], "source_bindings": {"apply": {"policy_id": "PRIVATE SOURCE EVIDENCE"}},
                "source_default": {"note": "PRIVATE SOURCE DEFAULT"},
                "settings": {"balance_goal": {"person_amounts": {own: 7, other: 19},
                    "per_person": [{"person_id": own, "value": 7}, {"person_id": other, "value": 19}]}}}
        fact.status = "proposed"  # Projection must protect unreviewed definitions too.
        fact.data = {**fact.data, "line": line}
    result = read(h, "staff")
    item = next(item for item in result["people"][0]["items"] if item["kind"] == "pending" and
                item["source"] == "scheduling_facts")
    line = item["details"]["line"]
    assert "source_bindings" not in line and "source_default" not in line
    assert line["settings"]["balance_goal"]["person_amounts"] == {own: 7}
    assert line["settings"]["balance_goal"]["per_person"] == [{"person_id": own, "value": 7}]


def test_timeline_keeps_revoked_grant_periods_without_their_private_evidence(h):
    ids = timeline_records(h)
    with transaction(h.app.state.engine, write=True) as db:
        grant = db.scalar(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == ids["person"]))
        grant.revoked = True
        grant_id = grant.id
    result = read(h, person_ids=[ids["person"]])
    item = next(item for item in result["people"][0]["items"] if item["id"] == grant_id)
    assert item["kind"] == "grant" and item["status"] == "revoked"
    assert "evidence" not in item["details"] and "SENSITIVE" not in json.dumps(result)


def test_timeline_rejects_invalid_range_and_paging(h):
    from apps.api.errors import Problem
    with transaction(h.app.state.engine) as db:
        actor = actor_for(h, db)
        for first, last, kwargs in ((START, START, {}), (START, date(2030, 1, 1), {}), (START, END, {"limit": 201}),
                                    (START, END, {"group_by": "unknown"})):
            with pytest.raises(Problem):
                list_data(db, actor, first, last, **kwargs)
