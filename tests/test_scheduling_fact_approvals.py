"""Pending scheduling facts share Requests and Home without widening decision authority."""
import uuid
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.time import now as clock_now
from tests.governance_support import identity
from tests.test_requests_search import read
from tests.test_requests_unified import leave
from tests.test_scheduling_facts_api import decide, replace, workload

NOW = clock_now()


def calendar_entry(h, programme=None, name="Synthetic teaching block"):
    programme = programme or h.configure("programmes", {
        "name": "Synthetic teaching programme", "starts_on": "2026-01-01"})["record"]
    return h.configure("programme_calendar_entries", {
        "programme_id": programme["id"], "name": name, "kind": "academic_block",
        "starts_on": "2026-10-04", "ends_on": "2026-10-18"})["record"]


def propose(h, kind="workload", person=None, entry=None, target_value=7):
    if kind == "workload":
        payload = workload(h, person_id=person or h.seed["people"][0])
    elif kind == "target":
        goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10},
                        severity="advisory")
        payload = {"kind": kind, "person_id": person or h.seed["people"][0], "catalogue_id": "fair_share",
                   "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
                   "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": target_value, "level": "medium"}}},
                   "reason": "Synthetic personal target"}
    else:
        payload = {"kind": kind, "catalogue_id": "overtime_offers" if kind == "overtime_offer" else "protected_time",
                   "line": {"id": str(uuid.uuid4()), "weekdays": [1], "start_time": "09:00", "end_time": "11:00",
                            "numbers": {"apply": {"value": None, "level": "medium" if kind == "overtime_offer" else "mandatory"}}},
                   "reason": "Synthetic scheduling proposal"}
        if kind == "overtime_offer":
            payload["person_id"] = person or h.seed["people"][0]
        else:
            entry = entry or calendar_entry(h)
            payload["calendar_entry_id"] = entry["id"]
            payload["line"].update(starts_on=entry["starts_on"], ends_on=entry["ends_on"])
    family = "person.overtime" if kind == "overtime_offer" else "calendar.protection" if kind == "protected_time" else "person.workload"
    return h.command("coordinator", family + ".propose", payload)["fact"]


def attention(h, role="approver"):
    return {row["key"]: row for row in h.get(role, "/api/home")["attention"]}


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_pending_fact_role_visibility_detail_and_activation(h, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role], person=h.seed["people"][1] if role == "programme_lead" else None)
    if role == "programme_lead":
        programme = h.configure("programmes", {"name": "Shared synthetic programme", "starts_on": "2026-01-01"})["record"]
        with transaction(h.app.state.engine, write=True) as db:
            db.add_all([m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=pid,
                programme_id=programme["id"], starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
                for pid in h.seed["people"][:2]])
    fact = propose(h)
    may_read = role in {"admin", "coordinator", "approver", "programme_lead", "auditor"}
    may_decide = role in {"admin", "approver"}
    if role in {"credentialer", "operator"}:
        h.get(role, "/api/requests/all", status=403)
        h.get(role, "/api/requests/all?id=" + fact["id"], status=403)
    else:
        listed = read(h, role)
        assert listed["counts"]["to_decide"] == int(may_decide)
        assert [row["id"] for row in listed["items"]] == ([fact["id"]] if may_read else [])
        if may_read:
            item = listed["items"][0]
            assert item["can_decide"] is may_decide
            detail = read(h, role, id=fact["id"])
            assert detail["item"] == item and detail["fact"] == fact
            assert detail["history"][0]["actor"] == item["proposer_name"]
            assert any(event["action"] == "person.workload.propose" for event in detail["history"])
        else:
            h.get(role, "/api/requests/all?id=" + fact["id"], status=404)
    home = attention(h, role)
    assert ("scheduling_facts" in home) is may_decide
    if may_decide:
        assert home["scheduling_facts"]["count"] == 1
        assert home["scheduling_facts"]["sample"] == listed["items"]
    decide(h, fact, role=role, status=200 if may_decide else 403)
    if role == "auditor":
        decide(h, fact, action="decline", role=role, status=403)


@pytest.mark.parametrize("kind", [
    "workload", "target", "overtime_offer", "protected_time",
])
def test_each_fact_kind_has_plain_metadata_and_opens_for_a_pure_approver(h, kind):
    fact = propose(h, kind)
    listed = read(h)
    item = next(row for row in listed["items"] if row["id"] == fact["id"])
    assert s.SchedulingFactApproval.model_validate(item).model_dump(mode="json") == item
    assert (item["source"], item["type"], item["stage"], item["status"], item["kind"]) == (
        "scheduling_fact", "other", "waiting", "proposed", kind)
    assert item["revision"] == fact["revision"]
    assert item["proposed_by"] == h.seed["identities"]["coordinator"]
    assert item["proposer_name"] and datetime.fromisoformat(item["proposed_at"]) == datetime.fromisoformat(fact["created_at"])
    assert item["link_target"] == "#requests?open=" + fact["id"] and item["can_decide"]
    assert "summary" not in item
    if kind == "workload":
        assert item["title"] == "Personal workload" and item["measure"] == "duties"
        assert item["period"]["kind"] == "calendar_week"
    elif kind == "target":
        assert item["title"] == "Personal target" and item["goal_measure"] == "hours"
        assert item["goal_period"]["kind"] == "rolling"
    elif kind == "overtime_offer":
        assert item["title"] == "Overtime offer" and item["weekdays"] == [1]
        assert (item["start_time"], item["end_time"]) == ("09:00", "11:00")
    else:
        assert item["title"] == "Calendar protection" and item["calendar_entry_name"] == "Synthetic teaching block"
    assert "data" not in item and "line" not in item
    if kind == "protected_time":
        assert item["person_id"] is None and item["person_name"] is None
        assert item["calendar_entry_id"] == fact["calendar_entry_id"]
        assert item["calendar_entry_name"] == "Synthetic teaching block"
    else:
        assert item["person_id"] == h.seed["people"][0] and item["person_name"]
        assert item["calendar_entry_id"] is None and item["calendar_entry_name"] is None
    detail = read(h, id=fact["id"])
    assert detail["fact"]["data"] == fact["data"]
    assert attention(h)["scheduling_facts"]["sample"] == [item]
    assert decide(h, detail["fact"])["fact"]["status"] == "active"


def test_null_personal_target_is_a_readable_pending_removal(h):
    fact = propose(h, "target", target_value=None)
    item = next(row for row in read(h)["items"] if row["id"] == fact["id"])
    assert item["title"] == "Personal target" and item["numbers"]["target"]["value"] is None
    assert attention(h)["scheduling_facts"]["sample"] == [item]
    assert read(h, id=fact["id"])["fact"]["data"]["line"]["numbers"]["target"]["value"] is None
    assert decide(h, fact)["fact"]["status"] == "active"


def test_approval_item_exposes_structured_dates_and_weekdays(h):
    dated = propose(h, "protected_time")
    undated = propose(h, "workload")
    items = {row["id"]: row for row in read(h)["items"]}

    assert items[dated["id"]]["starts_on"] == "2026-10-04"
    assert items[dated["id"]]["ends_on"] == "2026-10-18"
    assert items[dated["id"]]["weekdays"] == [1]
    assert items[undated["id"]]["starts_on"] is None
    assert items[undated["id"]]["ends_on"] is None
    assert items[undated["id"]]["weekdays"] == list(range(7))


def test_approval_item_exposes_structured_rule_line_parts(h):
    fact = propose(h, "overtime_offer")
    item = next(row for row in read(h)["items"] if row["id"] == fact["id"])

    assert item["title"] == "Overtime offer"
    assert item["catalogue_id"] == "overtime_offers"
    assert item["start_time"] == "09:00" and item["end_time"] == "11:00"
    assert item["numbers"] == {"apply": {"value": None, "level": "medium"}}


def test_approval_item_uses_rule_line_defaults_for_legacy_weekdays(h):
    facts = [propose(h, kind) for kind in ("workload", "target")]
    with transaction(h.app.state.engine, write=True) as db:
        for fact in facts:
            row = db.get(m.SchedulingFact, fact["id"])
            row.data = {**row.data, "line": {key: value for key, value in row.data["line"].items()
                                                if key != "weekdays"}}

    items = {row["id"]: row for row in read(h)["items"]}
    for fact in facts:
        assert items[fact["id"]]["starts_on"] is None
        assert items[fact["id"]]["ends_on"] is None
        assert items[fact["id"]]["weekdays"] == list(range(7))


def test_approval_item_exposes_resolved_measure_and_period(h):
    with transaction(h.app.state.engine, write=True) as db:
        typed = m.SchedulingFact(
            department_id=h.seed["department_id"], kind="workload", person_id=h.seed["people"][0],
            status="active", on=True, proposed_by=h.seed["identities"]["coordinator"],
            approved_by=h.seed["identities"]["approver"], reason="Synthetic typed quota",
            data={"catalogue_id": "workload_limit", "catalogue_version": 2,
                  "line": {"id": str(uuid.uuid4()), "measure": "scheduled_hours",
                           "period": {"kind": "rolling", "days": 14},
                           "numbers": {"max": {"value": 3, "level": "mandatory"}}}})
        legacy = m.SchedulingFact(
            department_id=h.seed["department_id"], kind="target", person_id=h.seed["people"][1],
            status="active", on=True, proposed_by=h.seed["identities"]["coordinator"],
            approved_by=h.seed["identities"]["approver"], reason="Synthetic legacy target",
            data={"catalogue_id": "workload_limit", "catalogue_version": 2,
                  "line": {"id": str(uuid.uuid4()),
                           "numbers": {"target": {"value": 3, "level": "medium"}},
                           "settings": {"block_quota": {"metric": "duties",
                                                           "window_alignment": "calendar_week"}}}})
        db.add_all([typed, legacy])
        db.flush()

    items = {row["id"]: row for row in read(h)["items"]}
    assert items[typed.id]["measure"] == "scheduled_hours"
    assert items[typed.id]["period"] == {"kind": "rolling", "days": 14, "starts_on": None,
                                          "ends_on": None, "anchor_on": None,
                                          "prorate_partial_windows": False, "repeat": False}
    assert items[legacy.id]["measure"] == "duties"
    assert items[legacy.id]["period"] == {"kind": "calendar_week", "days": None, "starts_on": None,
                                           "ends_on": None, "anchor_on": None,
                                           "prorate_partial_windows": False, "repeat": False}


def test_target_approval_exposes_balance_goal_defaults_and_explicit_window(h):
    default = propose(h, "target", target_value=4)
    explicit_goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10,
                              "period": "rolling", "period_days": 14}, severity="advisory")
    explicit = h.command("coordinator", "person.workload.propose", {
        "kind": "target", "person_id": h.seed["people"][1], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": explicit_goal["id"], "revision": explicit_goal["revision"]},
        "line": {"id": str(uuid.uuid4()), "numbers": {"target": {"value": 4, "level": "medium"}}},
        "reason": "Synthetic explicit target"})["fact"]

    items = {row["id"]: row for row in read(h)["items"]}
    assert items[default["id"]]["goal_measure"] == "hours"
    assert items[default["id"]]["goal_period"] == {"kind": "rolling", "days": None, "months": 12,
                                                       "starts_on": None, "ends_on": None}
    assert items[explicit["id"]]["goal_measure"] == "hours"
    assert items[explicit["id"]]["goal_period"] == {"kind": "rolling", "days": 14, "months": None,
                                                       "starts_on": None, "ends_on": None}


def test_requests_lists_facts_from_each_writer_and_legacy_import_shape(h):
    # Native scheduling-fact propose/edit path.
    original = propose(h)
    approved = decide(h, original)["fact"]
    edited = replace(h, approved, 3)

    # Overtime request approval writes a request-linked scheduling fact.
    from tests.test_overtime_volunteer import decide as decide_overtime, offer, offer_rule
    offer_rule(h)
    overtime_request = offer(h)["record"]
    overtime_decision = decide_overtime(h, overtime_request)
    assert overtime_decision["record"]["status"] == "approved"

    # The v2 conversion writer materializes a person fact from _fact_kind.
    from tests.test_rule_catalogue_v2_api import proposed
    h.policy({"kind": "max_duties", "maximum": 4, "window": "week"},
             {"person_id": h.seed["people"][1]})
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Review synthetic conversion"},
              conversion["id"], conversion["revision"])
    with transaction(h.app.state.engine) as db:
        converted = [row.id for row in db.scalars(select(m.SchedulingFact).where(
            m.SchedulingFact.migration_proposal_id == conversion["id"]))]

        # A retained seed/import row may carry historical line metadata unknown to RuleLine.
        imported = m.SchedulingFact(
            department_id=h.seed["department_id"], kind="workload", person_id=h.seed["people"][2],
            status="active", on=True, proposed_by=h.seed["identities"]["coordinator"],
            approved_by=h.seed["identities"]["approver"], reason="Synthetic retained import",
            data={"catalogue_id": "workload_limit", "catalogue_version": 2,
                  "line": {"id": str(uuid.uuid4()), "numbers": {"max": {"value": 2, "level": "mandatory"}},
                           "_source_identity": "legacy-import-line"}})
        db.add(imported)
        db.flush()
        imported_id = imported.id

    listed = read(h)
    listed_ids = {row["id"] for row in listed["items"]}
    assert {original["id"], approved["id"], edited["id"], *converted, imported_id} <= listed_ids
    assert listed["total"] == len(listed["items"])


def test_scheduling_fact_sort_uses_start_date_and_keeps_proposed_at(h):
    payloads = []
    for starts_on, ends_on in (("2026-11-10", "2026-11-12"), (None, None), ("2026-10-10", "2026-10-12")):
        payload = workload(h)
        payload["line"].update(starts_on=starts_on, ends_on=ends_on)
        payloads.append(h.command("coordinator", "person.workload.propose", payload)["fact"])

    by_start = read(h, type="other", sort_by="when")["items"]
    assert [row["id"] for row in by_start] == [payloads[2]["id"], payloads[0]["id"], payloads[1]["id"]]
    by_proposed = read(h, type="other", sort_by="proposed_at")["items"]
    assert [row["id"] for row in by_proposed] == [row["id"] for row in sorted(payloads, key=lambda row: row["created_at"])]


def test_scheduling_fact_proposed_at_filter_is_separate_from_fact_dates(h):
    facts = [propose(h, "workload") for _ in range(2)]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.SchedulingFact, facts[0]["id"]).created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        db.get(m.SchedulingFact, facts[1]["id"]).created_at = datetime(2026, 11, 1, tzinfo=timezone.utc)

    selected = read(h, type="other", filter=json.dumps({"conditions": {"conditions": [
        {"field": "proposed_at", "op": "on_or_after", "value": "2026-11-01"}]}}))
    assert [row["id"] for row in selected["items"]] == [facts[1]["id"]]


def test_linked_proposer_home_keeps_the_shared_safe_fact_projection(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = h.seed["people"][0]
    fact = propose(h)
    item = next(row for row in read(h, "coordinator")["items"] if row["id"] == fact["id"])
    assert h.get("coordinator", "/api/home")["my_requests"] == [item]
    assert item["title"] == "Personal workload" and item["link_target"] and not item["can_decide"]
    assert "summary" not in item
    assert "data" not in item and "reason" not in item
    assert h.get("staff", "/api/home")["my_requests"] == []


def test_alternate_proposer_account_is_visible_but_not_decidable_or_counted(h):
    person = h.seed["people"][1]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).person_id = person
    identity(h, "alternate_approver", ["approver"], person=person)
    fact = propose(h)
    ordinary = leave(h)
    listed = read(h, "alternate_approver")
    item = next(row for row in listed["items"] if row["id"] == fact["id"])
    assert not item["can_decide"] and listed["counts"]["to_decide"] == 1
    assert read(h, "alternate_approver", id=fact["id"])["item"] == item
    assert "scheduling_facts" not in attention(h, "alternate_approver")
    assert attention(h, "alternate_approver")["requests"]["count"] == 1
    assert decide(h, fact, role="alternate_approver", status=403)["error"]["code"] == "SELF_APPROVAL"
    h.command("alternate_approver", "request.decide", {"decision": "approve", "reason": "Independent request review"},
              ordinary["id"], ordinary["revision"])
    assert read(h, "alternate_approver")["counts"]["to_decide"] == 0


@pytest.mark.parametrize("action,status", [("activate", "active"), ("decline", "declined")])
def test_decision_removes_only_the_fact_from_pending_sources(h, action, status):
    fact = propose(h)
    ordinary = leave(h)
    before = read(h)
    assert before["counts"]["to_decide"] == before["counts"]["stages"]["waiting"] == 2
    assert {row["source"] for row in before["items"]} == {"request", "scheduling_fact"}
    assert decide(h, fact, action=action)["fact"]["status"] == status
    after = read(h)
    assert {row["id"] for row in after["items"]} == {ordinary["id"], fact["id"]}
    settled = next(row for row in after["items"] if row["id"] == fact["id"])
    assert settled["source"] == "scheduling_fact" and settled["status"] == status and not settled["can_decide"]
    assert [row["id"] for row in read(h, status="waiting")["items"]] == [ordinary["id"]]
    assert [row["id"] for row in read(h, decided="true")["items"]] == [fact["id"]]
    assert after["counts"]["to_decide"] == after["counts"]["stages"]["waiting"] == 1
    assert "scheduling_facts" not in attention(h)
    assert read(h, id=fact["id"])["fact"]["status"] == status


def test_programme_leads_see_only_current_programme_people_and_calendar_entries(h):
    lead, inside, outside = h.seed["people"][:3]
    expired = h.configure("people", {"display_name": "Expired synthetic programme member"})["record"]["id"]
    future = h.configure("people", {"display_name": "Future synthetic programme member"})["record"]["id"]
    a, b = [h.configure("programmes", {"name": name, "starts_on": "2026-01-01"})["record"]
            for name in ("Programme A", "Programme B")]
    identity(h, "programme_lead", ["programme_lead"], person=lead)
    identity(h, "unlinked_lead", ["programme_lead"])
    identity(h, "lead_approver", ["programme_lead", "approver"], person=lead)
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=pid,
            programme_id=programme["id"], starts_on=start, ends_on=end)
            for pid, programme, start, end in (
                (lead, a, NOW.date(), date(2027, 1, 1)),
                (inside, a, date(2026, 1, 1), NOW.date() + timedelta(days=1)),
                (outside, b, date(2026, 1, 1), date(2027, 1, 1)),
                (expired, a, date(2026, 1, 1), NOW.date()),
                (future, a, NOW.date() + timedelta(days=1), date(2027, 1, 1)))])
    mine = propose(h, person=inside)
    hidden = [propose(h, person=pid) for pid in (outside, expired, future)]
    calendar = propose(h, "protected_time", entry=calendar_entry(h, a, "Programme A teaching"))
    hidden.append(propose(h, "protected_time", entry=calendar_entry(h, b, "Programme B teaching")))
    ordinary = leave(h, "coordinator", inside, day=NOW.date().isoformat())
    listed = read(h, "programme_lead")
    # A trainee's own request is the lead's to decide (training.leads); person facts stay an approver's.
    assert {row["id"] for row in listed["items"]} == {mine["id"], calendar["id"], ordinary["id"]}
    assert listed["counts"]["to_decide"] == 1 and not any(row.get("can_decide") for row in listed["items"] if row["source"] == "scheduling_fact")
    for fact in (mine, calendar):
        assert read(h, "programme_lead", id=fact["id"])["fact"]["id"] == fact["id"]
    for fact in hidden:
        h.get("programme_lead", "/api/requests/all?id=" + fact["id"], status=404)
    assert read(h, "programme_lead", person_id=outside)["items"] == []
    assert read(h, "unlinked_lead")["items"] == []
    h.get("unlinked_lead", "/api/requests/all?id=" + calendar["id"], status=404)
    assert read(h, "lead_approver")["counts"]["to_decide"] == 7
    assert len(read(h, "lead_approver")["items"]) == 7
    assert "scheduling_facts" not in attention(h, "programme_lead")


def test_fact_filters_search_and_auditor_projection_keep_private_requests_out(h):
    person = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, person).display_name = "Searchable synthetic clinician"
        db.get(m.Identity, h.seed["identities"]["coordinator"]).display_name = "Synthetic proposer needle"
    fact = propose(h)
    calendar = propose(h, "protected_time", entry=calendar_entry(h, name="Calendar label needle"))
    ordinary = leave(h)
    for term, expected in (("searchable SYNTHETIC clinician", {fact["id"]}),
                           ("personal workload", {fact["id"]}), ("calendar protection", {calendar["id"]}),
                           ("calendar LABEL needle", {calendar["id"]}),
                           ("synthetic proposer needle", {fact["id"], calendar["id"]})):
        result = read(h, q=term, type="other")
        assert {row["id"] for row in result["items"]} == expected
        assert result["total"] == result["counts"]["stages"]["waiting"] == len(expected)
    assert [row["id"] for row in read(h, type="leave")["items"]] == [ordinary["id"]]
    assert {row["id"] for row in read(h, type="other", status="waiting")["items"]} == {fact["id"], calendar["id"]}
    assert read(h, type="other", status="approved")["items"] == []
    assert read(h, type="other", status="approved")["counts"]["stages"]["waiting"] == 2
    for query in ({"person_id": person}, {"person_ids": person}):
        assert [row["id"] for row in read(h, type="other", **query)["items"]] == [fact["id"]]
    auditor = read(h, "auditor")
    assert {row["id"] for row in auditor["items"]} == {fact["id"], calendar["id"]}
    assert auditor["counts"]["to_decide"] == 0
    assert read(h, "auditor", q="Family matter")["total"] == 0
    h.get("auditor", "/api/requests/all?id=" + ordinary["id"], status=404)
    assert read(h, q="nonexistent proposal")["total"] == 0


@pytest.mark.parametrize("key", s.REQUEST_SORT_FIELDS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_fact_and_request_sorting_pages_have_no_gaps_or_repeats(h, key, direction):
    facts = [propose(h, person=pid) for pid in h.seed["people"][:3]]
    facts.append(propose(h, "protected_time"))
    leave(h)
    all_items = read(h)["items"]
    expected = sorted(all_items, key=lambda row: (row["source"], row["id"]))
    def parsed(value):
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

    value = lambda row: (row["type"] if key == "type" else (row["person_name"] or "").lower() if key == "person"
                         else parsed(row["starts_on"]) if key == "when" and row["source"] == "scheduling_fact" and row["starts_on"]
                         else parsed(row.get("proposed_at", row.get("start"))) if key != "when" or row["source"] != "scheduling_fact" else None)
    if key == "decided":
        # All these rows await a decision; their unknown dates tie in stable source/ID order.
        assert all(row["decided_at"] is None for row in all_items)
    else:
        known = sorted((row for row in all_items if value(row) is not None), key=lambda row: (row["source"], row["id"]))
        known.sort(key=value, reverse=direction == "desc")
        unknown = sorted((row for row in all_items if value(row) is None), key=lambda row: (row["source"], row["id"]))
        expected = [*known, *unknown]
    pages = [read(h, sort_by=key, sort_direction=direction, limit=2, offset=offset) for offset in (0, 2, 4)]
    actual = [row for page in pages for row in page["items"]]
    assert [row["id"] for row in actual] == [row["id"] for row in expected]
    assert len({row["id"] for row in actual}) == 5
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert all(page["total"] == page["counts"]["stages"]["waiting"] == page["counts"]["to_decide"] == 5 for page in pages)


def test_other_department_fact_is_hidden_from_list_detail_search_and_home(h):
    fact = propose(h)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other synthetic approval department")
        db.add(other)
        db.flush()
        db.get(m.SchedulingFact, fact["id"]).department_id = other.id
    for role in ("approver", "coordinator", "auditor"):
        assert read(h, role)["total"] == read(h, role, q="personal workload")["total"] == 0
        assert read(h, role)["counts"]["to_decide"] == 0
        h.get(role, "/api/requests/all?id=" + fact["id"], status=404)
        assert "scheduling_facts" not in attention(h, role)
    decide(h, fact, status=404)


def test_department_conversion_does_not_offer_individual_fact_approvals(h):
    from tests.test_rule_catalogue_v2_api import proposed

    h.policy({"kind": "max_duties", "maximum": 4, "window": "week"}, {"person_id": h.seed["people"][0]})
    conversion = proposed(h)
    assert conversion["plan"]["scheduling_facts"]
    assert read(h)["items"] == []  # The pending envelope has not materialized its children.
    assert read(h)["counts"]["to_decide"] == 0 and "scheduling_facts" not in attention(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Independent whole department review"},
              conversion["id"], conversion["revision"])
    history = read(h)["items"]  # Approved children are history, never individual pending approvals.
    assert history and all(row["source"] == "scheduling_fact" and row["status"] == "active"
                           and not row["can_decide"] for row in history)
    assert read(h, status="waiting")["items"] == []
    assert read(h)["counts"]["to_decide"] == 0 and "scheduling_facts" not in attention(h)
    children = h.get("approver", f"/api/people/{h.seed['people'][0]}/scheduling-facts")["active"]
    assert children and all(row["migration_proposal_id"] == conversion["id"] for row in children)
    assert {row["id"] for row in history} == {row["id"] for row in children}
    assert decide(h, children[0], status=409)["error"]["code"] == "CONVERSION_CHILD"


@pytest.mark.postgres
def test_postgres_shared_fact_query_and_v2_calendar_projection(pg):
    h = pg
    facts = [propose(h, kind) for kind in ("workload", "target", "overtime_offer", "protected_time")]
    entry = calendar_entry(h, name="V2 academic protection")
    fact = h.command("coordinator", "calendar.protection.propose", {
        "kind": "protected_time", "calendar_entry_id": entry["id"], "catalogue_id": "workload_limit",
        "line": {"id": str(uuid.uuid4()), "mode": "no_duties", "weekdays": [1], "start_time": "09:00",
                 "end_time": "11:00", "starts_on": entry["starts_on"], "ends_on": entry["ends_on"],
                 "numbers": {"apply": {"value": None, "level": "mandatory"}}},
        "reason": "Synthetic v2 calendar protection"})["fact"]
    items = read(h, type="other", status="waiting", sort_by="person")["items"]
    assert {row["id"] for row in items} == {fact["id"], *(row["id"] for row in facts)}
    assert all(row["can_decide"] for row in items)
    assert read(h)["counts"]["to_decide"] == attention(h)["scheduling_facts"]["count"] == 5
    result = read(h, q="V2 academic protection")
    assert result["total"] == result["counts"]["stages"]["waiting"] == 1
    assert result["items"][0]["title"] == "Calendar protection"
    assert result["items"][0]["weekdays"] == [1]
    assert (result["items"][0]["start_time"], result["items"][0]["end_time"]) == ("09:00", "11:00")
    assert read(h, id=fact["id"])["fact"]["id"] == fact["id"]
    identity(h, "unlinked_lead", ["programme_lead"])
    assert read(h, "unlinked_lead")["items"] == []
    assert not any(row["can_decide"] for row in read(h, "auditor")["items"])



def converted_target_replacement(h, *, omit_metric=False):
    from copy import deepcopy
    from tests.test_rule_catalogue_v2_api import proposed

    person = h.seed["people"][0]
    goal = h.policy({"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10,
                     "person_amounts": {person: 7}}, severity="advisory")
    conversion = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": "Independent converted target review"},
              conversion["id"], conversion["revision"])
    catalogue = h.get("coordinator", "/api/rules/catalogue?version=2&include_hidden=true")
    current = next(entry["rule"] for entry in catalogue["entries"] if entry["id"] == "fair_share")
    facts = h.get("coordinator", f"/api/people/{person}/scheduling-facts")["active"]
    previous = next(fact for fact in facts if fact["kind"] == "target"
                    and fact["data"]["goal_ref"]["policy_id"] == goal["id"])
    line = deepcopy(previous["data"]["line"])
    assert line["source_bindings"] and line["settings"]["balance_goal"]["metric"] == "hours"
    line["numbers"]["target"]["value"] = 8
    if omit_metric:
        line["settings"]["balance_goal"].pop("metric")
    pending = h.command("coordinator", "person.workload.propose", {
        "kind": "target", "person_id": person, "catalogue_id": "fair_share", "catalogue_version": 2,
        "goal_ref": previous["data"]["goal_ref"], "line": line,
        "replaces_id": previous["id"], "expected_revision": previous["revision"],
        "reason": "Synthetic replacement of a converted personal amount"})["fact"]
    return goal, current, pending


def test_retired_converted_goal_keeps_pending_target_readable_in_requests_and_home(h):
    _, current, pending = converted_target_replacement(h, omit_metric=True)
    retired = h.command("approver", "policy.retire", {"reason": "Synthetic retirement of converted goal"},
                        current["id"], current["revision"])["record"]
    assert retired["status"] == "retired"
    listed = read(h)
    item = next(row for row in listed["items"] if row["id"] == pending["id"])
    assert item["title"] == "Personal target"
    assert item["numbers"]["target"]["value"] == 8
    assert item["goal_measure"] is None and item["goal_period"] is None
    assert listed["counts"]["to_decide"] == 1
    detail = read(h, id=pending["id"])
    assert detail["item"] == item and detail["fact"] == pending
    home = attention(h)["scheduling_facts"]
    assert home["count"] == 1 and home["sample"] == [item]


def test_pending_converted_target_uses_current_goal_metric_after_parent_edit(h):
    from copy import deepcopy

    goal, current, pending = converted_target_replacement(h)
    assert pending["data"]["line"]["settings"]["balance_goal"]["metric"] == "hours"
    lines = deepcopy(current["definition"]["rule"]["lines"])
    for line in lines:
        if any(pin['policy_id'] == goal['id'] for pin in line.get('source_bindings', {}).values()):
            line["settings"]["balance_goal"]["metric"] = "duties"
    replacement = h.command("coordinator", "rule.propose", {
        "catalogue_id": "fair_share", "catalogue_version": 2,
        "expected_policy_id": current["id"], "expected_revision": current["revision"], "lines": lines,
        "reason": "Synthetic metric edit of converted source goal"})["record"]
    h.command("approver", "policy.activate", {"reason": "Independent source metric review"},
              replacement["id"], replacement["revision"])
    effective = h.get("coordinator", f"/api/people/{pending['person_id']}/scheduling-facts")["effective"]
    resolved = next(rule for rule in effective if rule["policy_id"] == goal["id"])
    assert resolved["definition"]["rule"]["metric"] == "duties"
    item = next(row for row in read(h)["items"] if row["id"] == pending["id"])
    assert item["goal_measure"] == "duties"
    assert item["goal_period"]["kind"] == "rolling"
    assert read(h, id=pending["id"])["item"] == item
    assert attention(h)["scheduling_facts"]["sample"] == [item]
# Include this file in hosted supervision and team-lead qualification.
