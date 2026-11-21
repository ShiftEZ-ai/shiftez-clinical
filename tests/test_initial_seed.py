"""Explicit configuration bootstrap, safe repeats and atomic refusal; no operational data."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.errors import Problem
from apps.api.settings import Settings
from apps.api.services.scoring_defaults import SOURCE as SCORING_DEFAULT_SOURCE, line_id
from packages.contracts.schemas import EVENT_TYPE_DEFAULTS
from scripts import initial_seed, manage, system_command

ROOT = Path(__file__).resolve().parents[1]
REASON = "Owner explicitly chose the fictional initial configuration"
pytestmark = pytest.mark.usefixtures("synthetic_initial_seed")


@pytest.fixture(scope="session")
def empty_seed_database(tmp_path_factory):
    path = tmp_path_factory.mktemp("initial-seed-template") / "empty.db"
    manage.migrate("sqlite:///" + str(path))
    return path


@pytest.fixture
def installation(tmp_path, empty_seed_database):
    path = tmp_path / "installation.db"
    shutil.copy2(empty_seed_database, path)
    url = "sqlite:///" + str(path)
    engine = make_engine(url)
    yield engine, Settings(environment="test", database_url=url)
    engine.dispose()


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_mounted_seed_bundle_retains_a_predecessor_runtimes_defaults(installation, monkeypatch):
    engine, settings = installation
    monkeypatch.delattr(initial_seed.deployment_settings, "new_department_session_limits")
    # Execute the mounted script anew: an unconditional import would fail before load can run.
    bundle = runpy.run_path(str(ROOT / "scripts/initial_seed.py"))
    monkeypatch.setitem(bundle["load"].__globals__, "SEEDS", initial_seed.SEEDS)
    result = bundle["load"](engine, settings, 1, REASON)
    with transaction(engine) as db:
        department = db.get(m.Department, result["department_id"])
        assert (department.session_idle_minutes, department.session_max_hours) == (
            m.Department.__table__.c.session_idle_minutes.default.arg,
            m.Department.__table__.c.session_max_hours.default.arg)
        assert count(db, m.AuditEvent) == result["counts"]["catalogues"] + result["counts"]["rule_templates"] + 1


@pytest.mark.parametrize("event_defaults_available", [True, False])
def test_seed_creates_reviewable_configuration_and_linked_audit_only(installation, monkeypatch, event_defaults_available):
    engine, settings = installation
    if not event_defaults_available:
        monkeypatch.setitem(sys.modules, "apps.api.services.event_defaults", None)
    out = initial_seed.load(engine, settings, 1, REASON)
    with transaction(engine) as db:
        assert out["status"] == "applied" and out["version"] == 1 and len(out["sha256"]) == 64
        assert count(db, m.Organisation) == count(db, m.Department) == 1
        for model in (m.Person, m.Account, m.RoleBinding, m.Membership, m.CapabilityGrant, m.Workspace, m.Publication):
            assert count(db, model) == 0, model.__name__
        actor = db.scalars(select(m.Identity)).one()
        assert actor.disabled and actor.password_hash is None and actor.person_id is None
        assert actor.issuer == system_command.MAINTENANCE_ISSUER
        department = db.scalars(select(m.Department)).one()
        assert department.weekend_days == [4, 5]
        event_types = list(db.scalars(select(m.EventType)))
        assert {row.name for row in event_types} == (
            {definition["name"] for definition in EVENT_TYPE_DEFAULTS} if event_defaults_available else set())
        assert all(row.department_id == department.id for row in event_types)
        org = db.get(m.Organisation, department.organisation_id)
        assert "fictional initial template" in org.name and org.timezone == "Asia/Riyadh"
        assert count(db, m.Category) == 3 and count(db, m.Programme) == 2
        calendars = {r.id: r for r in db.scalars(select(m.OperatingCalendar))}
        assert len(calendars) == 2
        services = list(db.scalars(select(m.Service)))
        assert len(services) == 4 and all(s.calendar_id in calendars for s in services)
        locations = list(db.scalars(select(m.Location)))
        assert len(locations) == 3 and sum(bool(r.parent_id) for r in locations) == 2
        types = list(db.scalars(select(m.AssignmentType)))
        assert {r.definition["colour"] for r in types} == {"teal", "blue", "indigo", "pink"}
        policies = list(db.scalars(select(m.Policy)))
        assert len(policies) == 7
        [default] = [p for p in policies if p.source == SCORING_DEFAULT_SOURCE]
        assert default.status == "active" and default.created_by == default.approved_by == actor.id
        assert default.definition["rule"]["id"] == "fair_share"
        assert default.definition["rule"]["catalogue_version"] == 1
        assert line_id(department.id, "workload") in {line["id"] for line in default.definition["rule"]["lines"]}
        proposed = [p for p in policies if p.id != default.id]
        assert len(proposed) == 6 and all(p.status == "proposed" and p.approved_by is None for p in proposed)
        assert all(p.kind == "catalogue" and not p.definition["exception_allowed"] for p in policies)
        receipts = list(db.scalars(select(m.CommandReceipt)))
        assert len(receipts) == out["counts"]["catalogues"] + out["counts"]["rule_templates"]
        events = list(db.scalars(select(m.AuditEvent)))
        assert len(events) == len(receipts) + 1
        assert {e.detail["receipt_id"] for e in events if "receipt_id" in e.detail} == {r.id for r in receipts}
        assert all(e.actor_id == actor.id and e.reason == REASON for e in events)
        assert count(db, m.DefinitionHistory) == len(receipts) + 1  # the seeded default has its own immutable history


@pytest.mark.parametrize("version", [1, 2])
def test_reseed_preserves_operator_edits_records_and_rule_lifecycle(installation, version):
    engine, settings = installation
    first = initial_seed.load(engine, settings, 1, REASON)
    with transaction(engine, write=True) as db:
        department = db.get(m.Department, first["department_id"])
        operator = m.Identity(issuer="test", subject="seed-operator", display_name="Seed operator (test)")
        db.add(operator)
        db.flush()
        service, archived = list(db.scalars(select(m.Service).order_by(m.Service.id).limit(2)))
        original_name = service.name
        updated, archived, added, proposed = system_command.apply(db, operator, department, [
            {"kind": "catalogue.update", "target_id": service.id, "expected_revision": service.revision,
             "payload": {"catalogue": "services", "value": {"name": "Owner configured clinic",
                         "starts_on": str(service.starts_on), "calendar_id": service.calendar_id,
                         "mode": "virtual", "colour": "violet", "description": "Operator's clinical configuration"},
                         "reason": "Owner renamed and edited the initial template"}},
            {"kind": "catalogue.update", "target_id": archived.id, "expected_revision": archived.revision,
             "payload": {"catalogue": "services", "value": {"name": archived.name,
                         "starts_on": str(archived.starts_on), "calendar_id": archived.calendar_id,
                         "mode": archived.mode, "colour": archived.colour, "active": False},
                         "reason": "Owner archived an unused template"}},
            {"kind": "catalogue.create", "payload": {"catalogue": "services",
             "value": {"name": original_name, "starts_on": str(service.starts_on)},
             "reason": "Owner added a separate clinic using the original template name"}},
            {"kind": "rule.propose", "payload": {"catalogue_id": "duties_per_month",
             "lines": [{"id": m.uid(), "numbers": {"max": {"value": 18, "level": "mandatory"}}}],
             "reason": "Owner proposed an additional workload rule"}},
        ], "test-seed-operator-edits", settings)
        policies = list(db.scalars(select(m.Policy).where(m.Policy.status == 'proposed', m.Policy.created_by != operator.id)
                                   .order_by(m.Policy.id).limit(2)))
        activated = system_command.apply(db, operator, department, [
            {"kind": "policy.activate", "target_id": policy.id, "expected_revision": policy.revision,
             "payload": {"reason": "Independently reviewed the seeded proposal"}}
            for policy in policies
        ], "test-seed-approve", settings)
        retired = system_command.apply(db, operator, department, [
            {"kind": "policy.retire", "target_id": activated[1]["record"]["id"],
             "expected_revision": activated[1]["record"]["revision"],
             "payload": {"reason": "Owner retired the approved template"}},
        ], "test-seed-retire", settings)[0]
        assert updated["record"]["name"] == "Owner configured clinic"
        assert updated["record"]["revision"] == 2 and updated["record"]["mode"] == "virtual"
        assert not archived["record"]["active"]
        assert added["record"]["id"] != service.id and added["record"]["name"] == original_name
        assert proposed["record"]["status"] == "proposed"
        assert activated[0]["record"]["status"] == "active"
        assert activated[0]["record"]["approved_by"] == operator.id
        assert retired["record"]["status"] == "retired" and not retired["record"]["active"]
    with transaction(engine) as db:
        before = {table.name: db.execute(select(table).order_by(*table.primary_key)).all()
                  for table in m.Base.metadata.sorted_tables}
    if version == 1:
        repeat = initial_seed.load(engine, settings, version, "Owner repeated the seed command")
        assert repeat == {**first, "status": "already_applied"}
    else:
        with pytest.raises(ValueError, match="Only the reviewed initial seed version 1 is supported"):
            initial_seed.load(engine, settings, version, "Owner requested a higher seed version")
    with transaction(engine) as db:
        after = {table.name: db.execute(select(table).order_by(*table.primary_key)).all()
                 for table in m.Base.metadata.sorted_tables}
        assert after == before


def test_system_command_run_updates_seeded_department(installation, monkeypatch):
    engine, settings = installation
    first = initial_seed.load(engine, settings, 1, REASON)
    with transaction(engine) as db:
        service = db.scalars(select(m.Service)).first()
        service_id, revision, start = service.id, service.revision, str(service.starts_on)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("ROTA_ENV", "test")
    calls = []
    updated = system_command.run([{"kind": "catalogue.update", "target_id": service_id, "expected_revision": revision,
                                  "payload": {"catalogue": "services", "value": {"name": "Owner configured clinic", "starts_on": start},
                                              "reason": "Owner renamed the initial template"}}], "test-seed-edit",
                                 lambda db, identity, department: calls.append(department.id))[0]
    assert calls == [first["department_id"]] and updated["record"]["revision"] == revision + 1
    with transaction(engine) as db:
        assert db.get(m.Service, service_id).name == "Owner configured clinic"


def test_late_validation_failure_rolls_back_all_seed_records(installation, tmp_path, monkeypatch):
    engine, settings = installation
    data = json.loads((initial_seed.SEEDS / "production-v1.json").read_text())
    data["catalogues"][-1]["value"]["definition"]["maximum_duration_minutes"] = 15
    (tmp_path / "production-v1.json").write_text(json.dumps(data))
    monkeypatch.setattr(initial_seed, "SEEDS", tmp_path)
    with pytest.raises(ValidationError, match="maximum duration"):
        initial_seed.load(engine, settings, 1, REASON)
    with transaction(engine) as db:
        for model in (m.Organisation, m.Department, m.Identity, m.Category, m.Location, m.AuditEvent, m.CommandReceipt, m.DefinitionHistory):
            assert count(db, model) == 0, model.__name__


def test_nonempty_installation_is_refused_without_changes(installation):
    engine, settings = installation
    with transaction(engine, write=True) as db:
        row = m.Organisation(name="Owner's existing institution")
        db.add(row)
    with pytest.raises(RuntimeError, match="nonempty installation"):
        initial_seed.load(engine, settings, 1, REASON)
    with transaction(engine) as db:
        assert db.scalars(select(m.Organisation)).one().name == "Owner's existing institution"
        assert count(db, m.Identity) == count(db, m.AuditEvent) == count(db, m.Department) == 0


def test_changed_seed_is_refused_after_first_load(installation, tmp_path, monkeypatch):
    engine, settings = installation
    initial_seed.load(engine, settings, 1, REASON)
    data = json.loads((initial_seed.SEEDS / "production-v1.json").read_text())
    data["catalogues"][0]["value"]["name"] = "Changed shipped template"
    (tmp_path / "production-v1.json").write_text(json.dumps(data))
    monkeypatch.setattr(initial_seed, "SEEDS", tmp_path)
    with pytest.raises(RuntimeError, match="different seed"):
        initial_seed.load(engine, settings, 1, REASON)
    with transaction(engine) as db:
        assert not db.scalar(select(m.Category.id).where(m.Category.name == "Changed shipped template"))


def test_seed_requires_current_schema_and_never_migrates(tmp_path):
    engine = make_engine("sqlite:///" + str(tmp_path / "unmigrated.db"))
    try:
        with pytest.raises(Problem, match="schema is unavailable"):
            initial_seed.load(engine, Settings(environment="test"), 1, REASON)
    finally:
        engine.dispose()


@pytest.mark.parametrize("image_revision,missing", [
    ("0109_fair_share_dials", "apps.api.services.scoring_defaults"),
    ("0071_lower_numbered_head", "apps.api.services.scoring_defaults"),
    ("0110_future_image", "apps.api.services.scoring_defaults"),
    ("0066_ux_audit_b1", "synthetic_scoring_dependency"),
])
def test_missing_current_module_or_transitive_dependency_fails_atomically(installation, monkeypatch, image_revision, missing):
    import builtins
    from apps.api import schema
    original_import = builtins.__import__

    def missing_scoring_module(name, *args, **kwargs):
        if name == "apps.api.services.scoring_defaults":
            raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(schema, "supported_revision", lambda: image_revision)
    engine, settings = installation
    with engine.begin() as db:
        db.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": image_revision})
    assert (schema.MIGRATIONS / "versions/0109_fair_share_dials.py").is_file()
    # Load as a kit script after establishing the image's supported head, including any direct imports.
    load = runpy.run_path(str(ROOT / "scripts/initial_seed.py"))["load"]
    monkeypatch.setitem(load.__globals__, "SEEDS", initial_seed.SEEDS)
    monkeypatch.setattr(builtins, "__import__", missing_scoring_module)
    with pytest.raises(ModuleNotFoundError) as error:
        load(engine, settings, 1, REASON)
    assert error.value.name == missing
    with transaction(engine) as db:
        for model in (m.Organisation, m.Department, m.Identity, m.Policy, m.AuditEvent, m.DefinitionHistory):
            assert count(db, model) == 0


@pytest.mark.parametrize("entry", ["manage.py", "initial_seed.py"])
def test_cli_requires_explicit_seed_version_and_reason_before_writing(tmp_path, entry):
    path = tmp_path / "never-created.db"
    args = [sys.executable, str(ROOT / "scripts" / entry)]
    if entry == "manage.py":
        args.append("init-production-seed")
    args += ["--database-url", "sqlite:///" + str(path)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "ROTA_"))}
    result = subprocess.run(args, env={**env, "ROTA_ENV": "test"}, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "--seed-version" in result.stderr
    assert not path.exists()


def test_production_cli_refuses_test_flags_before_connecting(tmp_path):
    path = tmp_path / "never-created.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "ROTA_"))}
    result = subprocess.run([sys.executable, str(ROOT / "scripts/initial_seed.py"), "--seed-version", "1", "--reason", REASON,
                             "--database-url", "sqlite:///" + str(path)],
                            env={**env, "ROTA_ENV": "production", "ROTA_ALLOW_TEST_ROLES": "yes"},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "ROTA_ALLOW_TEST_ROLES" in result.stderr
    assert not path.exists()
