"""A department year: deterministic SQL/loaded-row/response budgets and the existing CPU gate."""
from datetime import date, datetime, timedelta, timezone
import copy
import json
import os
from pathlib import Path
import time

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services.timeline import LAYER_KEYS, list_data
from packages.contracts import schemas as s
from packages.domain.quotas import METRIC_VERSION
from tests.test_timeline_data import actor_for, timeline_records


YEAR_START, YEAR_END = date(2026, 1, 1), date(2027, 1, 1)


@pytest.fixture(scope="module")
def timeline_department_year(scale_harness):
    h = scale_harness()
    ids = timeline_records(h)
    dep, org = h.seed["department_id"], h.seed["organisation_id"]
    author = h.seed["identities"]["coordinator"]
    with transaction(h.app.state.engine, write=True) as db:
        first = ids["person"]
        for cls in (m.ProgrammeMembership, m.Cohort, m.ProgrammeCalendarEntry, m.Mentorship, m.Eligibility, m.CapabilityGrant):
            for row in db.scalars(select(cls).where(cls.department_id == dep)):
                if getattr(row, "programme_id", ids["programme"]) == ids["programme"]:
                    row.starts_on, row.ends_on = YEAR_START, YEAR_END
        base_rank = db.scalar(select(m.Membership).where(m.Membership.person_id == first, m.Membership.department_id == dep))
        grant = db.scalar(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == first, m.CapabilityGrant.department_id == dep))
        mentors = {row.person_id: row.id for row in db.scalars(select(m.Mentorship).where(m.Mentorship.department_id == dep))}
        people = [m.Person(organisation_id=org, display_name=f"Synthetic timeline person {n:03d}", short_code=f"T{n:03d}") for n in range(1, 150)]
        db.add_all(people); db.flush()
        person_ids = [first, *(row.id for row in people)]
        for person in people:
            pid = person.id
            mentor = m.Mentorship(department_id=dep, person_id=pid, mentor_id=h.seed["people"][1],
                programme_id=ids["programme"], starts_on=YEAR_START, ends_on=YEAR_END)
            db.add(mentor); db.flush()
            mentors[pid] = mentor.id
            db.add_all([
                m.Membership(department_id=dep, person_id=pid, category_id=base_rank.category_id, starts_on=YEAR_START, ends_on=YEAR_END),
                m.ProgrammeMembership(department_id=dep, person_id=pid, programme_id=ids["programme"], starts_on=YEAR_START, ends_on=YEAR_END),
                m.Eligibility(department_id=dep, person_id=pid, service_id=h.seed["services"][0], starts_on=YEAR_START, ends_on=YEAR_END),
                m.CapabilityGrant(department_id=dep, person_id=pid, capability_id=grant.capability_id,
                    starts_on=YEAR_START, ends_on=YEAR_END, granted_by=author, evidence="Synthetic grant"),
                m.Availability(department_id=dep, person_id=pid, start=datetime(2026, 10, 5, 8, tzinfo=timezone.utc),
                    end=datetime(2026, 10, 6, 8, tzinfo=timezone.utc), kind="recorded_leave", private_reason="Synthetic private reason",
                    recorded_by=author),
                m.Availability(department_id=dep, person_id=pid, start=datetime(2026, 10, 7, 8, tzinfo=timezone.utc),
                    end=datetime(2026, 10, 8, 8, tzinfo=timezone.utc), kind="external_commitment", private_reason="Synthetic time away",
                    recorded_by=author),
                m.RequestRecord(department_id=dep, person_id=pid, kind="leave", start=datetime(2026, 10, 9, 8, tzinfo=timezone.utc),
                    end=datetime(2026, 10, 10, 8, tzinfo=timezone.utc), private_reason="Synthetic pending reason", created_by=author),
            ])
        facts = list(db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.department_id == dep)))
        for fact in facts:
            fact.starts_on, fact.ends_on = YEAR_START, YEAR_END
            for pid in person_ids[1:]:
                data = copy.deepcopy(fact.data)
                data["line"]["id"] = m.uid()
                data["line"]["who"] = {"conditions": [{"field": "person_ids", "values": [pid]}]}
                data["line"]["starts_on"], data["line"]["ends_on"] = YEAR_START.isoformat(), YEAR_END.isoformat()
                db.add(m.SchedulingFact(department_id=dep, person_id=pid, calendar_entry_id=fact.calendar_entry_id,
                    kind=fact.kind, status="active", on=True, starts_on=YEAR_START, ends_on=YEAR_END,
                    proposed_by=author, approved_by=h.seed["identities"]["approver"], reason="Synthetic approved fact", data=data))
        original = db.get(m.Rotation, ids["rotation"])
        original.starts_on, original.ends_on = YEAR_START, date(2026, 2, 1)
        rotations = []
        boundaries = [date(2026, month, 1) for month in range(1, 13)] + [YEAR_END]
        for pid in person_ids:
            for month, (first_day, last_day) in enumerate(zip(boundaries, boundaries[1:])):
                if pid == first and month == 0:
                    continue
                rotations.append(m.Rotation(department_id=dep, person_id=pid, cohort_id=ids["cohort"],
                    rotation_type_id=ids["type"], starts_on=first_day, ends_on=last_day, department_available=False))
        db.add_all(rotations); db.flush()
        db.add_all(m.RotationAttachment(rotation_id=row.id, mentorship_id=mentors[row.person_id], synced_revision=1) for row in rotations)
        workspace = m.Workspace(department_id=dep, name="Synthetic published department year", starts_on=YEAR_START,
            ends_on=YEAR_END, status="draft", created_by=author, last_edited_by=author)
        db.add(workspace); db.flush()
        type_snapshot = record(db.get(m.AssignmentType, h.seed["type"]))
        shifts, assignments = [], []
        for n in range(6000):
            day = YEAR_START + timedelta(days=n % 365)
            start = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=10)
            end = start + timedelta(hours=4)
            sid = m.uid()
            shifts.append({"id": sid, "workspace_id": workspace.id, "assignment_type_id": h.seed["type"],
                "type_revision": type_snapshot["revision"], "type_snapshot": type_snapshot,
                "location_id": h.seed["locations"][0], "start": start, "end": end})
            assignments.append({"id": m.uid(), "shift_id": sid, "person_id": person_ids[n % len(person_ids)],
                                "start": start, "end": end})
        db.execute(m.Shift.__table__.insert(), shifts)
        db.execute(m.ShiftService.__table__.insert(), [{"shift_id": row["id"], "service_id": h.seed["services"][0]} for row in shifts])
        db.execute(m.Assignment.__table__.insert(), assignments)
        workspace.status = "published"
        db.add(m.Publication(department_id=dep, workspace_id=workspace.id, sequence=1, starts_on=YEAR_START, ends_on=YEAR_END,
            published_by=author, snapshot={"shifts": [], "services": {}, "locations": {}},
            validation={"contracts": {"block_quota": METRIC_VERSION, "programmes": "programmes-v1"}}, fingerprint="0" * 64))
    return h, person_ids


@pytest.fixture(scope="module")
def timeline_year_measurement(timeline_department_year):
    from collections import Counter
    from apps.api.services import timeline
    h, person_ids = timeline_department_year
    statements, loaded, measurements, results = [], Counter(), [], []
    phases = {}

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    def count_rows(session, row):
        loaded[type(row).__name__] += 1

    def timed(key, function):
        def call(*args, **kwargs):
            started = time.process_time()
            value = function(*args, **kwargs)
            phases[key] = phases.get(key, 0) + time.process_time() - started
            return value
        return call

    def forbidden(*args, **kwargs):
        pytest.fail("Timeline reads must not acquire a rule snapshot or run the validator")

    event.listen(h.app.state.engine, "before_cursor_execute", count_sql)
    event.listen(Session, "loaded_as_persistent", count_rows)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(timeline, "_light_data", timed("source_cpu_s", timeline._light_data))
            patch.setattr(timeline, "_stored_findings", timed("stored_findings_cpu_s", timeline._stored_findings))
            patch.setattr(timeline, "_check_data", forbidden)
            patch.setattr(timeline, "evaluate", forbidden)
            for label, selected, options in (("one_default", person_ids[:1], {}), ("all_default", person_ids, {}),
                    ("all_layers", person_ids, {"layers": LAYER_KEYS}),
                    ("rotation_leave", person_ids, {"layers": "rotation,leave"}),
                    ("one_page", person_ids, {"layers": "rotation,leave", "limit": 1})):
                with transaction(h.app.state.engine) as db:
                    actor = actor_for(h, db)
                    statements.clear(); loaded.clear(); phases.clear()
                    started, cpu = time.perf_counter(), time.process_time()
                    result = list_data(db, actor, YEAR_START, YEAR_END, person_ids=selected, group_by="programme", **options)
                    cpu, elapsed = time.process_time() - cpu, time.perf_counter() - started
                started = time.process_time()
                encoded = s.TimelineResponse.model_validate(result).model_dump_json().encode()
                phases["serialization_cpu_s"] = time.process_time() - started
                measurements.append({"case": label, "selects": len(statements), "orm_rows": sum(loaded.values()),
                    "rows_by_table": dict(loaded), "people": len(result["people"]),
                    "items": sum(len(p["items"]) for p in result["people"]),
                    "findings": sum(len(p["conflicts"]) for p in result["people"]),
                    "duty_days": sum(len(p["duty_density"]) for p in result["people"]),
                    "bytes": len(encoded), "cpu_s": cpu, "elapsed_s": elapsed, "phases": dict(phases)})
                results.append(result)
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", count_sql)
        event.remove(Session, "loaded_as_persistent", count_rows)
    print("timeline department year", json.dumps(measurements))
    if os.environ.get("TIMELINE_MEASUREMENTS"):
        Path(os.environ["TIMELINE_MEASUREMENTS"]).write_text(json.dumps(measurements, indent=2))
    return measurements, results


def test_timeline_department_year_has_bounded_queries_and_all_layers(timeline_year_measurement):
    measurements, results = timeline_year_measurement
    result = results[2]
    assert result["total"] == 150 and len(result["people"]) == 150
    assert sum(sum(day["count"] for day in person["duty_density"]) for person in result["people"]) == 6000
    assert all({"rotation", "attachment", "leave", "time_away", "academic", "protected_time", "overtime_offer",
                "eligibility", "grant", "programme", "rank", "pending"} <= {row["kind"] for row in person["items"]}
               for person in result["people"])
    assert all(not person["conflicts"] for person in result["people"])
    # Measured at 55/56 SELECTs and 11,574/11,726 ORM loads for default/all layers; allow a margin for helpers.
    assert measurements[1]["selects"] <= measurements[0]["selects"] + 16, measurements
    assert measurements[1]["selects"] <= 68 and measurements[2]["selects"] <= 70, measurements
    assert measurements[1]["orm_rows"] <= 14000 and measurements[2]["orm_rows"] <= 14500, measurements
    assert measurements[1]["bytes"] <= 2100000 and measurements[2]["bytes"] <= 2200000, measurements


def test_timeline_default_and_page_load_only_requested_work(timeline_year_measurement):
    measurements, results = timeline_year_measurement
    assert "Eligibility" not in measurements[1]["rows_by_table"]
    assert "Shift" not in measurements[3]["rows_by_table"]
    assert {i["kind"] for p in results[3]["people"] for i in p["items"]} == {"rotation", "leave"}
    assert measurements[4]["people"] == 1 and results[4]["has_more"]
    assert measurements[4]["rows_by_table"]["Rotation"] == 12
    assert measurements[4]["rows_by_table"]["Availability"] <= 2
    assert measurements[3]["selects"] <= 28 and measurements[4]["selects"] <= 25, measurements
    assert measurements[3]["orm_rows"] <= 5500 and measurements[4]["orm_rows"] <= 450, measurements
    assert measurements[3]["bytes"] <= 1150000 and measurements[4]["bytes"] <= 11000, measurements


def test_timeline_rota_eligibility_150_people_500_sessions_measurement(timeline_department_year):
    """Record the first department-scale eligibility measurement before adding a CI budget."""
    from apps.api.snapshots import domain_snapshot
    from apps.api.services import timeline
    h, person_ids = timeline_department_year
    with transaction(h.app.state.engine) as db:
        workspace = db.scalar(select(m.Workspace).where(m.Workspace.name == "Synthetic published department year"))
        data = domain_snapshot(db, workspace, include_fairness=False, include_objectives=False)
    data["shifts"] = data["shifts"][:500]
    data["focus_ids"] = [shift["id"] for shift in data["shifts"]]
    started = time.process_time()
    eligible, sessions = timeline._evaluate_rota_eligibility(data, person_ids[:150])
    elapsed = time.process_time() - started
    assert sessions == 500 and len(eligible) == 150
    print("timeline rota eligibility 150x500", {"cpu_seconds": elapsed, "eligible": sum(value is True for value in eligible.values())})


def test_timeline_year_does_not_load_historical_or_hidden_layer_rows(timeline_department_year):
    from collections import Counter
    h, person_ids = timeline_department_year
    loaded = Counter()
    def count_rows(session, row):
        loaded[type(row).__name__] += 1
    with transaction(h.app.state.engine, write=True) as db:
        savepoint = db.begin_nested()
        cohort = db.scalar(select(m.Cohort).where(m.Cohort.department_id == h.seed["department_id"]))
        typ = db.scalar(select(m.RotationType).where(m.RotationType.department_id == h.seed["department_id"]))
        for cls, extra in ((m.Rotation, {"cohort_id": cohort.id, "rotation_type_id": typ.id}),
                           (m.Eligibility, {"service_id": h.seed["services"][0]})):
            db.execute(cls.__table__.insert(), [{"id": m.uid(), "department_id": h.seed["department_id"],
                "person_id": pid, "starts_on": date(2020, 1, 1), "ends_on": date(2021, 1, 1), **extra}
                for pid in person_ids for _ in range(20)])
        actor = actor_for(h, db)
        event.listen(Session, "loaded_as_persistent", count_rows)
        try:
            result = list_data(db, actor, YEAR_START, YEAR_END, person_ids=person_ids, layers="rotation,leave")
            projection_loads = loaded.copy()
            loaded.clear()
            filt = s.CollectionFilter.model_validate({"conditions": {"conditions": [
                {"field": "eligible_service_ids", "values": [h.seed["services"][0]]}]}}, context={"table_key": "people"})
            filtered = list_data(db, actor, YEAR_START, YEAR_END, person_ids=person_ids,
                                 layers="none", collection_filter=filt)
        finally:
            event.remove(Session, "loaded_as_persistent", count_rows)
            savepoint.rollback()
    assert len(result["people"]) == 150
    assert projection_loads["Rotation"] == 1800 and projection_loads["Availability"] == 300, projection_loads
    assert projection_loads["Eligibility"] == projection_loads["Shift"] == projection_loads["SchedulingFact"] == 0, projection_loads
    assert len(filtered["people"]) == 150 and loaded["Eligibility"] <= 160, loaded
    assert loaded["Shift"] == loaded["SchedulingFact"] == 0, loaded


@pytest.mark.cpu_budget
def test_timeline_department_year_cpu_budget(timeline_year_measurement):
    import coverage
    elapsed = timeline_year_measurement[0][1]["cpu_s"]
    assert elapsed < 15 * (3 if coverage.Coverage.current() else 1), elapsed


@pytest.mark.cpu_budget
def test_rotation_preview_reuses_bounded_full_year_checks(timeline_department_year):
    import coverage
    from apps.api.services.timeline_commands import preview
    h, _ = timeline_department_year
    with transaction(h.app.state.engine, write=True) as db:
        cohort = db.scalar(select(m.Cohort).where(m.Cohort.department_id == h.seed["department_id"]))
        typ = db.scalar(select(m.RotationType).where(m.RotationType.department_id == h.seed["department_id"]))
        target = m.Person(organisation_id=h.seed["organisation_id"], display_name="Synthetic empty preview target")
        db.add(target); db.flush()
        db.add(m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=target.id,
            programme_id=cohort.programme_id, starts_on=YEAR_START, ends_on=YEAR_END))
        db.flush()
        before = set(db.scalars(select(m.Rotation.id)))
        started = time.process_time()
        result = preview(db, actor_for(h, db), {"action": "create", "person_id": target.id, "cohort_id": cohort.id,
            "rotation_type_id": typ.id, "starts_on": YEAR_START.isoformat(), "ends_on": YEAR_END.isoformat(),
            "reason": "Synthetic full-year preview performance"})
        elapsed = time.process_time() - started
        assert result["can_apply"] and not result["requires_approval"]
        assert set(db.scalars(select(m.Rotation.id))) == before
        assert elapsed < 30 * (3 if coverage.Coverage.current() else 1), elapsed
        print("timeline full-year preview", {"cpu_seconds": elapsed, "published_duties": 6000, "rotations": 1800})
