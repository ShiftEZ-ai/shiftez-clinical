from copy import deepcopy
from datetime import date
from statistics import median
from time import perf_counter

import pytest
from sqlalchemy import insert, select

from apps.api import models as m, snapshots
from apps.api.auth import Actor
from apps.api.catalogue_snapshot_sources import capture_catalogue_sources, capture_issued_sources, catalogue_source_reader
from apps.api.db import transaction
from apps.api.services import read_projections, rule_catalogue
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from tests.test_backend_scale import scale_department


def _all_policy_rows(_db, department_ids, _binding_where):
    return m.Policy.department_id.in_(department_ids)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_snapshot_policy_row_count_at_department_scale(database, scale_harness, monkeypatch, record_property):
    h = scale_harness(database)
    workspace, _, _, _ = scale_department(h, assigned=False)
    department_id = h.seed["department_id"]
    with transaction(h.app.state.engine, write=True) as db:
        sample = db.scalar(select(m.Policy).where(m.Policy.department_id == department_id))
        db.execute(insert(m.Policy), [
            dict(department_id=department_id, name=f"Retired rule {index}", kind=sample.kind,
                 definition=sample.definition, active=False, status="retired", owner=sample.owner,
                 source=sample.source, starts_on=date(2018, 1, 1), ends_on=date(2019, 1, 1),
                 created_by=sample.created_by, approved_by=sample.approved_by)
            for index in range(1300)
        ])

    hydrated = []
    records_many = snapshots.records_many

    def count_policies(*args, **kwargs):
        result = records_many(*args, **kwargs)
        if "policies" in result:
            hydrated.append(len(result["policies"]))
        return result

    def measure(label):
        timings = []
        hydrated.clear()
        for _ in range(3):
            start = perf_counter()
            data = h.snapshot(workspace)
            timings.append(perf_counter() - start)
        record_property(f"{label}_seconds_median", median(timings))
        record_property(f"{label}_policy_rows", max(hydrated))
        assert len(data["people"]) == 163 and len(data["focus_ids"]) == 768

    monkeypatch.setattr(snapshots, "records_many", count_policies)
    measure("after")
    bounded = max(hydrated)
    assert bounded <= 20
    monkeypatch.setattr(snapshots, "_snapshot_policy_where", _all_policy_rows)
    measure("before")
    assert max(hydrated) == bounded + 1300


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_published_rule_pin_and_read_parity(h, monkeypatch):
    rule = h.policy({"kind": "min_rest", "hours": 11})
    expired = h.policy({"kind": "max_hours", "hours": 40, "window": "week"}, severity="advisory")
    h.command("approver", "policy.retire", {"reason": "Retire before issue"}, expired["id"], expired["revision"])
    draft = h.shift(h.workspace("Issued rule fixture"))["workspace"]
    draft = h.shift(draft, start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")["workspace"]
    workspace = h.publish(draft)["workspace"]
    wid = workspace["id"]
    issued = h.snapshot(workspace)

    def outputs(data):
        check = evaluate(data)
        return check["findings"], schedule_score(data, check, data["generation_options"])

    paths = (
        "/api/rules/catalogue?version=2&include_hidden=true",
        "/api/rules/catalogue/workload_limit/detail?version=2",
        "/api/rules/catalogue/workload_limit/history?version=2&page=1&limit=50",
        f"/api/workspaces/{wid}/rules-explained",
    )

    def read():
        rule_catalogue._catalogue_cache.clear()
        replies = [h.get("coordinator", path) for path in paths]
        for reply in replies:
            reply.pop("elapsed_ms", None)
        return replies

    def tokens():
        with monkeypatch.context() as previous:
            previous.setattr(snapshots, "_snapshot_policy_where", _all_policy_rows)
            old = h.snapshot(workspace)["fingerprint"]
        return old, h.snapshot(workspace)["fingerprint"]

    monkeypatch.setattr(read_projections, "cached_department",
                        lambda db, actor, kind, stamp, build, **kwargs: build())
    before = tokens()
    h.workspace("Unrelated draft", start="2026-12-06", end="2026-12-13")
    assert tokens() == before
    h.command("approver", "policy.retire", {"reason": "Retire the issued rule"}, rule["id"], rule["revision"])
    assert all(now != initial for now, initial in zip(tokens(), before, strict=True))

    bounded = h.snapshot(workspace)
    assert expired["id"] not in {row["id"] for row in bounded["policies"]}
    bounded_reads = read()
    with monkeypatch.context() as previous:
        previous.setattr(snapshots, "_snapshot_policy_where", _all_policy_rows)
        assert outputs(h.snapshot(workspace)) == outputs(bounded)
        assert read() == bounded_reads

    # Legacy publications can be read from workspace-less catalogue/bulk inputs.
    with monkeypatch.context() as legacy:
        legacy.setattr(snapshots, "_verified_baseline", lambda db, workspace: (None, None))
        engine = h.app.state.engine
        organisation = h.seed["organisation_id"]
        actor = Actor(h.seed["identities"]["admin"], h.seed["department_id"], organisation,
                      "Synthetic operator", frozenset({"admin", "coordinator"}), None, "synthetic")
        with transaction(engine, write=True) as db:
            row = db.get(m.Workspace, wid)
            issued_sources = capture_issued_sources(db, organisation, [row])
            trial = db.begin_nested()
            try:
                shared = snapshots.snapshot_inputs(db, db.get(m.Organisation, organisation))
                assert rule["id"] not in {policy["id"] for policy in shared["policies"]}
                legacy_issued = snapshots.domain_snapshot(db, row, shared_inputs=shared)
                captured = capture_catalogue_sources(db, organisation, [row], captured_at=m.now(),
                                                     issued_sources=issued_sources, before_baselines={})
            finally:
                trial.rollback()
        with catalogue_source_reader(engine, captured, actor) as (db, _):
            shared = snapshots.snapshot_inputs(db, db.get(m.Organisation, organisation))
            replayed = snapshots.domain_snapshot(db, db.get(m.Workspace, wid), shared_inputs=shared)
            assert replayed["policies"] == legacy_issued["policies"]
    expected = next(row for row in issued["policies"] if row["id"] == rule["id"])
    for data in (bounded, legacy_issued):
        assert next(row for row in data["policies"] if row["id"] == rule["id"]) == expected

    unsafe = deepcopy(bounded)
    sessions = sorted((row for row in unsafe["shifts"] if row["id"] in unsafe["focus_ids"]), key=lambda row: row["start"])
    late = sessions[1]
    late["start"], late["end"] = "2026-10-04T11:00:00+00:00", "2026-10-04T15:00:00+00:00"
    for participant in late["participants"]:
        participant["start"], participant["end"] = late["start"], late["end"]
    assert any(finding["code"] == "MIN_REST" for finding in evaluate(unsafe)["findings"])


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_approval_stays_current_after_a_captured_rule_retires(h):
    rule = h.policy({"kind": "max_hours", "hours": 40, "window": "week"})
    draft = h.shift(h.workspace("Review after rule retirement"))["workspace"]
    assert not h.snapshot(draft)["wishes"]
    h.command("approver", "policy.retire", {"reason": "Retire without replacement"}, rule["id"], rule["revision"])

    submitted = h.lifecycle(draft, "submit")["workspace"]
    assert submitted["status"] == "submitted"
    assert h.detail(submitted)["review_progress"]["stale_inputs"] is False
    approved = h.lifecycle(submitted, "approve", "approver")["workspace"]
    assert approved["status"] == "approved"
    assert h.detail(approved)["review_progress"]["stale_inputs"] is False


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_review_inputs_keep_a_captured_retirement_after_refreshing_current_sources(h):
    rule = h.policy({"kind": "max_hours", "hours": 40, "window": "week"})
    draft = h.workspace("Review source refresh")
    with transaction(h.app.state.engine) as db:
        stale = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
    retired = h.command("approver", "policy.retire", {"reason": "Retire without replacement"},
                        rule["id"], rule["revision"])["record"]
    with transaction(h.app.state.engine) as db:
        current = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed["organisation_id"]))
        assert current["organisation"]["input_revision"] != stale["organisation"]["input_revision"]
        assert rule["id"] not in {row["id"] for row in current["policies"]}
        workspace = db.get(m.Workspace, draft["id"])
        for inputs in (stale, current):
            data = snapshots.domain_snapshot(db, workspace, shared_inputs=inputs, current_mandatory=True)
            rows = {row["id"]: row for row in data["_live_safety_staleness"]["policies"]}
            assert (rows[rule["id"]]["status"], rows[rule["id"]]["revision"]) == ("retired", retired["revision"])
            assert rule["id"] in data["_changed_department_rules"]
