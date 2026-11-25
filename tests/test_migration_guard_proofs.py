"""Narrow schema/index/guard proofs never trust migration names or declarations."""
import ast
from shutil import copyfile
from textwrap import indent
import pytest

from migrations import safety
from migrations.source_safety import analyze_sources
from migrations.sql_safety import analyze_sql, schema_template

from .test_migration_classes import APPEND, APPEND_TABLE, migration


REVISION = safety.VERSIONS / '0124_type_lead_ownership.py'

GUARD_FUNCTION = """CREATE FUNCTION scope_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable'; END IF;
  IF NOT EXISTS (SELECT 1) THEN RAISE EXCEPTION 'invalid'; END IF;
  RETURN NEW;
END $$"""
GUARD_BRANCH = """if op.get_bind().dialect.name == 'sqlite':
    op.execute("CREATE TRIGGER guard_u BEFORE UPDATE ON fresh BEGIN SELECT RAISE(ABORT, 'immutable'); END")
    op.execute("CREATE TRIGGER guard_d BEFORE DELETE ON fresh BEGIN SELECT RAISE(ABORT, 'immutable'); END")
    op.execute("CREATE TRIGGER guard_i BEFORE INSERT ON fresh WHEN NOT EXISTS (SELECT 1) BEGIN SELECT RAISE(ABORT, 'invalid'); END")
else:
    op.execute("CREATE TRIGGER guard BEFORE INSERT OR UPDATE OR DELETE ON fresh FOR EACH ROW EXECUTE FUNCTION scope_guard()")
"""
GUARD_BODY = "op.create_table('fresh', sa.Column('id', sa.Integer(), primary_key=True))\n" + GUARD_BRANCH + \
    "op.execute('INSERT INTO fresh (id) VALUES (1) ON CONFLICT DO NOTHING')\n"


def guard_source(body):
    return 'from alembic import op\nimport sqlalchemy as sa\ndef upgrade():\n' + indent(body, '    ')


@pytest.mark.parametrize('position', ['helper_before', 'helper_after', 'downgrade'])
def test_non_upgrade_bodies_cannot_supply_executed_guard_proof(tmp_path, position):
    helper = '\ndef ' + ('downgrade' if position == 'downgrade' else 'never_called') + '():\n' + \
        indent('op.execute(' + repr(GUARD_FUNCTION) + ')\n', '    ')
    source = guard_source(GUARD_BODY)
    source = helper + source if position == 'helper_before' else source + helper
    assert analyze_sources(source, ())['class'] == 'destructive'
    path = tmp_path / 'dormant_guard.py'
    path.write_text(source + "\nrevision='9999_dormant_guard'\nSAFETY_CLASS='fill_only'\n")
    assert safety.declaration_errors(path)
    assert safety.needs_confirmation(path)


@pytest.mark.parametrize('later', ['', "op.execute('DROP FUNCTION scope_guard()')\n",
    'op.execute("CREATE OR REPLACE FUNCTION scope_guard() RETURNS trigger LANGUAGE plpgsql AS '
    '$$ BEGIN DELETE FROM retained; RETURN NEW; END $$")\n', 'unproved_mutation()\n'])
def test_ancestor_callable_proofs_never_authorize_a_new_upgrade(later):
    ancestors = (guard_source('op.execute(' + repr(GUARD_FUNCTION) + ')\n'),)
    if later:
        ancestors += (guard_source(later),)
    assert analyze_sources(guard_source(GUARD_BODY), ancestors)['class'] == 'destructive'


def test_guard_creation_must_still_exist_on_the_current_upgrade_path():
    created = '    op.execute(' + repr(GUARD_FUNCTION) + ')\n'
    valid = GUARD_BODY.replace('else:\n', 'else:\n' + created)
    assert analyze_sources(guard_source(valid), ()) == {'class': 'fill_only', 'findings': [], 'needs_schema': True}
    dropped = valid.replace(created, created + "    op.execute('DROP FUNCTION scope_guard()')\n")
    assert analyze_sources(guard_source(dropped), ())['class'] == 'destructive'


def test_dormant_destructive_bodies_are_still_scanned():
    source = guard_source("op.create_table('fresh', sa.Column('id', sa.Integer(), primary_key=True))\n")
    source += "\ndef never_called():\n    op.execute('DELETE FROM retained')\n"
    assert analyze_sources(source, ())['class'] == 'destructive'


def test_unknown_sql_invalidates_callable_proof_in_the_shared_sql_context():
    schema = {'tables': {}}
    assert analyze_sql(GUARD_FUNCTION, schema=schema)['class'] == 'additive'
    assert schema['guard_functions']
    assert analyze_sql('unproved syntax', schema=schema)['class'] == 'destructive'
    assert not schema.get('guard_functions')


def leadership_source():
    return REVISION.read_text(encoding='utf-8')


def changed_source(old, new):
    source = leadership_source()
    assert old in source
    return source.replace(old, new, 1)


def test_owner_approved_exact_leadership_source_is_additive_without_review_override(tmp_path):
    source = leadership_source()
    assert 'DESTRUCTIVE_REVIEWED' not in source
    assert safety.analyze(REVISION) == {'class': 'additive', 'findings': [], 'needs_schema': True}
    path = tmp_path / 'unrelated_filename.py'
    path.write_text(source.replace("revision = '0124_type_lead_ownership'", "revision = 'arbitrary_revision'"))
    assert safety.analyze(path) == {'class': 'additive', 'findings': [], 'needs_schema': True}
    assert not safety.declaration_errors(path)
    assert not safety.needs_confirmation(path)
    assert analyze_sources(source.replace("SAFETY_CLASS = 'additive'", "SAFETY_CLASS = 'destructive'"), ())['class'] == 'additive'


def test_original_loop_and_unguarded_dollar_body_remain_unproved():
    source = leadership_source()
    unguarded = source.replace("        if '$' in schema:\n            raise RuntimeError('Schema dollar delimiters are unsupported')\n", '')
    assert analyze_sources(unguarded, ())['class'] == 'destructive'
    loop = source.replace('    if op.get_bind().dialect.name', "    for action in ('UPDATE', 'DELETE'):\n        op.execute(action)\n    if op.get_bind().dialect.name", 1)
    assert analyze_sources(loop, ())['class'] == 'destructive'


def test_existing_append_proof_keeps_extra_unconditional_read_raise_guards(tmp_path):
    body = APPEND_TABLE + '''
if op.get_bind().dialect.name == 'sqlite':
    op.execute("CREATE TRIGGER immutable BEFORE UPDATE ON ordinals BEGIN SELECT RAISE(ABORT, 'keep'); END")
    op.execute("CREATE TRIGGER remember AFTER INSERT ON audit_events BEGIN ''' + APPEND + '''; END")
else:
    op.execute("""CREATE FUNCTION remember() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        ''' + APPEND + '''; RETURN NEW; END; $$""")
    op.execute("CREATE TRIGGER remember AFTER INSERT ON audit_events FOR EACH ROW EXECUTE FUNCTION remember()")
'''
    assert safety.analyze(migration(tmp_path, body))['class'] == 'additive'


@pytest.mark.parametrize('old,new', [
    ("policies.c.definition['rule']['id'].as_string()", "sa.func.erase(policies.c.definition)"),
    ("policies.c.definition['rule']['id'].as_string()", "sa.func.custom.coalesce(policies.c.definition, 1)"),
    ("policies.c.definition['rule']['id'].as_string()", "policies.c.definition[erase()].as_string()"),
    ("policies.c.definition['rule']['id'].as_string()", "policies.c.department_id['rule'].as_string()"),
    ("policies.c.definition['rule']['id'].as_string()", "policies.c.definition['rule'].as_string(erase())"),
    ("policies.c.definition['rule']['id'].as_string()", "policies.c.definition['rule'].as_string(untrusted=True)"),
    ("sa.func.coalesce(policies.c.definition['rule']['catalogue_version'].as_integer(), 1)",
     "sa.func.coalesce(policies.c.definition['rule']['catalogue_version'].as_integer(), erase())"),
    ("policies.c.created_at.desc()", "policies.c.created_at.desc(erase())"),
    ("sqlite_where=policies.c.kind == 'catalogue'", "sqlite_where=sa.text('erase()')"),
    ("sqlite_where=policies.c.kind == 'catalogue'", "sqlite_where=policies.update().values(kind='changed')"),
    ("policies.c.created_at.desc()", "sa.select(sa.text('erase()'))"),
    ("schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()", "schema = supplied_schema()"),
    ("SELECT current_schema()", "SELECT untrusted.current_schema()"),
    ("SELECT current_schema()", "SELECT current_schema(); DELETE FROM policies"),
    (".scalar_one()", ".scalar_one(erase())"),
    (".quote_identifier(schema)", ".quote_identifier(supplied_schema)"),
    (".quote_identifier(schema)", ".quote_identifier(schema, erase())"),
    (".identifier_preparer.quote_identifier(schema)", ".untrusted.quote_identifier(schema)"),
    ("if '$' in schema:", "if '$$' in schema:"),
    ("if '$' in schema:", "if '$' not in schema:"),
    ("raise RuntimeError('Schema dollar delimiters are unsupported')", "pass"),
    ("raise RuntimeError('Schema dollar delimiters are unsupported')", "raise custom_error()"),
    ("{q}.type_lead_guard() RETURNS", "{schema}.type_lead_guard() RETURNS"),
    ("{q}.type_lead_guard() RETURNS", "{q!s}.type_lead_guard() RETURNS"),
    ("{q}.type_lead_guard() RETURNS", "{q:unsafe}.type_lead_guard() RETURNS"),
    ("{q}.type_lead_guard() RETURNS", "untrusted.{q}.type_lead_guard() RETURNS"),
    ("FROM {q}.assignment_types", "FROM '{q}'.assignment_types"),
    ("FROM {q}.assignment_types", 'FROM "__migration_current_schema__".assignment_types -- {q}.unused\n'),
    ("END $$\"\"\"", "END $$; DELETE FROM policies\"\"\""),
    ("RETURN NEW;", "DELETE FROM policies; RETURN NEW;"),
    ("RETURN NEW;", "PERFORM erase(); RETURN NEW;"),
    ("RETURN NEW;", "CALL erase(); RETURN NEW;"),
    ("RETURN NEW;", "RETURN erase();"),
    ("SELECT 1 FROM {q}.assignment_types", "SELECT erase() FROM {q}.assignment_types"),
    ("SECURITY INVOKER", "SECURITY DEFINER"),
    ("pg_catalog, pg_temp", "untrusted, pg_temp"),
    ("BEFORE INSERT OR UPDATE OR DELETE", "AFTER INSERT OR UPDATE OR DELETE"),
    ("BEFORE INSERT OR UPDATE OR DELETE", "BEFORE INSERT OR UPDATE"),
    ("BEFORE UPDATE ON type_lead_ownership", "BEFORE DELETE ON type_lead_ownership"),
    ("h.object_revision = NEW.source_type_revision AND h.kind = 'assignment_types'", "h.object_revision = NEW.source_type_revision"),
    ("BEFORE INSERT ON type_lead_ownership", "BEFORE INSERT ON policies"),
    ("EXECUTE FUNCTION {q}.type_lead_guard()", "EXECUTE FUNCTION {q}.unknown_guard()"),
    (".type_lead_ownership \"", ".policies \""),
    ("dialect.name == 'sqlite'", "dialect.name != 'sqlite'"),
])
def test_near_neighbors_require_review(old, new):
    result = analyze_sources(changed_source(old, new), ())
    assert result['class'] == 'destructive', result
    assert result['findings']


@pytest.mark.parametrize('body', [
    "policies = sa.table('policies', sa.column('definition', sa.JSON()))\n"
    "op.create_index('ix', 'policies', [policies.c.definition['rule'].as_string()])",
    "policies = sa.table('policies', sa.column('definition', sa.JSON()))\n"
    "key = policies.c.definition['rule'].as_integer()\n"
    "op.create_index('ix', 'policies', [sa.func.coalesce(key, 1).desc()], sqlite_where=key == 1)",
])
def test_pure_json_index_receivers_and_arguments_are_modeled(tmp_path, body):
    assert safety.analyze(migration(tmp_path, body))['class'] == 'additive'


@pytest.mark.parametrize('body', [
    "op.create_index('ix', 'policies', [unknown.c.definition['rule'].as_string()])",
    "policies = sa.table('policies', sa.column('definition', sa.JSON()))\n"
    "sa.func = op.get_bind().scalar\nop.create_index('ix', 'policies', [sa.func.coalesce(1, 2)])",
    "policies = sa.table('policies', sa.column('definition', sa.JSON()))\n"
    "alias = policies.c.definition['rule']\nalias = unknown\nop.create_index('ix', 'policies', [alias.as_string()])",
    "op.create_index('ix', 'policies', [sa.func.coalesce(op.get_bind().execute(sa.text('DELETE FROM policies')), 1)])",
])
def test_index_aliases_shadowing_and_execution_do_not_inherit_purity(tmp_path, body):
    assert safety.analyze(migration(tmp_path, body, 'destructive'))['class'] == 'destructive'


def test_template_substitutions_cannot_cross_sql_token_boundaries():
    marker = '"__migration_current_schema__"'
    assert schema_template(f'CREATE FUNCTION {marker}.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$', 1)
    for sql in (f"SELECT '{marker}.items'", f'SELECT other.{marker}.items',
                f'CREATE INDEX {marker} ON items (id)', f'SELECT 1 -- {marker}.items'):
        assert not schema_template(sql, 1)


def test_schema_identifier_quotes_do_not_escape_enclosing_dollar_delimiters():
    from sqlalchemy.dialects.postgresql import dialect

    schema = 'odd$$; DELETE FROM policies; --'
    quoted = dialect().identifier_preparer.quote_identifier(schema)
    assert '$$' in quoted
    sql = f'CREATE FUNCTION {quoted}.guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN '
    sql += f'IF EXISTS (SELECT 1 FROM {quoted}.policies) THEN RETURN NEW; END IF; END $$'
    assert analyze_sql(sql)['class'] == 'destructive'


def test_pg_guard_body_is_independently_proved_and_cannot_smuggle_calls_or_dml():
    tree = ast.parse(leadership_source())
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                    and isinstance(node.values[0], ast.Constant) and node.values[0].value.startswith('CREATE FUNCTION'))
    sql = ''.join(part.value if isinstance(part, ast.Constant) else '"public"' for part in function.values)
    assert analyze_sql(sql)['class'] == 'additive'
    for replacement in ('DELETE FROM policies; RETURN NEW;', 'RETURN erase();', 'EXECUTE arbitrary; RETURN NEW;'):
        assert analyze_sql(sql.replace('RETURN NEW;', replacement))['class'] == 'destructive'


PUBLICATION_HEAD = '0152_feasibility_results'
ADD_NULLABLE = "op.add_column('publications', sa.Column('marker', sa.Boolean(), nullable=True))\n"
UPDATE_REFUSAL = (
    "CREATE TRIGGER guard_marker BEFORE UPDATE OF marker ON publications "
    "WHEN NEW.marker IS NOT OLD.marker BEGIN SELECT RAISE(ABORT, 'immutable marker'); END"
)


def nullable_guard_revision(directory, revision, parent, body):
    path = directory / f'{revision}.py'
    path.write_text(guard_source(body) + f'\nrevision = {revision!r}\ndown_revision = {parent!r}\n'
                    "SAFETY_CLASS = 'additive'\n", encoding='utf-8')
    return path


def retained_guard_revision(directory, *, sql=UPDATE_REFUSAL, add=ADD_NULLABLE, parent=PUBLICATION_HEAD, after=''):
    return nullable_guard_revision(directory, 'nullable_guard', parent, add +
                                   "if op.get_bind().dialect.name == 'sqlite':\n"
                                   f"    op.execute({sql!r})\n" + after)


@pytest.fixture(scope='module')
def publication_history(tmp_path_factory):
    directory = tmp_path_factory.mktemp('publication_history')
    for version in safety.VERSIONS.glob('*.py'):
        copyfile(version, directory / version.name)
    return directory


def test_retained_publication_guard_protects_new_nullable_column(publication_history):
    parent = nullable_guard_revision(publication_history, 'additive_parent', PUBLICATION_HEAD,
                                     "op.add_column('other', sa.Column('label', sa.Text(), nullable=True))\n")
    path = retained_guard_revision(publication_history, parent=safety.revision(parent))
    assert safety.analyze(path)['class'] == 'additive'
    assert safety.declaration_errors(path) == []
    assert not safety.needs_confirmation(path)


@pytest.mark.parametrize(('old', 'new'), [
    ('NEW.marker', 'NEW.label'),
    ('OLD.marker', 'NEW.marker'),
    ('IS NOT', '<>'),
    ('OF marker', 'OF label'),
    ('ON publications', 'ON other.publications'),
    ('ON publications', 'ON other'),
    ('BEFORE UPDATE', 'AFTER UPDATE'),
    ('RAISE(ABORT', 'RAISE(FAIL'),
    ("BEGIN SELECT", "BEGIN UPDATE publications SET marker = NULL; SELECT"),
    ("END", "END; DELETE FROM publications"),
    ('WHEN', '/* outer /* nested */ marker */ WHEN'),
    ('WHEN', '-- dialect-dependent comment\nWHEN'),
    ('WHEN', '\u00a0WHEN'),
])
def test_retained_nullable_guard_requires_exact_target_comparison_and_abort(publication_history, old, new):
    path = retained_guard_revision(publication_history, sql=UPDATE_REFUSAL.replace(old, new))
    assert safety.analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('add', [
    '',
    ADD_NULLABLE.replace("'marker'", "'unrelated'"),
    ADD_NULLABLE.replace("'publications'", "'other'"),
    ADD_NULLABLE.replace("nullable=True", "nullable=False, server_default='false'"),
    ADD_NULLABLE.replace("nullable=True))", "nullable=True), if_not_exists=True)"),
    ADD_NULLABLE.replace("sa.Boolean()", "sa.JSON()"),
    ADD_NULLABLE.replace("sa.Boolean()", "sa.String(36)"),
    ADD_NULLABLE.replace("sa.Boolean()", "sa.Text()"),
    ADD_NULLABLE.replace("sa.Boolean()", "sa.Boolean(create_constraint=True)"),
])
def test_retained_nullable_guard_requires_a_nullable_column_added_by_this_revision(publication_history, add):
    assert safety.analyze(retained_guard_revision(publication_history, add=add))['class'] == 'destructive'


@pytest.mark.parametrize('column', ['id', 'current', 'effective_until', 'prior_marker'])
def test_retained_nullable_guard_cannot_claim_an_existing_column_as_new(publication_history, column):
    parent = nullable_guard_revision(publication_history, 'prior_added', PUBLICATION_HEAD,
                                     ADD_NULLABLE.replace("'marker'", "'prior_marker'"))
    path = retained_guard_revision(publication_history, parent=safety.revision(parent),
                                   add=ADD_NULLABLE.replace("'marker'", repr(column)),
                                   sql=UPDATE_REFUSAL.replace('marker', column))
    assert safety.analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('extra', [
    "else:\n    " + ADD_NULLABLE.replace("'marker'", "'other'"),
    ADD_NULLABLE.replace("'marker'", "'other'"),
    "op.create_index('ix_marker', 'publications', ['marker'], unique=True, "
    "sqlite_where=sa.table('publications', sa.column('marker')).c.marker == True)\n",
    "op.execute('CREATE INDEX ix_marker ON publications (marker)')\n",
])
def test_retained_nullable_guard_requires_equal_schema(publication_history, extra):
    path = retained_guard_revision(publication_history, after=extra)
    assert safety.analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize('extra', [
    "\nop.execute('CREATE INDEX ix_marker ON publications (marker)')\n",
    "\ndef downgrade():\n    op.execute('CREATE INDEX ix_marker ON publications (marker)')\n",
])
def test_retained_nullable_guard_refuses_ddl_outside_upgrade(publication_history, extra):
    path = retained_guard_revision(publication_history)
    with path.open('a', encoding='utf-8') as source:
        source.write(extra)
    assert safety.analyze(path)['class'] == 'destructive'


def test_retained_nullable_guard_cannot_authorize_a_fill(publication_history):
    sql = "INSERT INTO ordinals (seq, kind, target) VALUES (1, 'audit', 'one') ON CONFLICT DO NOTHING"
    path = retained_guard_revision(publication_history, after=APPEND_TABLE + f'op.execute({sql!r})\n')
    assert safety.analyze(path)['class'] == 'destructive'


@pytest.mark.parametrize(('filename', 'old', 'new'), [
    ('0002_guards.py', 'BEFORE UPDATE OR DELETE ON publications', 'BEFORE DELETE ON publications'),
    ('0002_guards.py', 'BEFORE UPDATE OR DELETE ON publications', 'BEFORE UPDATE OF current OR DELETE ON publications'),
    ('0050_publication_effective_range.py', " - 'effective_until'", " - 'effective_until' - 'marker'"),
])
def test_retained_nullable_guard_requires_complete_reachable_pg_coverage(publication_history, filename, old, new):
    ancestor = publication_history / filename
    original = ancestor.read_text(encoding='utf-8')
    assert old in original
    try:
        ancestor.write_text(original.replace(old, new), encoding='utf-8')
        assert safety.analyze(retained_guard_revision(publication_history))['class'] == 'destructive'
    finally:
        ancestor.write_text(original, encoding='utf-8')


@pytest.mark.parametrize('sql', [
    'DROP TRIGGER publications_guard ON publications',
    'ALTER TABLE publications DISABLE TRIGGER ALL',
    'CREATE OR REPLACE FUNCTION rota_publication_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$',
    'CREATE FUNCTION to_jsonb(publications) RETURNS jsonb LANGUAGE sql AS $$ SELECT NULL $$',
    'CREATE TABLE other.publications (id INT)',
])
def test_retained_nullable_guard_refuses_superseded_pg_coverage(publication_history, sql):
    operation = f'op.execute({sql!r})\n'
    ancestor = nullable_guard_revision(publication_history, 'changed_guard', PUBLICATION_HEAD, operation)
    path = retained_guard_revision(publication_history, parent=safety.revision(ancestor))
    assert safety.analyze(path)['class'] == 'destructive'
    path = retained_guard_revision(publication_history, after=operation)
    assert safety.analyze(path)['class'] == 'destructive'


def test_unreachable_retained_guard_cannot_authorize_nullable_guard(publication_history):
    other_root = nullable_guard_revision(publication_history, 'unrelated_root', None, 'pass\n')
    path = retained_guard_revision(publication_history, parent=safety.revision(other_root))
    assert safety.analyze(path)['class'] == 'destructive'
