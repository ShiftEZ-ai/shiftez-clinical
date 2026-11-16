"""Proofs cannot survive hidden SQL expressions or uncertain execution paths."""
from textwrap import dedent

import pytest

from migrations.source_safety import analyze

from .test_migration_classes import TABLE, migration


@pytest.mark.parametrize('body,prefix', [
    ("sql = ['SELECT 1']\nsql.append('DELETE FROM notes')\nop.execute('; '.join(sql))", ''),
    ("sql = ['SELECT 1']\nsql.extend(['DELETE FROM notes'])\nop.execute('; '.join(sql))", ''),
    ("sql = {'SELECT 1'}\nsql.add('DELETE FROM notes')\nop.execute('; '.join(sql))", ''),
    ("sql = ['SELECT 1']\nalias = sql\nalias.append('DELETE FROM notes')\nop.execute('; '.join(sql))", ''),
    ("sql = (['SELECT 1'],)\nsql[0].append('DELETE FROM notes')\nop.execute('; '.join(sql[0]))", ''),
    ("sql = ['SELECT 1']\nalias = sql\nsql += ['DELETE FROM notes']\nop.execute('; '.join(alias))", ''),
    ("mutate()\nop.execute('; '.join(sql))", "sql = ['SELECT 1']\ndef mutate():\n    sql.append('DELETE FROM notes')\n"),
])
def test_mutable_sql_sources_require_confirmation(tmp_path, body, prefix):
    from migrations import safety

    path = migration(tmp_path, body, 'destructive', prefix=prefix)
    assert analyze(path)['class'] == 'destructive'
    assert not safety.declaration_errors(path)
    assert safety.needs_confirmation(path)


@pytest.mark.parametrize('queries', ["('SELECT 1', 'SELECT 2')", "['SELECT 1', 'SELECT 2']"])
def test_computed_sql_requires_review_even_with_literal_inputs(tmp_path, queries):
    path = migration(tmp_path, f"sql = {queries}\nop.execute('; '.join(sql))", 'destructive')
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('body,prefix', [
    ("fragment = sa.text('prune_history()')\nop.get_bind().execute(sa.select(fragment))", ''),
    ("fragment = sa.text('prune_history()')\nquery = sa.select(fragment)\nop.get_bind().execute(query)", ''),
    ("fragment = sa.text('prune_history()')\nop.add_column('notes', sa.Column('extra', sa.Integer(), nullable=False, server_default=fragment))", ''),
    ("constraint = sa.CheckConstraint('prune_history() > 0')\nop.create_table('extra', sa.Column('id', sa.Integer()), constraint)", ''),
    ("fragment = sa.text('prune_history()')\nop.create_index('ix_notes_side_effect', 'notes', [fragment])", ''),
    ("op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact=sa.text('prune_history()')))", ''),
    ("fragment = sa.text('prune_history()')\nop.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact=fragment))", ''),
    ("op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None), sa.text('prune_history() > 0')).values(fact='known'))", ''),
    ("op.get_bind().execute(insert(notes).values(key='one', fact=sa.text('prune_history()')).on_conflict_do_nothing())",
     'from sqlalchemy.dialects.sqlite import insert\n'),
], ids=['select_alias', 'select_query_alias', 'default_alias', 'constraint_alias', 'index_alias',
        'update_text', 'update_text_alias', 'where_text', 'insert_text'])
def test_embedded_textual_sql_requires_a_read_only_proof(tmp_path, body, prefix):
    path = migration(tmp_path, TABLE + body, 'destructive', prefix=prefix)
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('body', [
    """
    sql = 'SELECT 1'
    for unused in range(2):
        op.execute(sql)
        sql = 'DELETE FROM notes'
    """,
    """
    sql = 'SELECT 1'
    while pending:
        op.execute(sql)
        sql = 'DELETE FROM notes'
    """,
    """
    sql = 'SELECT 1'
    match flag:
        case True:
            sql = 'DELETE FROM notes'
        case _:
            sql = 'SELECT 1'
    op.execute(sql)
    """,
    """
    sql = 'DELETE FROM notes'
    try:
        int('invalid')
        sql = 'SELECT 1'
    except ValueError:
        op.execute(sql)
    """,
], ids=['for_second_iteration', 'while_second_iteration', 'match_alternative', 'exception_before_assignment'])
def test_sql_proofs_cover_every_control_flow_path(tmp_path, body):
    path = migration(tmp_path, body, 'destructive')
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('body', [
    """
    for row in op.get_bind().execute(sa.select(notes)).mappings():
        row = {'key': notes.c.key}
        op.get_bind().execute(notes.update().where(notes.c.key == row['key'], notes.c.fact.is_(None)).values(fact='known'))
    """,
    """
    for row in ({'key': notes.c.key} for unused in op.get_bind().execute(sa.select(notes))):
        op.get_bind().execute(notes.update().where(notes.c.key == row['key'], notes.c.fact.is_(None)).values(fact='known'))
    """,
], ids=['reassigned_mapping', 'transformed_iterator'])
def test_batch_bound_requires_an_unchanged_database_row(tmp_path, body):
    path = migration(tmp_path, TABLE + dedent(body), 'destructive')
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('mutation', [
    "op.execute('SELECT remove_notes_primary_key()')",
    "sql = ['SELECT 1']\nmutate = sql.append\nmutate('ALTER TABLE notes DROP CONSTRAINT notes_pkey')\nop.execute('; '.join(sql))",
    "op.execute(sa.select(sa.text('remove_notes_primary_key()')))",
    "something_completely_new()",
    "sql = ['SELECT 1']\nsql.append('ALTER TABLE notes DROP CONSTRAINT notes_pkey')\nop.execute('; '.join(sql))",
    "sql = ['SELECT 1']\nsql.append('ALTER TABLE notes DROP CONSTRAINT notes_pkey')\nop.execute(sa.text('; '.join(sql)))",
    "sql = ['SELECT 1']\nsql[0] = 'ALTER TABLE notes DROP CONSTRAINT notes_pkey'\nop.execute('; '.join(sql))",
    "op.execute(unproved.select())",
    "fragment = ''.join([''])\nop.execute(f'CREATE FUNCTION noop() RETURNS int LANGUAGE sql AS $$ SELECT 1{fragment} $$; ALTER TABLE notes DROP CONSTRAINT notes_pkey')",
    "op.execute('ALTER TABLE ONLY notes DROP CONSTRAINT notes_pkey')",
], ids=['unknown_function', 'aliased_append', 'embedded_executable_text', 'unrecognised_call',
        'mutated_sql', 'mutated_text_sql', 'assigned_sql', 'unknown_select',
        'routine_template_suffix', 'unparsed_schema_target'])
def test_unproved_ancestor_sql_cannot_preserve_primary_key_evidence(tmp_path, mutation):
    base = tmp_path / '0001_base.py'
    base.write_text('from alembic import op\nimport sqlalchemy as sa\nrevision = "0001_base"\n'
                    'down_revision = None\ndef upgrade():\n' +
                    '\n'.join('    ' + line for line in (TABLE + mutation).splitlines()) + '\n', encoding='utf-8')
    from migrations.source_safety import schema_at
    assert analyze(base)['class'] == 'destructive'
    assert not schema_at((base.read_text(encoding='utf-8'),))['tables']['notes']['primary_key']
    path = migration(tmp_path,
        "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
        "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
        'destructive')
    path.write_text(path.read_text(encoding='utf-8').replace('down_revision = None',
                    'down_revision = "0001_base"'), encoding='utf-8')
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('control', [
    'for unused in unknown:\n{create}',
    'while unknown:\n{create}\n    break',
    'try:\n{create}\nexcept Exception:\n    pass',
    'match unknown:\n    case True:\n{nested}\n    case _:\n        pass',
    'for unused in (1,):\n    break\n{create}',
    'unknown and (\n{create}\n)',
    'factory = lambda: (\n{create}\n)',
])
def test_conditional_creation_cannot_prove_existing_table_uniqueness(tmp_path, control):
    create = TABLE.split('notes =')[0].strip()
    body = control.format(create='    ' + create, nested='        ' + create)
    body += "\nnotes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
    body += "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='new'))"
    assert analyze(migration(tmp_path, body, 'destructive'))['class'] == 'destructive'


@pytest.mark.parametrize('body,prefix', [
    ("sa.table = op.get_bind().scalar\nsa.table(sa.text('DELETE FROM notes'))", ''),
    ("select = op.get_bind().scalar\nselect(sa.text('DELETE FROM notes'))", 'from sqlalchemy import select\n'),
    ("str = op.get_bind().scalar\nstr(sa.text('DELETE FROM notes'))", ''),
])
def test_shadowed_trusted_apis_require_review(tmp_path, body, prefix):
    assert analyze(migration(tmp_path, body, 'destructive', prefix=prefix))['class'] == 'destructive'


@pytest.mark.parametrize('body', [
    "sa.table: object = op.get_bind().scalar\nsa.table(sa.text('DELETE FROM notes'))",
    "select, unused = replacements\nselect(sa.text('DELETE FROM notes'))",
    "op.get_bind().execute(sa.select(sa.func.custom.min()))",
])
def test_remaining_assignment_forms_and_qualified_functions_fail_closed(tmp_path, body):
    path = migration(tmp_path, body, 'destructive', prefix='from sqlalchemy import select\n')
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('body', [
    'if unknown:\n    return\n' + TABLE,
    'yield\n' + TABLE,
    TABLE + "\ndef remove_key():\n    op.execute('ALTER TABLE notes DROP CONSTRAINT notes_pkey')\nremove_key()",
])
def test_ancestor_early_returns_and_helpers_cannot_claim_a_primary_key(tmp_path, body):
    base = migration(tmp_path, body, 'destructive')
    previous = tmp_path / '9998_prior.py'
    previous.write_text(base.read_text(encoding='utf-8').replace('9999_classes', '9998_prior'), encoding='utf-8')
    path = migration(tmp_path, "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
                     "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='new'))",
                     'destructive')
    path.write_text(path.read_text(encoding='utf-8').replace('down_revision = None', 'down_revision = "9998_prior"'), encoding='utf-8')
    assert analyze(path)['class'] == 'destructive'


def test_global_helper_mutations_are_applied_after_upgrade_creates_the_table(tmp_path):
    prefix = "def remove_key():\n    op.execute('ALTER TABLE notes DROP CONSTRAINT notes_pkey')\n"
    base = migration(tmp_path, TABLE + 'remove_key()', 'destructive', prefix=prefix)
    previous = tmp_path / '9998_prior.py'
    previous.write_text(base.read_text(encoding='utf-8').replace('9999_classes', '9998_prior'), encoding='utf-8')
    path = migration(tmp_path, "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
                     "op.get_bind().execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='new'))",
                     'destructive')
    path.write_text(path.read_text(encoding='utf-8').replace('down_revision = None', 'down_revision = "9998_prior"'), encoding='utf-8')
    assert analyze(path)['class'] == 'destructive'


def test_function_globals_use_the_completed_module_not_definition_time(tmp_path):
    path = migration(tmp_path, 'op.execute(sql)', 'destructive', prefix="sql = 'SELECT 1'\n")
    path.write_text(path.read_text(encoding='utf-8') + "\nsql = 'DELETE FROM notes'\n", encoding='utf-8')
    assert analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('body,prefix', [
    ('mutate()\nop.execute(sql)', "sql = 'SELECT 1'\ndef mutate():\n    global sql\n    sql = 'DELETE FROM notes'\n"),
    ("sql = 'SELECT 1'\ndef mutate():\n    nonlocal sql\n    sql = 'DELETE FROM notes'\nmutate()\nop.execute(sql)", ''),
    ("sql = 'SELECT 1'\ntry:\n    sql = 'DELETE FROM notes'\nexcept* Exception:\n    sql = 'SELECT 1'\nop.execute(sql)", ''),
])
def test_scope_mutation_and_exception_groups_fail_closed(tmp_path, body, prefix):
    assert analyze(migration(tmp_path, body, 'destructive', prefix=prefix))['class'] == 'destructive'


@pytest.mark.parametrize('body', [
    "op.execute('SELECT 1', execution_options=something_completely_new())",
    "op.get_bind().execute(sa.select(1), something_completely_new())",
    "op.execute(sa.select(sa.column('remove_notes_primary_key()', is_literal=True)))",
    "op.execute(sa.select(sa.column('remove_notes_primary_key()', None, True)))",
    "sa.MetaData('unsafe_schema', False)",
    "op.execute(sa.select(sa.column('remove_notes_primary_key()', quote=False)))",
    "notes = sa.Table('notes', sa.MetaData(), sa.Column('fact', sa.String(), key='key'))",
    "sa.MetaData(schema='unsafe', quote_schema=False)",
    "op.create_index('unsafe', 'notes', ['fact'], quote=False)",
    "op.create_table('extra', sa.Column('kind', sa.Enum('one', name='unsafe', quote=False)))",
    "op.execute('SELECT ' + '1')", "op.execute(f'SELECT {1}')",
    "if True:\n    op.execute('SELECT 1')",
    "for sql in ('SELECT 1',):\n    op.execute(sql)",
    "run = op.execute\nrun('SELECT 1')",
    "run = sa.select\nrun(1)",
])
def test_only_explicitly_modelled_arguments_and_calls_are_admitted(tmp_path, body):
    assert analyze(migration(tmp_path, TABLE + body, 'destructive'))['class'] == 'destructive'


@pytest.mark.parametrize('body', [TABLE + 'something_completely_new()', 'something_completely_new()\n' + TABLE])
def test_unproved_ancestor_cannot_reintroduce_facts_later_in_the_same_migration(tmp_path, body):
    from migrations.source_safety import schema_at
    path = migration(tmp_path, body, 'destructive')
    assert not schema_at((path.read_text(encoding='utf-8'),))['tables']['notes']['primary_key']


@pytest.mark.parametrize('body', [
    "op.execute(sa.Table('notes', sa.MetaData(schema='other'), sa.Column('key', sa.String()), sa.Column('fact', sa.String())).update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))",
    "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\nnotes = sa.table('other', sa.column('key'), sa.column('fact'))",
    "def unused():\n    def upgrade():\n        op.create_table('manufactured', sa.Column('key', sa.String(), primary_key=True))",
])
def test_unmodelled_identity_and_scope_cannot_reuse_schema_proofs(tmp_path, body):
    assert analyze(migration(tmp_path, TABLE + body, 'destructive'))['class'] == 'destructive'


def test_quoted_dotted_table_is_not_a_schema_qualified_table(tmp_path):
    body = TABLE.replace("create_table('notes'", "create_table('other.notes'")
    body = body.replace("sa.column('fact'))", "sa.column('fact'), schema='other')")
    body += "op.execute(notes.update().where(notes.c.key == 'one', notes.c.fact.is_(None)).values(fact='known'))"
    assert analyze(migration(tmp_path, body, 'destructive'))['class'] == 'destructive'


def test_ancestor_fill_proof_does_not_treat_an_earlier_table_as_new(tmp_path):
    from migrations.source_safety import schema_at
    first = migration(tmp_path, TABLE).read_text(encoding='utf-8')
    second = migration(tmp_path, "notes = sa.table('notes', sa.column('key'), sa.column('fact'))\n"
        "op.execute(insert(notes).values(key='stable', fact='new').on_conflict_do_nothing())",
        'destructive', prefix='from sqlalchemy.dialects.sqlite import insert\n').read_text(encoding='utf-8')
    assert not schema_at((first, second))['tables']['notes']['primary_key']


def test_imported_callable_alias_requires_review(tmp_path):
    path = migration(tmp_path, 'op.execute(build(1))', 'destructive',
        prefix='from sqlalchemy import select as build\n')
    assert analyze(path)['class'] == 'destructive'
