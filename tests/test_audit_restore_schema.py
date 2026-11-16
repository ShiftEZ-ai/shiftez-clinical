"""BE-12: structural drill success requires every table in the restored app image."""
import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine

from apps.api.models import Base
from apps.api.schema import supported_revision
from scripts import restore_drill


def database(tmp_path, names, head):
    url = 'sqlite:///' + str(tmp_path / 'synthetic.db')
    metadata = MetaData()
    for name in names:
        Table(name, metadata, Column('id', String, primary_key=True))
    versions = Table('alembic_version', metadata, Column('version_num', String))
    engine = create_engine(url)
    try:
        with engine.begin() as db:
            # sqlite3 legacy mode otherwise commits each synthetic table separately.
            db.exec_driver_sql('BEGIN')
            metadata.create_all(db)
            db.execute(versions.insert().values(version_num=head))
    finally:
        engine.dispose()
    return url


def drill(tmp_path, monkeypatch, url):
    # Inspect the real synthetic schema; archive transport is outside this structural check.
    monkeypatch.setattr(restore_drill, 'restore', lambda *args, **kwargs: {'sessions_revoked': True})
    return restore_drill.drill(tmp_path / 'synthetic.dump', url)


def test_current_head_with_only_count_summary_tables_fails(tmp_path, monkeypatch):
    url = database(tmp_path, restore_drill.COUNTED, supported_revision())
    result = drill(tmp_path, monkeypatch, url)
    assert result['head_ok']
    assert not result['ok'] and not result['tables_ok']
    assert result['missing_tables'] == sorted(set(Base.metadata.tables) - set(restore_drill.COUNTED))
    assert set(result['counts']) == set(restore_drill.COUNTED)


@pytest.mark.parametrize('missing', sorted(set(Base.metadata.tables) - set(restore_drill.COUNTED)))
def test_each_non_counted_model_table_is_required(tmp_path, monkeypatch, missing):
    url = database(tmp_path, set(Base.metadata.tables) - {missing}, supported_revision())
    result = drill(tmp_path, monkeypatch, url)
    assert result['head_ok']
    assert not result['ok'] and not result['tables_ok']
    assert result['missing_tables'] == [missing]


def test_complete_migrated_schema_passes_with_small_count_summary(h, tmp_path, monkeypatch):
    result = drill(tmp_path, monkeypatch, h.settings.database_url)
    assert result['ok'] and result['head_ok'] and result['tables_ok']
    assert result['missing_tables'] == []
    assert set(result['counts']) == set(restore_drill.COUNTED)
    assert result['counts']['identities'] > 0


@pytest.mark.parametrize('missing', [False, True])
def test_required_schema_comes_from_restored_images_own_models(tmp_path, monkeypatch, missing):
    from apps.api import schema
    metadata = MetaData()
    for name in ('identities', 'image_evidence'):
        Table(name, metadata, Column('id', String, primary_key=True))
    monkeypatch.setattr(Base, 'metadata', metadata)
    monkeypatch.setattr(schema, 'supported_revision', lambda: 'older_image_head')
    url = database(tmp_path, {'identities'} if missing else set(metadata.tables), 'older_image_head')
    result = drill(tmp_path, monkeypatch, url)
    assert result['head_ok'] and result['expected_head'] == 'older_image_head'
    assert result['ok'] is not missing and result['tables_ok'] is not missing
    assert result['missing_tables'] == (['image_evidence'] if missing else [])
    assert result['counts'] == {'identities': 0}


def test_synthetic_schema_creation_batches_ddl_in_one_transaction(tmp_path, monkeypatch):
    from sqlalchemy import event
    statements = []
    raw_create_engine = create_engine

    def traced_engine(url):
        engine = raw_create_engine(url)
        event.listen(engine, 'connect', lambda connection, _: connection.set_trace_callback(statements.append))
        return engine

    monkeypatch.setattr(__name__ + '.create_engine', traced_engine)
    database(tmp_path, {'synthetic_one', 'synthetic_two'}, 'synthetic_head')
    assert [sql for sql in statements if sql in ('BEGIN', 'COMMIT', 'ROLLBACK')] == ['BEGIN', 'COMMIT']
    ddl = [i for i, sql in enumerate(statements) if sql.lstrip().upper().startswith('CREATE TABLE')]
    inserted = [i for i, sql in enumerate(statements) if sql.lstrip().upper().startswith('INSERT INTO ALEMBIC_VERSION')]
    assert len(ddl) == 3 and len(inserted) == 1
    assert statements.index('BEGIN') < min(ddl) <= max(ddl) < inserted[0] < statements.index('COMMIT')
