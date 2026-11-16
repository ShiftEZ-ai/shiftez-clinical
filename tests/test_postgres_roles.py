"""Opt-in real owner/runtime separation in an explicitly disposable PostgreSQL.

This fixture creates/drops two random cluster roles in addition to pg's private
schema, so ROTA_ALLOW_TEST_ROLES=yes is an additional required authorization.
It never changes grants on the public schema or uses SET ROLE as fake isolation.
"""
import os
import importlib
import importlib.util
import re
import secrets
import shutil
import uuid
from pathlib import Path
import pytest
from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from apps.api.db import make_engine
from scripts.manage import alembic_config
from .conftest import Harness

pytestmark=pytest.mark.postgres


def apply_grants(engine, schema, role, *, worker_role=None):
    source=(Path(__file__).resolve().parents[1]/'infra/database-roles.sql').read_text()
    sql='\n'.join(line for line in source.splitlines() if not line.strip().startswith('--'))
    sql=sql.replace('SCHEMA public','SCHEMA '+schema).replace('public.policy_seed_lines',schema+'.policy_seed_lines').replace('rota_app',role)
    sql=sql.replace('rota_worker',worker_role or role+'_worker')
    sql=re.sub(r'^\s*(BEGIN|COMMIT);\s*$', '', sql, flags=re.M)
    with engine.begin() as db:
        db.exec_driver_sql(sql)


@pytest.fixture
def runtime_preflight_root(tmp_path):
    """Preflight owns its release files; another worker/process may rebuild the checkout's dist."""
    root = tmp_path / 'runtime-release'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'migrations', root / 'migrations',
                    ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('index.html', 'assets/' + 'a' * 64 + '/app.js'):
        asset = root / 'apps/web/dist' / name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text('Synthetic preflight file-existence fixture', encoding='utf-8')
    (root / 'apps/web/dist/index.html').write_text(
        '<script type="module" src="/assets/' + 'a' * 64 + '/app.js"></script>')
    return root


@pytest.fixture
def runtime(pg, request):
    if os.getenv('ROTA_ALLOW_TEST_ROLES')!='yes':
        pytest.fail('Runtime-role qualification additionally requires ROTA_ALLOW_TEST_ROLES=yes; it creates/drops two random test-only PostgreSQL roles')
    with pg.app.state.engine.connect() as db:
        schema=db.scalar(text('SELECT current_schema()'))
        allowed=db.scalar(text('SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user'))
    if not re.fullmatch(r'rota_test_[0-9a-f]{32}',schema or '') or not allowed:
        pytest.fail('A private disposable test schema and authorized role provisioning are required')
    name='rota_rt_'+uuid.uuid4().hex
    password=secrets.token_hex(32)
    worker_name=name+'_worker'
    worker_password=secrets.token_hex(32)
    harness=None;created=[];worker_engine=None
    try:
        with pg.app.state.engine.begin() as db:
            db.execute(text('CREATE ROLE '+name+' NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'))
            # Parameterized secret transport; do not embed a password into a SQL
            # statement that could be reproduced in a failed test's traceback.
            db.execute(text("SELECT set_config('rota.test_role_password', :secret, true)"),{'secret':password})
            db.execute(text("DO $$ BEGIN EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', '"+name+"', current_setting('rota.test_role_password')); END $$"))
        created.append(name)
        # Execute the supported production-upgrade provisioning SQL, isolated to a random test role.
        spec = importlib.util.spec_from_file_location('worker_role', Path(__file__).resolve().parents[1] / 'tests/fixtures/guards/worker-role.py')
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        secret_dir = request.getfixturevalue('tmp_path') / 'worker-secrets'
        secret_dir.mkdir()
        (secret_dir / 'worker_database_url.txt').write_text(
            'postgresql+psycopg://rota_worker:' + worker_password + '@localhost:5432/synthetic\n')
        connection = pg.app.state.engine.raw_connection()
        try:
            with connection.cursor() as cursor:
                for _ in range(2):  # First install and idempotent upgrade use the same credential.
                    cursor.execute(provision.role_sql(secret_dir).replace('rota_worker', worker_name))
            connection.commit()
        finally:
            connection.close()
        created.append(worker_name)
        apply_grants(pg.app.state.engine, schema, name, worker_role=worker_name)
        worker_url=make_url(pg.settings.database_url).set(username=worker_name,password=worker_password).render_as_string(hide_password=False)
        url=worker_url if getattr(request, 'param', 'api') == 'worker' else make_url(pg.settings.database_url).set(username=name,password=password).render_as_string(hide_password=False)
        harness=Harness(url,pg.seed)
        worker_engine=make_engine(worker_url)
        harness.worker_engine=worker_engine
        harness.api_role=name
        harness.worker_role=worker_name
        with harness.app.state.engine.connect() as db:
            identity=db.execute(text('SELECT current_user,session_user')).one()
            expected=worker_name if getattr(request, 'param', 'api') == 'worker' else name
            assert tuple(identity)==(expected,expected)
            assert not db.scalar(text('SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls FROM pg_roles WHERE rolname=current_user'))
        yield harness
    finally:
        if harness:
            for client in harness.clients.values():client.close()
            harness.app.state.engine.dispose()
        if worker_engine:
            worker_engine.dispose()
        for role in reversed(created):
            with pg.app.state.engine.begin() as db:
                db.execute(text('DROP OWNED BY '+role))
                db.execute(text('DROP ROLE '+role))


def test_predecessor_grants_and_0109_preserve_append_only_runtime_access(pg, runtime, monkeypatch):
    migration=importlib.import_module('migrations.versions.0109_fair_share_dials')
    schema='rota_test_'+uuid.uuid4().hex
    role=make_url(runtime.settings.database_url).username
    url=make_url(pg.settings.database_url).update_query_dict({'options':'-csearch_path='+schema}).render_as_string(hide_password=False)
    engine=make_engine(url)
    runtime_engine=make_engine(make_url(runtime.settings.database_url).update_query_dict(
        {'options':'-csearch_path='+schema}).render_as_string(hide_password=False))
    with pg.app.state.engine.begin() as db:
        db.execute(text('CREATE SCHEMA '+schema))
    try:
        command.upgrade(alembic_config(url),'0066_ux_audit_b1')
        with engine.connect() as db:
            assert db.scalar(text("SELECT to_regclass(:table)"),{'table':schema+'.policy_seed_lines'}) is None
        apply_grants(engine,schema,role)
        command.upgrade(alembic_config(url),migration.down_revision)
        with engine.begin() as db:
            # Reproduce legacy broad defaults so the migration must revoke actual mutation grants.
            db.execute(text('ALTER DEFAULT PRIVILEGES IN SCHEMA '+schema+
                            ' GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO '+role))
            operations=Operations(MigrationContext.configure(db))
            execute=operations.execute
            with monkeypatch.context() as patch:
                patch.setattr(migration,'op',operations)
                patch.setattr(operations,'execute',lambda sql:execute(sql.replace('rota_app',role)))
                migration.upgrade()
            for privilege in ('SELECT','INSERT','UPDATE','DELETE'):
                assert db.scalar(text('SELECT has_table_privilege(:role,:table,:privilege)'),
                    {'role':role,'table':schema+'.policy_seed_lines','privilege':privilege}) == (privilege in {'SELECT','INSERT'})
        # Check permissions before grants are reapplied; no rows or triggers can mask a missing revoke.
        for statement in ('UPDATE policy_seed_lines SET line=line WHERE false','DELETE FROM policy_seed_lines WHERE false'):
            with pytest.raises(DBAPIError) as error, runtime_engine.begin() as db:
                db.execute(text(statement))
            assert error.value.orig.sqlstate == '42501'
        apply_grants(engine,schema,role)
    finally:
        runtime_engine.dispose()
        engine.dispose()
        with pg.app.state.engine.begin() as db:
            db.execute(text('DROP SCHEMA '+schema+' CASCADE'))


def test_preflight_detects_dangerous_grants_on_tables_outside_runtime_metadata(pg, runtime):
    from scripts.preflight import runtime_privilege_findings
    with pg.app.state.engine.begin() as db:
        db.execute(text('CREATE TABLE synthetic_extra_privileges (id integer PRIMARY KEY)'))
        db.exec_driver_sql('GRANT TRUNCATE, TRIGGER ON synthetic_extra_privileges TO ' + runtime.api_role)
    with runtime.app.state.engine.connect() as db:
        findings = runtime_privilege_findings(db)
    assert 'synthetic_extra_privileges:TRUNCATE' in findings
    assert 'synthetic_extra_privileges:TRIGGER' in findings


def test_password_hardening_upgrade_preserves_rows_and_revokes_previous_api_prune_grant(pg, runtime, monkeypatch):
    migration = importlib.import_module('migrations.versions.0116_password_login_hardening')
    schema = 'rota_test_' + uuid.uuid4().hex
    role = make_url(runtime.settings.database_url).username
    url = make_url(pg.settings.database_url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    engine = make_engine(url)
    with pg.app.state.engine.begin() as db:
        db.execute(text('CREATE SCHEMA ' + schema))
    try:
        command.upgrade(alembic_config(url), migration.down_revision)
        with engine.begin() as db:
            db.execute(text("""INSERT INTO auth_login_events
                (id, revision, created_at, account_hash, peer_hash, outcome, correlation_id)
                VALUES ('retained', 1, statement_timestamp() - interval '2 days', :account, :peer,
                        'credentials_rejected', 'synthetic-upgrade')"""), {'account': 'a' * 64, 'peer': 'b' * 64})
            db.exec_driver_sql('GRANT EXECUTE ON FUNCTION prune_auth_login_events(integer, integer) TO ' + role)
            assert db.scalar(text("SELECT has_function_privilege(:role, 'prune_auth_login_events(integer,integer)', 'EXECUTE')"), {'role': role})
            with monkeypatch.context() as patch:
                patch.setattr(migration, 'op', Operations(MigrationContext.configure(db)))
                migration.upgrade()
            assert db.scalar(text("SELECT count(*) FROM auth_login_events WHERE id='retained'")) == 1
            assert not db.scalar(text("SELECT has_function_privilege(:role, 'prune_auth_login_events(integer,integer)', 'EXECUTE')"), {'role': role})
        with pytest.raises(DBAPIError, match='retention bounds'), engine.begin() as db:
            db.execute(text('SELECT prune_auth_login_events(1, 500)'))
        with pytest.raises(DBAPIError, match='immutable evidence'), engine.begin() as db:
            db.execute(text("DELETE FROM auth_login_events WHERE id='retained'"))
    finally:
        engine.dispose()
        with pg.app.state.engine.begin() as db:
            db.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))


def test_runtime_credentials_support_actual_catalogue_review_and_publication(runtime):
    from .test_resources_calendars import pool,typ
    from .test_series_reconciliation import template,materialise,edit
    resource=pool(runtime);assignment_type=typ(runtime,resource)
    w=runtime.shift(runtime.workspace(),typ=assignment_type['id'])['workspace']
    pub=runtime.publish(w)
    assert runtime.get('coordinator','/api/reports/'+pub['publication_id'])['resources']['rows'][0]['unit_hours']==4
    # Exercise material history, series DML, audit and immutable evidence inserts
    # through the same runtime DB login; no temporary migrator connection is used.
    series=template(runtime,days=(0,));w=materialise(runtime,series,runtime.workspace('Runtime recurrence','2026-10-11','2026-10-18'));row=runtime.detail(w)['shifts'][0]
    w=edit(runtime,w,row,participants=[{'person_id':runtime.seed['people'][0],'start':row['start'],'end':row['end']}])
    runtime.publish(w)


def test_runtime_credentials_cannot_rewrite_evidence_disable_guards_or_change_schema(runtime):
    runtime.publish(runtime.shift(runtime.workspace())['workspace'])
    statements=['ALTER TABLE shifts ADD COLUMN unauthorized integer',
                'ALTER TABLE shifts DISABLE TRIGGER ALL',
                'TRUNCATE shifts CASCADE',
                'CREATE TABLE unauthorized (id integer)',
                "UPDATE audit_events SET reason='forged'", "UPDATE auth_login_events SET outcome='forged'",
                'DELETE FROM auth_login_events', 'DELETE FROM definition_history',
                'DELETE FROM command_receipts','DELETE FROM review_decisions',
                "UPDATE reference_imports SET manifest='{}'", 'DELETE FROM reference_imports',
                "UPDATE source_mappings SET target_id='forged'", 'DELETE FROM source_mappings',
                'UPDATE policy_seed_lines SET line=line', 'DELETE FROM policy_seed_lines',
                "UPDATE publications SET snapshot='{}'",'DELETE FROM publications',
                "UPDATE alembic_version SET version_num='forged'",
                'DROP TABLE resource_pools']
    for statement in statements:
        with pytest.raises(DBAPIError),runtime.app.state.engine.begin() as db:
            db.execute(text(statement))


def test_runtime_readonly_preflight_and_segmented_option_workflow(runtime, runtime_preflight_root):
    from scripts.preflight import inspect
    from .test_participation_segments import with_relief
    from .test_staffing_suggestions import ask
    result=inspect(runtime.settings,connect=True,root=runtime_preflight_root)
    assert result['passed'],result
    w=runtime.shift(runtime.workspace('Runtime segmented duty'),people=with_relief(runtime))['workspace']
    sh=runtime.detail(w)['shifts'][0]
    original=next(p for p in sh['participants'] if p['person_id']==runtime.seed['people'][0])
    options=ask(runtime,w,sh,replace_assignment_id=original['id'])
    assert options['candidates'][0]['whole_candidate_valid']
    pub=runtime.publish(w)
    report=runtime.get('coordinator','/api/reports/'+pub['publication_id'])
    assert report['metric_version']=='scheduled-v3'
    assert sum(row['off_cover_hours'] for row in report['rows'])==0.5


def test_owner_preflight_is_not_mistaken_for_runtime_isolation(pg, runtime_preflight_root):
    from scripts.preflight import inspect
    result=inspect(pg.settings,connect=True,root=runtime_preflight_root)
    assert not result['passed']
    checks=next(c for c in result['checks'] if c['check']=='runtime_privileges')
    assert 'OBJECT_OWNER_OR_MEMBERSHIP' in checks['detail']


@pytest.mark.parametrize('runtime', ['worker'], indirect=True)
def test_runtime_authentication_retention_has_narrow_owner_authority(runtime):
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    engine = runtime.app.state.engine
    with transaction(engine, write=True) as db:
        moment = db.scalar(text('SELECT statement_timestamp()'))
        events = [m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome=outcome,
                                  correlation_id='synthetic-runtime-retention', created_at=moment - timedelta(days=days))
                  for days, outcome in ((40, 'success'), (35, 'credentials_rejected'), (0, 'success'))]
        db.add_all(events)
        db.flush()
        old, second_old, current = [event.id for event in events]
        definition = db.execute(text("""SELECT p.prosecdef, p.proconfig, p.proowner = c.relowner AS same_owner
            FROM pg_proc p CROSS JOIN pg_class c
            WHERE p.oid = 'prune_auth_login_events(integer,integer)'::regprocedure
            AND c.oid = 'auth_login_events'::regclass""")).one()
        assert definition.prosecdef and definition.same_owner
        assert definition.proconfig == ['search_path=pg_catalog, pg_temp']
        assert not db.scalar(text("""SELECT EXISTS (
            SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
            WHERE p.oid = 'prune_auth_login_events(integer,integer)'::regprocedure AND a.grantee = 0)"""))
    for event_id in (old, current):
        for statement in ('DELETE FROM auth_login_events WHERE id=:id',
                          "UPDATE auth_login_events SET outcome='forged' WHERE id=:id"):
            with pytest.raises(DBAPIError), transaction(engine, write=True) as db:
                db.execute(text(statement), {'id': event_id})
    for days, batch in ((0, 1), (1, 1), (29, 1), (3651, 1), (30, 0), (30, 501), (None, 1), (30, None)):
        with pytest.raises(DBAPIError, match='retention bounds'), transaction(engine, write=True) as db:
            db.execute(text('SELECT prune_auth_login_events(:days, :batch)'), {'days': days, 'batch': batch})
    with transaction(engine, write=True) as db:
        assert db.scalar(text('SELECT prune_auth_login_events(30, 1)')) == 1
        assert db.get(m.AuthLoginEvent, old) is None
        assert db.get(m.AuthLoginEvent, second_old)
        assert db.get(m.AuthLoginEvent, current)
        assert db.scalar(text('SELECT prune_auth_login_events(30, 500)')) == 1
        assert db.get(m.AuthLoginEvent, current)


def test_api_runtime_cannot_call_authentication_retention_even_for_old_events(runtime):
    with pytest.raises(DBAPIError) as error, runtime.app.state.engine.begin() as db:
        db.execute(text('SELECT prune_auth_login_events(30, 500)'))
    assert error.value.orig.sqlstate == '42501'


def test_provisioned_worker_credentials_commit_housekeeping_and_api_failure_cannot_rollback_it(runtime, monkeypatch):
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.worker import main as worker
    from packages.domain import time as domain_time
    from .test_login_audit_retention import add_event, expired_security_state, assert_security_state_removed

    monkeypatch.setattr(domain_time, '_offset', timedelta(0))
    monkeypatch.setattr(worker, '_pruned', {})
    api_engine, worker_engine = runtime.app.state.engine, runtime.worker_engine
    with worker_engine.connect() as db:
        assert db.scalar(text('SELECT current_user')) == runtime.worker_role != runtime.api_role
        assert not db.scalar(text('SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls FROM pg_roles WHERE rolname=current_user'))
    with transaction(api_engine, write=True) as db:
        ids = expired_security_state(db, runtime.seed)
        expired, recent = add_event(db, 40), add_event(db, 0)
    with pytest.raises(DBAPIError) as error:
        worker.prune_security_state(api_engine, settings=runtime.settings)
    assert error.value.orig.sqlstate == '42501'
    assert_security_state_removed(api_engine, ids)
    assert api_engine not in worker._pruned
    with transaction(api_engine, write=True) as db:
        assert db.get(m.AuthLoginEvent, expired)
        ids = expired_security_state(db, runtime.seed)
    assert worker.prune_security_state(worker_engine, settings=runtime.settings) == 4
    assert_security_state_removed(api_engine, ids)
    with transaction(api_engine) as db:
        assert db.get(m.AuthLoginEvent, expired) is None
        assert db.get(m.AuthLoginEvent, recent)
    assert worker.prune_security_state(worker_engine, settings=runtime.settings) == 0


def test_runtime_cannot_bypass_authentication_retention_guard_with_session_settings(pg, runtime):
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    engine = runtime.app.state.engine
    with transaction(engine, write=True) as db:
        runtime_user = db.scalar(text('SELECT current_user'))
        moment = db.scalar(text('SELECT statement_timestamp()'))
        event = m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome='success',
                                correlation_id='synthetic-guard', created_at=moment - timedelta(days=40))
        db.add(event)
        db.flush()
        event_id = event.id
    # Even accidental grant drift cannot turn a caller-set flag into retention authority.
    with pg.app.state.engine.begin() as db:
        db.exec_driver_sql('GRANT DELETE ON auth_login_events TO ' + runtime_user)
    try:
        with pytest.raises(DBAPIError, match='immutable evidence'), transaction(engine, write=True) as db:
            db.execute(text("SELECT set_config('rota.allow_login_audit_purge', 'yes', true)"))
            db.execute(text('DELETE FROM auth_login_events WHERE id=:id'), {'id': event_id})
    finally:
        with pg.app.state.engine.begin() as db:
            db.exec_driver_sql('REVOKE DELETE ON auth_login_events FROM ' + runtime_user)
    with transaction(engine) as db:
        assert db.get(m.AuthLoginEvent, event_id).outcome == 'success'


@pytest.mark.parametrize('runtime', ['worker'], indirect=True)
def test_runtime_retention_cannot_be_redirected_by_a_temporary_table(runtime):
    from datetime import timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    engine = runtime.app.state.engine
    with transaction(engine, write=True) as db:
        schema = db.scalar(text('SELECT current_schema()'))
        moment = db.scalar(text('SELECT statement_timestamp()'))
        event = m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome='success',
                                correlation_id='synthetic-search-path', created_at=moment - timedelta(days=40))
        db.add(event)
        db.flush()
        event_id = event.id
        table = engine.dialect.identifier_preparer.quote_identifier(schema) + '.auth_login_events'
        prune = engine.dialect.identifier_preparer.quote_identifier(schema) + '.prune_auth_login_events'
        db.execute(text(f'CREATE TEMP TABLE auth_login_events ON COMMIT DROP AS SELECT * FROM {table}'))
        db.execute(text('SET LOCAL search_path = pg_temp, ' + schema))
        assert db.scalar(text(f'SELECT {prune}(30, 500)')) == 1
        assert db.scalar(text('SELECT count(*) FROM pg_temp.auth_login_events')) == 1
        assert db.scalar(text(f'SELECT count(*) FROM {table} WHERE id=:id'), {'id': event_id}) == 0
