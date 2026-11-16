"""Condition previews share bounded reads; ordinary filters load only calendar configuration."""
from datetime import date, timedelta
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.services import condition_context
from packages.domain.conditions import resolve_operand
from tests.test_bulk_selection import actor
from .clock_support import freeze_clock


ENDPOINTS = ("catalogue", "resolve-operand", "resolve-operands", "parse-date")


def call(client, endpoint):
    path = "/api/conditions/" + endpoint
    if endpoint == "catalogue":
        return client.get(path)
    item = {"field": "date", "operand": {"kind": "relative", "token": "today"}}
    body = {"operands": [item]} if endpoint == "resolve-operands" else {"text": "today"} if endpoint == "parse-date" else item
    return client.post(path, json=body)


def set_limit(h, rate):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"rate_bulk_preview_per_minute": rate}


def blocked(response):
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert 1 <= int(response.headers["retry-after"]) <= 60


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_condition_endpoints_share_department_bulk_limit_and_cannot_switch_to_bypass(h, monkeypatch, endpoint):
    moment = freeze_clock(monkeypatch)
    set_limit(h, 1)
    client = h.client("coordinator")
    accepted = call(client, endpoint)
    assert accepted.status_code == 200, accepted.text
    for other in ENDPOINTS:
        blocked(call(client, other))
    blocked(client.post("/api/bulk/resolve", json={"collection": "people"}))
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(client.cookies["rota_session"])))
        key = digest(f"bulk-resolve:{session.id}:{int(moment.timestamp()) // 60 * 60}")
        assert db.get(m.RateLimit, key).hits == 6  # Rejected requests count, too.
    assert call(h.client("approver"), endpoint).status_code == 200
    freeze_clock(monkeypatch, moment + timedelta(seconds=61))
    assert call(client, endpoint).status_code == 200


def test_zero_department_limit_disables_the_shared_condition_and_bulk_read_cap(h):
    object.__setattr__(h.settings, "rate_bulk_preview_per_minute", 1)
    set_limit(h, 0)
    client = h.client("coordinator")
    for _ in range(2):
        for endpoint in ENDPOINTS:
            response = call(client, endpoint)
            assert response.status_code == 200, response.text
        assert client.post("/api/bulk/resolve", json={"collection": "people"}).status_code == 200


def test_guard_keeps_authentication_csrf_and_safe_get_behavior(h, monkeypatch):
    freeze_clock(monkeypatch)
    set_limit(h, 1)
    with TestClient(h.app, base_url=h.settings.public_origin) as anonymous:
        anonymous.headers["origin"] = h.settings.public_origin
        for endpoint in ENDPOINTS:
            assert call(anonymous, endpoint).status_code == 401
    client = h.client("coordinator")
    del client.headers["x-csrf-token"]
    for endpoint in ENDPOINTS[1:]:
        assert call(client, endpoint).status_code == 403
    assert call(client, "catalogue").status_code == 200
    blocked(call(client, "catalogue"))


def test_semantically_invalid_operands_consume_the_read_budget(h, monkeypatch):
    freeze_clock(monkeypatch)
    set_limit(h, 1)
    client = h.client("staff")
    refused = client.post("/api/conditions/resolve-operand", json={"field": "person_ids",
        "operand": {"kind": "relative", "token": "people_i_approve"}})
    assert refused.status_code == 422, refused.text
    blocked(call(client, "parse-date"))


def no_viewer_loads(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Ordinary conditions must not load viewer relationships or numeric aggregates")
    monkeypatch.setattr(condition_context, "held_roles", unexpected)
    monkeypatch.setattr(condition_context, "_metrics", unexpected)


@pytest.mark.parametrize("line", [
    {"field": "date", "op": "is", "value": "2026-10-03"},
    {"field": "date", "op": "within", "value": {"kind": "relative", "token": "this_week"}},
    {"field": "status", "op": "empty"},
])
def test_filtered_collection_reads_skip_viewer_facts_for_absolute_empty_and_calendar_values(h, monkeypatch, line):
    no_viewer_loads(monkeypatch)
    for path, role in (("/api/requests/all", "coordinator"), ("/api/operations", "operator"), ("/api/audit", "auditor")):
        local_line = {**line, "field": "error" if role == "operator" else "reason"} if line["field"] == "status" else line
        if role == "coordinator":
            local_line = {**line, "field": "start"} if line["field"] == "date" else line
        response = h.client(role).get(path, params={"filter": json.dumps({"conditions": {"conditions": [local_line]},
            "as_of": "2026-10-03"})})
        assert response.status_code == 200, response.text
    bulk_line = {**line, "field": "note"} if line["field"] == "status" else line
    response = h.client("coordinator").post("/api/bulk/resolve", json={"collection": "people",
        "where": {"conditions": {"conditions": [bulk_line]}, "as_of": "2026-10-03"}})
    assert response.status_code == 200, response.text


def test_absolute_previews_parser_and_me_need_no_viewer_relationships_or_aggregates(h, monkeypatch):
    no_viewer_loads(monkeypatch)
    client = h.client("staff")
    absolute = {"field": "capacity", "operand": 2, "collection": "locations"}
    assert client.post("/api/conditions/resolve-operand", json=absolute).status_code == 403
    response = h.client("coordinator").post("/api/conditions/resolve-operand", json=absolute)
    assert response.status_code == 200, response.text
    response = client.post("/api/conditions/resolve-operands", json={"operands": [
        {"field": "date", "operand": "2026-10-03"}, {"field": "person_ids", "operand": {"kind": "relative", "token": "me"}},
    ]})
    assert response.status_code == 200, response.text
    with transaction(h.app.state.engine) as db:
        person = db.get(m.Identity, h.seed["identities"]["staff"]).person_id
    assert response.json()["results"][1]["ids"] == [person]
    response = call(client, "parse-date")
    assert response.status_code == 200 and response.json()["matches"]


def test_light_context_cache_does_not_mask_later_viewer_validation_and_is_reused(h, monkeypatch):
    calls = []
    original = condition_context.held_roles
    def roles(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(condition_context, "held_roles", roles)
    with transaction(h.app.state.engine) as db:
        viewer = actor(db, h, "staff")
        light = condition_context.evaluation_context(db, viewer, tokens=(), as_of=date(2026, 10, 3))
        assert not calls
        assert condition_context.evaluation_context(db, viewer, tokens=(), as_of=date(2026, 10, 3)) is light
        relative = condition_context.evaluation_context(db, viewer, tokens={"people_i_approve"}, as_of=date(2026, 10, 3))
        assert calls == [1]
        with pytest.raises(ValueError, match="not meaningful"):
            resolve_operand({"kind": "relative", "token": "people_i_approve"}, relative, "person_ids")
        assert condition_context.evaluation_context(db, viewer, tokens={"people_i_approve"}, as_of=date(2026, 10, 3)) is relative
        assert condition_context.evaluation_context(db, viewer, as_of=date(2026, 10, 3)).values == relative.values
        assert calls == [1]


def test_lazy_numeric_and_viewer_contexts_preserve_aggregate_and_effective_day(h, monkeypatch):
    client = h.client("staff")
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Identity, h.seed["identities"]["staff"]).person_id
        clinic = m.Service(department_id=h.seed["department_id"], name="Dated guard fixture clinic", starts_on=date(2020, 1, 1))
        db.add(clinic)
        db.flush()
        clinic_id = clinic.id
        db.add(m.Eligibility(department_id=h.seed["department_id"], person_id=person, service_id=clinic.id,
            starts_on=date(2026, 10, 3), ends_on=date(2026, 10, 4)))
    roles = condition_context.held_roles
    def unexpected(*args, **kwargs):
        raise AssertionError("Numeric resolution does not need viewer relationships")
    monkeypatch.setattr(condition_context, "held_roles", unexpected)
    response = h.client("coordinator").post("/api/conditions/resolve-operand", json={"field": "capacity", "collection": "locations",
        "operand": {"kind": "relative", "token": "department_average"}})
    assert response.status_code == 200 and isinstance(response.json()["value"], (int, float)), response.text
    monkeypatch.setattr(condition_context, "held_roles", roles)
    for day, includes in (("2026-10-03", True), ("2026-10-04", False)):
        response = client.post("/api/conditions/resolve-operand", json={"field": "service_ids", "as_of": day,
            "operand": {"kind": "relative", "token": "my_clinics"}})
        assert response.status_code == 200, response.text  # Seed permissions remain meaningful after fixture expiry.
        assert (clinic_id in response.json()["value"]) == includes
