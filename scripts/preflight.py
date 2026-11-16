#!/usr/bin/env python3
"""Read-only deployment checks. A successful preflight is not release acceptance.

No migrations, DDL, role changes, seed data, sign-in or external IdP requests.
Use the actual API/worker credentials, NOT the migration owner's credentials.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect as schema_inspect, text
from sqlalchemy.engine import make_url
from apps.api import models as m
from packages.web_assets import bundle_directory
from apps.api.schema import supported_revision
from apps.api.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ('audit_events', 'auth_login_events', 'definition_history', 'approvals', 'command_receipts', 'review_decisions', 'swap_events', 'reference_imports', 'source_mappings')
RETIRED = ('review_routes', 'review_delegations', 'swap_requests')
HISTORY_REDACTION_PRIVILEGE_SQL = "SELECT COALESCE(has_function_privilege(current_user, to_regprocedure('rota_redact_person_history(text)'), 'EXECUTE'), false)"
HISTORY_REDACTION_REQUIRED = 'Runtime role cannot EXECUTE rota_redact_person_history(text); reapply infra/database-roles.sql using the migration owner'


def runtime_privilege_findings(db) -> list[str]:
    """Conservative PostgreSQL checks, including privileged role memberships.

    They do not prove absence of dangerous server extensions, SECURITY DEFINER
    functions, grants in other databases, network or operating-system access.
    Actual denied-write tests are a separate disposable-database qualification.
    """
    findings = []
    if not db.scalar(text(HISTORY_REDACTION_PRIVILEGE_SQL)):
        findings.append(HISTORY_REDACTION_REQUIRED)
    if db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_roles r WHERE pg_has_role(current_user,r.oid,'MEMBER') AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls))")):
        findings.append('PRIVILEGED_ROLE_OR_MEMBERSHIP')
    if db.scalar(text("SELECT has_schema_privilege(current_schema(),'CREATE') OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname=current_schema() AND pg_has_role(current_user,n.nspowner,'MEMBER'))")):
        findings.append('SCHEMA_OWNER_OR_CREATE')
    if db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_database d WHERE d.datname=current_database() AND pg_has_role(current_user,d.datdba,'MEMBER'))")):
        findings.append('DATABASE_OWNER_OR_MEMBERSHIP')
    if db.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','v','m','S') AND pg_has_role(current_user,c.relowner,'MEMBER'))")):
        findings.append('OBJECT_OWNER_OR_MEMBERSHIP')
    tables = set(db.scalars(text("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
                                "WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')")))
    for name in sorted(tables):
        for privilege in ('TRUNCATE', 'TRIGGER'):
            if db.scalar(text('SELECT has_table_privilege(current_user,:table,:privilege)'), {'table':name,'privilege':privilege}):
                findings.append(name + ':' + privilege)
    for name in EVIDENCE + ('alembic_version',):
        if name not in tables:
            continue  # Schema readiness is checked separately; grants must also work on older migration stages.
        privileges = ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE') if name == 'alembic_version' else ('UPDATE', 'DELETE')
        for privilege in privileges:
            function = 'has_any_column_privilege' if privilege in {'INSERT','UPDATE'} else 'has_table_privilege'
            if db.scalar(text('SELECT '+function+'(current_user,:table,:privilege)'), {'table':name,'privilege':privilege}):
                findings.append(name + ':' + privilege)
    for name in RETIRED + ('publications',):
        if db.scalar(text("SELECT has_table_privilege(current_user,:table,'DELETE')"), {'table':name}):
            findings.append(name + ':DELETE')
    # Column-level UPDATE(current, effective_until) must not mask an inherited whole-table grant.
    columns = db.scalars(text("SELECT attname FROM pg_attribute WHERE attrelid='publications'::regclass AND attnum>0 AND NOT attisdropped AND attname NOT IN ('current','effective_until')"))
    for column in columns:
        if db.scalar(text("SELECT has_column_privilege(current_user,'publications',:column,'UPDATE')"), {'column':column}):
            findings.append('publications.' + column + ':UPDATE')
    return sorted(set(findings))


def inspect(settings: Settings, *, connect: bool = False, require_production: bool = False,
            release_image: str | None = None, root: Path = ROOT, worker: bool = False) -> dict:
    checks = []
    def add(name, passed, detail):
        checks.append({'check':name, 'status':'passed' if passed else 'failed', 'detail':detail})
    try:
        settings.check(require_authentication=not worker)
        add('configuration', True, 'Configuration accepted; secret values and database address omitted')
    except (RuntimeError, ValueError) as error:
        # Data-key validation messages contain configuration names and format requirements, never secret values.
        detail = str(error) if str(error).startswith('FMUC_DATA_KEY ') else 'Invalid deployment configuration; check protected environment and secret-file settings'
        add('configuration', False, detail)
    add('required_environment', not require_production or settings.environment == 'production', settings.environment if settings.environment in {'production','development','test'} else 'unrecognized')
    try:
        head = supported_revision(root / 'migrations')
        add('migration_graph', True, head)
    except Exception:
        head = None
        add('migration_graph', False, 'Release must contain exactly one migration head')
    try:
        web_ready = (bundle_directory(root / 'apps/web/dist') / 'app.js').is_file()
    except (OSError, ValueError):
        web_ready = False
    add('web_assets', web_ready,
        'Required prebuilt client entry points; not browser or content-hash acceptance')
    try:
        parsed = make_url(settings.database_url)
        if parsed.drivername not in {'sqlite','sqlite+pysqlite','postgresql+psycopg'}:
            raise ValueError('Unsupported runtime driver')
        driver = 'psycopg' if parsed.get_backend_name() == 'postgresql' else 'sqlite3'
        add('database_driver', importlib.util.find_spec(driver) is not None, driver)
    except Exception:
        parsed = None
        add('database_driver', False, 'Unusable database URL or missing driver')
    if release_image is not None:
        add('immutable_image_reference', bool(re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}',release_image)),
            'Syntax only; no registry pull, signature, provenance or vulnerability qualification')
    if connect and all(row['status']=='passed' for row in checks):
        engine = None
        try:
            if parsed.get_backend_name() == 'sqlite':
                # Unlike make_engine(), preflight must not create a database or directory.
                path = Path(parsed.database or '')
                if not path.is_file():
                    raise ValueError('Missing durable database')
                url = 'sqlite:///' + path.resolve().as_uri() + '?mode=ro&uri=true'
                engine = create_engine(url)
            else:
                engine = create_engine(settings.database_url, connect_args={'connect_timeout':5})
            with engine.connect() as db:
                if engine.dialect.name == 'postgresql':
                    db.execute(text('SET TRANSACTION READ ONLY'))
                    db.execute(text("SET LOCAL statement_timeout='5000ms'"))
                else:
                    db.execute(text('PRAGMA query_only=ON'))
                revisions = db.scalars(text('SELECT version_num FROM alembic_version')).all()
                add('database_migration', revisions == [head], 'Recorded migration agrees with this release' if revisions == [head] else 'Absent, multiple, older or newer migration head')
                missing = sorted(set(m.Base.metadata.tables) - set(schema_inspect(db).get_table_names()))
                add('database_tables', not missing, missing or 'Required runtime tables are present')
                if engine.dialect.name == 'postgresql':
                    findings = runtime_privilege_findings(db)
                    add('runtime_privileges', not findings, findings or 'Bounded read-only privilege checks passed; separate denied-write tests still required')
                else:
                    checks.append({'check':'runtime_privileges','status':'not_applicable','detail':'SQLite development only; no production role isolation claimed'})
                db.rollback()
        except Exception:
            add('database_connection_and_checks', False, 'Connection or read-only checks failed; raw driver errors and credentials intentionally omitted')
        finally:
            if engine is not None:
                engine.dispose()
    elif connect:
        checks.append({'check':'database_connection_and_checks','status':'not_run','detail':'Prerequisite failed; no database connection attempted'})
    else:
        checks.append({'check':'database_connection_and_checks','status':'not_run','detail':'Explicit --connect required; no connectivity, schema or grants qualification'})
    return {'timestamp':datetime.now(timezone.utc).isoformat(), 'checks':checks, 'runtime_role':'worker' if worker else 'api',
        'passed':not any(row['status']=='failed' for row in checks), 'connection_requested':connect,
        'production_ready':False, 'scope':'Selected preflight checks only. No migration, external authentication, browser, restore, load or institutional acceptance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true', help='Read the explicitly configured database using runtime credentials')
    parser.add_argument('--require-production', action='store_true')
    parser.add_argument('--worker', action='store_true', help='Use the worker startup configuration contract; never require API identity/key mounts')
    parser.add_argument('--release-image', default=os.getenv('ROTA_IMAGE'))
    parser.add_argument('--output', type=Path, help='Write a new, non-overwriting private JSON report')
    args = parser.parse_args()
    try:
        result = inspect(Settings.from_env(), connect=args.connect, require_production=args.require_production,
                         release_image=args.release_image, worker=args.worker)
    except Exception:
        result = {'passed':False,'production_ready':False,'checks':[{'check':'environment','status':'failed','detail':'Environment or secret-file loading failed; values and paths omitted'}]}
    encoded = json.dumps(result,indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        try:
            descriptor = os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            parser.error('Choose a new report path; existing evidence is not overwritten')
        with os.fdopen(descriptor,'w') as stream:
            stream.write(encoded)
    print(encoded,end='')
    for check in result['checks']:
        if check['status'] == 'failed' and check['check'] in {'configuration', 'environment'}:
            print(check['detail'], file=sys.stderr)
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
