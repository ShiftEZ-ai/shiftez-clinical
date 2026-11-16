"""Availability recurrences share the dated-input wall-time policy."""
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.availability_patterns import expand, occurrences
from apps.api.services.overtime_availability import _clock
from packages.domain.time import aware, local_stamp


ZONE = "America/New_York"


@pytest.mark.parametrize("day,clock,end", [
    (date(2027, 3, 14), "02:30", "03:00"),
    (date(2027, 11, 7), "01:30", "02:00"),
    (date(2027, 3, 14), "01:00", "02:30"),
    (date(2027, 11, 7), "00:30", "01:30"),
])
def test_recurring_availability_rejects_gap_and_fold_bounds(day, clock, end):
    pattern = SimpleNamespace(starts_on=day, ends_on=None, weekdays=[day.weekday()],
                              start_time=clock, end_time=end)
    with pytest.raises(ValueError, match="Ambiguous or nonexistent local wall time"):
        list(occurrences(pattern, day, day, ZoneInfo(ZONE)))


@pytest.mark.parametrize("day,clock", [
    (date(2027, 3, 14), "02:30"), (date(2027, 11, 7), "01:30"),
])
def test_overtime_clock_rejects_gap_and_fold(day, clock):
    with pytest.raises(ValueError, match="Ambiguous or nonexistent local wall time"):
        _clock(day, clock, ZONE)


@pytest.mark.parametrize("day,hours", [(date(2027, 3, 14), 23), (date(2027, 11, 7), 25)])
def test_recurring_and_overtime_midnight_bounds_have_positive_elapsed_duration(day, hours):
    pattern = SimpleNamespace(starts_on=day, ends_on=None, weekdays=[day.weekday()],
                              start_time="00:00", end_time="24:00")
    start, end, occurrence = next(occurrences(pattern, day, day, ZoneInfo(ZONE)))
    assert occurrence == day
    assert (aware(end) - aware(start)).total_seconds() == hours * 3600
    assert aware(start) == _clock(day, "00:00", ZONE) == local_stamp(day, "00:00", ZONE)
    assert aware(end) == _clock(day, "24:00", ZONE) == local_stamp(day + timedelta(days=1), "00:00", ZONE)


def test_invalid_weekly_occurrence_returns_actionable_validation_response(h):
    day = date(2027, 3, 14)
    workspace = h.workspace("Synthetic DST recurrence", day.isoformat(), (day + timedelta(days=1)).isoformat())
    with transaction(h.app.state.engine, write=True) as db:
        organisation = db.get(m.Organisation, db.get(m.Department, h.seed["department_id"]).organisation_id)
        organisation.timezone = ZONE
    h.command("coordinator", "availability_pattern.propose", {
        "person_id": h.seed["people"][1], "weekdays": [day.weekday()],
        "start_time": "02:30", "end_time": "03:00", "starts_on": day.isoformat(),
    })
    result = h.command("coordinator", "workspace.validate", {"reason": "Synthetic DST check"},
                       workspace["id"], workspace["revision"], status=422)
    assert result["error"]["code"] == "VALUE_INVALID"
    message = result["error"]["message"]
    assert "Ambiguous or nonexistent local wall time" in message
    assert "explicit dated instant" in message


@pytest.mark.parametrize("first,last", [(date(2027, 3, 13), date(2027, 3, 14)),
                                       (date(2027, 3, 15), date(2027, 3, 16))])
def test_expansion_does_not_validate_invalid_occurrences_outside_the_requested_days(h, first, last):
    person = h.seed["people"][1]
    h.command("coordinator", "availability_pattern.propose", {
        "person_id": person, "weekdays": [6], "start_time": "02:30", "end_time": "03:00",
        "starts_on": "2027-03-14", "ends_on": "2027-03-15",
    })
    with transaction(h.app.state.engine) as db:
        assert expand(db, [h.seed["department_id"]], [person],
                      local_stamp(first, "00:00", ZONE), local_stamp(last, "00:00", ZONE), ZONE) == []


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize("start,end,available", [
    ("11:00", "12:00", True), ("11:30", "12:30", False),
    ("12:00", "13:00", False), ("13:00", "14:00", True),
])
def test_fairness_samples_local_noon_on_transition_days(day, start, end, available):
    from packages.domain.fairness import AVAILABLE, goal_report, ledger_facts, member_days
    from tests.test_engine_fairness_review import A, B, duty, policy, world

    following, last = day + timedelta(days=1), day + timedelta(days=2)
    data = world([duty(1, following.isoformat(), [A]), duty(2, following.isoformat(), [B])],
                 first=day.isoformat(), last=last.isoformat())
    zone = "Europe/Berlin"
    data["organisation"]["timezone"] = zone
    data["availability"] = [{"person_id": A, "kind": "recorded_leave",
                             "start": local_stamp(day, start, zone), "end": local_stamp(day, end, zone)}]
    days = [day, following] if available else [following]
    assert member_days(data, A, day, last) == days
    assert sum(value for pid, _, key, _, value in ledger_facts(data) if pid == A and key == AVAILABLE) == len(days)

    report = goal_report(data, policy({"metric": "duties", "period": "rota"}))
    rows = {row["person_id"]: row for row in report["people"]}
    assert rows[A]["available_days"] == len(days)
    assert rows[B]["available_days"] == 2
    assert rows[A]["expected"] == round(2 * len(days) / (len(days) + 2), 2)
    assert rows[B]["expected"] == round(4 / (len(days) + 2), 2)


@pytest.mark.parametrize("day,start,end", [(date(2027, 3, 14), "02:30", "03:00"),
                                         (date(2027, 11, 7), "01:30", "02:00")])
@pytest.mark.parametrize("first,last,overlap", [("00:00", "00:30", False),
                                             ("03:30", "04:00", False),
                                             ("00:00", "04:00", True)])
def test_exact_window_ignores_invalid_times_only_when_outside_query(day, start, end, first, last, overlap):
    pattern = SimpleNamespace(id="synthetic-pattern", person_id="synthetic-person", department_id="synthetic-department",
                              starts_on=day, ends_on=None, weekdays=[day.weekday()], start_time=start, end_time=end)
    db = SimpleNamespace(scalars=lambda _query: [pattern])
    args = (db, [pattern.department_id], [pattern.person_id],
            local_stamp(day, first, ZONE), local_stamp(day, last, ZONE), ZONE)
    if overlap:
        with pytest.raises(ValueError, match="Ambiguous or nonexistent local wall time"):
            expand(*args)
    else:
        assert expand(*args) == []


@pytest.mark.parametrize("first,last,overlap", [
    ("01:00:00-04:00", "01:15:00-04:00", False),
    ("01:15:00-04:00", "01:30:00-04:00", False),
    ("01:45:00-05:00", "02:00:00-05:00", False),
    ("01:30:00-04:00", "01:45:00-04:00", True),
    ("01:30:00-05:00", "01:45:00-05:00", True),
])
def test_exact_window_in_repeated_hour_ignores_only_proven_nonoverlap(first, last, overlap):
    day = date(2027, 11, 7)
    pattern = SimpleNamespace(id="synthetic-pattern", person_id="synthetic-person", department_id="synthetic-department",
                              starts_on=day, ends_on=None, weekdays=[6], start_time="01:30", end_time="01:45")
    db = SimpleNamespace(scalars=lambda _query: [pattern])
    args = (db, [pattern.department_id], [pattern.person_id],
            aware("2027-11-07T" + first), aware("2027-11-07T" + last), ZONE)
    if overlap:
        with pytest.raises(ValueError, match="Ambiguous or nonexistent local wall time"):
            expand(*args)
    else:
        assert expand(*args) == []


def test_exact_window_in_repeated_hour_preserves_ambiguous_occurrence_rejection():
    day = date(2027, 11, 7)
    pattern = SimpleNamespace(id="synthetic-pattern", person_id="synthetic-person", department_id="synthetic-department",
                              starts_on=day, ends_on=None, weekdays=[6], start_time="01:30", end_time="02:00")
    db = SimpleNamespace(scalars=lambda _query: [pattern])
    # 01:30's first occurrence can overlap 01:00's second occurrence despite their nominal clock order.
    with pytest.raises(ValueError, match="Ambiguous or nonexistent local wall time"):
        expand(db, [pattern.department_id], [pattern.person_id], aware("2027-11-07T01:00:00-05:00"),
               aware("2027-11-07T01:15:00-05:00"), ZONE)


@pytest.fixture
def invalid_weekly_pattern(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        db.get(m.Organisation, department.organisation_id).timezone = ZONE
        department.leave_reason_display = "type"
        row = m.AvailabilityPattern(department_id=department.id, person_id=h.seed["people"][1], weekdays=[6],
            start_time="02:30", end_time="03:00", starts_on=date(2027, 3, 14), ends_on=date(2027, 3, 15),
            status="active", created_by=h.seed["identities"]["coordinator"])
        db.add(row)
        db.flush()
        actor = SimpleNamespace(department_id=department.id, organisation_id=department.organisation_id,
                                permits=lambda *_roles: True)
        return actor, row.id


def test_request_impact_excludes_invalid_occurrence_after_rota_horizon(h, invalid_weekly_pattern):
    from apps.api.services.availability import _impact

    actor, pattern_id = invalid_weekly_pattern
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Workspace(department_id=actor.department_id, name="Synthetic rota before transition",
            starts_on=date(2027, 3, 13), ends_on=date(2027, 3, 14),
            created_by=h.seed["identities"]["coordinator"], last_edited_by=h.seed["identities"]["coordinator"]))
    items = [{"id": pattern_id, "source": "pattern", "stage": "approved", "person_id": h.seed["people"][1]}]
    with transaction(h.app.state.engine) as db:
        _impact(db, actor, items)
    assert items[0]["impact_count"] == items[0]["schedule_impact"]["count"] == 0


def test_leave_disclosure_excludes_invalid_occurrence_after_finding(h, invalid_weekly_pattern):
    from apps.api.services.leave_disclosure import disclose_leave

    actor, _ = invalid_weekly_pattern
    start, end = local_stamp(date(2027, 3, 13), "12:00", ZONE), local_stamp(date(2027, 3, 13), "24:00", ZONE)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Availability(department_id=actor.department_id, person_id=h.seed["people"][1], start=start, end=end,
            kind="recorded_leave", leave_type="Synthetic leave", private_reason="Synthetic absence",
            recorded_by=h.seed["identities"]["coordinator"]))
    finding = {"code": "UNAVAILABLE", "person_id": h.seed["people"][1], "start": start.isoformat(), "end": end.isoformat()}
    with transaction(h.app.state.engine) as db:
        assert disclose_leave(db, actor, finding) is finding
    assert finding["leave"] == {"type": "Synthetic leave", "starts_on": "2027-03-13", "ends_on": "2027-03-13"}
