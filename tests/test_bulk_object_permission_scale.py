"""Receipt batching keeps every current and historical object boundary at department scale."""
from datetime import date, datetime, timezone
import json
from time import perf_counter, process_time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select

from apps.api import commands, models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import availability, training
from apps.api.services.common import scoped
from tests.test_performance_budget import python_budget


def receipt(replies):
    return SimpleNamespace(id=m.uid(), command_kind="bulk.apply", required_roles=["coordinator"], result={
        "authorization_commands": [reply["authorization_command"] for reply in replies],
        "items": [{"results": [reply]} for reply in replies]})


def measured(db, operation):
    queries = []
    def query(*args):
        queries.append(args[2])
    connection = db.connection()
    event.listen(connection, "before_cursor_execute", query)
    cpu, wall = process_time(), perf_counter()
    try:
        operation()
    finally:
        metrics = {"sql": len(queries), "cpu_seconds": process_time() - cpu,
                   "wall_seconds": perf_counter() - wall}
        event.remove(connection, "before_cursor_execute", query)
    return metrics


@pytest.fixture(scope="module", params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def object_receipts(request, scale_harness):
    h = scale_harness(request.param)
    department = h.seed["department_id"]
    owner, submitter = (h.seed["identities"][role] for role in ("admin", "staff"))
    programme, other_programme, cohort, other_cohort = [m.uid() for _ in range(4)]
    with transaction(h.app.state.engine, write=True) as db:
        organisation = db.get(m.Department, department).organisation_id
        db.execute(insert(m.Programme), [{"id": programme, "department_id": department,
            "name": "Synthetic led programme", "starts_on": date(2026, 1, 1), "owner_identity_id": owner},
            {"id": other_programme, "department_id": department, "name": "Synthetic other programme",
             "starts_on": date(2026, 1, 1), "owner_identity_id": submitter}])
        db.execute(insert(m.Cohort), [{"id": key, "department_id": department, "name": "Synthetic cohort",
            "programme_id": pid, "starts_on": date(2026, 1, 1), "ends_on": date(2027, 1, 1)}
            for key, pid in ((cohort, programme), (other_cohort, other_programme))])
        rotations = [{"id": m.uid(), "cohort_id": cohort, "revision": 2} for _ in range(500)]
        db.execute(insert(m.DefinitionHistory), [{"department_id": department, "kind": "rotations",
            "object_id": row["id"], "object_revision": revision, "snapshot": {"programme_id": programme},
            "actor_id": owner, "reason": "Synthetic retained rotation authority"}
            for row in rotations for revision in (1, 2)])
        history_cases = {boundary: m.uid() for boundary in ("history", "previous_history")}
        db.execute(insert(m.DefinitionHistory), [{"department_id": department, "kind": "rotations",
            "object_id": key, "object_revision": revision,
            "snapshot": {"programme_id": other_programme if revision == denied_revision else programme},
            "actor_id": owner, "reason": "Synthetic historical programme transfer"}
            for boundary, key in history_cases.items()
            for denied_revision in [1 if boundary == "previous_history" else 2] for revision in (1, 2)])
        requests = [{"id": m.uid(), "department_id": department, "person_id": h.seed["people"][0],
            "kind": "overtime_availability" if index % 2 else "leave", "status": "approved",
            "start": datetime(2026, 10, 4, 8, tzinfo=timezone.utc),
            "end": datetime(2026, 10, 4, 12, tzinfo=timezone.utc), "private_reason": "",
            "created_by": submitter} for index in range(500)]
        db.execute(insert(m.RequestRecord), requests)
    lead = Actor(owner, department, organisation, "Synthetic", frozenset({"programme_lead"}), None, "session")
    decider = Actor(owner, department, organisation, "Synthetic", frozenset({"coordinator", "approver"}), None, "session")
    rotation_replies = [{"authorization_command": {"kind": "catalogue.update", "payload": {"catalogue": "rotations"}},
        **({"record": row, "previous_value": {"cohort_id": cohort}} if index % 2 else
           {"records": [row], "previous_values": [{"cohort_id": cohort}]})} for index, row in enumerate(rotations)]
    request_replies = [{"authorization_command": {"kind": "request.decide", "payload": {"decision": "approve"}},
                        "record": row} for row in requests]
    return SimpleNamespace(h=h, lead=lead, decider=decider, programme=programme, other_programme=other_programme,
        cohort=cohort, other_cohort=other_cohort, rotations=rotations, requests=requests,
        rotation_receipt=receipt(rotation_replies), request_receipt=receipt(request_replies), history_cases=history_cases)


@pytest.fixture(scope="module")
def object_permission_measurement(object_receipts):
    value = object_receipts
    with transaction(value.h.app.state.engine) as db:
        # Execute the original per-result authority operations on identical inputs for comparison.
        rotation_before = measured(db, lambda: [training.rotation_records_access(db, value.lead,
            [row, {**row, "revision": 1}]) for row in value.rotations])
        rotation_after = measured(db, lambda: commands.authorize_receipt(db, value.lead, value.rotation_receipt))
        def request_before():
            for item in value.requests:
                row = scoped(db, m.RequestRecord, item["id"], value.decider)
                assert availability.may_decide(db, value.decider, row)
        request_before_metrics = measured(db, request_before)
        request_after = measured(db, lambda: commands.authorize_receipt(db, value.decider, value.request_receipt))
    metrics = {"rotation_before": rotation_before, "rotation_after": rotation_after,
               "request_before": request_before_metrics, "request_after": request_after}
    print("bulk-object-permission-scale", value.h.app.state.engine.dialect.name, json.dumps(metrics, sort_keys=True))
    return metrics


def test_500_sensitive_receipt_checks_use_bounded_authority_queries(object_permission_measurement):
    metrics = object_permission_measurement
    assert metrics["rotation_before"]["sql"] >= 1500, metrics
    assert metrics["request_before"]["sql"] >= 1000, metrics
    assert metrics["rotation_after"]["sql"] <= 8, metrics
    assert metrics["request_after"]["sql"] <= 2, metrics


@pytest.mark.cpu_budget
def test_500_sensitive_receipt_checks_cpu_budget(object_permission_measurement):
    for phase in ("rotation_after", "request_after"):
        assert object_permission_measurement[phase]["cpu_seconds"] < python_budget(0.75), object_permission_measurement


@pytest.mark.parametrize("boundary", ["owner", "current_cohort", "previous_cohort", "history", "previous_history"])
def test_rotation_receipt_rechecks_current_and_historical_authority(object_receipts, boundary):
    import copy
    value = object_receipts
    source = copy.deepcopy(value.rotation_receipt)
    with transaction(value.h.app.state.engine, write=True) as db:
        if boundary == "owner":
            db.get(m.Programme, value.programme).owner_identity_id = None
        elif boundary in {"current_cohort", "previous_cohort"}:
            reply = source.result["items"][-1]["results"][0]
            reply["record" if boundary == "current_cohort" else "previous_value"]["cohort_id"] = value.other_cohort
        else:
            source.result["items"][-1]["results"][0]["record"]["id"] = value.history_cases[boundary]
        db.flush()
        with pytest.raises(Problem) as caught:
            commands.authorize_receipt(db, value.lead, source)
        assert caught.value.status == 403
        db.rollback()


@pytest.mark.parametrize("boundary,status", [("wrong_kind", 403), ("missing", 404), ("department", 404)])
def test_request_receipt_rechecks_every_row_scope(object_receipts, boundary, status):
    import copy
    value = object_receipts
    source = copy.deepcopy(value.request_receipt)
    coordinator = Actor(value.decider.identity_id, value.decider.department_id, value.decider.organisation_id,
                        "Synthetic", frozenset({"coordinator"}), None, "session")
    # All rows except the final forbidden/missing row belong to the coordinator's overtime authority.
    with transaction(value.h.app.state.engine, write=True) as db:
        for row in db.scalars(select(m.RequestRecord).where(m.RequestRecord.id.in_([item["id"] for item in value.requests]))):
            row.kind = "overtime_availability"
        final = value.requests[-1]
        if boundary == "wrong_kind":
            db.get(m.RequestRecord, final["id"]).kind = "leave"
        elif boundary == "missing":
            source.result["items"][-1]["results"][0]["record"]["id"] = m.uid()
        else:
            coordinator = Actor(coordinator.identity_id, m.uid(), coordinator.organisation_id,
                                "Synthetic", coordinator.roles, None, "session")
        db.flush()
        with pytest.raises(Problem) as caught:
            commands.authorize_receipt(db, coordinator, source)
        assert caught.value.status == status
        db.rollback()


def test_receipt_preserves_withdrawal_own_undo_and_pattern_boundaries(object_receipts):
    value = object_receipts
    own = Actor(value.decider.identity_id, value.decider.department_id, value.decider.organisation_id,
                "Synthetic", frozenset({"staff"}), None, "session")
    replies = [{"authorization_command": {"kind": "request.decide", "payload": {"decision": "withdraw"}},
                "record": {"id": m.uid(), "status": "withdrawn"}},
               {"authorization_command": {"kind": "request.undo", "payload": {}},
                "record": {"id": m.uid(), "status": "submitted", "created_by": own.identity_id}}]
    with transaction(value.h.app.state.engine, write=True) as db:
        # Historical receipt exemptions still need an existing, currently owned request.
        db.execute(insert(m.RequestRecord), [{**value.requests[index], **reply["record"],
            "created_by": own.identity_id} for index, reply in enumerate(replies)])
        commands.authorize_receipt(db, own, receipt(replies))
        pattern = {"authorization_command": {"kind": "request.undo", "payload": {}}, "batch_id": m.uid(),
                   "records": [{"id": m.uid(), "source": "pattern", "status": "active"}]}
        commands.authorize_receipt(db, value.decider, receipt(replies + [pattern]))
        approver = Actor(value.decider.identity_id, value.decider.department_id, value.decider.organisation_id,
                         "Synthetic", frozenset({"approver"}), None, "session")
        with pytest.raises(Problem) as caught:
            commands.authorize_receipt(db, approver, receipt([pattern]))
        assert caught.value.status == 403
        for reply in replies:
            db.get(m.RequestRecord, reply["record"]["id"]).created_by = value.h.seed["identities"]["staff"]
        db.flush()
        with pytest.raises(Problem) as caught:
            commands.authorize_receipt(db, own, receipt(replies))
        assert caught.value.status == 403
        missing = {**replies[0], "record": {"id": m.uid(), "status": "withdrawn"}}
        with pytest.raises(Problem) as caught:
            commands.authorize_receipt(db, own, receipt([missing]))
        assert caught.value.status == 404
        db.rollback()
