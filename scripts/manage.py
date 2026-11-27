#!/usr/bin/env python3
"""Explicit operator entry points. No migrations or demo seeding on normal reads."""
import argparse
from collections import defaultdict
from contextlib import suppress
from dataclasses import replace
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
from pydantic import ValidationError
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from apps.api.settings import (Settings, new_department_display_settings, new_department_session_limits,
                               refuse_test_flags, secret_from_env)
from apps.api.db import make_engine, transaction
from apps.api.auth import PASSWORDS
from apps.api import models as m
from apps.api.serialization import record
from packages.contracts.schemas import AccountInvite, Decision, INSTALLATION_ADMIN_ROLE, PERMISSIONS, ROLE_LABELS, TypeDefinition
from migrations import safety
from migrations.source_safety import assigned, preflight_analyzer
from migrations.sql_safety import CURRENT_SCHEMA, duplicate_precheck_tokens

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
    fills = defaultdict(lambda: defaultdict(set))

    def name_key(name):
        return name.lower() if db.dialect.name == 'sqlite' else name

    def relation_key(table):
        schema, _, name = name_key(table).rpartition('.')
        return name, '' if schema == CURRENT_SCHEMA else schema

    def record_fill(table, columns):
        name, schema = relation_key(table)
        fills[name][schema].update({None} if columns is None else map(name_key, columns))

    unproved = False
    for step in pending:
        indexes = []

        def record_precheck(query, new_table, table, columns):
            name, schema = relation_key(table)
            inputs = None if columns is None else set(map(name_key, columns))
            # Unqualified relations can resolve to an explicitly named schema.
            affected = any((not schema or not scope or schema == scope)
                           and (inputs is None or None in writes or not inputs.isdisjoint(writes))
                           for scope, writes in fills.get(name, {}).items())
            indexes.append((query, new_table, affected))

        analyzer = preflight_analyzer(step.path, record_fill=record_fill, record_duplicate_precheck=record_precheck)
        kind = analyzer.run()['class']
        unproved |= kind == 'destructive'
        declared = assigned(analyzer.tree, 'DUPLICATE_PRECHECKS')
        if 'DUPLICATE_PRECHECKS' not in analyzer.globals:
            declared = ()
        try:
            if not isinstance(declared, (list, tuple)) or not all(isinstance(query, str) for query in declared):
                raise ValueError('DUPLICATE_PRECHECKS must be a literal sequence of queries')
            # A newly created table is empty only when the revision is proven additive.
            required, empty = set(), set()
            affected = False
            for query, new_table, inputs_changed in indexes:
                tokens = duplicate_precheck_tokens(query)
                if new_table and kind == 'additive':
                    empty.add(tokens)
                else:
                    required.add(tokens)
                    affected |= inputs_changed
            queries = [(duplicate_precheck_tokens(query), query) for query in declared]
            if required - {tokens for tokens, _ in queries}:
                raise ValueError('missing duplicate pre-check matching the unique expression index')
            if affected:
                raise ValueError('pending fill may change unique expression index inputs before creation')
            if required and unproved and not fresh:
                raise ValueError('unproved pending migration may change unique expression index inputs before creation')
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
    for number in range(1, 4):
        # Keep the number with its marker so it is visible in short labels too.
        label = f"{ROLE_LABELS['staff']} {number:02}(test)"
        row = m.Person(organisation_id=organisation.id, display_name=label)
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


BOOTSTRAP_AUDIT_ACTION = "installation.bootstrap"
UNVERIFIED_BOOTSTRAP = ("No unique pending first administrator with verifiable setup evidence. "
                        "Use the original setup link if it still works. Otherwise ask the installation "
                        "owner for verified recovery before upgrading.")


def first_admin_invitation(db):
    """Find the original pending bootstrap invitation from its receipt, audit and stored link."""
    from apps.api.auth import held_roles
    from apps.api.services.accounts import LOCAL_ISSUER, lock_link_account
    from scripts.system_command import MAINTENANCE_ISSUER, MAINTENANCE_SUBJECT

    maintenance = db.scalar(select(m.Identity).where(m.Identity.issuer == MAINTENANCE_ISSUER,
        m.Identity.subject == MAINTENANCE_SUBJECT, m.Identity.disabled.is_(True)))
    if maintenance is None:
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    events = list(db.scalars(select(m.AuditEvent).where(
        m.AuditEvent.actor_id == maintenance.id, m.AuditEvent.action == BOOTSTRAP_AUDIT_ACTION).limit(2)))
    if len(events) != 1:
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    event, = events
    receipt_id = event.detail.get("receipt_id")
    receipt = db.get(m.CommandReceipt, receipt_id) if receipt_id is not None else None
    if (not event.detail.get("maintenance") or receipt is None or receipt.actor_id != maintenance.id
            or receipt.department_id != event.department_id or receipt.command_kind != "account.invite"):
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    target_id = receipt.result.get("record", {}).get("id")
    link_id = receipt.result.get("link", {}).get("id")
    if not target_id or target_id != event.target_id or not link_id or link_id != event.detail.get("link_id"):
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    # Acceptance and disable can change eligibility while recovery waits for the account.
    if lock_link_account(db, target_id) is None:
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    link = db.get(m.AccountLink, link_id)
    if (link is None or link.identity_id != target_id or link.department_id != event.department_id
            or link.created_by != maintenance.id or link.purpose != "invite" or link.used_at is not None):
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    pending = db.scalar(select(m.Account).join(m.Identity, m.Identity.id == m.Account.identity_id).where(
        m.Account.identity_id == target_id, m.Account.accepted_at.is_(None), m.Identity.disabled.is_(False),
        m.Identity.issuer == LOCAL_ISSUER, m.Identity.subject == m.Account.username))
    department = db.get(m.Department, event.department_id)
    if pending is None or department is None or "admin" not in held_roles(db, target_id, department.id):
        raise RuntimeError(UNVERIFIED_BOOTSTRAP)
    return maintenance, department, pending


def reissue_first_admin(settings, reason, output):
    from apps.api.auth import Actor
    from apps.api.schema import require_current_schema
    from apps.api.services.accounts import issue_link, store

    if os.name != "posix":
        raise RuntimeError("First administrator recovery needs a POSIX host and a private output directory")
    directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    engine = None
    created = None
    prepared = False
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise RuntimeError("Use an existing output directory accessible only to its owner")
        engine = make_engine(settings.database_url, settings=settings)
        with transaction(engine, write=True) as db:
            require_current_schema(db)
            maintenance, department, account = first_admin_invitation(db)
            target_id = account.identity_id
            user = store(settings).get(account.username)
            if not user or user.get("disabled") is not True or user.get("email") != account.email:
                raise RuntimeError("The pending first administrator is missing or changed in the sign-in store")
            # Keep the requested name free until the private inode is known and its contents are durable.
            staging = f".first-admin-{m.uid()}"
            staged_link = f"{staging}/link"
            os.mkdir(staging, mode=0o700, dir_fd=directory)
            try:
                with open(staged_link, "x", encoding="utf-8",
                          opener=lambda name, flags: os.open(name, flags, 0o600, dir_fd=directory)) as private:
                    created = os.fstat(private.fileno())
                    if created.st_uid != os.geteuid() or created.st_mode & 0o077:
                        raise RuntimeError("The output filesystem did not create a private file")
                    actor = Actor(maintenance.id, department.id, department.organisation_id, maintenance.display_name,
                                  frozenset(PERMISSIONS["access.system_roles"]), None, "maintenance")
                    link = issue_link(db, actor, target_id, "invite", settings)
                    db.add(m.AuditEvent(department_id=department.id, actor_id=maintenance.id, action="account.resend",
                        target_id=target_id, reason=reason, detail={"link_id": link["id"], "maintenance": True},
                        correlation_id=m.uid()))
                    db.flush()
                    private.write(db.info.pop("one_time_link") + "\n")
                    private.flush()
                    os.fsync(private.fileno())
                os.link(staged_link, output.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                if not os.path.samestat(os.stat(output.name, dir_fd=directory, follow_symlinks=False), created):
                    raise RuntimeError("The private output file changed during recovery")
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(staged_link, dir_fd=directory)
                os.rmdir(staging, dir_fd=directory)
            os.fsync(directory)
            prepared = True
    except BaseException as exc:
        if prepared:
            raise RuntimeError("Could not confirm the recovery transaction. Keep the output private. "
                               "If the link does not work, retry recovery to a new file.") from exc
        if created is not None:
            try:
                current = os.stat(output.name, dir_fd=directory, follow_symlinks=False)
                if os.path.samestat(current, created):
                    os.unlink(output.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(directory)
        if engine is not None:
            engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "check-migrations", "init-demo", "init-institution",
                        "reissue-first-admin", "init-production-seed", "rebuild-fairness"])
    parser.add_argument("--confirm-destructive", default="", metavar="REV[,REV]",
                        help="Production: allow these exact reviewed destructive revisions to run")
    parser.add_argument("--database-url", default=secret_from_env("DATABASE_URL", "sqlite:///./var/rota.db"))
    parser.add_argument("--empty", action="store_true", help="Demo identities/people only, no operational catalogues")
    parser.add_argument("--organisation")
    parser.add_argument("--department")
    parser.add_argument("--username", help="First administrator's sign-in name")
    parser.add_argument("--email", help="First administrator's email")
    parser.add_argument("--display-name", help="First administrator's display name")
    parser.add_argument("--seed-version", type=int, choices=[1])
    parser.add_argument("--reason", help="Recorded owner reason for the initial seed or administrator invitation")
    parser.add_argument("--output", type=Path, help="New file in a private directory for the replacement setup link")
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
        if not all([args.organisation, args.department, args.username, args.email, args.display_name, args.reason]):
            parser.error("Institution bootstrap requires --organisation, --department, --username, --email, --display-name and --reason")
        from zoneinfo import ZoneInfo
        ZoneInfo(args.timezone)
        try:
            invite = AccountInvite(username=args.username, email=args.email, display_name=args.display_name,
                                   roles=["admin", INSTALLATION_ADMIN_ROLE], reason=args.reason)
            settings = replace(Settings.from_env(), database_url=args.database_url)
            if not settings.identity_users_file:
                parser.error("Institution bootstrap requires a managed sign-in users file")
            settings.check()
        except ValidationError:
            parser.error("Enter a valid sign-in name, email, display name and reason for the first administrator")
        except RuntimeError as exc:
            parser.error(str(exc))
    if os.getenv("ROTA_ENV") == "production" and not args.database_url.startswith("postgresql+psycopg://"):
        parser.error("Production migration/bootstrap requires an explicit PostgreSQL psycopg URL")
    if args.action == "reissue-first-admin":
        if args.output is None:
            parser.error("First administrator recovery requires --output and --reason")
        try:
            decision = Decision(reason=args.reason)
            settings = replace(Settings.from_env(), database_url=args.database_url)
            if not settings.identity_users_file:
                parser.error("First administrator recovery requires a managed sign-in users file")
            settings.check()
        except ValidationError:
            parser.error("Enter a valid reason for replacing the first administrator invitation")
        reissue_first_admin(settings, decision.reason, args.output)
        return
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
                db.add(m.RoleBinding(identity_id=values["identities"]["admin"], department_id=values["department_id"],
                                     role=INSTALLATION_ADMIN_ROLE, starts_on=date(2020, 1, 1)))
            directory = ROOT / ".run"
            directory.mkdir(exist_ok=True)
            path = directory / "demo-credentials.json"
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
                json.dump({"warning": "Local fictional demonstration only", "accounts": values["credentials"]}, handle, indent=2)
            print(f"Fictional demo initialized. Generated credentials: {path}\nNo credentials are committed or included in deliverable archives.")
        else:
            from scripts.system_command import apply, maintenance_identity
            with transaction(engine, write=True) as db:
                if db.scalar(select(func.count()).select_from(m.Organisation)):
                    raise RuntimeError("Refusing to bootstrap a nonempty installation")
                org = m.Organisation(name=args.organisation, timezone=args.timezone, week_start=args.week_start)
                db.add(org); db.flush()
                dept = m.Department(organisation_id=org.id, name=args.department,
                                    display_settings=new_department_display_settings(), **new_department_session_limits())
                db.add(dept); db.flush()
                from apps.api.services.event_defaults import seed_department
                seed_department(db, dept)
                from apps.api.services.leave_types import seed as seed_leave_types
                seed_leave_types(db, dept.id)
                from apps.api.services.contacts import seed as seed_contacts
                seed_contacts(db, dept.id)
                from apps.api.services.scoring_defaults import seed as seed_scoring_defaults
                seed_scoring_defaults(db, dept.id)
                actor = maintenance_identity(db, m, select)
                correlation_id = m.uid()
                [receipt] = apply(db, actor, dept, [{"kind": "account.invite", "payload": invite.model_dump(mode="json")}],
                                  correlation_id, settings)
                db.add(m.AuditEvent(department_id=dept.id, actor_id=actor.id, action=BOOTSTRAP_AUDIT_ACTION,
                    target_id=receipt["record"]["id"], reason=invite.reason, correlation_id=correlation_id,
                    detail={"receipt_id": receipt["receipt_id"], "link_id": receipt["link"]["id"], "maintenance": True}))
            print("Institution initialized. Give this private one-time setup link to the first administrator:")
            print(receipt["one_time_link"])
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
