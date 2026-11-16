"""Draft configuration schema is additive, defaulted and append-only on both databases."""
from datetime import date
import importlib

from alembic import command
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.db import make_engine, transaction
from migrations import safety
from packages.contracts.schemas import DRAFT_CONFIG_DEFAULTS
from scripts.manage import alembic_config
from .migration_support import historical_seed
from .test_email_migration import migration_url  # noqa: F401


@pytest.fixture
def upgraded(migration_url):
    config = alembic_config(migration_url)
    command.upgrade(config, "0144_tag_member_dates")
    engine = make_engine(migration_url)
    try:
        metadata = MetaData()
        workspace = Table("workspaces", metadata, autoload_with=engine)
        baseline = Table("workspace_baselines", metadata, autoload_with=engine)
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            actor = seed["identities"]["coordinator"]
            base = {"revision": 1, "created_at": m.now(), "department_id": seed["department_id"]}
            db.execute(workspace.insert().values(**base, id="legacy-draft", name="Legacy custom draft (test)",
                starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status="draft", content_revision=1,
                created_by=actor, last_edited_by=actor))
            for number, scope in enumerate(("department", "custom"), 1):
                db.execute(baseline.insert().values(**base, id=f"baseline-{number}", workspace_id="legacy-draft",
                    workspace_revision=number, scope_mode=scope, canonical_payload={"retained": scope},
                    payload_hash=str(number) * 64, actor_id=actor, requested_sources=["score"],
                    included_sources=["score"], omitted_sources=["duties", "rules", "auto", "display"]))
            before = [dict(row) for row in db.execute(select(baseline).order_by(baseline.c.id)).mappings()]
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        yield engine, seed, before
    finally:
        engine.dispose()


def test_upgrade_keeps_legacy_baselines_readable_with_null_columns(upgraded):
    engine, _, before = upgraded
    with Session(engine) as db:
        for saved in before:
            row = db.get(m.WorkspaceBaseline, saved["id"])
            assert row.followed is None and row.safety_receipts is None
            actual = {name: getattr(row, name) for name in saved}
            assert actual == saved
        assert db.scalar(select(m.WorkspaceBaseline.id).where(
            m.WorkspaceBaseline.followed.is_not(None) | m.WorkspaceBaseline.safety_receipts.is_not(None))) is None
        assert db.scalar(select(m.DepartmentDraftSettings.id)) is None
        assert db.scalar(select(m.WorkspaceConfigKeep.id)) is None
    columns = {column["name"]: column for column in inspect(engine).get_columns("workspace_baselines")}
    for name in ("followed", "safety_receipts"):
        assert columns[name]["nullable"] and columns[name]["default"] is None


def test_settings_defaults_options_and_governance_round_trip(upgraded):
    engine, seed, _ = upgraded
    assert DRAFT_CONFIG_DEFAULTS == {
        "safety_change": "stricter_only", "safety_use_default": "allowed", "editors": "department_roles",
        "non_safety_approval": "free", "rules_source": "own_snapshot", "duties_source": "own_snapshot",
        "score_source": "own_snapshot", "auto_source": "own_snapshot", "display_source": "own_snapshot",
        "default_change": "notify", "reason_required": "safety_only",
    }
    table = m.DepartmentDraftSettings.__table__
    base = {"created_at": m.now(), "department_id": seed["department_id"],
            "actor_id": seed["identities"]["admin"], "proposed_by": seed["identities"]["admin"],
            "reason": "Synthetic department draft settings"}
    # Reflected inserts exercise migration defaults without SQLAlchemy's Python defaults.
    stored = Table(table.name, MetaData(), autoload_with=engine)
    with engine.begin() as db:
        db.execute(stored.insert().values(**base, id="settings-1", revision=1))
    with Session(engine) as db:
        first = db.get(m.DepartmentDraftSettings, "settings-1")
        assert {name: getattr(first, name) for name in DRAFT_CONFIG_DEFAULTS} == DRAFT_CONFIG_DEFAULTS
        assert first.status == "proposed" and first.approved_by is None and first.previous_id is None
        second = m.DepartmentDraftSettings(**base, revision=2, previous_id=first.id, status="active",
            approved_by=seed["identities"]["approver"], approved_at=m.now(),
            starts_on=date(2026, 10, 4), ends_on=date(2026, 11, 4))
        db.add(second)
        db.commit()
        db.refresh(second)
        assert {name: getattr(second, name) for name in DRAFT_CONFIG_DEFAULTS} == DRAFT_CONFIG_DEFAULTS
        assert second.previous_id == first.id and second.approved_by != second.proposed_by
        assert second.starts_on == date(2026, 10, 4) and second.approved_at is not None
    revision = 2
    for name in (*DRAFT_CONFIG_DEFAULTS, "status"):
        for value in table.c[name].type.enums:
            revision += 1
            with engine.begin() as db:
                approved = {"approved_by": seed["identities"]["approver"]} if name == "status" and value == "active" else {}
                db.execute(stored.insert().values(**base, id=m.uid(), revision=revision, **approved, **{name: value}))
        with pytest.raises(DBAPIError), engine.begin() as db:
            db.execute(stored.insert().values(**base, id=m.uid(), revision=revision + 1, **{name: "invalid"}))
    for invalid in ({"revision": 0}, {"revision": 1}, {"revision": revision + 1,
            "starts_on": date(2026, 11, 4), "ends_on": date(2026, 10, 4)}):
        with pytest.raises(DBAPIError), engine.begin() as db:
            db.execute(stored.insert().values(**base, id=m.uid(), **invalid))


def test_configuration_evidence_is_append_only_and_keeps_allow_null_reason(upgraded):
    engine, seed, _ = upgraded
    with Session(engine) as db:
        keep = m.WorkspaceConfigKeep(department_id=seed["department_id"], workspace_id="legacy-draft",
            item_key="policy:synthetic:line:one", department_item_hash="a" * 64,
            actor_id=seed["identities"]["coordinator"], reason=None)
        settings = m.DepartmentDraftSettings(department_id=seed["department_id"],
            actor_id=seed["identities"]["admin"], proposed_by=seed["identities"]["admin"], reason="Synthetic settings")
        db.add_all([keep, settings])
        db.commit()
        assert keep.reason is None
        # A later department hash is a new receipt for the same item, not an update.
        db.add(m.WorkspaceConfigKeep(department_id=keep.department_id, workspace_id=keep.workspace_id,
            item_key=keep.item_key, department_item_hash="b" * 64, actor_id=keep.actor_id, reason="Reviewed change"))
        baseline = m.WorkspaceBaseline(department_id=keep.department_id, workspace_id=keep.workspace_id,
            workspace_revision=3, canonical_payload={"retained": "new"}, payload_hash="c" * 64,
            actor_id=keep.actor_id, followed={keep.item_key: keep.department_item_hash},
            safety_receipts=[{"item_key": keep.item_key, "department_item_hash": keep.department_item_hash,
                              "reason": "Synthetic receipt"}])
        db.add(baseline)
        db.commit()
        db.refresh(baseline)
        assert baseline.followed == {keep.item_key: "a" * 64}
        assert baseline.safety_receipts[0]["reason"] == "Synthetic receipt"
    for model in (m.WorkspaceConfigKeep, m.DepartmentDraftSettings):
        table = model.__table__
        with engine.connect() as db:
            before = [dict(row) for row in db.execute(select(table).order_by(table.c.id)).mappings()]
        for operation in (table.update().values(reason="rewrite"), table.delete()):
            with pytest.raises(DBAPIError, match="immutable draft configuration"), engine.begin() as db:
                db.execute(operation)
        if engine.dialect.name == "sqlite":
            with pytest.raises(DBAPIError, match="invalid draft configuration"), engine.begin() as db:
                db.execute(table.insert().prefix_with("OR REPLACE").values(**{**before[0], "reason": "replacement"}))
        with engine.connect() as db:
            assert [dict(row) for row in db.execute(select(table).order_by(table.c.id)).mappings()] == before
    with Session(engine) as db:
        assert db.get(m.WorkspaceBaseline, "baseline-1").followed is None
        assert db.scalar(select(m.WorkspaceBaseline.canonical_payload).where(m.WorkspaceBaseline.id == "baseline-2")) == {"retained": "custom"}


def test_active_settings_require_an_approver(upgraded):
    engine, seed, _ = upgraded
    table = Table(m.DepartmentDraftSettings.__tablename__, MetaData(), autoload_with=engine)
    values = {
        "id": m.uid(), "revision": 1, "created_at": m.now(), "department_id": seed["department_id"],
        "actor_id": seed["identities"]["admin"], "proposed_by": seed["identities"]["admin"],
        "status": "active", "reason": "Missing approver must be refused",
    }
    with pytest.raises(DBAPIError):
        with engine.begin() as db:
            db.execute(table.insert().values(**values))


def test_draft_config_migration_is_single_additive_head_and_forward_only():
    graph = ScriptDirectory.from_config(alembic_config("sqlite://"))
    head, = graph.get_heads()
    assert "0145_draft_config" in {row.revision for row in graph.iterate_revisions(head, "base")}
    migration = importlib.import_module("migrations.versions.0145_draft_config")
    assert migration.down_revision == "0144_tag_member_dates"
    assert safety.classification(safety.VERSIONS / "0145_draft_config.py") == "additive"
    assert safety.findings(safety.VERSIONS / "0145_draft_config.py") == []
    with pytest.raises(RuntimeError, match="Forward-only"):
        migration.downgrade()
