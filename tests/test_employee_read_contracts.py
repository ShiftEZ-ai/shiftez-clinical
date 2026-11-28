"""Compare modeled employee reads with the same projections before serialization."""
from fastapi.routing import APIRoute, request_response
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s

from .clock_support import freeze_clock
from .governance_support import identity
from .test_announcement_receipts import notice, seen
from .test_event_calendar import event_row
from .test_home_team_payload import supervision_type
from .test_requests_unified import leave
from .test_staff_swaps import offered, source_pair


DATES = {"start": "2026-10-04", "end": "2026-10-07"}
READ_MODELS = {
    "/api/calendar": s.CalendarResponse,
    "/api/schedule/sessions": s.ScheduleWindow,
    "/api/content": s.DepartmentContentPage,
    "/api/requests/all": s.RequestListPage | s.RequestListDetail,
    "/api/directory": s.DirectoryResponse,
}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_employee_read_models_preserve_json_for_each_role(request, monkeypatch, database):
    h = request.getfixturevalue(database)
    freeze_clock(monkeypatch)
    publication_id, assignment_id, _, _ = source_pair(h)
    offered(h, publication_id, assignment_id)
    staff, colleague, _ = h.seed["people"]
    leave_id = leave(h)["id"]
    h.command("staff", "availability_pattern.propose", {
        "person_id": staff, "weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01",
    })
    h.command("staff", "wish.create", {
        "person_id": staff, "first_day": "2026-11-10", "last_day": "2026-11-10", "kind": "prefer_off", "points": 2,
    })
    h.command("coordinator", "availability.record", {
        "person_id": staff, "start": "2026-10-06T00:00:00+03:00", "end": "2026-10-07T00:00:00+03:00",
        "private_reason": "Synthetic time away", "kind": "off_duty",
    })
    event_row(h)
    bulletin = notice(h, {"roles": ["staff"]}, importance="must_acknowledge")
    seen(h, [bulletin["id"]])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, colleague).work_phone = "5550100"
        mobile = db.scalar(select(m.ContactType).where(m.ContactType.department_id == h.seed["department_id"],
                                                       m.ContactType.legacy_field == "work_phone"))
        mobile.visibility = {"mode": "roles", "roles": ["coordinator"]}

    clients = {}
    for role in (*s.ROLE_NAMES, "unlinked_staff"):
        if role not in h.seed["identities"]:
            identity(h, role, ["staff" if role == "unlinked_staff" else role])
        clients[role] = h.client(role)

    routes = {route.path: route for route in h.app.routes if isinstance(route, APIRoute) and route.path in READ_MODELS}
    assert {path: route.response_model for path, route in routes.items()} == READ_MODELS
    reads = [
        ("/api/calendar", DATES),
        ("/api/schedule/sessions", DATES),
        ("/api/content", {}),
        ("/api/requests/all", {}),
        ("/api/requests/all", {"id": leave_id}),
        ("/api/directory", {}),
    ]
    calendar_model = s.TypeAdapter(s.CalendarResponse)
    modeled = {}
    for role, client in clients.items():
        for index, (path, params) in enumerate(reads):
            response = client.get(path, params=params)
            assert response.status_code in {200, 403, 404}, (role, path, response.text)
            modeled[role, index] = response.status_code, response.json()

    with monkeypatch.context() as raw:
        # Keep the same router, dependencies and actor. Remove only the response serialization boundary.
        raw.setattr(s.CalendarResponse, "model_validate", lambda value: value)
        for route in routes.values():
            raw.setattr(route, "response_field", None)
            raw.setattr(route, "app", request_response(route.get_route_handler()))
        for role, client in clients.items():
            for index, (path, params) in enumerate(reads):
                response = client.get(path, params=params)
                status, body = modeled[role, index]
                assert response.status_code == status, (role, path, response.text)
                if status == 200:
                    assert response.json() == body, (role, path)
                    if path == "/api/calendar":
                        projected = calendar_model.validate_python(body)
                        assert projected.model_dump(mode="json", exclude_unset=True) == body
                else:
                    assert response.json()["error"]["code"] == body["error"]["code"], (role, path)

    staff_calendar = modeled["staff", 0][1]
    assert {row["kind"] for row in staff_calendar["events"]} >= {"duty", "availability"}
    staff_list = modeled["staff", 3][1]
    assert {row["source"] for row in staff_list["items"]} == {"request", "availability", "pattern", "wish", "swap"}
    assert modeled["staff", 2][1]["items"][0]["receipt"]["read_at"]
    assert modeled["staff", 4][1]["history"]
    for role in ("staff", "coordinator"):
        entry = next(row for row in modeled[role, 5][1]["people"] if row["id"] == colleague)
        assert ("work_phone" in entry) == (role == "coordinator")
        assert any(contact["legacy_field"] == "work_phone" for contact in entry.get("contacts", ())) == (role == "coordinator")


def test_calendar_duty_id_filters_sessions_inside_the_viewers_scope(h, monkeypatch):
    freeze_clock(monkeypatch)
    staff, supervisor, unrelated = h.seed["people"]
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    participants = [
        {"person_id": staff, "start": start, "end": end, "participation": "trainee", "supervisor_person_id": supervisor},
        {"person_id": supervisor, "start": start, "end": end, "participation": "supervisor"},
        {"person_id": unrelated, "start": start, "end": end},
    ]
    rota = h.shift(h.workspace(), start, end, people=participants, typ=supervision_type(h))["workspace"]
    rota = h.shift(rota, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[unrelated])["workspace"]
    h.publish(rota)
    client = h.client("staff")
    calendar = client.get("/api/calendar", params=DATES).json()
    duty, = [row for row in calendar["events"] if row["kind"] == "duty"]
    assert duty["supervisor_person_id"] == supervisor
    window = client.get("/api/schedule/sessions", params=DATES)
    requested = {**DATES, "session_id": duty["shift_id"]}
    selected = client.get("/api/schedule/sessions", params=requested, headers={"If-None-Match": window.headers["etag"]})
    assert selected.status_code == 200, selected.text
    assert selected.headers["etag"] != window.headers["etag"]
    shown, = selected.json()["shifts"]
    assert shown["id"] == duty["shift_id"] != duty["id"]
    assert shown in window.json()["shifts"]
    assert {person["person_id"] for person in shown["participants"]} == {staff, supervisor}
    assert unrelated not in selected.json()["people"]
    assert client.get("/api/schedule/sessions", params=requested,
                      headers={"If-None-Match": selected.headers["etag"]}).status_code == 304
    assert client.get("/api/schedule/sessions", params={
        "start": "2026-10-05", "end": "2026-10-06", "session_id": duty["shift_id"],
    }).json()["shifts"] == []
    team = h.client("coordinator").get("/api/schedule/sessions", params=DATES).json()
    hidden, = [row for row in team["shifts"] if row["id"] != duty["shift_id"]]
    assert client.get("/api/schedule/sessions", params={**DATES, "session_id": hidden["id"]}).json()["shifts"] == []
