"""Same-day request checks use one local calendar day without weakening range or access bounds."""
import pytest


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_same_day_calendar_matches_one_day_window_and_preserves_staff_scope(request, database):
    h = request.getfixturevalue(database)
    workspace = h.shift(h.workspace(), people=h.seed["people"][:2])["workspace"]
    first_day = h.detail(workspace)["shifts"][0]["id"]
    workspace = h.shift(workspace, start="2026-10-05T08:00:00+03:00",
                        end="2026-10-05T12:00:00+03:00")["workspace"]
    h.publish(workspace)
    same_day = "/api/calendar?start=2026-10-04&end=2026-10-04"
    one_day = "/api/calendar?start=2026-10-04&end=2026-10-05"

    for role in ("staff", "coordinator"):
        calendar = h.get(role, same_day)
        assert calendar == h.get(role, one_day)
        assert (calendar["starts_on"], calendar["ends_on"]) == ("2026-10-04", "2026-10-05")
        assert {event["shift_id"] for event in calendar["events"] if event["kind"] == "duty"} == {first_day}
        if role == "staff":
            assert {event["person_id"] for event in calendar["events"]} == {h.seed["people"][0]}
            assert calendar["sessions"] == []
        else:
            assert {event["person_id"] for event in calendar["events"]} == set(h.seed["people"][:2])
            assert {session["shift_id"] for session in calendar["sessions"]} == {first_day}

    h.get("staff", same_day + "&person_id=" + h.seed["people"][1], status=403)
    h.get("staff", same_day + "&workspace_id=" + workspace["id"], status=403)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_same_day_calendar_accepts_empty_days_and_keeps_range_bounded(request, database):
    h = request.getfixturevalue(database)
    calendar = h.get("staff", "/api/calendar?start=2026-10-03&end=2026-10-03")
    assert calendar["events"] == []
    assert calendar["ends_on"] == "2026-10-04"
    h.get("staff", "/api/calendar?start=2026-10-01&end=2026-11-01")
    for start, end in (("2026-10-04", "2026-10-03"), ("2026-10-01", "2026-11-02"),
                       ("9999-12-31", "9999-12-31")):
        error = h.get("staff", f"/api/calendar?start={start}&end={end}", status=422)
        assert error["error"]["code"] == "CALENDAR_RANGE"
