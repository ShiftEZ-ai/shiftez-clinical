"""Historical redaction works with deployment grants, without runtime history mutation rights."""
import json
from pathlib import Path
import uuid

from alembic import command
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.serialization import record
from migrations import safety
from scripts.manage import alembic_config
from .migration_support import historical_seed
from .test_bulk_history_privacy import SECRET, availability, legacy_history
from .test_postgres_roles import apply_grants, runtime, runtime_preflight_root  # noqa: F401


@pytest.mark.postgres
def test_deployment_grants_support_pre_privacy_schema_then_require_runtime_redaction(pg, runtime):
    from scripts.preflight import HISTORY_REDACTION_REQUIRED, runtime_privilege_findings

    schema = 'rota_test_' + uuid.uuid4().hex
    owner = pg.app.state.engine
    with owner.begin() as db:
        db.execute(text('CREATE SCHEMA ' + schema))
    scoped = make_url(pg.settings.database_url).update_query_dict({'options': '-csearch_path=' + schema})
    owner_engine = make_engine(scoped.render_as_string(hide_password=False))
    runtime_url = make_url(runtime.settings.database_url).update_query_dict({'options': '-csearch_path=' + schema})
    runtime_engine = make_engine(runtime_url.render_as_string(hide_password=False))
    role = runtime_url.username
    try:
        config = alembic_config(scoped.render_as_string(hide_password=False))
        command.upgrade(config, '0093_bulk_changes')
        # Current privilege preflight inspects this additive metadata too; keep the privacy predecessor intact.
        m.RecordInsertion.__table__.create(owner_engine)
        with owner_engine.connect() as db:
            assert db.scalar(text("SELECT to_regprocedure('rota_redact_person_history(text)')")) is None
        for revision in ('0093_bulk_changes', '0098_bulk_privacy'):
            command.upgrade(config, revision)
            apply_grants(owner_engine, schema, role)
            with runtime_engine.connect() as db:
                assert tuple(db.execute(text('SELECT current_user, session_user')).one()) == (role, role)
                assert db.scalar(text('SELECT count(*) FROM definition_history')) == 0
                assert runtime_privilege_findings(db) == (
                    [HISTORY_REDACTION_REQUIRED] if revision == '0093_bulk_changes' else [])
                if revision == '0098_bulk_privacy':
                    assert db.scalar(text("SELECT rota_redact_person_history('nonexistent synthetic person')")) == 0
            with pytest.raises(DBAPIError) as refused, runtime_engine.begin() as db:
                db.execute(text('UPDATE definition_history SET snapshot = snapshot'))
            assert refused.value.orig.sqlstate == '42501'
    finally:
        runtime_engine.dispose()
        owner_engine.dispose()
        with owner.begin() as db:
            db.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))


@pytest.mark.postgres
@pytest.mark.parametrize('bulk', [False, True])
def test_anonymise_redacts_history_with_real_runtime_grants_and_keeps_direct_update_denied(pg, runtime, runtime_preflight_root, bulk):
    person = runtime.configure('people', {'display_name': 'Synthetic runtime former colleague', 'active': False})['record']
    row = availability(runtime, person['id'])
    legacy = legacy_history(pg, row)
    other = legacy_history(pg, availability(runtime, runtime.seed['people'][1], 'Other private explanation'))
    unrelated_kind = legacy_history(pg, row, kind='policy')
    # The fixture connects using a separate runtime login and applies the actual deployment grants template.
    for statement in ("UPDATE definition_history SET snapshot = snapshot WHERE id = :id",
                      "DELETE FROM definition_history WHERE id = :id"):
        with pytest.raises(DBAPIError) as refused, runtime.app.state.engine.begin() as db:
            db.execute(text(statement), {'id': legacy['id']})
        assert refused.value.orig.sqlstate == '42501'
    with runtime.app.state.engine.connect() as db:
        assert not db.scalar(text("SELECT has_table_privilege(current_user, 'definition_history', 'UPDATE')"))
        assert db.scalar(text("SELECT has_function_privilege(current_user, 'rota_redact_person_history(text)', 'EXECUTE')"))
        owner, table_owner, definer, config, public_execute = db.execute(text("""
            SELECT p.proowner, c.relowner, p.prosecdef, p.proconfig,
                   EXISTS (SELECT 1 FROM aclexplode(p.proacl) AS a WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = 'definition_history'
            WHERE n.nspname = current_schema() AND p.proname = 'rota_redact_person_history'
        """)).one()
        assert owner == table_owner and definer and not public_execute
        assert config == ['search_path=pg_catalog, pg_temp']
    reason = {'reason': 'Remove former colleague details through the runtime account'}
    if bulk:
        result = runtime.command('admin', 'bulk.apply', {'item_kind': 'person.anonymise', 'items': [
            {'target_id': person['id'], 'expected_revision': person['revision']}], **reason})
    else:
        result = runtime.command('admin', 'person.anonymise', reason, person['id'], person['revision'])
    assert SECRET not in json.dumps(result)
    with transaction(runtime.app.state.engine) as db:
        assert not db.get(m.Person, person['id']).active
        assert db.get(m.Availability, row['id']).private_reason == ''
        assert record(db.get(m.DefinitionHistory, legacy['id'])) == {
            **legacy, 'snapshot': {key: value for key, value in legacy['snapshot'].items() if key != 'private_reason'}}
        assert record(db.get(m.DefinitionHistory, other['id'])) == other
        assert record(db.get(m.DefinitionHistory, unrelated_kind['id'])) == unrelated_kind
    with pytest.raises(DBAPIError) as refused, runtime.app.state.engine.begin() as db:
        db.execute(text("UPDATE definition_history SET reason = 'forged' WHERE id = :id"), {'id': legacy['id']})
    assert refused.value.orig.sqlstate == '42501'
    # A caller cannot supply keys, arbitrary row identifiers or SQL; missing person IDs change nothing.
    with runtime.app.state.engine.begin() as db:
        assert db.scalar(text('SELECT rota_redact_person_history(:person)'), {'person': "' OR true --"}) == 0
        assert db.scalar(text('SELECT rota_redact_person_history(:person)'), {'person': runtime.seed['people'][1]}) == 0
    from scripts.preflight import inspect
    result = inspect(runtime.settings, connect=True, root=runtime_preflight_root)
    assert result['passed'], result


@pytest.mark.postgres
def test_owner_privacy_migration_scrubs_only_legacy_private_keys_and_keeps_owner_and_guards(postgres_worker_url):
    schema = 'rota_test_' + uuid.uuid4().hex
    admin = create_engine(postgres_worker_url, isolation_level='AUTOCOMMIT')
    with admin.connect() as db:
        db.execute(text('CREATE SCHEMA ' + schema))
    url = make_url(postgres_worker_url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    engine = None
    try:
        config = alembic_config(url)
        command.upgrade(config, '0093_bulk_changes')
        engine = make_engine(url)
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            rows = [m.DefinitionHistory(department_id=seed['department_id'], kind=kind, object_id=m.uid(),
                object_revision=1, snapshot=snapshot, actor_id=seed['identities']['coordinator'], reason='Legacy synthetic history')
                for kind, snapshot in [('availability.update', {'private_reason': SECRET, 'kind': 'recorded_leave'}),
                    ('availability.restore', {'private_reason': None, 'kind': 'off_duty'}),
                    ('availability.update', {'kind': 'off_duty'}), ('policy', {'private_reason': SECRET}),
                    ('availability.update', ['private_reason', SECRET])]]
            db.add_all(rows); db.flush()
            before = [record(row) for row in rows]
        command.upgrade(config, '0098_bulk_privacy')
        command.upgrade(config, '0098_bulk_privacy')
        with transaction(engine) as db:
            for original in before:
                snapshot = original['snapshot']
                if original['kind'].startswith('availability') and isinstance(snapshot, dict):
                    snapshot = {key: value for key, value in snapshot.items() if key != 'private_reason'}
                assert record(db.get(m.DefinitionHistory, original['id'])) == {**original, 'snapshot': snapshot}
        with pytest.raises(DBAPIError, match='immutable evidence'), transaction(engine, write=True) as db:
            db.get(m.DefinitionHistory, before[0]['id']).reason = 'forged'
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as db:
            db.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        admin.dispose()


def test_privacy_migration_retains_explicit_owner_review_and_existing_revision():
    path = Path(__file__).resolve().parents[1] / 'migrations/versions/0098_bulk_privacy.py'
    assert safety.revision(path) == '0098_bulk_privacy'
    assert safety.findings(path) and not safety.unreviewed(path) and safety.needs_confirmation(path)
    assert 'Remove existing accidental copies once as owner' in safety.marker(path)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_anonymise_leaves_non_object_history_unchanged_instead_of_failing(request, database):
    h = request.getfixturevalue(database)
    person = h.configure('people', {'display_name': 'Synthetic malformed history owner', 'active': False})['record']
    row = availability(h, person['id'])
    with transaction(h.app.state.engine, write=True) as db:
        versions = [m.DefinitionHistory(department_id=h.seed['department_id'], kind='availability.update',
            object_id=row['id'], object_revision=100 + index, snapshot=snapshot,
            actor_id=h.seed['identities']['coordinator'], reason='Legacy non-object snapshot')
            for index, snapshot in enumerate((None, ['private_reason', 'Keep this non-object evidence']))]
        db.add_all(versions); db.flush()
        before = [record(version) for version in versions]
    h.command('admin', 'person.anonymise', {'reason': 'Remove details without rewriting unrelated history'},
              person['id'], person['revision'])
    with transaction(h.app.state.engine) as db:
        assert [record(db.get(m.DefinitionHistory, item['id'])) for item in before] == before
