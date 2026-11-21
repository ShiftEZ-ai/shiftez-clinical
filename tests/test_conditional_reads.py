"""Revalidation saves response bytes while always applying current access and row projection."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.requests import Request

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import conditional_reads
from apps.api.services import bulk_selection
from packages.contracts import schemas as s
from .clock_support import freeze_clock
from .test_backend_scale import measure, scale_department


ACTOR = SimpleNamespace(identity_id="reader", department_id="department", person_id=None, roles={"coordinator"})


def request(method="GET", headers=()):
    return Request({"type": "http", "method": method, "headers": headers})


@pytest.mark.parametrize("known", ["tag", "strong", "list", "multiple", "*"])
def test_if_none_match_uses_weak_comparison_and_returns_no_body(known):
    first = conditional_reads.conditional_json(request(), {"rows": ["unchanged"]}, ACTOR)
    tag = first.headers["etag"]
    values = {"tag": [tag], "strong": [tag.removeprefix("W/")], "list": ['"old", ' + tag],
              "multiple": ['"old"', tag], "*": ["*"]}[known]
    response = conditional_reads.conditional_json(request(headers=[(b"if-none-match", v.encode()) for v in values]),
                                                   {"rows": ["unchanged"]}, ACTOR)
    assert response.status_code == 304 and response.body == b""
    assert response.headers["etag"] == tag
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie, Accept-Encoding"


def test_conditional_json_serializes_once_and_binds_current_reader_scope(monkeypatch):
    renders = []
    render = conditional_reads.JSONResponse.render
    def counted(self, value):
        renders.append(value)
        return render(self, value)
    monkeypatch.setattr(conditional_reads.JSONResponse, "render", counted)
    first = conditional_reads.conditional_json(request(), {"rows": ["same"]}, ACTOR)
    assert renders == [{"rows": ["same"]}]
    assert json.loads(first.body) == {"rows": ["same"]}
    for change in ({"identity_id": "other"}, {"department_id": "other"}, {"person_id": "other"}, {"roles": {"auditor"}}):
        actor = SimpleNamespace(**{**vars(ACTOR), **change})
        current = conditional_reads.conditional_json(request(headers=[(b"if-none-match", first.headers["etag"].encode())]),
                                                       {"rows": ["same"]}, actor)
        assert current.status_code == 200 and current.headers["etag"] != first.headers["etag"]
    post = conditional_reads.conditional_json(request("POST", [(b"if-none-match", first.headers["etag"].encode())]),
                                               {"rows": ["same"]}, ACTOR)
    assert post.status_code == 412 and post.body == b""


def test_calendar_revalidates_current_rows_and_checks_access_first(h):
    workspace = h.shift(h.workspace())["workspace"]
    params = {"start": "2026-10-04", "end": "2026-10-05", "workspace_id": workspace["id"]}
    client = h.client("coordinator")
    first = client.get("/api/calendar", params=params)
    assert first.status_code == 200 and first.json()["sessions"]
    identity = client.get("/api/calendar", params=params, headers={"accept-encoding": "identity"})
    assert identity.content == first.content and identity.headers["etag"] == first.headers["etag"]
    assert first.headers["etag"].startswith('W/"')
    headers = {"if-none-match": first.headers["etag"]}
    repeat = client.get("/api/calendar", params=params, headers=headers)
    assert repeat.status_code == 304 and repeat.content == b""
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, h.seed["locations"][0]).name = "Changed calendar place"
    changed = client.get("/api/calendar", params=params, headers=headers)
    assert changed.status_code == 200 and changed.headers["etag"] != first.headers["etag"]
    assert changed.json()["sessions"][0]["location_name"] == "Changed calendar place"
    assert h.client("credentialer").get("/api/calendar", params=params, headers=headers).status_code == 403
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).disabled = True
    assert client.get("/api/calendar", params=params, headers=headers).status_code == 401


def test_bulk_get_reuses_post_contract_and_keeps_conditional_methods_correct(h):
    client = h.client("coordinator")
    value = {"selection": {"collection": "people"}, "columns": ["display_name"], "limit": 1}
    posted = client.post("/api/bulk/rows", json=value)
    assert posted.status_code == 200
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies["rota_session"], settings=h.settings)
        expected = bulk_selection.rows(db, actor, s.BulkRowsInput.model_validate(value))
    assert posted.content == JSONResponse(jsonable_encoder(expected)).body
    params = {"query": json.dumps(value)}
    headers = {"if-none-match": posted.headers["etag"]}
    assert client.post("/api/bulk/rows", json=value, headers=headers).status_code == 412
    del client.headers["x-csrf-token"]
    current = client.get("/api/bulk/rows", params=params)
    assert current.status_code == 200 and current.json() == posted.json()
    assert current.headers["etag"] == posted.headers["etag"]
    repeated = client.get("/api/bulk/rows", params=params, headers=headers)
    assert repeated.status_code == 304 and repeated.content == b""
    with TestClient(h.app, base_url=h.settings.public_origin) as anonymous:
        assert anonymous.get("/api/bulk/rows", params=params, headers=headers).status_code == 401
    assert h.client("credentialer").get("/api/bulk/rows", params={"query": json.dumps({
        "selection": {"collection": "sessions"}})}, headers=headers).status_code == 403
    assert client.post("/api/bulk/rows", json=value, headers=headers).status_code == 403
    for invalid in ("not json", json.dumps({"selection": {"collection": "people"}, "limit": 0})):
        assert client.get("/api/bulk/rows", params={"query": invalid}).status_code == 422
    changed = client.get("/api/bulk/rows", params={"query": json.dumps({**value, "offset": 1})}, headers=headers)
    assert changed.status_code == 200 and changed.headers["etag"] != current.headers["etag"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["coordinator"]).disabled = True
    assert client.get("/api/bulk/rows", params=params, headers=headers).status_code == 401


def test_bulk_get_revalidation_keeps_the_current_reader_rate_limit(h, monkeypatch):
    freeze_clock(monkeypatch)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"rate_bulk_preview_per_minute": 1}
    client = h.client("coordinator")
    params = {"query": json.dumps({"selection": {"collection": "people"}, "limit": 1})}
    first = client.get("/api/bulk/rows", params=params)
    assert first.status_code == 200
    limited = client.get("/api/bulk/rows", params=params, headers={"if-none-match": first.headers["etag"]})
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "RATE_LIMITED"


def test_department_scale_conditional_reads_have_zero_response_byte_budget(scale_harness):
    # Byte and serialization counts are deterministic under CI and coverage instrumentation.
    h = scale_harness()
    workspace, data, *_ = scale_department(h)
    assert len(data["people"]) == 163 and len(data["focus_ids"]) == 768
    client = h.client("coordinator")
    paths = [("/api/calendar", {"start": "2026-10-04", "end": "2026-11-01", "workspace_id": workspace["id"]}),
             ("/api/bulk/rows", {"query": json.dumps({"selection": {"collection": "sessions"}, "limit": 500})})]
    observations = []
    for path, params in paths:
        first = client.get(path, params=params)
        assert first.status_code == 200 and len(first.content) > 1_000_000
        repeat = client.get(path, params=params, headers={"if-none-match": first.headers["etag"]})
        assert repeat.status_code == 304 and len(repeat.content) == 0
        observations.append((path, len(first.content), len(repeat.content)))
    print("Conditional byte budgets:", observations)

    first = client.get("/api/requests/all")
    assert first.status_code == 200
    repeat, metrics = measure(h, "requests-validator", lambda: client.get(
        "/api/requests/all", headers={"if-none-match": first.headers["etag"]}))
    assert repeat.status_code == 304 and repeat.content == b""
    assert metrics["sql"]["SELECT"] <= 12, metrics
