#!/usr/bin/env python3
"""Explicit operator entry points. No migrations or demo seeding on normal reads."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import secrets
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from apps.api.settings import (Settings, new_department_display_settings, new_department_session_limits,
                               refuse_test_flags, secret_from_env)
from apps.api.db import make_engine, transaction
from apps.api.auth import PASSWORDS
from apps.api import models as m
from apps.api.serialization import record
from packages.contracts.schemas import TypeDefinition
from migrations import safety
from migrations.source_safety import Analyzer, assigned
from migrations.sql_safety import duplicate_precheck_tokens

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(url):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def migrate(url):
    command.upgrade(alembic_config(url), "head")


def check_duplicate_prechecks(db, pending, fresh):
    """DUPLICATE_PRECHECKS contains literal SELECTs that return a row on conflict."""
    for step in pending:
        indexes = []
        source = Path(step.path).read_text(encoding='utf-8')
        analyzer = Analyzer(source, {'tables': {}, 'record_duplicate_precheck': indexes.append})
        analyzer.run()
        declared = assigned(analyzer.tree, 'DUPLICATE_PRECHECKS')
        if 'DUPLICATE_PRECHECKS' not in analyzer.globals:
            declared = ()
        try:
            if not isinstance(declared, (list, tuple)) or not all(isinstance(query, str) for query in declared):
                raise ValueError('DUPLICATE_PRECHECKS must be a literal sequence of queries')
            # A newly created table is empty only when the revision is proven additive.
            allow_empty = any(new for _, new in indexes) and safety.analyze(step.path)['class'] == 'additive'
            required, empty = set(), set()
            for query, new_table in indexes:
                (empty if new_table and allow_empty else required).add(duplicate_precheck_tokens(query))
            queries = [(duplicate_precheck_tokens(query), query) for query in declared]
            if required - {tokens for tokens, _ in queries}:
                raise ValueError('missing duplicate pre-check matching the unique expression index')
        except ValueError as exc:
            raise SystemExit(f'Refusing migration {step.revision}: {exc}') from None
        if fresh:
            continue  # There are no retained rows to check on a fresh database.
        for tokens, query in queries:
            if tokens in empty and tokens not in required:
                continue
            try:
                with db.exec_driver_sql(query) as result:
                    duplicate = result.fetchone() is not None
            except SQLAlchemyError:
                raise SystemExit(f'Refusing migration {step.revision}: could not run duplicate pre-check') from None
            if duplicate:
                raise SystemExit(f'Refusing migration {step.revision}: duplicate rows found by pre-check')


def check_migrations(url, confirmed=()):
    """Read-only deploy gate, run before any backup, writer stop or upgrade.

    Refuses: a database whose recorded head this image does not contain (it is newer: an image downgrade), several
    recorded heads, any pending destructive revision without DESTRUCTIVE_REVIEWED, and, in production, a reviewed
    destructive revision the operator has not confirmed by id, unless the schema is fresh and empty.
    Returns the pending revision ids, oldest first.
    """
    script = ScriptDirectory.from_config(alembic_config(url))
    engine = make_engine(url)
    try:
        with engine.connect() as db:
            db.exec_driver_sql('SET TRANSACTION READ ONLY' if db.dialect.name == 'postgresql'
                               else 'PRAGMA query_only=ON')
            return _check_pending_migrations(db, script, confirmed)
    finally:
        engine.dispose()


def _check_pending_migrations(db, script, confirmed):
    current = MigrationContext.configure(db).get_current_heads()
    fresh = not current and not (set(inspect(db).get_table_names()) - {"alembic_version"})
    if len(current) > 1:
        raise SystemExit(f"Refusing: the database records several migration heads {list(current)}; repair it first")
    base = current[0] if current else None
    try:
        pending = list(reversed(list(script.iterate_revisions("heads", base or "base"))))
    except ResolutionError:
        raise SystemExit(f"Refusing: the database is at migration {base}, which this image does not contain. The database "
                         "is newer than this release; deploying it would be a downgrade. Deploy the matching or a newer release.") from None
    invalid = {r.revision: safety.declaration_errors(r.path) for r in pending if safety.declaration_errors(r.path)}
    if invalid:
        raise SystemExit(f"Refusing migration SAFETY_CLASS declarations: {invalid}. "
                         "Correct the declaration or review the changed source; see docs/release/data-safety.md")
    unsafe = {r.revision: safety.unreviewed(r.path) for r in pending if safety.unreviewed(r.path)}
    if unsafe:
        raise SystemExit(f"Refusing destructive migration(s) without DESTRUCTIVE_REVIEWED: {unsafe}. See docs/release/data-safety.md")
    need = [r.revision for r in pending if safety.needs_confirmation(r.path)]
    missing = [r for r in need if r not in confirmed]
    if fresh:
        print("fresh database: destructive confirmations not required")
    if missing and not fresh and os.getenv("ROTA_ENV") == "production":
        raise SystemExit(f"Refusing: {missing} are reviewed DESTRUCTIVE migrations. Confirm a fresh verified backup, read "
                         f"their DESTRUCTIVE_REVIEWED notes, then rerun with --confirm-destructive {','.join(missing)} "
                         f"(deploy: FMUC_CONFIRM_DESTRUCTIVE={','.join(missing)})")
    check_duplicate_prechecks(db, pending, fresh)
    print(f"Database head: {base or 'empty'}; image head: {script.get_current_head()}; pending: {len(pending)}")
    for r in pending:
        print(f"  {r.revision}  {safety.classification(r.path)}" +
              (f" (reviewed): {safety.marker(r.path)}" if r.revision in need else ""))
    return [r.revision for r in pending]


def demo_data(db, passwords=None, empty=False):
    if db.scalar(select(func.count()).select_from(m.Organisation)):
        raise RuntimeError("Refusing to seed a nonempty installation")
    credentials = passwords or {name: secrets.token_urlsafe(18) for name in ("admin", "coordinator", "approver", "credentialer", "staff", "operator", "auditor")}
    organisation = m.Organisation(name="Demonstration organisation · fictional data", timezone="Asia/Riyadh", week_start=6)
    db.add(organisation); db.flush()
    department = m.Department(organisation_id=organisation.id, name="Department A",
                              **new_department_session_limits())
    db.add(department); db.flush()
    from apps.api.services.event_defaults import seed_department
    seed_department(db, department)
    from apps.api.services.leave_types import seed as seed_leave_types
    seed_leave_types(db, department.id)
    from apps.api.services.contacts import seed as seed_contacts
    seed_contacts(db, department.id)
    people = []
    for name in ("Alex Morgan", "Jordan Lee", "Taylor Noor"):
        row = m.Person(organisation_id=organisation.id, display_name=name)
        db.add(row); db.flush(); people.append(row)
    identities = {}
    hashes = {}
    for username, password in credentials.items():
        if password not in hashes:
            hashes[password] = PASSWORDS.hash(password)
        identity = m.Identity(issuer="local-development", subject=username, display_name=username.title() + " · Demo",
                              password_hash=hashes[password], person_id=people[0].id if username == "staff" else None)
        db.add(identity); db.flush(); identities[username] = identity
        db.add(m.RoleBinding(identity_id=identity.id, department_id=department.id, role=username, starts_on=date(2020, 1, 1)))
    db.flush()
    from apps.api.services.scoring_defaults import seed as seed_scoring_defaults
    seed_scoring_defaults(db, department.id)
    result = {"organisation_id": organisation.id, "department_id": department.id, "people": [p.id for p in people],
              "identities": {k: v.id for k, v in identities.items()}, "credentials": credentials}
    if empty:
        return result
    effective = {"department_id": department.id, "starts_on": date(2020, 1, 1), "active": True}
    from packages.contracts.schemas import OperatingCalendarDefinition
    calendar = m.OperatingCalendar(name="Demo departmental working days", description="Fictional quota calendar",
        definition=OperatingCalendarDefinition(timezone=organisation.timezone, week=[
            {'weekday': day, 'windows': [{'start_time': '00:00', 'end_time': '24:00'}]}
            for day in range(7) if day not in department.weekend_days]).model_dump(mode='json'), **effective)
    db.add(calendar); db.flush()
    department.operating_calendar_id = calendar.id
    category = m.Category(name="Demo practitioners", description="Fictional category; no implicit privileges", **effective)
    trainee_category = m.Category(name="Demo learners", description="Fictional training group", **effective)
    cap = m.Capability(name="Demo independent practice", description="Synthetic evidence only", **effective)
    supervisor = m.Capability(name="Demo supervision", description="Synthetic evidence only", **effective)
    location_type = m.LocationType(name="Outpatient room", description="Demo resource category", **effective)
    service = m.Service(name="Continuity clinic", description="Fictional service", **effective)
    secondary = m.Service(name="Review clinic", description="Fictional second service", **effective)
    db.add_all([category, trainee_category, cap, supervisor, location_type, service, secondary]); db.flush()
    location = m.Location(name="North consulting room", location_type_id=location_type.id, capacity=2, **effective)
    location2 = m.Location(name="South consulting room", location_type_id=location_type.id, capacity=2, **effective)
    definition = TypeDefinition(maximum_duration_minutes=960).model_dump(mode="json")
    typ = m.AssignmentType(name="Clinical review session", description="Fictional manual planning example", definition=definition, **effective)
    db.add_all([location, location2, typ]); db.flush()
    for person in people:
        db.add(m.Membership(person_id=person.id, category_id=category.id, department_id=department.id, starts_on=date(2020, 1, 1)))
        for svc in (service, secondary):
            db.add(m.Eligibility(person_id=person.id, service_id=svc.id, department_id=department.id, starts_on=date(2020, 1, 1)))
        for capability in (cap, supervisor):
            db.add(m.CapabilityGrant(person_id=person.id, capability_id=capability.id, department_id=department.id,
                                     granted_by=identities["credentialer"].id, evidence="Fictional demo evidence, not a clinical authorisation", starts_on=date(2020, 1, 1)))
    for kind, row in (("operating_calendars", calendar), ("categories", category), ("categories", trainee_category), ("capabilities", cap), ("capabilities", supervisor),
                      ("location_types", location_type), ("locations", location), ("locations", location2), ("services", service), ("services", secondary), ("assignment_types", typ)):
        db.add(m.DefinitionHistory(department_id=department.id, kind=kind, object_id=row.id, object_revision=row.revision,
                                   snapshot=record(row), actor_id=identities["admin"].id, reason="Fictional bootstrap configuration"))
    result.update({"category": category.id, "trainee_category": trainee_category.id, "capability": cap.id, "supervisor_capability": supervisor.id,
                   "location_type": location_type.id, "locations": [location.id, location2.id], "services": [service.id, secondary.id], "type": typ.id})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "check-migrations", "init-demo", "init-institution", "init-production-seed", "rebuild-fairness"])
    parser.add_argument("--confirm-destructive", default="", metavar="REV[,REV]",
                        help="Production: allow these exact reviewed destructive revisions to run")
    parser.add_argument("--database-url", default=secret_from_env("DATABASE_URL", "sqlite:///./var/rota.db"))
    parser.add_argument("--empty", action="store_true", help="Demo identities/people only, no operational catalogues")
    parser.add_argument("--organisation")
    parser.add_argument("--department")
    parser.add_argument("--issuer")
    parser.add_argument("--subject")
    parser.add_argument("--display-name")
    parser.add_argument("--seed-version", type=int, choices=[1])
    parser.add_argument("--reason", help="Recorded owner reason for the explicit initial seed")
    parser.add_argument("--timezone", default="Asia/Riyadh")
    parser.add_argument("--week-start", type=int, choices=range(7), default=6, help="Institution calendar: Monday=0 through Sunday=6")
    args = parser.parse_args()
    try:
        refuse_test_flags(os.getenv("ROTA_ENV", "development"))
    except RuntimeError as exc:
        parser.error(str(exc))
    # Reject invalid bootstrap requests before any migration/database creation.
    if args.action == "init-demo" and os.getenv("ROTA_ENV") == "production":
        parser.error("Demo bootstrap is prohibited in production")
    if args.action == "init-institution":
        if not all([args.organisation, args.department, args.issuer, args.subject, args.display_name]):
            parser.error("Institution bootstrap requires --organisation, --department, --issuer, --subject and --display-name")
        from zoneinfo import ZoneInfo
        ZoneInfo(args.timezone)
    if os.getenv("ROTA_ENV") == "production" and not args.database_url.startswith("postgresql+psycopg://"):
        parser.error("Production migration/bootstrap requires an explicit PostgreSQL psycopg URL")
    if args.action == "init-production-seed":
        if args.seed_version is None or not args.reason:
            parser.error("Initial seed requires --seed-version 1 and --reason; it never runs automatically")
        from scripts.initial_seed import load
        engine = make_engine(args.database_url)
        try:
            print(json.dumps(load(engine, Settings(environment=os.getenv("ROTA_ENV", "production"), database_url=args.database_url),
                                  args.seed_version, args.reason), sort_keys=True))
        finally:
            engine.dispose()
        return
    check_migrations(args.database_url, {r for r in args.confirm_destructive.split(",") if r})
    if args.action == "check-migrations":
        return
    migrate(args.database_url)
    if args.action == "migrate":
        print("Migrations are at head. No data was seeded.")
        return
    engine = make_engine(args.database_url)
    try:
        if args.action == "rebuild-fairness":
            # Backfill (or repair) the fairness ledger from every current publication; safe to repeat.
            from apps.api.services.fairness import backfill
            with transaction(engine, write=True) as db:
                print(json.dumps(backfill(db, args.department)))
            return
        if args.action == "init-demo":
            if os.getenv("ROTA_ENV") == "production":
                raise RuntimeError("Demo bootstrap is prohibited in production")
            with transaction(engine, write=True) as db:
                values = demo_data(db, empty=args.empty)
            directory = ROOT / ".run"
            directory.mkdir(exist_ok=True)
            path = directory / "demo-credentials.json"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
                json.dump({"warning": "Local fictional demonstration only", "accounts": values["credentials"]}, handle, indent=2)
            print(f"Fictional demo initialized. Generated credentials: {path}\nNo credentials are committed or included in deliverable archives.")
        else:
            if not all([args.organisation, args.department, args.issuer, args.subject, args.display_name]):
                parser.error("Institution bootstrap requires --organisation, --department, --issuer, --subject and --display-name")
            from zoneinfo import ZoneInfo
            ZoneInfo(args.timezone)
            with transaction(engine, write=True) as db:
                if db.scalar(select(func.count()).select_from(m.Organisation)):
                    raise RuntimeError("Refusing to bootstrap a nonempty installation")
                org = m.Organisation(name=args.organisation, timezone=args.timezone, week_start=args.week_start)
                db.add(org); db.flush()
                dept = m.Department(organisation_id=org.id, name=args.department,
                                    display_settings=new_department_display_settings(), **new_department_session_limits())
                identity = m.Identity(issuer=args.issuer, subject=args.subject, display_name=args.display_name)
                db.add_all([dept, identity]); db.flush()
                from apps.api.services.event_defaults import seed_department
                seed_department(db, dept)
                from apps.api.services.leave_types import seed as seed_leave_types
                seed_leave_types(db, dept.id)
                from apps.api.services.contacts import seed as seed_contacts
                seed_contacts(db, dept.id)
                from apps.api.services.scoring_defaults import seed as seed_scoring_defaults
                seed_scoring_defaults(db, dept.id)
                db.add(m.RoleBinding(identity_id=identity.id, department_id=dept.id, role="admin", starts_on=m.now().date()))
            print("Institution initialized with one explicitly bound administrative identity. No clinical/approval role was inferred.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
