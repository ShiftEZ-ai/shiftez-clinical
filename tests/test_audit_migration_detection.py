"""maintenance: SQLAlchemy backfills use the same review/confirmation gate as SQL."""
from contextlib import closing
import sqlite3
from types import SimpleNamespace

import pytest

from migrations import safety
from scripts import manage


UPDATES = (
    'op.execute("UPDATE notifications SET read_at = acknowledged_at")',
    'bind.execute(sa.text("UPDATE notifications SET read_at = acknowledged_at"))',
    'bind.execute(notes.update().where(notes.c.read_at.is_(None)).values(read_at=notes.c.acknowledged_at))',
    'bind.execute(notes.update().values(read_at=None))',
    'bind.execute(notes.update(), {"read_at": None})',
    'bind.execute(sa.update(notes).values(read_at=None))',
    'bind.execute(update(notes).where(notes.c.read_at.is_(None)).values(read_at=None))',
    'change = sa.update; bind.execute(change(notes), {"read_at": None})',
)


def migration(tmp_path, body, *, reviewed=False, declared='destructive'):
    path = tmp_path / '9999_backfill.py'
    reason = ('DESTRUCTIVE_REVIEWED = "Synthetic backfill review; recover using the verified pre-deploy backup."\n'
              if reviewed else '')
    path.write_text('from alembic import op\nimport sqlalchemy as sa\n'
                    'from sqlalchemy import update\n'
                    'revision = "9999_backfill"\n' + f'SAFETY_CLASS = {declared!r}\n' + reason +
                    'def upgrade():\n'
                    '    notes = sa.table("notifications", sa.column("read_at"), sa.column("acknowledged_at"))\n'
                    '    bind = op.get_bind()\n    ' + body + '\n', encoding='utf-8')
    return path


@pytest.mark.parametrize('body', UPDATES)
@pytest.mark.parametrize('reviewed', [False, True])
@pytest.mark.parametrize('existing_tables', [False, True])
def test_equivalent_updates_require_review_and_exact_production_confirmation(tmp_path, monkeypatch, body, reviewed, existing_tables):
    path = migration(tmp_path, body, reviewed=reviewed)
    assert safety.classification(path) == 'destructive' and not safety.declaration_errors(path)
    assert safety.findings(path)
    assert bool(safety.unreviewed(path)) is not reviewed
    assert safety.needs_confirmation(path)
    graph = SimpleNamespace(iterate_revisions=lambda *args: [SimpleNamespace(revision='9999_backfill', path=path)],
                            get_current_head=lambda: '9999_backfill')
    monkeypatch.setattr(manage.ScriptDirectory, 'from_config', lambda config: graph)
    monkeypatch.setenv('ROTA_ENV', 'production')
    database = tmp_path / 'unversioned.db'
    with closing(sqlite3.connect(database)) as db, db:
        if existing_tables:
            db.execute('CREATE TABLE notifications (read_at TEXT, acknowledged_at TEXT)')
    url = 'sqlite:///' + str(database)
    if not reviewed:
        with pytest.raises(SystemExit, match='without DESTRUCTIVE_REVIEWED'):
            manage.check_migrations(url, confirmed=('9999_backfill',))
    elif existing_tables:
        for confirmed in ((), ('9998_wrong_revision',)):
            with pytest.raises(SystemExit, match='--confirm-destructive 9999_backfill'):
                manage.check_migrations(url, confirmed=confirmed)
        assert manage.check_migrations(url, confirmed=('9999_backfill',)) == ['9999_backfill']
    else:
        assert manage.check_migrations(url) == ['9999_backfill']


@pytest.mark.parametrize('setup,statement', [
    ('import sqlalchemy as sql', 'sql.Table("notes", sql.MetaData(), sql.Column("read_at", sql.DateTime()))'),
    ('from sqlalchemy import table as make_table, column', 'make_table("notes", column("read_at"))'),
    ('from sqlalchemy import Table, MetaData, Column, DateTime', 'Table("notes", MetaData(), Column("read_at", DateTime()))'),
])
def test_table_constructor_aliases_are_detected(tmp_path, setup, statement):
    path = tmp_path / 'alias.py'
    path.write_text('from alembic import op\n' + setup + '\nrevision = "9999_alias"\n'
                    'SAFETY_CLASS = "destructive"\ndef upgrade():\n    notes = ' + statement +
                    '\n    op.get_bind().execute(notes.update().values(read_at=None))\n', encoding='utf-8')
    assert safety.findings(path) and not safety.declaration_errors(path)
    assert safety.unreviewed(path) and safety.needs_confirmation(path)


@pytest.mark.parametrize('body', [
    'notes.insert().values(read_at=None)',
    'sa.select(notes).where(notes.c.read_at.is_(None))',
    'op.add_column("notifications", sa.Column("new_fact", sa.String()))',
    '"""notes.update().values(read_at=None)"""',
])
def test_modelled_additive_operations_stay_unflagged(tmp_path, body):
    path = migration(tmp_path, body, declared='additive')
    assert safety.findings(path) == []
    assert safety.classification(path) == 'additive' and not safety.declaration_errors(path)
    assert not safety.needs_confirmation(path)


@pytest.mark.parametrize('body', [
    'existing = set(); existing.update(["receipt"])',
    'proven = {}; proven.update((key, state) for key, state in [])',
    'proven = {}; proven.update(); proven.values()',
    'something_completely_new()',
])
def test_unrecognised_calls_require_review_even_without_known_sql(tmp_path, body):
    path = migration(tmp_path, body)
    assert safety.analyze(path)['class'] == 'destructive'
    assert safety.findings(path) and safety.needs_confirmation(path)


@pytest.mark.parametrize('revision,classification', [
    ('0065_leave_types', 'destructive'), ('0080_announcement_receipts', 'destructive'),
    ('0114_request_decision_times', 'destructive'),
])
def test_delivered_backfill_declarations_require_exact_source(tmp_path, revision, classification):
    path = safety.VERSIONS / (revision + '.py')
    assert safety.classification(path) == classification and not safety.declaration_errors(path)
    assert not safety.unreviewed(path)
    assert safety.needs_confirmation(path) is (classification == 'destructive')
    assert (revision in safety.reviewed_confirmations()) is (classification == 'destructive')
    if classification == 'destructive':
        assert safety.findings(path) and safety.marker(path)
    assert revision not in safety.GRANDFATHERED
    changed = tmp_path / path.name
    changed.write_bytes(path.read_bytes() + b'\n# Changed after the recorded review.\n')
    assert safety.marker(changed) is None
    assert safety.declaration_errors(changed)
    assert safety.unreviewed(changed) and safety.needs_confirmation(changed)
