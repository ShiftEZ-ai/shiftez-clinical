"""Bulk role checks scale with distinct contexts while object authority stays per row."""
import json
from time import perf_counter, process_time
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import insert

from apps.api import commands, models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from packages.contracts import schemas as s
from tests.test_performance_budget import python_budget


def actor(*roles):
    return Actor("identity", "department", "organisation", "Synthetic", frozenset(roles), None, "session")


def receipt(contexts):
    return SimpleNamespace(command_kind="bulk.apply", required_roles=["coordinator"], result={
        "authorization_commands": contexts,
        "items": [{"results": [{"authorization_command": context}]} for context in contexts]})


def test_receipt_batches_500_contexts_without_skipping_object_checks(monkeypatch):
    context = {"kind": "catalogue.update", "payload": {"catalogue": "people"}}
    resolved, objects = [], []
    original = commands.command_roles
    monkeypatch.setattr(commands, "command_roles", lambda command: (resolved.append(command.kind), original(command))[1])
    monkeypatch.setattr(commands, "_authorize_result", lambda db, actor, kind, result, **kw: objects.append(result))
    commands.authorize_receipt(None, actor("coordinator"), receipt([context.copy() for _ in range(500)]))
    assert resolved == ["bulk.apply", "catalogue.update"]
    assert len(objects) == 500


@pytest.mark.parametrize("first,second,permission", [
    ({"catalogue": "people"}, {"catalogue": "services"}, "catalogue.update"),
    ({"decision": "withdraw"}, {"decision": "approve"}, "request.decide"),
])
def test_receipt_cache_preserves_each_permission_context(monkeypatch, first, second, permission):
    kind = "catalogue.update" if "catalogue" in first else "request.decide"
    monkeypatch.setitem(s.PERMISSIONS, "catalogue.department", ("coordinator",))
    monkeypatch.setitem(s.PERMISSIONS, "request.withdraw", ("coordinator",))
    monkeypatch.setitem(s.PERMISSIONS, permission, ("auditor",))
    monkeypatch.setattr(commands, "_authorize_result", lambda *args, **kwargs: None)
    source = receipt([{"kind": kind, "payload": first}])
    source.result["items"][0]["results"].append({"authorization_command": {"kind": kind, "payload": second}})
    with pytest.raises(Problem) as caught:
        commands.authorize_receipt(None, actor("coordinator"), source)
    assert caught.value.status == 403


def test_receipt_cache_still_validates_complete_context():
    context = {"kind": "catalogue.update", "payload": {"catalogue": "people"}}
    source = receipt([context])
    source.result["items"][0]["results"].append({"authorization_command": {**context, "unexpected": True}})
    with pytest.raises(ValidationError):
        commands.authorize_receipt(None, actor("coordinator"), source)


def measured(operation):
    resolved, objects = [], []
    original_roles, original_objects = commands.command_roles, commands._authorize_result
    with pytest.MonkeyPatch.context() as monkeypatch:
        def roles(command):
            resolved.append(command.kind)
            return original_roles(command)
        def authorize_object(*args, **kwargs):
            objects.append(args[2])
            return original_objects(*args, **kwargs)
        monkeypatch.setattr(commands, "command_roles", roles)
        monkeypatch.setattr(commands, "_authorize_result", authorize_object)
        cpu, wall = process_time(), perf_counter()
        result = operation()
        metrics = {"cpu_seconds": process_time() - cpu, "wall_seconds": perf_counter() - wall,
                   "role_resolutions": len(resolved), "object_checks": len(objects)}
    return result, metrics


@pytest.fixture(scope="module", params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def bulk_permission_measurement(request, scale_harness):
    h = scale_harness(request.param)
    tags = [{"id": str(uuid.uuid4()), "department_id": h.seed["department_id"], "name": f"Before {i}", "labels": ["people"]}
            for i in range(500)]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Tag), tags)
    h.client("coordinator")
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "tags"},
             "items": [{"target_id": tag["id"], "expected_revision": 1,
                        "changes": [{"field": "name", "value": f"After {i}"}]} for i, tag in enumerate(tags)],
             "reason": "Synthetic 500-row permission workload"}
    def preview():
        response = h.client("coordinator").post("/api/commands/preview", json={"kind": "bulk.apply", "payload": value})
        assert response.status_code == 200, response.text
        return response.json()
    check, preview_metrics = measured(preview)
    assert check["counts"]["ready"] == 500, check["items"][:1]
    key = str(uuid.uuid4())
    applied, apply_metrics = measured(lambda: h.command("coordinator", "bulk.apply", value, key=key))
    assert applied["counts"]["changed"] == 500
    recovered, receipt_metrics = measured(lambda: h.get("coordinator", "/api/commands/" + key))
    assert recovered["result"] == applied
    retried, retry_metrics = measured(lambda: h.command("coordinator", "bulk.apply", value, key=key))
    assert retried == applied
    metrics = dict(preview=preview_metrics, apply=apply_metrics, receipt=receipt_metrics, retry=retry_metrics)
    print("bulk-permission-scale", request.param, json.dumps(metrics, sort_keys=True))
    return h, key, value, metrics


def test_department_bulk_role_checks(bulk_permission_measurement, monkeypatch):
    h, key, value, metrics = bulk_permission_measurement
    assert metrics["preview"]["role_resolutions"] <= 8, metrics
    assert metrics["apply"]["role_resolutions"] <= 8, metrics
    assert metrics["receipt"]["role_resolutions"] == 2, metrics
    # Admission, receipt lookup and final current authority remain independent.
    assert metrics["retry"]["role_resolutions"] == 4, metrics
    assert metrics["receipt"]["object_checks"] == metrics["retry"]["object_checks"] == 500
    monkeypatch.setitem(s.PERMISSIONS, "catalogue.department", ("staff",))
    h.get("coordinator", "/api/commands/" + key, status=403)
    h.command("coordinator", "bulk.apply", value, key=key, status=403)


@pytest.mark.cpu_budget
def test_department_bulk_role_checks_cpu_budget(bulk_permission_measurement):
    _, _, _, metrics = bulk_permission_measurement
    budgets = dict(preview=12, apply=20, receipt=1.5, retry=1)
    for phase, measured in metrics.items():
        assert measured["cpu_seconds"] < python_budget(budgets[phase]), (phase, measured)
