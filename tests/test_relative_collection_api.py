"""Relative and empty conditions filter complete authorized collections before paging."""
from .clock_support import freeze_clock
from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event, literal, select
from packages.domain import conditions
from packages.domain.time import local_midnight

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import audiences, collection_filters, condition_context
from apps.api.services.bulk_staged import _project
from .test_requests_search import both, request_row  # noqa: F401
from .governance_support import identity


def relative(token, **options):
    return {"kind": "relative", "token": token, **options}


@pytest.mark.parametrize("date_line", [
    {"field": "date", "op": "is", "value": relative("yesterday")},
    {"field": "created_at", "op": "within", "value": relative("last_n", n=1, unit="days")},
    {"field": "created_at", "op": "between", "value": ["2026-10-02", relative("today")]},
])
def test_paged_operations_relative_dates_and_empty_filter_before_every_page(both, monkeypatch, date_line):
    h = both
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 3, 6, tzinfo=timezone.utc))
    # department-local local day starts at 21:00 UTC on the preceding date.
    with transaction(h.app.state.engine, write=True) as db:
        matches = [m.Outbox(department_id=h.seed["department_id"], event_key=f"relative-old-{n}", kind="notification",
            payload={}, state="failed", last_error=None if n % 2 else "",
            created_at=datetime(2026, 10, 2, 21, tzinfo=timezone.utc) + timedelta(minutes=n)) for n in range(205)]
        db.add_all(matches)
        db.add_all(m.Outbox(department_id=h.seed["department_id"], event_key=f"relative-new-{n}", kind="notification",
            payload={}, state="failed", last_error=None if n % 2 else "Visible error",
            created_at=datetime(2026, 10, 4 if n % 2 else 3, 12, tzinfo=timezone.utc))
            for n in range(105))
        db.flush()
        expected = {row.id for row in matches}
    # Yesterday tests a different, still populated local date.
    if date_line["value"] == relative("yesterday"):
        freeze_clock(monkeypatch, lambda: datetime(2026, 10, 4, 6, tzinfo=timezone.utc))
    unfiltered = h.get("operator", "/api/operations")
    assert not expected.intersection(row["id"] for row in unfiltered["outbox"])
    tree = {"conditions": [date_line, {"field": "error", "op": "empty"}]}
    found = []
    for offset in (0, 100, 200):
        response = h.client("operator").get("/api/operations", params={"offset": offset,
            "filter": json.dumps({"conditions": tree, "as_of": "2026-10-03"})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 205 and page["has_more"] == (offset < 200)
        found.extend(row["id"] for row in page["outbox"])
    assert len(found) == 205 and set(found) == expected


def test_child_relative_dates_and_open_ends_filter_before_cached_second_page(both, monkeypatch):
    h = both
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 3, 6, tzinfo=timezone.utc))
    me = h.seed["people"][0]
    identity(h, "relative_child_reader", ["coordinator"], person=me)
    capability = h.configure("capabilities", {"name": "Relative child credential", "starts_on": "2020-01-01"})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        common = dict(department_id=h.seed["department_id"], person_id=me, capability_id=capability["id"],
                      granted_by=h.seed["identities"]["credentialer"], evidence="Synthetic paging qualification")
        matches = [m.CapabilityGrant(**common, starts_on=date(2026, 10, 3), ends_on=None) for _ in range(5)]
        db.add_all(matches)
        db.add_all(m.CapabilityGrant(**common, starts_on=date(2020, 1, 1) if n % 2 else date(2026, 10, 3),
            ends_on=None if n % 2 else date(2026, 10, 10)) for n in range(105))
        db.flush()
        expected = {row.id for row in matches}
    calls = []
    original = condition_context.evaluation_context
    def context(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result
    monkeypatch.setattr(condition_context, "evaluation_context", context)
    selection = {"collection": "grants", "parent": {"collection": "people", "selection": {
        "collection": "people", "where": {"as_of": "2026-10-03", "conditions": {"conditions": [
            {"field": "person_ids", "op": "is", "value": relative("me")}]}}}}, "where": {
        "as_of": "2026-10-03", "filters": {"capability_id": [capability["id"]]}, "conditions": {"conditions": [
            {"field": "starts_on", "op": "within", "value": relative("next_n", n=2, unit="days")},
            {"field": "ends_on", "op": "empty"}]}}}
    found = []
    for offset in (0, 2, 4):
        calls.clear()
        response = h.client("relative_child_reader").post("/api/bulk/rows", json={"selection": selection,
            "sort": [{"field": "id"}], "offset": offset, "limit": 2})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["count"] == 5 and page["has_more"] == (offset < 4)
        found.extend(row["id"] for row in page["rows"])
        if calls:
            # Date-only child contexts need no viewer facts; parent contexts share the one load.
            assert len({id(value.values["me"]) for value in calls if "me" in value.values}) <= 1
    assert len(found) == 5 and set(found) == expected
    resolved = h.client("relative_child_reader").post("/api/bulk/resolve", json=selection)
    assert resolved.status_code == 200 and resolved.json()["count"] == 5
    selection["where"]["conditions"]["conditions"][1]["op"] = "not_empty"
    response = h.client("relative_child_reader").post("/api/bulk/rows", json={"selection": selection})
    assert response.status_code == 200 and response.json()["count"] == 53
    selection["where"]["conditions"]["conditions"][1]["op"] = "empty"
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 4, 6, tzinfo=timezone.utc))
    h.clients.pop('relative_child_reader').close()
    response = h.client("relative_child_reader").post("/api/bulk/rows", json={"selection": selection})
    assert response.status_code == 200 and response.json()["count"] == 0  # No writes: today's range still expires the cached IDs.


def test_requests_relative_person_and_mixed_date_operands_share_context_with_counts(both, monkeypatch):
    h = both
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 4, 6, tzinfo=timezone.utc))
    me = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(request_row(h, me, "Synthetic ordinary request", day=-n-1) for n in range(105))
        matches = [request_row(h, me, "Synthetic relative request", day=n) for n in range(3)]
        db.add_all(matches)
        db.add(request_row(h, h.seed["people"][1], "Other person's request"))
        db.flush()
        expected = [row.id for row in matches]
    calls = []
    original = condition_context.evaluation_context
    def context(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result
    monkeypatch.setattr(condition_context, "evaluation_context", context)
    tree = {"conditions": [{"field": "person", "op": "is", "value": relative("me")},
        {"field": "start", "op": "between", "value": [relative("today"), "2026-10-06"]}]}
    found = []
    tree["conditions"][1]["field"] = "dates.start"
    for offset in (0, 2):
        calls.clear()
        response = h.client("staff").get("/api/requests/all", params={"limit": 2, "offset": offset,
            "filter": json.dumps({"conditions": tree, "as_of": "2026-10-04"})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == sum(page["counts"]["stages"].values()) == 3
        found.extend(row["id"] for row in page["items"])
        assert len({id(value) for value in calls}) == 1
    assert found == expected


def test_staged_projection_keeps_only_catalogue_relative_token_names():
    actor = Actor(identity_id="identity", department_id="department", organisation_id="organisation",
                  display_name="Test coordinator", roles=frozenset({"coordinator"}), person_id=None,
                  session_id="session")
    row = m.BulkStaged(payload={})
    operand = relative("today")
    result = _project(None, actor, row, {"source_selection": {"value": operand},
        "credential": {"token": "secret"}, "forged": {"kind": "relative", "token": "secret"}}, details=False)
    assert result["source_selection"]["value"] == operand
    assert "token" not in result["credential"] and "token" not in result["forged"]



def test_audit_own_person_text_filter_and_csv_use_audit_metadata(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        who = db.get(m.Identity, h.seed["identities"]["operator"])
        who.display_name = "Audit text needle"
        row = m.AuditEvent(department_id=h.seed["department_id"], actor_id=who.id,
            action="relative.audit", reason="Synthetic table metadata regression", detail={}, correlation_id="relative-audit")
        db.add(row)
        db.flush()
        expected = row.id
    query = {"filter": json.dumps({"conditions": {"conditions": [
        {"field": "person", "op": "contains", "value": "TEXT needle"},
        {"field": "action", "op": "any", "values": ["relative.audit"]}]}})}
    response = h.client("operator").get("/api/audit", params=query)
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["events"]] == [expected]
    csv = h.client("operator").get("/api/audit.csv", params=query)
    assert csv.status_code == 200 and "relative.audit" in csv.text and "Audit text needle" in csv.text


def test_requests_validate_unavailable_operands_and_readable_fields_with_no_sources(both):
    h = both
    for line in ({"field": "person", "op": "is", "value": relative("my_team")},
                 {"field": "person", "op": "is", "value": relative("people_i_approve")},
                 {"field": "location_ids", "op": "empty"},
                 {"field": "private_reason", "op": "contains", "value": "private"}):
        response = h.client("staff").get("/api/requests/all", params={"type": "wish", "status": "waiting",
            "filter": json.dumps({"conditions": {"conditions": [line]}})})
        assert response.status_code == 422, response.text


def test_requests_end_filters_match_included_last_day_and_open_weekly_end(both, monkeypatch):
    h = both
    day = date(2026, 10, 18)
    freeze_clock(monkeypatch, lambda: datetime(2026, 10, 3, 6, tzinfo=timezone.utc))
    with transaction(h.app.state.engine, write=True) as db:
        common = dict(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
                      created_by=h.seed["identities"]["coordinator"])
        midnight = m.RequestRecord(**common, start=local_midnight(day - timedelta(days=1), "Asia/Riyadh"),
            end=local_midnight(day + timedelta(days=1), "Asia/Riyadh"), private_reason="Synthetic all-day request")
        daytime = m.RequestRecord(**common, start=local_midnight(day, "Asia/Riyadh") + timedelta(hours=8),
            end=local_midnight(day, "Asia/Riyadh") + timedelta(hours=12), private_reason="Synthetic timed request")
        earlier = m.RequestRecord(**common, start=local_midnight(day - timedelta(days=1), "Asia/Riyadh"),
            end=local_midnight(day, "Asia/Riyadh"), private_reason="Synthetic earlier last day")
        availability = m.Availability(department_id=h.seed["department_id"], person_id=h.seed["people"][0],
            recorded_by=h.seed["identities"]["coordinator"], kind="recorded_leave", private_reason="Synthetic recorded leave",
            start=local_midnight(day - timedelta(days=1), "Asia/Riyadh"), end=local_midnight(day + timedelta(days=1), "Asia/Riyadh"))
        patterns = [m.AvailabilityPattern(**common, starts_on=date(2026, 10, 1), ends_on=end, weekdays=[0],
            start_time="08:00", end_time="12:00", note="Synthetic weekly last day", status="active")
            for end in (day + timedelta(days=1), None)]
        wish = m.StaffWish(**common, first_day=day, last_day=day, kind="prefer_off", points=1, note="Synthetic inclusive wish")
        db.add_all([midnight, daytime, earlier, availability, *patterns, wish])
        db.flush()
        expected = {midnight.id, daytime.id, availability.id, patterns[0].id, wish.id}
        request_ids = {midnight.id, daytime.id}
        open_id = patterns[1].id
    for value in (day.isoformat(), relative("today")):
        if isinstance(value, dict):
            freeze_clock(monkeypatch, lambda: datetime(2026, 10, 18, 6, tzinfo=timezone.utc))
            h.clients.pop('approver').close()
        for field in ("end", "dates.end"):
            response = h.client("approver").get("/api/requests/all", params={"filter": json.dumps({"conditions": {
                "conditions": [{"field": field, "op": "is", "value": value}]}})})
            assert response.status_code == 200, response.text
            assert {row["id"] for row in response.json()["items"]} == expected
            selection = {"collection": "requests", "where": {"conditions": {"conditions": [
                {"field": field, "op": "is", "value": value}]}}}
            resolved = h.client("approver").post("/api/bulk/resolve", json=selection)
            rows = h.client("approver").post("/api/bulk/rows", json={"selection": selection})
            assert resolved.status_code == rows.status_code == 200, resolved.text + rows.text
            assert {row["id"] for row in resolved.json()["sample"]} == request_ids
            assert {row["id"] for row in rows.json()["rows"]} == request_ids
    response = h.client("approver").get("/api/requests/all", params={"type": "weekly", "filter": json.dumps({
        "conditions": {"conditions": [{"field": "end", "op": "empty"}]}})})
    assert response.status_code == 200 and [row["id"] for row in response.json()["items"]] == [open_id]


def test_sql_exclusive_end_operators_agree_with_inclusive_calendar_evaluator(both):
    day = date(2026, 11, 1)
    for zone in ("Asia/Riyadh", "America/New_York"):
        context = conditions.EvaluationContext(timezone=zone, week_start=6, today=day, table_key="requests")
        for raw, exclusive in ((local_midnight(day + timedelta(days=1), zone), True),
                               (local_midnight(day, zone) + timedelta(hours=12), True),
                               (day + timedelta(days=1), True), (day, False)):
            for op, operand in (("is", relative("today")), ("before", relative("today")),
                ("before", relative("tomorrow")), ("after", relative("today")), ("after", relative("yesterday")),
                ("on_or_before", relative("today")), ("on_or_before", relative("yesterday")),
                ("on_or_after", relative("today")), ("on_or_after", relative("tomorrow")),
                ("between", [relative("yesterday"), day.isoformat()]),
                ("within", relative("last_n", n=2, unit="days"))):
                tree = {"conditions": [{"field": "end", "op": op, "value": operand}]}
                expected = conditions.evaluate({"conditions": [{"field": "date", "op": op, "value": operand}]},
                                               lambda field: {day}, context, row={"date": day})
                with transaction(both.app.state.engine) as db:
                    actual = db.scalar(select(collection_filters.predicate(tree, {"end": literal(raw).label("end" if isinstance(raw, datetime) else "ends_on" if exclusive else "last_day")},
                        context, table_key="requests")))
                assert actual is expected, (zone, raw, op, operand)


def test_prepared_recipient_context_reuses_duty_date_for_unlinked_identities_without_sql(h):
    day = date(2026, 10, 3)
    with transaction(h.app.state.engine) as db:
        prepared = audiences.recipient_context(db, h.seed["department_id"], day)
        statements = []
        def sql(*args):
            statements.append(args[2])
        event.listen(h.app.state.engine, "before_cursor_execute", sql)
        try:
            for _ in range(3):
                result = audiences.recipients(db, h.seed["department_id"], {"where": {"conditions": [
                    {"field": "roles", "values": ["coordinator"]},
                    {"field": "date", "op": "is", "value": relative("duty_day")}] }}, day, prepared)
                assert h.seed["identities"]["coordinator"] in result
            assert not statements
        finally:
            event.remove(h.app.state.engine, "before_cursor_execute", sql)



def test_requests_swap_end_filters_use_offered_assignment_last_day(both):
    h = both
    workspace = h.shift(h.workspace(), start="2026-10-04T22:00:00+03:00", end="2026-10-05T02:00:00+03:00")["workspace"]
    publication = h.publish(workspace)["publication_id"]
    snapshot = h.get("coordinator", "/api/publications/" + publication)["snapshot"]
    assignment_id = snapshot["shifts"][0]["participants"][0]["id"]
    with transaction(h.app.state.engine, write=True) as db:
        row = m.SwapRequest(department_id=h.seed["department_id"], kind="give", publication_id=publication,
            proposer_id=h.seed["identities"]["staff"], proposer_person_id=h.seed["people"][0],
            recipient_person_id=h.seed["people"][1], first_assignment_id=assignment_id,
            expires_at=m.now() + timedelta(days=1), reason="Synthetic offered last day")
        db.add(row)
        db.flush()
        expected = row.id
    response = h.client("approver").get("/api/requests/all", params={"type": "swap", "filter": json.dumps({
        "conditions": {"conditions": [{"field": "end", "op": "is", "value": "2026-10-05"}]}})})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [expected]
