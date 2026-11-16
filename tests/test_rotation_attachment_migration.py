"""Legacy rotation mentors become attachments without rewriting existing records or evidence."""
import copy
from datetime import date
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import MetaData, Table, select

from apps.api import models as m
from apps.api.db import make_engine, transaction
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from scripts.manage import alembic_config, migrate
from .migration_support import historical_seed

migration = import_module("migrations.versions.0100_rotation_attachments")


def legacy_rotations(db, seed):
    metadata = MetaData()

    def insert(table_name, **values):
        # Reflect the seeded revision so later ORM columns cannot change this historical fixture.
        table = Table(table_name, metadata, autoload_with=db.connection(), resolve_fks=False)
        values = {"id": str(uuid4()), "revision": 1, "created_at": m.now(), **values}
        db.execute(table.insert(), values)
        return SimpleNamespace(id=values["id"])

    department, people = seed["department_id"], seed["people"]
    programme = insert("programmes", department_id=department, name="Synthetic migration programme", active=True, description="",
                            starts_on=date(2026, 1, 1))
    other_programme = insert("programmes", department_id=department, name="Unrelated migration programme", active=True, description="",
                                  starts_on=date(2026, 1, 1))
    typ = insert("rotation_types", department_id=department, name="Synthetic migration rotation", active=True, description="",
                         starts_on=date(2026, 1, 1))
    cohort = insert("cohorts", department_id=department, programme_id=programme.id, name="Synthetic migration cohort",
                      starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1))
    common = {"department_id": department, "cohort_id": cohort.id, "starts_on": date(2026, 10, 1),
              "ends_on": date(2026, 11, 1)}
    rows = {
        "scoped": insert("rotations", **common, person_id=people[0], mentor_id=people[1], service_id=seed["services"][0]),
        "duplicate_scoped": insert("rotations", **common, person_id=people[0], mentor_id=people[1], service_id=seed["services"][0]),
        "other_clinic": insert("rotations", **common, person_id=people[0], mentor_id=people[1], service_id=seed["services"][1]),
        "all_services": insert("rotations", **common, person_id=people[1], mentor_id=people[2], rotation_type_id=typ.id),
        "custom_scope": insert("rotations", **common, person_id=people[2], mentor_id=people[0], service_id=seed["services"][0]),
        "different_end": insert("rotations", **{**common, "ends_on": date(2026, 12, 1)}, person_id=people[2],
                                     mentor_id=people[0], service_id=seed["services"][0]),
        "exact_existing": insert("rotations", **common, person_id=people[2], mentor_id=people[1], service_id=seed["services"][0]),
        "legacy_broad": insert("rotations", **common, person_id=people[1], mentor_id=people[0], service_id=seed["services"][0]),
        "exact_over_broad": insert("rotations", **common, person_id=people[1], mentor_id=people[0], service_id=seed["services"][1]),
        "multi_clinic": insert("rotations", **common, person_id=people[0], mentor_id=people[2], service_id=seed["services"][0]),
        "multi_clinic_second": insert("rotations", **common, person_id=people[0], mentor_id=people[2], service_id=seed["services"][1]),
        "no_mentor": insert("rotations", **common, person_id=people[0], service_id=seed["services"][1]),
    }
    dated = {"department_id": department, "starts_on": common["starts_on"], "ends_on": common["ends_on"]}
    existing = {
        "custom_scope": insert("mentorships", **dated, person_id=people[2], mentor_id=people[0], revision=7,
            programme_id=programme.id, assignment_type_ids=[seed["type"]], service_ids=[seed["services"][1]],
            strength="required"),
        "programme_only": insert("mentorships", **dated, person_id=people[2], mentor_id=people[0], revision=4,
            programme_id=programme.id, service_ids=[seed["services"][0]], strength="required"),
        "foreign_programme": insert("mentorships", **dated, person_id=people[0], mentor_id=people[1], revision=6,
            programme_id=other_programme.id, service_ids=[seed["services"][0]], strength="required"),
        "duty_type_only": insert("mentorships", **dated, person_id=people[2], mentor_id=people[0], revision=4,
            assignment_type_ids=[seed["type"]], service_ids=[seed["services"][0]]),
        "other_clinic_only": insert("mentorships", **dated, person_id=people[2], mentor_id=people[0], revision=4,
            service_ids=[seed["services"][1]]),
        "exact_existing": insert("mentorships", **dated, person_id=people[2], mentor_id=people[1], revision=7,
            service_ids=[seed["services"][0]], strength="required"),
        "programme_broad": insert("mentorships", **dated, person_id=people[2], mentor_id=people[1], revision=5,
            programme_id=programme.id, strength="required"),
        "legacy_broad": insert("mentorships", **dated, person_id=people[1], mentor_id=people[0], revision=3),
        "exact_over_broad": insert("mentorships", **dated, person_id=people[1], mentor_id=people[0], revision=5,
            service_ids=[seed["services"][1]], strength="required"),
        "multi_clinic": insert("mentorships", **dated, person_id=people[0], mentor_id=people[2], revision=8,
            service_ids=seed["services"][:2], strength="required"),
        "open_ended": insert("mentorships", **{**dated, "ends_on": None}, person_id=people[0], mentor_id=people[2], revision=9,
            service_ids=seed["services"][:2], strength="required"),
    }
    return {name: row.id for name, row in rows.items()}, {name: row.id for name, row in existing.items()}


def stored_rows(engine):
    with engine.connect() as bind:
        return {table.name: [dict(row) for row in bind.execute(select(table).order_by(*table.primary_key)).mappings()]
                for table in (m.Rotation.__table__, m.Mentorship.__table__, m.RotationAttachment.__table__)}


def check_conversion(engine, ids, existing_ids):
    with engine.connect() as bind:
        existing_before = {row["id"]: dict(row) for row in bind.execute(select(m.Mentorship.__table__).where(
            m.Mentorship.id.in_(existing_ids.values()))).mappings()}
    with engine.begin() as bind:
        migration.convert_rotations(bind)
    first = stored_rows(engine)
    with engine.begin() as bind:
        migration.convert_rotations(bind)
    assert stored_rows(engine) == first
    attachments = {row["id"]: row for row in first["mentorships"]}
    links = {row["rotation_id"]: row for row in first["rotation_attachments"]}
    assert set(links) == set(ids.values()) - {ids["no_mentor"]}
    assert len(attachments) == len(existing_before) + 4
    assert {key: attachments[key] for key in existing_before} == existing_before
    for name in ("exact_existing", "legacy_broad", "exact_over_broad", "multi_clinic"):
        assert links[ids[name]] == {"rotation_id": ids[name], "mentorship_id": existing_ids[name], "synced_revision": None}
    assert links[ids["multi_clinic_second"]] == {"rotation_id": ids["multi_clinic_second"],
        "mentorship_id": existing_ids["multi_clinic"], "synced_revision": None}
    assert existing_ids["open_ended"] not in {link["mentorship_id"] for link in links.values()}
    assert existing_ids["foreign_programme"] not in {link["mentorship_id"] for link in links.values()}
    assert existing_ids["programme_broad"] not in {link["mentorship_id"] for link in links.values()}
    assert links[ids["custom_scope"]]["mentorship_id"] != existing_ids["custom_scope"]
    assert links[ids["custom_scope"]] == {"rotation_id": ids["custom_scope"],
        "mentorship_id": existing_ids["programme_only"], "synced_revision": None}
    assert links[ids["different_end"]]["mentorship_id"] != links[ids["custom_scope"]]["mentorship_id"]
    assert links[ids["scoped"]]["mentorship_id"] == links[ids["duplicate_scoped"]]["mentorship_id"]
    assert links[ids["scoped"]]["mentorship_id"] != links[ids["other_clinic"]]["mentorship_id"]
    rotations = {row["id"]: row for row in first["rotations"]}
    for name in ("scoped", "duplicate_scoped", "other_clinic", "all_services", "different_end"):
        link, rotation = links[ids[name]], rotations[ids[name]]
        attachment = attachments[link["mentorship_id"]]
        assert link["synced_revision"] == attachment["revision"] == 1
        assert all(attachment[key] == rotation[key] for key in ("department_id", "person_id", "mentor_id", "starts_on", "ends_on"))
        assert attachment["service_ids"] == ([rotation["service_id"]] if rotation["service_id"] else [])
        assert attachment["assignment_type_ids"] == [] and attachment["programme_id"] is None
        assert attachment["strength"] == "preferred"

    # Replays share current automatic ownership and retain manual ownership.
    with engine.begin() as bind:
        later_ids = {name: str(uuid4()) for name in ("scoped", "exact_existing")}
        bind.execute(m.Rotation.__table__.insert(), [{**rotations[ids[name]], "id": later_id}
                                                    for name, later_id in later_ids.items()])
        migration.convert_rotations(bind)
        for name, later_id in later_ids.items():
            later = bind.execute(select(m.RotationAttachment.__table__).where(
                m.RotationAttachment.rotation_id == later_id)).mappings().one()
            assert later["mentorship_id"] == links[ids[name]]["mentorship_id"]
            assert later["synced_revision"] == (1 if name == "scoped" else None)


def test_upgrade_from_current_dev_head_converts_existing_rotation_mentors(tmp_path):
    url = "sqlite:///" + str(tmp_path / "legacy-rotations.db")
    command.upgrade(alembic_config(url), migration.down_revision)
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            services = Table("services", MetaData(), autoload_with=db.connection(), resolve_fks=False)
            service = dict(db.execute(select(services).where(services.c.id == seed["services"][0])).mappings().one())
            second_service = str(uuid4())
            db.execute(services.insert(), {**service, "id": second_service, "name": "Other historical clinic"})
            seed["services"].append(second_service)
            ids, existing = legacy_rotations(db, seed)
        migrate(url)
        check_conversion(engine, ids, existing)
    finally:
        engine.dispose()


def seeded_outcomes(h):
    # The rota predates the October attachments; compare its full score and preserve stored evidence unchanged.
    h.policy({"kind": "max_duties", "maximum": 1, "window": "week"}, severity="advisory")
    workspace = h.shift(h.workspace(start="2026-09-06", end="2026-09-13"),
                        start="2026-09-06T08:00:00+03:00", end="2026-09-06T12:00:00+03:00",
                        people=[h.seed["people"][0]])["workspace"]
    workspace = h.shift(workspace, start="2026-09-07T08:00:00+03:00", end="2026-09-07T12:00:00+03:00",
                        people=[h.seed["people"][0]])["workspace"]
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        ids, existing = legacy_rotations(db, h.seed)
    before = h.snapshot(workspace)
    check_before = evaluate(copy.deepcopy(before))
    assert check_before["findings"]  # A real rule outcome, not an empty comparison.
    score_before = schedule_score(copy.deepcopy(before), check_before)
    old_snapshot = copy.deepcopy(before)
    old_rotation = next(row for row in old_snapshot["rotations"] if row["id"] == ids["scoped"])
    old_rotation["mentor_id"] = h.seed["people"][1]
    with transaction(engine, write=True) as db:
        job = m.Job(department_id=h.seed["department_id"], kind="validation", workspace_id=workspace["id"],
            requested_by=h.seed["identities"]["coordinator"], input_fingerprint=before["fingerprint"],
            input_snapshot=old_snapshot)
        history = m.DefinitionHistory(department_id=h.seed["department_id"], kind="rotations", object_id=ids["scoped"],
            object_revision=1, snapshot=old_rotation, actor_id=h.seed["identities"]["coordinator"],
            reason="Synthetic pre-migration evidence")
        db.add_all([job, history]); db.flush()
        job_id, history_id = job.id, history.id
    check_conversion(engine, ids, existing)
    after = h.snapshot(workspace)
    check_after = evaluate(copy.deepcopy(after))
    assert check_before['fingerprint'] == before['fingerprint']
    assert check_after['fingerprint'] == after['fingerprint'] != before['fingerprint']
    # Converted mentorship source IDs enter the manifest; the clinical outcomes and frozen evidence stay exact.
    assert {k: v for k, v in check_after.items() if k != 'fingerprint'} == {
        k: v for k, v in check_before.items() if k != 'fingerprint'}
    assert schedule_score(copy.deepcopy(after), check_after) == score_before
    with transaction(engine) as db:
        assert db.get(m.Job, job_id).input_snapshot == old_snapshot
        assert db.get(m.DefinitionHistory, history_id).snapshot == old_rotation
        assert db.get(m.DefinitionHistory, history_id).snapshot["mentor_id"] == h.seed["people"][1]


def test_conversion_is_idempotent_and_preserves_seeded_department_outcomes_and_snapshots(h):
    seeded_outcomes(h)


@pytest.mark.postgres
def test_postgres_conversion_is_idempotent_and_preserves_seeded_department_outcomes_and_snapshots(pg):
    seeded_outcomes(pg)
