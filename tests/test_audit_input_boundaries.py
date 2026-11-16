"""Malformed numeric and offset boundaries must use the normal validation response."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


ID = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400), float("inf"), float("-inf"), float("nan")])
def test_custom_number_outside_representable_range_is_validation_error(value):
    definition = s.TypeDefinition(fields=[s.CustomField(key="number", label="Capacity", kind="number")])
    with pytest.raises(ValueError, match="Capacity: number outside allowed range"):
        s.validate_attributes(definition, {"number": value})


@pytest.mark.parametrize("value", [0, 10 ** 300, -(10 ** 300), 1.25])
def test_finite_custom_number_preserves_value(value):
    definition = s.TypeDefinition(fields=[s.CustomField(key="number", label="Capacity", kind="number")])
    assert s.validate_attributes(definition, {"number": value}) == {"number": value}


def test_conditional_numeric_field_uses_model_validation_for_oversized_value():
    with pytest.raises(ValidationError) as raised:
        s.TypeDefinition(fields=[
            s.CustomField(key="number", label="Capacity", kind="number"),
            s.CustomField(key="detail", label="Details", kind="text",
                          required_when={"field": "number", "equals": 10 ** 400}),
        ])
    assert all(error["loc"][:2] == ("required_when", "equals") for error in raised.value.errors())


@pytest.mark.parametrize("model,payload,field", [
    (s.EventInput, {"event_type_id": ID, "heading": "Boundary event", "place": "Synthetic room",
                    "start": "2026-10-04T08:00:00Z", "end": "2026-10-04T09:00:00Z"}, "start"),
    (s.EventInput, {"event_type_id": ID, "heading": "Boundary event", "place": "Synthetic room",
                    "start": "2026-10-04T08:00:00Z", "end": "2026-10-04T09:00:00Z"}, "end"),
    (s.ReviewDelegationInput, {"route_id": ID, "step_key": "approval", "delegate_id": ID,
                              "starts_at": "2026-10-04T08:00:00Z", "expires_at": "2026-10-05T08:00:00Z",
                              "reason": "Synthetic review"}, "starts_at"),
    (s.ReviewDelegationInput, {"route_id": ID, "step_key": "approval", "delegate_id": ID,
                              "starts_at": "2026-10-04T08:00:00Z", "expires_at": "2026-10-05T08:00:00Z",
                              "reason": "Synthetic review"}, "expires_at"),
    (s.SwapOfferInput, {"publication_id": ID, "assignment_id": ID, "reason": "Synthetic offer",
                       "expires_at": "2026-10-05T08:00:00Z"}, "expires_at"),
    (s.InstantInterval, {"start": "2026-10-04T08:00:00Z", "end": "2026-10-04T09:00:00Z"}, "start"),
])
@pytest.mark.parametrize("instant", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_unrepresentable_utc_instant_has_field_error(model, payload, field, instant):
    with pytest.raises(ValidationError) as raised:
        model.model_validate({**payload, field: instant})
    errors = raised.value.errors()
    assert errors[0]["loc"] == (field,)
    assert "representable in UTC" in errors[0]["msg"]


@pytest.mark.parametrize("instant,expected", [
    ("0001-01-01T01:00:00+01:00", datetime.min.replace(tzinfo=timezone.utc)),
    ("9999-12-31T22:59:59-01:00", datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
    ("2026-10-04T08:00:00+03:00", datetime(2026, 10, 4, 5, tzinfo=timezone.utc)),
])
def test_representable_offset_boundary_is_normalized(instant, expected):
    value = s.SwapOfferInput(publication_id=ID, assignment_id=ID, expires_at=instant, reason="Synthetic offer")
    assert value.expires_at == expected
    assert value.expires_at.tzinfo is timezone.utc


def _post(h, command, key):
    authenticated = h.client("coordinator")
    with TestClient(h.app, base_url=h.settings.public_origin, raise_server_exceptions=False) as client:
        client.cookies.update(authenticated.cookies)
        return client.post("/api/commands", headers={
            "origin": h.settings.public_origin,
            "x-csrf-token": authenticated.cookies["rota_csrf"],
            "idempotency-key": key,
        }, json=command)


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400)])
def test_oversized_shift_number_returns_422_and_rolls_back(h, value):
    typ = h.configure("assignment_types", {"name": "Boundary numbers", "starts_on": "2020-01-01",
        "definition": {"fields": [{"key": "number", "label": "Capacity", "kind": "number"}]}})["record"]
    workspace = h.workspace()
    key = str(uuid4())
    response = _post(h, {"kind": "shift.create", "expected_revision": workspace["revision"], "payload": {
        "workspace_id": workspace["id"], "assignment_type_id": typ["id"], "participants": [],
        "service_ids": [h.seed["services"][0]], "location_id": h.seed["locations"][0],
        "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00",
        "attributes": {"number": value},
    }}, key)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == {"code": "VALUE_INVALID", "message": "Capacity: number outside allowed range"}
    reloaded = h.detail(workspace)
    assert reloaded["shifts"] == []
    assert reloaded["workspace"]["revision"] == workspace["revision"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(m.CommandReceipt.key == key)) == 0


@pytest.mark.parametrize("instant,field", [
    ("0001-01-01T00:00:00+01:00", "start"),
    ("9999-12-31T23:59:59-01:00", "end"),
])
def test_event_utc_overflow_returns_422_field_error_and_no_write(h, instant, field):
    value = {"event_type_id": ID, "heading": "Invalid UTC boundary", "place": "Synthetic room",
             "start": "2026-10-04T08:00:00Z", "end": "2026-10-04T09:00:00Z", field: instant}
    key = str(uuid4())
    with transaction(h.app.state.engine) as db:
        before = db.scalar(select(func.count()).select_from(m.Event))
    response = _post(h, {"kind": "catalogue.create", "payload": {
        "catalogue": "events", "reason": "Synthetic audit", "value": value,
    }}, key)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "SCHEMA_INVALID"
    assert error["details"][0]["field"] == field
    assert "representable in UTC" in error["details"][0]["message"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Event)) == before
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(m.CommandReceipt.key == key)) == 0
