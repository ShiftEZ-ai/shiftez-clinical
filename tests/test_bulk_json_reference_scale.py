"""Creation Undo keeps its JSON guard bounded with five years of unrelated rotas."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import catalogue
from packages.domain.quotas import METRIC_VERSION
from tests.test_backend_scale import measure
from tests.test_performance_budget import python_budget


@pytest.fixture(scope="module", params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def json_reference_measurement(request, scale_harness):
    h = scale_harness(request.param)
    targets = [m.uid() for _ in range(500)]
    people = [m.uid() for _ in range(150)]
    shifts = [m.uid() for _ in range(500)]
    snapshots = []
    owner = h.seed["identities"]["coordinator"]
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Person(id=ident, organisation_id=h.seed["organisation_id"],
                           display_name=f"Synthetic scale colleague {n}") for n, ident in enumerate(people))
        db.add_all(m.Service(id=ident, department_id=h.seed["department_id"],
                            name=f"Synthetic Undo candidate {n}", starts_on=date(2020, 1, 1))
                   for n, ident in enumerate(targets))
        for month in range(60):
            start = date(2020 + month // 12, month % 12 + 1, 1)
            end = date(2020 + (month + 1) // 12, (month + 1) % 12 + 1, 1)
            workspace = m.Workspace(id=m.uid(), department_id=h.seed["department_id"],
                name=f"Synthetic historical month {month}", starts_on=start, ends_on=end,
                created_by=owner, last_edited_by=owner, status="draft" if month == 0 else "published")
            db.add(workspace)
            db.flush()
            snapshot = {"people": {ident: {"id": ident, "display_name": f"Synthetic colleague {n}"}
                                   for n, ident in enumerate(people)}, "shifts": []}
            for n, ident in enumerate(shifts):
                began = datetime.combine(start + timedelta(days=n % 28), datetime.min.time(), timezone.utc)
                value = {"id": ident, "assignment_type_id": h.seed["type"],
                    "location_id": h.seed["locations"][0], "service_ids": [h.seed["services"][0]],
                    "start": began.isoformat(), "end": (began + timedelta(hours=4)).isoformat(),
                    "participants": [{"person_id": people[n % len(people)]}]}
                snapshot["shifts"].append(value)
                if month == 0:
                    db.add(m.Shift(id=ident, workspace_id=workspace.id, assignment_type_id=h.seed["type"],
                        type_revision=1, type_snapshot={"name": "Synthetic historical duty"},
                        location_id=h.seed["locations"][0], start=began, end=began + timedelta(hours=4)))
            db.flush()
            workspace.status = "published"
            snapshots.append(snapshot)
            db.add(m.Publication(department_id=h.seed["department_id"], workspace_id=workspace.id,
                sequence=month + 1, starts_on=start, ends_on=end, current=False, published_by=owner,
                snapshot=snapshot, validation={"contracts": {
                    "block_quota": METRIC_VERSION, "programmes": "programmes-v1"}}, fingerprint="0" * 64))
    decoded = {"values": 0, "snapshots": 0}
    with Session(h.app.state.engine, autoflush=False) as db, pytest.MonkeyPatch.context() as patch:
        rows = list(db.scalars(select(m.Service).where(m.Service.id.in_(targets))))
        db.info["bulk"] = {"creation_undo_targets": {m.Service: set(targets)}}
        original = catalogue._json_strings
        def strings(value):
            decoded["values"] += 1
            decoded["snapshots"] += int(isinstance(value, dict) and "shifts" in value and "people" in value)
            return original(value)
        patch.setattr(catalogue, "_json_strings", strings)
        _, metrics = measure(h, "creation-undo-json-500", lambda: [
            catalogue._require_json_unreferenced(db, row) for row in rows])
        references = db.info["bulk"]["creation_json_references"]
    metrics["decoded_json_values"] = decoded["values"]
    metrics["decoded_snapshots"] = decoded["snapshots"]
    metrics["history_bytes"] = sum(len(json.dumps(snapshot)) for snapshot in snapshots)
    print("creation-undo-json-history", metrics)
    return references, metrics


def test_department_creation_undo_json_work(json_reference_measurement):
    references, metrics = json_reference_measurement
    assert len(references) == 500 and not any(references.values())
    assert metrics["sql"] == {"SELECT": 1}, metrics
    assert metrics["decoded_snapshots"] == 60, metrics
    assert metrics["decoded_json_values"] < 4_000, metrics
    assert metrics["history_bytes"] > 10_000_000, metrics


@pytest.mark.cpu_budget
def test_department_creation_undo_json_cpu_budget(json_reference_measurement):
    _, metrics = json_reference_measurement
    assert metrics["cpu_seconds"] < python_budget(2), metrics


@pytest.mark.wall_benchmark
def test_department_creation_undo_json_wall_benchmark(json_reference_measurement):
    _, metrics = json_reference_measurement
    assert metrics["wall_seconds"] < python_budget(6), metrics
