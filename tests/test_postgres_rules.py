"""Publication rechecks, quota evidence and scoped rule enforcement on PostgreSQL."""

import csv
from datetime import datetime, timezone
from io import StringIO

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from apps.api.snapshots import publication_snapshot
from packages.domain.generation import generate

from .clock_support import freeze_clock
from .test_block_quotas import quota
from .test_catalogue_preview import apply, change, preview, state
from .test_participation_segments import at
from .test_resources_calendars import calendar, pool, typ, update

pytestmark = pytest.mark.postgres


def approve(pg, workspace):
    submitted = pg.lifecycle(workspace, "submit")["workspace"]
    return pg.lifecycle(submitted, "approve", "approver")["workspace"]


def test_pool_preview_rolls_back_and_current_capacity_and_calendar_gate_publication(pg, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 9, 18, 10, tzinfo=timezone.utc))
    openings = calendar(pg)
    resource = pool(pg, capacity=2, calendar_id=openings["id"])
    duty_type = typ(pg, resource)
    workspace = pg.shift(pg.workspace(), typ=duty_type["id"])["workspace"]
    workspace = pg.shift(workspace, typ=duty_type["id"], location=1,
                         people=[pg.seed["people"][1]])["workspace"]
    pg.client("admin")
    detail = pg.detail(workspace)
    before = state(pg)
    command = change(resource, capacity=1, calendar_id=openings["id"])
    result = preview(pg, command)
    impact, = result["impacts"]
    assert impact["workspace_id"] == workspace["id"]
    assert {finding["code"] for finding in impact["introduced"]} == {"POOLED_RESOURCE_CAPACITY"}
    assert (result["before"]["capacity"], result["proposed"]["capacity"]) == (2, 1)
    assert result["committed"] is False and state(pg) == before and pg.detail(workspace) == detail
    assert apply(pg, change(resource, capacity=3, calendar_id=openings["id"]),
                 result["source_fingerprint"], status=409)["error"]["code"] == "STALE_PREVIEW"
    assert state(pg) == before

    approved = approve(pg, workspace)
    assert apply(pg, command, result["source_fingerprint"], status=409)["error"]["code"] == "STALE_PREVIEW"
    refreshed = preview(pg, command)
    resource = apply(pg, command, refreshed["source_fingerprint"])["record"]
    assert resource["capacity"] == 1

    original_evaluate = planning.evaluate
    seen = set()

    def observe(*args, **kwargs):
        evaluated = original_evaluate(*args, **kwargs)
        seen.update(finding["code"] for finding in evaluated["findings"])
        return evaluated

    monkeypatch.setattr(planning, "evaluate", observe)
    # Current mandatory findings take precedence over stale approval, which is still reported.
    refused = pg.lifecycle(approved, "publish", status=422)["error"]
    assert refused["code"] == "VALIDATION_FAILED"
    assert refused["details"]["stale_approval"]["code"] == "STALE_APPROVAL"
    assert {finding["code"] for finding in refused["details"]["findings"]} == {"POOLED_RESOURCE_CAPACITY"}
    assert "POOLED_RESOURCE_CAPACITY" in seen
    assert pg.get("coordinator", "/api/publications")["publications"] == []

    resource = update(pg, "resource_pools", resource, capacity=2)
    closed = update(pg, "operating_calendars", openings,
                    definition={**openings["definition"], "week": []})
    seen.clear()
    refused = pg.lifecycle(approved, "publish", status=422)["error"]
    assert refused["code"] == "VALIDATION_FAILED"
    assert refused["details"]["stale_approval"]["code"] == "STALE_APPROVAL"
    assert {finding["code"] for finding in refused["details"]["findings"]} == {"CALENDAR_CLOSED"}
    assert "CALENDAR_CLOSED" in seen
    assert "POOLED_RESOURCE_CAPACITY" not in seen
    assert pg.get("coordinator", "/api/publications")["publications"] == []

    update(pg, "operating_calendars", closed, definition=openings["definition"])
    reopened = pg.lifecycle(approved, "reopen")["workspace"]
    issued = pg.publish(reopened)["publication_id"]
    path = f"/api/publications/{issued}"
    snapshot = pg.get("coordinator", path)["snapshot"]
    assert snapshot["resource_pools"][resource["id"]]["capacity"] == 2
    update(pg, "resource_pools", resource, capacity=1)
    assert pg.get("coordinator", path)["snapshot"] == snapshot


def test_issued_quota_reports_are_frozen_and_personal(pg):
    person, other = pg.seed["people"][:2]
    policy = quota(pg, scope={}, comparison="target", severity="advisory", amount=2)
    workspace = pg.shift(pg.workspace(start="2026-10-01", end="2026-11-01"))["workspace"]
    workspace = pg.shift(workspace, start=at("08:00", "2026-10-05"),
                         end=at("12:00", "2026-10-05"), people=[other])["workspace"]
    issued = pg.publish(workspace)["publication_id"]
    path = f"/api/reports/{issued}"
    team = pg.get("coordinator", path)["quotas"]
    assert {row["person_id"] for row in team["rows"]} == set(pg.seed["people"])
    personally = pg.get("staff", path)["quotas"]
    row, = personally["rows"]
    assert (row["person_id"], row["actual"], row["amount"]) == (person, 1, 2)
    assert (row["starts_on"], row["ends_on"]) == ("2026-10-01", "2026-11-01")
    assert pg.get("staff", f"/api/publications/{issued}")["snapshot"]["quota_report"] == personally

    export = pg.client("staff").get(path + "?format=quotas")
    assert export.status_code == 200, export.text
    exported, = csv.DictReader(StringIO(export.text))
    assert (exported["person_id"], exported["actual"], exported["amount"]) == (person, "1", "2.0")
    coverage = f"/api/publications/{issued}/coverage"
    assert pg.get("staff", coverage)["evaluation"]["findings"] == []
    assert "QUOTA_BELOW_TARGET" in {
        finding["code"] for finding in pg.get("coordinator", coverage)["evaluation"]["findings"]
    }

    pg.command("approver", "policy.retire", {"reason": "Retire the issued target"},
               policy["id"], policy["revision"])
    assert pg.get("coordinator", path)["quotas"] == team
    assert pg.get("staff", path)["quotas"] == personally
    assert pg.client("staff").get(path + "?format=quotas").text == export.text


def test_long_quota_counts_distant_official_duties_and_attributes_publication(pg):
    person = pg.seed["people"][0]
    policy = quota(pg, ends_on="2027-03-01", amount=1)
    early = pg.publish(pg.shift(pg.workspace())["workspace"])["publication_id"]
    later = pg.workspace(start="2027-02-14", end="2027-02-21")
    drafted = pg.shift(later, start=at("08:00", "2027-02-14"), end=at("12:00", "2027-02-14"))
    report = drafted["validation"]["quota_report"]
    row, = report["rows"]
    assert (row["policy_id"], row["person_id"], row["actual"], row["amount"]) == (policy["id"], person, 2, 1)
    assert report["complete"] is True
    assert {source["publication_id"] for source in report["source_publications"]} == {early}
    assert "QUOTA_ABOVE_MAXIMUM" in {finding["code"] for finding in drafted["validation"]["findings"]}
    refused = pg.lifecycle(drafted["workspace"], "submit", status=422)
    assert refused["error"]["code"] == "VALIDATION_FAILED"


def test_programme_cap_blocks_review_and_limits_allocation_even_with_absence(pg):
    trainees = pg.seed["people"][:2]
    programme = pg.configure("programmes", {"name": "Test learners", "starts_on": "2026-01-01"})["record"]
    for person in trainees:
        pg.configure("programme_memberships", {"person_id": person, "programme_id": programme["id"],
                     "starts_on": "2026-10-01", "ends_on": "2026-11-01"}, role="coordinator")
    pg.policy({"kind": "maximum_participants", "maximum": 1},
              {"programme_id": programme["id"], "service_id": pg.seed["services"][0],
               "weekdays": [6], "start_time": "09:00", "end_time": "10:00"})
    excessive = pg.shift(pg.workspace(), people=trainees)
    workspace = excessive["workspace"]
    limit, = (f for f in excessive["validation"]["findings"] if f["code"] == "SCOPED_PARTICIPANT_LIMIT")
    assert (limit["detail"]["maximum"], limit["detail"]["scheduled"]) == (1, 2)
    assert pg.lifecycle(workspace, "submit", status=422)["error"]["code"] == "VALIDATION_FAILED"

    pg.command("coordinator", "availability.record", {"person_id": trainees[0], "kind": "actual_absence",
               "start": at("08:00"), "end": at("12:00"), "private_reason": "Private fixture"})
    checked = pg.lifecycle(workspace, "validate")["validation"]
    assert "SCOPED_PARTICIPANT_LIMIT" in {f["code"] for f in checked["findings"]}
    assert pg.lifecycle(workspace, "submit", status=422)["error"]["code"] == "VALIDATION_FAILED"

    pg.policy({"kind": "minimum_cover", "minimum": 2})
    next_week = pg.workspace(start="2026-10-11", end="2026-10-18")
    vacancy = pg.shift(next_week, start=at("08:00", "2026-10-11"),
                       end=at("12:00", "2026-10-11"), people=[])["workspace"]
    candidate = generate(pg.snapshot(vacancy))
    assert candidate["status"] == "candidate_feasible"
    assigned = {addition["participant"]["person_id"] for addition in candidate["additions"]}
    assert len(candidate["additions"]) == 2
    assert len(assigned.intersection(trainees)) == 1 and pg.seed["people"][2] in assigned


def test_database_requires_quota_validation_before_a_publication_can_exist(pg):
    first = pg.publish(pg.shift(pg.workspace())["workspace"])["publication_id"]
    candidate = pg.workspace(start="2026-10-11", end="2026-10-18")
    candidate = approve(pg, pg.shift(candidate, start=at("08:00", "2026-10-11"),
                                     end=at("12:00", "2026-10-11"))["workspace"])
    with transaction(pg.app.state.engine) as db:
        issued = db.get(m.Publication, first)
        workspace = db.get(m.Workspace, candidate["id"])
        complete = workspace.validation
        assert complete["contracts"]["block_quota"]
        legacy = {**complete, "contracts": {k: v for k, v in complete["contracts"].items() if k != "block_quota"}}
        proposed = {"department_id": issued.department_id, "workspace_id": candidate["id"],
                    "sequence": issued.sequence + 1, "starts_on": workspace.starts_on,
                    "ends_on": workspace.ends_on, "published_by": issued.published_by,
                    "snapshot": publication_snapshot(db, workspace, complete), "fingerprint": complete["fingerprint"]}

    with pytest.raises(IntegrityError, match="quota-aware publication validation required"):
        with transaction(pg.app.state.engine, write=True) as db:
            db.add(m.Publication(**proposed, validation=legacy))
            db.flush()

    with transaction(pg.app.state.engine) as db:
        assert db.execute(select(m.Publication.id, m.Publication.current)).all() == [(first, True)]
        assert db.get(m.Workspace, candidate["id"]).status == "approved"
    assert pg.lifecycle(candidate, "publish")["sequence"] == 2
