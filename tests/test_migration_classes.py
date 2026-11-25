"""Owner-approved additive / bounded fill-only / destructive admission contract."""
from contextlib import closing
import sqlite3
from textwrap import dedent, indent
from types import SimpleNamespace
import time

import pytest

from migrations import safety
from scripts import manage


def migration(tmp_path, body, declared='additive', *, reviewed=True, prefix='', revision='9999_classes', down_revision=None):
    path = tmp_path / f'{revision}.py'
    declaration = f'SAFETY_CLASS = {declared!r}\n' if declared is not None else ''
    review = ('DESTRUCTIVE_REVIEWED = "Synthetic reviewed operation; restore the verified pre-deploy backup if needed."\n'
              if reviewed else '')
    path.write_text(f'from alembic import op\nimport sqlalchemy as sa\nrevision = "{revision}"\n'
                    f'down_revision = {down_revision!r}\n' + declaration + review + prefix + '\ndef upgrade():\n' +
                    indent(dedent(body).strip(), '    ') + '\n', encoding='utf-8')
    return path


@pytest.mark.parametrize('body', [
    "op.create_table('new_rows', sa.Column('id', sa.String(), primary_key=True))",
    "op.add_column('people', sa.Column('extra', sa.String(), nullable=True))",
    "op.add_column('people', sa.Column('extra', sa.String(), nullable=False, server_default=''))",
    "op.create_index('ix_people_extra', 'people', ['extra'])",
    "op.execute('CREATE TABLE new_rows (id TEXT PRIMARY KEY)')",
    "op.execute('CREATE INDEX ix_people_extra ON people (extra)')",
    '''op.execute("CREATE FUNCTION constant() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$")''',
    '''op.execute("CREATE TRIGGER protect BEFORE DELETE ON history BEGIN SELECT RAISE(ABORT, 'keep'); END")''',
])
def test_additive_requires_no_confirmation(tmp_path, body):
    path = migration(tmp_path, body)
    assert safety.classification(path) == 'additive'
    assert safety.declaration_errors(path) == []
    assert not safety.unreviewed(path) and not safety.needs_confirmation(path)


APPEND_TABLE = """
op.create_table('ordinals', sa.Column('seq', sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column('kind', sa.String()), sa.Column('target', sa.String()), sa.UniqueConstraint('kind', 'target'))
"""
APPEND = "INSERT INTO ordinals (kind, target) VALUES ('audit', NEW.id) ON CONFLICT (kind, target) DO NOTHING"


def test_new_table_copy_and_insert_observers_are_proved_without_allowing_retained_writes(tmp_path):
    body = APPEND_TABLE + '''
op.execute("INSERT INTO ordinals (kind, target) SELECT 'audit', id FROM audit_events WHERE true ORDER BY created_at, id ON CONFLICT (kind, target) DO NOTHING")
if op.get_bind().dialect.name == 'sqlite':
    op.execute("CREATE TRIGGER remember AFTER INSERT ON audit_events BEGIN ''' + APPEND + '''; END")
else:
    op.execute("""CREATE FUNCTION remember() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO ordinals (kind, target) VALUES (TG_TABLE_NAME, NEW.id) ON CONFLICT (kind, target) DO NOTHING;
        RETURN NEW; END; $$""")
    op.execute("CREATE TRIGGER remember AFTER INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION remember()")
'''
    path = migration(tmp_path, body, 'fill_only')
    assert safety.declaration_errors(path) == []
    assert not safety.needs_confirmation(path)


@pytest.mark.parametrize('sql', [
    "INSERT INTO audit_events (id) SELECT target FROM ordinals ON CONFLICT (id) DO NOTHING",
    "INSERT INTO ordinals (kind, target) SELECT 'audit', prune() ON CONFLICT (kind, target) DO NOTHING",
    "INSERT INTO ordinals (kind, target) SELECT 'audit', id FROM audit_events ON CONFLICT (kind, target) DO UPDATE SET target = excluded.target",
    "INSERT INTO ordinals (kind, target) SELECT 'audit', id FROM audit_events ON CONFLICT (seq) DO NOTHING",
    "INSERT INTO ordinals (kind, target) VALUES ('audit', NEW.id) ON CONFLICT (kind, target) DO NOTHING; DELETE FROM audit_events",
    "CREATE TRIGGER unsafe AFTER INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION unproved()",
    "CREATE TRIGGER unsafe AFTER INSERT ON audit_events BEGIN UPDATE audit_events SET reason='changed'; END",
    "CREATE FUNCTION unsafe() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN DELETE FROM audit_events; RETURN NEW; END; $$",
])
def test_sequence_metadata_proof_rejects_unproved_or_retained_side_effects(tmp_path, sql):
    path = migration(tmp_path, APPEND_TABLE + '\nop.execute(' + repr(sql) + ')', 'destructive')
    assert safety.analyze(path)['class'] == 'destructive'
    assert safety.needs_confirmation(path)


@pytest.mark.parametrize('condition', ["op.get_bind().dialect.name == untrusted()", "unknown == 'sqlite'",
    "op.get_bind().dialect.name != 'sqlite'"])
def test_only_literal_dialect_equality_is_a_proved_branch(tmp_path, condition):
    path = migration(tmp_path, 'if ' + condition + ":\n    op.create_table('new_rows', sa.Column('id', sa.String(), primary_key=True))",
                     'destructive')
    assert safety.needs_confirmation(path)


TABLE = """
op.create_table('notes', sa.Column('key', sa.String(), primary_key=True), sa.Column('fact', sa.String()))
notes = sa.table('notes', sa.column('key'), sa.column('fact'))
"""


@pytest.mark.parametrize('statement', [
    "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
    "op.get_bind().execute(sa.update(notes).where(notes.c.fact.is_(None), notes.c.key == 'one').values(fact='known'))",
    "op.execute(\"UPDATE notes SET fact = 'known' WHERE fact IS NULL AND key = 'one'\")",
])
def test_guarded_primary_key_writes_are_fill_only_batches(tmp_path, statement):
    path = migration(tmp_path, TABLE + statement, 'fill_only')
    assert safety.classification(path) == 'fill_only'
    assert safety.declaration_errors(path) == []
    assert not safety.unreviewed(path) and not safety.needs_confirmation(path)


@pytest.mark.parametrize('statement', [
    "op.get_bind().execute(notes.update().where(notes.c.key == 'one').values(fact='changed'))",
    "op.get_bind().execute(notes.update().where(notes.c.fact.is_(None)).values(fact='known'))",
    "op.get_bind().execute(notes.update().where(sa.or_(notes.c.fact.is_(None), notes.c.key == 'one')).values(fact='known'))",
    "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known', key='changed'))",
    "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)), {'fact': 'hidden'})",
    "op.get_bind().execute(notes.insert().values(key='random', fact='new'))",
    "op.execute(\"UPDATE notes SET fact = 'known' WHERE fact IS NULL\")",
    "op.execute(\"UPDATE notes SET fact = 'known' WHERE key = 'one'\")",
    "op.execute('DELETE FROM notes')",
    "op.execute('TRUNCATE notes')",
    "op.drop_column('notes', 'fact')",
    "op.rename_table('notes', 'other')",
    "op.alter_column('notes', 'fact', type_=sa.String(1))",
])
def test_unbounded_or_overwriting_writes_remain_destructive(tmp_path, statement):
    path = migration(tmp_path, TABLE + statement, 'destructive')
    assert safety.classification(path) == 'destructive'
    assert safety.declaration_errors(path) == []
    assert not safety.unreviewed(path) and safety.needs_confirmation(path)


@pytest.mark.parametrize('body', [
    "op.execute(sql_from_elsewhere)",
    "unknown_helper(op.get_bind())",
    "op.mystery_operation('people')",
    "op.execute(f'CREATE INDEX {unknown} ON people (name)')",
    "op.add_column('people', sa.Column('extra', sa.String(), nullable=False, default=''))",
    '''op.execute("DO $$ BEGIN DELETE FROM history; END $$")''',
    '''op.execute("CREATE FUNCTION prune() RETURNS void LANGUAGE plpgsql AS $$ BEGIN DELETE FROM history; END $$")''',
    '''op.execute("CREATE FUNCTION prune() RETURNS void LANGUAGE plpgsql AS $$ BEGIN DELETE FROM history; END $$; DELETE FROM history")''',
    '''op.execute("CREATE FUNCTION prune() RETURNS void LANGUAGE plpgsql AS $$ BEGIN DELETE FROM history; END $$; SELECT prune()")''',
])
def test_unknown_and_migration_time_side_effects_fail_closed(tmp_path, body):
    path = migration(tmp_path, body, 'destructive')
    assert safety.classification(path) == 'destructive'
    assert safety.findings(path) and safety.needs_confirmation(path)


@pytest.mark.parametrize('declaration,body', [
    ('additive', "op.execute('DELETE FROM notes')"),
    ('fill_only', TABLE + "op.get_bind().execute(notes.update().values(fact='changed'))"),
    ('destructive', "op.create_index('ix', 'people', ['name'])"),
    ('not-a-class', "op.create_index('ix', 'people', ['name'])"),
    (None, "op.create_index('ix', 'people', ['name'])"),
])
def test_declaration_is_verified_not_an_override(tmp_path, declaration, body):
    path = migration(tmp_path, body, declaration)
    assert safety.declaration_errors(path)
    assert safety.unreviewed(path), 'Even a reviewed marker cannot bypass an invalid declaration'
    assert safety.classification(path) == 'destructive'


def test_delivered_source_cannot_replace_its_hash_bound_declaration(tmp_path):
    original = safety.VERSIONS / '0108_password_login.py'
    changed = tmp_path / original.name
    changed.write_text(original.read_text(encoding='utf-8') + '\nSAFETY_CLASS = "additive"\n', encoding='utf-8')
    assert safety.declaration_errors(changed), 'A module constant cannot bypass delivered-source review'


def test_id_name_without_primary_key_does_not_prove_a_bounded_batch(tmp_path):
    path = migration(tmp_path, """
        op.create_table('notes', sa.Column('id', sa.String()), sa.Column('fact', sa.String()))
        notes = sa.table('notes', sa.column('id'), sa.column('fact'))
        op.get_bind().execute(notes.update().where(notes.c.id == 'one', notes.c.fact.is_(None)).values(fact='known'))
    """, 'destructive')
    assert safety.classification(path) == 'destructive' and not safety.declaration_errors(path)


@pytest.mark.parametrize('options', ['primary_key=True', 'nullable=unknown'])
def test_implicit_or_unknown_nonnull_column_without_server_default_requires_review(tmp_path, options):
    path = migration(tmp_path, f"op.add_column('notes', sa.Column('extra', sa.String(), {options}))", 'destructive')
    assert safety.analyze(path)['class'] == 'destructive'


def test_add_column_primary_key_option_is_not_a_database_constraint(tmp_path):
    path = migration(tmp_path, """
        op.create_table('notes', sa.Column('fact', sa.String()))
        op.add_column('notes', sa.Column('key', sa.String(), nullable=True, primary_key=True, server_default='same'))
        notes = sa.table('notes', sa.column('key'), sa.column('fact'))
        op.get_bind().execute(notes.update().where(notes.c.key == 'same', notes.c.fact.is_(None)).values(fact='known'))
    """, 'destructive')
    assert safety.analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('schema_on_creation', [False, True])
def test_primary_key_proofs_never_cross_sql_schemas(tmp_path, schema_on_creation):
    table = TABLE
    if schema_on_creation:
        table = table.replace("sa.Column('fact', sa.String()))", "sa.Column('fact', sa.String()), schema='other')")
    else:
        table = table.replace("sa.column('fact'))", "sa.column('fact'), schema='other')")
    path = migration(tmp_path, table + "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
                     'destructive')
    assert safety.analyze(path)['class'] == 'destructive'


def test_earlier_column_and_primary_key_are_proven_from_the_migration_graph(tmp_path):
    prior = tmp_path / '9998_prior.py'
    prior.write_text('from alembic import op\nimport sqlalchemy as sa\nrevision="9998_prior"\ndown_revision=None\n'
                     'def upgrade():\n    ' + TABLE.split('notes =')[0].strip().replace('\n', '\n    ') + '\n', encoding='utf-8')
    path = migration(tmp_path, "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
                     "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
                     'fill_only')
    path.write_text(path.read_text().replace('down_revision = None', 'down_revision = "9998_prior"'), encoding='utf-8')
    assert safety.classification(path) == 'fill_only' and not safety.declaration_errors(path)


def test_delivered_decision_backfill_requires_confirmation_when_ancestor_metadata_is_unproved():
    path = safety.VERSIONS / '0114_request_decision_times.py'
    assert safety.classification(path) == 'destructive'
    assert not safety.declaration_errors(path) and not safety.unreviewed(path)
    assert safety.needs_confirmation(path)


@pytest.mark.parametrize('revision', [
    '0002_guards', '0003_integrity', '0004_governance', '0008_series_lineage', '0010_validation_contract',
    '0019_source_evidence', '0050_publication_effective_range', '0083_swap_approval_settings', '0108_password_login',
])
def test_unproved_delivered_guards_and_retention_require_confirmation(revision):
    path = safety.VERSIONS / (revision + '.py')
    assert safety.classification(path) == 'destructive'
    assert not safety.declaration_errors(path) and not safety.unreviewed(path)
    assert safety.needs_confirmation(path)


@pytest.mark.parametrize('declared', ['destructive', 'additive'])
@pytest.mark.parametrize('existing_tables', [False, True])
def test_production_lists_only_class_three_and_never_bypasses_a_mismatch(tmp_path, monkeypatch, declared, existing_tables):
    migrations = []
    for number, (body, kind) in enumerate([
        ("op.create_index('ix_people_extra', 'people', ['extra'])", 'additive'),
        (TABLE + "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='new'))", 'fill_only'),
        ("op.execute('DELETE FROM notes')", declared),
    ]):
        directory = tmp_path / str(number)
        directory.mkdir()
        path = migration(directory, body, kind)
        revision = f'999{number}_case'
        path.write_text(path.read_text().replace('9999_classes', revision), encoding='utf-8')
        migrations.append(SimpleNamespace(revision=revision, path=path))
    graph = SimpleNamespace(iterate_revisions=lambda *args: list(reversed(migrations)),
                            get_current_head=lambda: migrations[-1].revision)
    monkeypatch.setattr(manage.ScriptDirectory, 'from_config', lambda config: graph)
    monkeypatch.setenv('ROTA_ENV', 'production')
    database = tmp_path / 'unversioned.db'
    with closing(sqlite3.connect(database)) as db, db:
        if existing_tables:
            db.execute('CREATE TABLE notes (key TEXT, fact TEXT)')
    url = 'sqlite:///' + str(database)
    if declared == 'additive':
        with pytest.raises(SystemExit, match='SAFETY_CLASS declarations'):
            manage.check_migrations(url, confirmed=('9992_case',))
    elif existing_tables:
        with pytest.raises(SystemExit, match='--confirm-destructive 9992_case '):
            manage.check_migrations(url, confirmed=('9990_case', '9991_case'))
        assert manage.check_migrations(url, confirmed=('9992_case',)) == [item.revision for item in migrations]
    else:
        assert manage.check_migrations(url) == [item.revision for item in migrations]


@pytest.mark.parametrize('body,prefix', [
    ("sql = 'SELECT 1'\nif unknown:\n    sql = 'DELETE FROM notes'\nop.execute(sql)", ''),
    ("sql = 'SELECT 1'\nfor item in (1,):\n    sql = 'DELETE FROM notes'\nop.execute(sql)", ''),
    ("sql = 'SELECT 1'\nfor item in unknown:\n    sql = 'DELETE FROM notes'\nop.execute(sql)", ''),
    ("sql = 'SELECT 1'\ntry:\n    sql = 'DELETE FROM notes'\nexcept Exception:\n    sql = 'SELECT 1'\nop.execute(sql)", ''),
    ("sql = 'SELECT 1'\nsql += '; DELETE FROM notes'\nop.execute(sql)", ''),
    ("sql = 'SELECT 1'\n[(sql := 'DELETE FROM notes') for item in (1,)]\nop.execute(sql)", ''),
    ("run('DELETE FROM notes')", "sql = 'SELECT 1'\ndef run(sql):\n    op.execute(sql)\n"),
    ("run = op.get_bind().scalar\nrun(sa.text('DELETE FROM notes RETURNING id'))", 'def run():\n    pass\n'),
    ("op.get_bind().scalar(sa.text('DELETE FROM notes RETURNING id'))", ''),
    ("op.get_bind().scalars(sa.text('DELETE FROM notes RETURNING id'))", ''),
    ("op.get_bind().execute(sa.select(sa.text('prune_history()')))", ''),
    ("op.get_bind().execute(sa.select(sa.literal_column('1; DELETE FROM notes')))", ''),
    ("op.add_column('notes', sa.Column('new', sa.Integer(), nullable=False, server_default=sa.text('prune_history()')))", ''),
    ("op.create_index('ix', 'notes', [sa.text('prune_history()')])", ''),
    ("op.create_check_constraint('ck', 'notes', 'prune_history() > 0')", ''),
    ("import os\nos.replace('live.db', 'other.db')", ''),
    (TABLE + "op.get_bind().execute(notes.update().where(notes.c.fact.is_(None), notes.c.key == notes.c.key + '').values(fact='new'))", ''),
])
def test_control_flow_aliases_and_embedded_expressions_cannot_hide_effects(tmp_path, body, prefix):
    path = migration(tmp_path, body, 'destructive', prefix=prefix)
    assert safety.analyze(path)['class'] == 'destructive'
    assert not safety.declaration_errors(path)


@pytest.mark.parametrize('prior_body', [
    TABLE + "op.execute('ALTER TABLE notes DROP CONSTRAINT notes_pkey')",
    TABLE + "op.drop_table('notes')\nop.create_table('notes', sa.Column('key', sa.String()), sa.Column('fact', sa.String()))",
    TABLE + "op.execute('DROP TABLE notes')\nop.create_table('notes', sa.Column('key', sa.String()), sa.Column('fact', sa.String()))",
])
def test_stale_primary_keys_cannot_prove_a_batch(tmp_path, prior_body):
    prior = migration(tmp_path, prior_body, 'destructive')
    previous = tmp_path / '9998_prior.py'
    previous.write_text(prior.read_text().replace('9999_classes', '9998_prior'), encoding='utf-8')
    path = migration(tmp_path, "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
                     "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
                     'destructive')
    path.write_text(path.read_text().replace('down_revision = None', 'down_revision = "9998_prior"'), encoding='utf-8')
    assert safety.analyze(path)['class'] == 'destructive'


def test_dormant_helper_does_not_manufacture_schema_evidence(tmp_path):
    prefix = "def unused():\n    op.create_table('notes', sa.Column('key', sa.String(), primary_key=True), sa.Column('fact', sa.String()))\n"
    path = migration(tmp_path, "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
                     "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
                     'destructive', prefix=prefix)
    assert safety.analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('conditional,expected', [(False, 'fill_only'), (True, 'destructive')])
def test_new_table_upsert_requires_a_proven_new_table(tmp_path, conditional, expected):
    body = TABLE.replace("sa.Column('fact', sa.String()))", f"sa.Column('fact', sa.String()), if_not_exists={conditional})")
    body += "op.get_bind().execute(insert(notes).values(key='stable', fact='new').on_conflict_do_nothing())"
    path = migration(tmp_path, body, expected, prefix='from sqlalchemy.dialects.sqlite import insert\n')
    assert safety.analyze(path)['class'] == expected
    assert not safety.declaration_errors(path)


@pytest.mark.cpu_budget
def test_delivered_source_classification_cpu_budget():
    import coverage
    from migrations.source_safety import analyze_sources, parse, schema_at
    analyze_sources.cache_clear()
    parse.cache_clear()
    schema_at.cache_clear()
    began, cpu = time.perf_counter(), time.process_time()
    for path in sorted(safety.VERSIONS.glob('*.py')):
        safety.analyze(path)
    elapsed, cpu = time.perf_counter() - began, time.process_time() - cpu
    budget = 6.0 * (7 if coverage.Coverage.current() else 1)
    print(f'migration-classification: wall={elapsed:.3f}s cpu={cpu:.3f}s budget={budget:.1f}s')
    assert cpu < budget, (cpu, budget)


def test_delivered_inventory_declarations_and_documented_counts_agree():
    from collections import Counter
    import json
    import re

    paths = sorted(safety.VERSIONS.glob('*.py'))
    for path in paths:
        revision = safety.revision(path)
        assert len(revision) <= 32, path.name
        assert not safety.declaration_errors(path), path.name
    classified = {safety.revision(path): safety.analyze(path)['class'] for path in paths}
    records = json.loads((safety.VERSIONS.parent / 'safety_declarations.json').read_text(encoding='utf-8'))
    assert {revision: record['class'] for revision, record in records.items()} == classified



def test_null_primary_key_comparison_does_not_prove_a_bounded_fill(tmp_path):
    from contextlib import closing
    import sqlite3

    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript("CREATE TABLE notes (key TEXT PRIMARY KEY, fact TEXT); "
            "INSERT INTO notes VALUES (NULL, NULL), (NULL, NULL)")
        assert db.execute('UPDATE notes SET fact = ? WHERE key IS NULL AND fact IS NULL', ('known',)).rowcount == 2
    body = TABLE.replace('primary_key=True', 'primary_key=True, nullable=True')
    body += "op.execute(notes.update().where(notes.c.key == None, notes.c.fact.is_(None)).values(fact='known'))"
    assert safety.analyze(migration(tmp_path, body, 'destructive'))['class'] == 'destructive'

@pytest.mark.parametrize('mode', ['clear', 'duplicate', 'missing', 'wrong', 'write', 'query-error', 'fresh'])
def test_manage_checks_migration_duplicates_read_only(tmp_path, monkeypatch, mode):
    sql = ('CREATE UNIQUE INDEX uq_label ON items (scope, lower(trim(label))) '
           'WHERE active IS TRUE')
    check = ('SELECT 1 FROM items WHERE (active IS TRUE) AND (scope) IS NOT NULL '
             'AND (lower(trim(label))) IS NOT NULL GROUP BY scope, lower(trim(label)) HAVING count(*) > 1')
    declared = {
        'missing': (),
        'wrong': ('SELECT 1 FROM items WHERE FALSE',),
        'write': (check, 'DELETE FROM items'),
        'query-error': (check, 'SELECT 1 FROM missing_table'),
    }.get(mode, (check,))
    path = migration(tmp_path, f'op.execute({sql!r})', prefix=f'DUPLICATE_PRECHECKS = {declared!r}\n')
    pending = SimpleNamespace(revision='9999_classes', path=path)
    script = SimpleNamespace(iterate_revisions=lambda *args: [pending],
                             get_current_head=lambda: pending.revision)
    monkeypatch.setattr(manage.ScriptDirectory, 'from_config', lambda config: script)
    db_path = tmp_path / 'precheck.db'
    if mode != 'fresh':
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript('CREATE TABLE alembic_version (version_num TEXT); '
                             "INSERT INTO alembic_version VALUES ('earlier'); "
                             'CREATE TABLE items (scope INT, label TEXT, active BOOLEAN)')
            rows = [(1, ' Ab ', True), (2, 'ab', True), (1, 'ab', False),
                    (1, None, True), (1, None, True), (None, 'ab', True), (None, 'ab', True)]
            if mode == 'duplicate':
                rows.append((1, 'aB', True))
            db.executemany('INSERT INTO items VALUES (?, ?, ?)', rows)

    url = 'sqlite:///' + str(db_path)
    failures = {'duplicate': 'duplicate rows', 'missing': 'missing duplicate pre-check',
                'wrong': 'missing duplicate pre-check', 'write': 'must be a read-only SELECT',
                'query-error': 'could not run duplicate pre-check'}
    if mode in failures:
        with pytest.raises(SystemExit, match=failures[mode]):
            manage.check_migrations(url)
    else:
        assert manage.check_migrations(url) == [pending.revision]
    if mode != 'fresh':
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute('SELECT scope, label, active FROM items').fetchall() == rows
            assert not db.execute('SELECT name FROM sqlite_master WHERE type = \'index\'').fetchall()


def pending_for_preflight(monkeypatch, *paths):
    pending = [SimpleNamespace(revision=safety.revision(path), path=path) for path in reversed(paths)]
    script = SimpleNamespace(iterate_revisions=lambda *args: pending,
                             get_current_head=lambda: pending[0].revision)
    monkeypatch.setattr(manage.ScriptDirectory, 'from_config', lambda config: script)
    return pending[0].revision


def test_manage_requires_duplicate_precheck_after_ancestor_dependent_fill(tmp_path, monkeypatch):
    prior = tmp_path / 'earlier.py'
    prior.write_text('from alembic import op\nimport sqlalchemy as sa\nrevision = "earlier"\n'
                     'down_revision = None\n\ndef upgrade():\n'
                     '    op.create_table("items", sa.Column("id", sa.Integer(), primary_key=True), '
                     'sa.Column("label", sa.Text()), sa.Column("marker", sa.Text()))\n', encoding='utf-8')
    sql = ("UPDATE items SET marker = 'filled' WHERE id = 1 AND marker IS NULL; "
           "CREATE UNIQUE INDEX uq_label ON items (lower(trim(label)))")
    path = migration(tmp_path, f'op.execute({sql!r})', declared='fill_only')
    path.write_text(path.read_text(encoding='utf-8').replace('down_revision = None', 'down_revision = "earlier"'),
                    encoding='utf-8')
    assert safety.classification(path) == 'fill_only'
    pending_for_preflight(monkeypatch, path)
    db_path = tmp_path / 'precheck.db'
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript("CREATE TABLE alembic_version (version_num TEXT); "
                         "INSERT INTO alembic_version VALUES ('earlier'); "
                         "CREATE TABLE items (id INT PRIMARY KEY, label TEXT, marker TEXT); "
                         "INSERT INTO items (id, label) VALUES (1, ' Ab '), (2, 'ab')")

    with pytest.raises(SystemExit, match='missing duplicate pre-check'):
        manage.check_migrations('sqlite:///' + str(db_path))
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute('SELECT marker FROM items ORDER BY id').fetchall() == [(None,), (None,)]


@pytest.mark.parametrize(('mode', 'split'), [
    ('key', False), ('key', True), ('predicate', True), ('sqlalchemy', True),
    ('marker', False), ('marker', True), ('unproved', True),
])
def test_manage_refuses_prechecks_that_pending_fills_can_invalidate(tmp_path, monkeypatch, mode, split):
    migration(tmp_path, 'op.create_table("items", sa.Column("id", sa.Integer(), primary_key=True), '
              'sa.Column("label", sa.Text()), sa.Column("active", sa.Boolean()), sa.Column("marker", sa.Text()))',
              revision='earlier')
    column, value = {'predicate': ('active', 'TRUE'), 'marker': ('marker', "'filled'")}.get(mode, ('label', "'A'"))
    fill = f'UPDATE items SET {column} = {value} WHERE id = 1'
    if mode != 'unproved':
        fill += f' AND {column} IS NULL'
    write = f'op.execute({fill!r})'
    if mode == 'sqlalchemy':
        write = ("items = sa.table('items', sa.column('id'), sa.column('label'))\n"
                 "op.get_bind().execute(items.update().where(items.c.id == 1, items.c.label.is_(None)).values(label='A'))")
    predicate = ' WHERE "active" IS TRUE' if mode == 'predicate' else ''
    index = f'CREATE UNIQUE INDEX uq_label ON items (lower(label)){predicate}'
    inputs = '("active" IS TRUE) AND ' if predicate else ''
    check = f'SELECT 1 FROM items WHERE {inputs}(lower(label)) IS NOT NULL GROUP BY lower(label) HAVING count(*) > 1'
    prefix = f'DUPLICATE_PRECHECKS = {(check,)!r}\n'

    if split:
        kind = 'destructive' if mode == 'unproved' else 'fill_only'
        fill_path = migration(tmp_path, write, kind, revision='fill', down_revision='earlier')
        assert safety.classification(fill_path) == kind
        index_path = migration(tmp_path, f'op.execute({index!r})', prefix=prefix, down_revision='fill')
        pending_for_preflight(monkeypatch, fill_path, index_path)
    else:
        sql = f'{fill}; {index}'
        path = migration(tmp_path, f'op.execute({sql!r})', 'fill_only',
                         prefix=prefix, down_revision='earlier')
        assert safety.classification(path) == 'fill_only'
        pending_for_preflight(monkeypatch, path)

    db_path = tmp_path / 'precheck.db'
    rows = [(1, 'A' if predicate else None, None, None), (2, 'a', True, None)]
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript("CREATE TABLE alembic_version (version_num TEXT); INSERT INTO alembic_version VALUES ('earlier'); "
                         'CREATE TABLE items (id INT PRIMARY KEY, label TEXT, active BOOLEAN, marker TEXT)')
        db.executemany('INSERT INTO items VALUES (?, ?, ?, ?)', rows)
        assert db.execute(check).fetchone() is None

    url = 'sqlite:///' + str(db_path)
    if mode == 'marker':
        assert manage.check_migrations(url) == (['fill'] if split else []) + ['9999_classes']
    else:
        source = 'unproved pending migration' if mode == 'unproved' else 'pending fill'
        with pytest.raises(SystemExit, match=source + ' may change unique expression index inputs before creation'):
            manage.check_migrations(url, confirmed=('fill',))
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute('SELECT * FROM items ORDER BY id').fetchall() == rows
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'uq_label'").fetchall()
        db.execute(fill)
        assert (db.execute(check).fetchone() is None) == (mode == 'marker')
        db.rollback()


@pytest.mark.parametrize('mode', ['sql', 'alembic', 'declared', 'conditional', 'populated'])
def test_manage_exempts_only_proven_empty_new_index_tables(tmp_path, monkeypatch, mode):
    table = ('CREATE TABLE IF NOT EXISTS items (id INT PRIMARY KEY, label TEXT)' if mode == 'conditional'
             else 'CREATE TABLE items (id INT PRIMARY KEY, label TEXT)')
    body = ('op.create_table("items", sa.Column("id", sa.Integer(), primary_key=True), '
            'sa.Column("label", sa.Text()))' if mode in {'alembic', 'populated'} else f'op.execute({table!r})')
    if mode == 'populated':
        body += '\nop.execute("INSERT INTO items (id, label) VALUES (1, \'a\'), (2, \'A\') ON CONFLICT DO NOTHING")'
    body += '\nop.execute("CREATE UNIQUE INDEX uq_label ON items (lower(label))")'
    check = 'SELECT 1 FROM items WHERE (lower(label)) IS NOT NULL GROUP BY lower(label) HAVING count(*) > 1'
    prefix = f'DUPLICATE_PRECHECKS = {(check,)!r}\n' if mode == 'declared' else ''
    path = migration(tmp_path, body, declared='fill_only' if mode == 'populated' else 'additive', prefix=prefix)
    revision = pending_for_preflight(monkeypatch, path)
    db_path = tmp_path / 'precheck.db'
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript("CREATE TABLE alembic_version (version_num TEXT); "
                         "INSERT INTO alembic_version VALUES ('earlier'); "
                         "CREATE TABLE retained (id INT)")

    url = 'sqlite:///' + str(db_path)
    if mode in {'conditional', 'populated'}:
        with pytest.raises(SystemExit, match='missing duplicate pre-check'):
            manage.check_migrations(url)
    else:
        assert manage.check_migrations(url) == [revision]
        with closing(sqlite3.connect(db_path)) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
            assert tables == [('alembic_version',), ('retained',)]


def test_manage_runs_qualified_duplicate_precheck(tmp_path, monkeypatch, postgres_worker_url):
    from uuid import uuid4
    from .conftest import postgres_database

    key = "pg_catalog.lower(pg_catalog.btrim(label, '-'))"
    sql = f'CREATE UNIQUE INDEX uq_label ON items ({key})'
    query = f'SELECT 1 FROM items WHERE ({key}) IS NOT NULL GROUP BY {key} HAVING count(*) > 1'
    path = migration(tmp_path, f'op.execute({sql!r})', prefix=f'DUPLICATE_PRECHECKS = {(query,)!r}\n')
    revision = pending_for_preflight(monkeypatch, path)

    with postgres_database(postgres_worker_url, 'rota_test_' + uuid4().hex) as url:
        engine = manage.make_engine(url)
        try:
            with engine.begin() as db:
                db.exec_driver_sql('CREATE TABLE alembic_version (version_num TEXT)')
                db.exec_driver_sql("INSERT INTO alembic_version VALUES ('earlier')")
                db.exec_driver_sql('CREATE TABLE items (label TEXT)')
                db.exec_driver_sql("INSERT INTO items VALUES ('-Ab-'), ('cd'), (NULL), (NULL)")
            assert manage.check_migrations(url) == [revision]
            with engine.begin() as db:
                db.exec_driver_sql("INSERT INTO items VALUES ('ab')")
            with pytest.raises(SystemExit, match='duplicate rows'):
                manage.check_migrations(url)
        finally:
            engine.dispose()
