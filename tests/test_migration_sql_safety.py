"""Raw SQL safety proofs include deferred trigger and routine execution."""
import pytest

from migrations.sql_safety import analyze_sql


def test_nested_boolean_inline_check_is_additive():
    sql = """ALTER TABLE swap_requests ADD COLUMN open_offer BOOLEAN NOT NULL DEFAULT false
        CHECK (recipient_person_id IS NOT NULL OR
            (open_offer AND status IN ('offered','withdrawn','rejected')))"""
    assert analyze_sql(sql)['class'] == 'additive'


def test_named_inline_foreign_key_is_additive():
    assert analyze_sql('ALTER TABLE events ADD COLUMN email_sender_identity_id VARCHAR(36) '
                       'CONSTRAINT fk_events_email_sender REFERENCES identities(id)')['class'] == 'additive'


@pytest.mark.parametrize('suffix,kind', [('', 'destructive'), ('DELETE FROM history;', 'destructive')])
def test_sql_control_flow_requires_review_even_for_privileges(suffix, kind):
    sql = """DO $$ BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'rota_app') THEN
            REVOKE UPDATE, DELETE ON policy_seed_lines FROM rota_app;
        END IF;
    END $$; """ + suffix
    assert analyze_sql(sql)['class'] == kind


SCHEMA = {"tables": {"items": {"columns": ["id", "value", "other"],
                               "primary_key": ["id"], "new": False},
                     "new_items": {"columns": ["id", "value"],
                                   "primary_key": ["id"], "new": True}}}


@pytest.mark.parametrize("sql", [
    "CREATE TABLE extra (id INTEGER PRIMARY KEY, value TEXT)",
    "ALTER TABLE items ADD COLUMN extra TEXT",
    "ALTER TABLE items ADD COLUMN extra VARCHAR(36) REFERENCES items(id)",
    "ALTER TABLE items ADD COLUMN extra TEXT NOT NULL DEFAULT 'new'",
    "CREATE INDEX ix_items_value ON items (value, id)",
    "CREATE FUNCTION guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable evidence'; END $$",
    "CREATE FUNCTION guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    "CREATE FUNCTION answer() RETURNS integer LANGUAGE sql AS 'SELECT 42'",
    "CREATE TRIGGER guard BEFORE DELETE ON items BEGIN SELECT RAISE(ABORT, 'DELETE'); END",
    "REVOKE ALL ON FUNCTION prune(integer, integer) FROM PUBLIC",
    "SELECT CAST('1' AS INTEGER)",
    "SELECT '1'::INTEGER",
])
def test_proven_additive_sql(sql):
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "additive"


@pytest.mark.parametrize("sql", [
    "UPDATE items SET value = :value WHERE id = :id AND value IS NULL",
    "UPDATE items SET value = 'filled', other = 1 WHERE value IS NULL AND other IS NULL AND id = 1",
    "UPDATE items SET value = 'filled' WHERE value IS NULL AND id IN "
    "(SELECT id FROM items WHERE value IS NULL ORDER BY id LIMIT 100)",
    "UPDATE items SET value = 'filled' WHERE ((id = 1) AND (value IS NULL))",
    "UPDATE items SET value = 'filled' WHERE id = 1 AND (value IS NULL AND other = 0)",
    "INSERT INTO new_items (id, value) VALUES (1, 'a'), (2, 'b') ON CONFLICT DO NOTHING",
])
def test_proven_bounded_idempotent_fill(sql):
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "fill_only"


@pytest.mark.parametrize('predicate,retained,rows', [
    ('id = 1 AND CASE WHEN other = 0 AND value IS NULL AND other = 0 THEN TRUE ELSE TRUE END', 99, 1),
    ('id = 1 AND NULL BETWEEN 0 AND value IS NULL', 99, 1),
    ('value IS NULL AND CASE WHEN other = 0 AND id = 1 AND other = 0 THEN TRUE ELSE TRUE END', None, 1002),
    ('value IS NULL AND other BETWEEN 0 AND id = 1 AND other = 0', None, 1002),
], ids=['case-overwrite', 'between-overwrite', 'case-row-bound', 'between-row-bound'])
def test_unsupported_boolean_grammar_cannot_invent_null_or_primary_key_guards(tmp_path, predicate, retained, rows):
    from contextlib import closing
    import sqlite3
    from migrations import safety
    from .test_migration_classes import migration

    sql = 'UPDATE items SET value = 7 WHERE ' + predicate
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, value INTEGER, other INTEGER)')
        db.executemany('INSERT INTO items VALUES (?, ?, 0)', [(i, retained) for i in range(1, rows + 1)])
        assert db.execute(sql).rowcount == rows
        assert db.execute('SELECT count(*) FROM items WHERE value = 7').fetchone() == (rows,)
    assert analyze_sql(sql, schema=SCHEMA)['class'] == 'destructive'
    path = migration(tmp_path,
        "op.create_table('items', sa.Column('id', sa.Integer(), primary_key=True), "
        "sa.Column('value', sa.Integer()), sa.Column('other', sa.Integer()))\n"
        f'op.execute({sql!r})', 'fill_only')
    assert safety.analyze(path)['class'] == 'destructive'
    assert safety.declaration_errors(path)
    assert safety.needs_confirmation(path)


@pytest.mark.parametrize('predicate', [
    'id = 1 AND value IS NULL AND (other = 0 AND other = 0) = TRUE',
    'id = 1 AND value IS NULL AND NOT (other = 0 AND other = 0)',
    '(id = 1 AND value IS NULL) IS FALSE',
    'id = 1 AND value IS NULL AND other = (other = 0 AND other = 0)',
    'value IS NULL AND id IN (SELECT id FROM items WHERE other = 0 AND other = 0 LIMIT 1)',
    'id = 1 AND value IS NULL AND (other = 0 OR other = 1)',
])
def test_unparsed_nested_boolean_expressions_cannot_supply_fill_proofs(predicate):
    assert analyze_sql('UPDATE items SET value = 7 WHERE ' + predicate, schema=SCHEMA)['class'] == 'destructive'


@pytest.mark.parametrize("sql", [
    "DELETE FROM items", "TRUNCATE items", "DROP TABLE items", "ALTER TABLE items RENAME TO old_items",
    "ALTER TABLE items ALTER COLUMN value TYPE INTEGER", "ALTER TABLE items ADD COLUMN extra TEXT NOT NULL",
    "UPDATE items SET value = 1 WHERE id = 1", "UPDATE items SET value = 1 WHERE value IS NULL",
    "UPDATE items SET value = 1 WHERE value IS NULL OR id = 1",
    "UPDATE items SET value = 1, other = 2 WHERE value IS NULL AND id = 1",
    "UPDATE missing SET value = 1 WHERE value IS NULL AND id = 1",
    "UPDATE items SET value = erase() WHERE value IS NULL AND id = 1",
    "UPDATE items SET value = 1 WHERE value IS NULL AND id IN (SELECT id FROM items LIMIT :size)",
    "UPDATE items SET value = 1 WHERE value IS NULL AND id IN (SELECT id FROM items LIMIT 1001)",
    "INSERT INTO items (id) VALUES (1) ON CONFLICT DO NOTHING",
    "INSERT INTO new_items (id) VALUES (1)",
    "INSERT INTO new_items (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET id = 2",
    "SELECT prune()", "SELECT 1; DELETE FROM items",
    "SELECT untrusted.current_schema()",
    "SELECT CAST('value' AS untrusted_type)",
    "SELECT CAST('value' AS INTEGER.untrusted_type)",
    "SELECT 'value'::untrusted_type",
    "SELECT 'value'::INTEGER.untrusted_type",
    "SELECT 'value' COLLATE untrusted_collation",
    "UPDATE items SET value = CAST('x' AS untrusted_type) WHERE value IS NULL AND id = 1",
    'UPDATE "Items" SET value = 1 WHERE value IS NULL AND id = 1',
    'UPDATE items SET "Value" = 1 WHERE "Value" IS NULL AND id = 1',
    'UPDATE items SET value = 1 WHERE value IS NULL AND "Id" = 1',
    'INSERT INTO "new_Items" (id) VALUES (1) ON CONFLICT DO NOTHING',
    "CREATE FUNCTION prune() RETURNS void LANGUAGE plpgsql AS $$ BEGIN DELETE FROM items; END $$; SELECT prune()",
    "CREATE TRIGGER guard BEFORE DELETE ON items BEGIN SELECT RAISE(ABORT, 'x'); END; DELETE FROM items",
    "DO $$ BEGIN DELETE FROM items; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rota_worker') THEN "
    "GRANT EXECUTE ON FUNCTION prune() TO rota_worker; DELETE FROM items; END IF; END $$",
    "CREATE TABLE extra AS SELECT prune()", "CREATE INDEX ix ON items (prune(value))",
    "CREATE TABLE extra (id INTEGER DEFAULT prune())",
    "CREATE TABLE extra (id dangerous_type DEFAULT 'value')",
    "CREATE INDEX ix ON items (value malicious_ops)",
    "ALTER TABLE items ADD COLUMN extra TEXT, RENAME TO gone",
    "ALTER TABLE items ADD COLUMN extra INTEGER CHECK (prune() = 1)",
    "INSERT INTO new_items (value) VALUES ('a') ON CONFLICT DO NOTHING",
    "INSERT INTO new_items (id) VALUES (random()) ON CONFLICT DO NOTHING",
    "VACUUM", "SELECT 'unterminated", "/* unterminated", "DO $$ unterminated",
])
def test_unknown_overwrite_and_immediate_execution_fail_closed(sql):
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "destructive"


def test_comments_quotes_and_dollar_bodies_preserve_statement_boundaries():
    sql = "/* nested /* DELETE FROM items */ comment */ CREATE FUNCTION f() RETURNS text LANGUAGE sql AS $body$ "
    sql += "SELECT '; DELETE FROM items'; $body$; -- DELETE FROM items\n SELECT 'it''s; DELETE FROM items'"
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "additive"
    assert analyze_sql(sql + "; DELETE /* hidden */ FROM items", schema=SCHEMA)["class"] == "destructive"


def test_missing_schema_cannot_prove_fill_only():
    assert analyze_sql("UPDATE items SET value = 1 WHERE value IS NULL AND id = 1")["class"] == "destructive"


@pytest.mark.parametrize("sql", [
    "SELECT id, value FROM items WHERE value IS NULL",
    "UPDATE items SET value = 'new' WHERE id = 1",
    "DELETE FROM items WHERE id = 1",
    "DELETE FROM items",
    "INSERT INTO items (id, value) VALUES (1, 'value')",
    "CREATE FUNCTION guard() RETURNS void LANGUAGE plpgsql AS $$ BEGIN ALTER TABLE items DROP COLUMN value; END $$",
    "CREATE TRIGGER guard BEFORE DELETE ON items BEGIN DELETE FROM items; END",
    "DROP TRIGGER IF EXISTS guard",
    "DROP TRIGGER guard ON public.items",
    "DROP FUNCTION IF EXISTS guard(integer) RESTRICT",
    "REVOKE ALL ON FUNCTION guard(integer) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION guard(integer) TO rota_worker",
    "PRAGMA schema_version", "PRAGMA schema_version=42",
    "PRAGMA writable_schema=ON", "PRAGMA writable_schema=OFF",
    "PRAGMA foreign_keys=OFF", "PRAGMA foreign_keys=1",
    "PRAGMA table_info(items)", "PRAGMA foreign_key_check", "PRAGMA integrity_check(items)",
    "BEGIN", "BEGIN TRANSACTION", "COMMIT", "END TRANSACTION",
    "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE TRANSACTION",
    "SAVEPOINT migration_step", "RELEASE SAVEPOINT migration_step",
])
def test_only_proven_nonstructural_sql_preserves_ancestor_schema(sql):
    from migrations import sql_safety
    assert sql_safety.preserves_schema(sql) is True


@pytest.mark.parametrize("sql", [
    "SELECT remove_notes_primary_key()",
    "SELECT untrusted.current_schema()",
    "UPDATE items SET value = remove_notes_primary_key()",
    "DELETE FROM items WHERE remove_notes_primary_key()",
    "INSERT INTO items (id) VALUES (remove_notes_primary_key())",
    "ALTER TABLE ONLY items DROP CONSTRAINT items_pkey",
    "ALTER TABLE items ADD COLUMN added TEXT", "DROP TABLE items", "CREATE TABLE extra (id INTEGER)",
    "CREATE FUNCTION guard() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$; ALTER TABLE items DROP COLUMN id",
    "CREATE FUNCTION guard() RETURNS void LANGUAGE sql AS $$ SELECT 1 $$; SELECT remove_notes_primary_key()",
    "DROP FUNCTION guard() CASCADE", "DROP TYPE value_type CASCADE",
    "PRAGMA unknown_setting=ON", "PRAGMA table_info(remove_notes_primary_key())",
    "PRAGMA foreign_keys=OFF; SELECT remove_notes_primary_key()",
    "UPDATE sqlite_schema SET sql = 'CREATE TABLE items (id TEXT)' WHERE name = 'items'",
    "DELETE FROM sqlite_master WHERE name = 'items'",
    "UPDATE pg_catalog.pg_constraint SET contype = 'x'",
    "DO $$ BEGIN ALTER TABLE items DROP CONSTRAINT items_pkey; END $$",
    "CALL remove_notes_primary_key()", "VACUUM", "ROLLBACK", "ROLLBACK TO SAVEPOINT migration_step",
    "/* unterminated", "SELECT 'unterminated",
])
def test_unknown_execution_or_structural_sql_invalidates_ancestor_schema(sql):
    from migrations import sql_safety
    assert sql_safety.preserves_schema(sql) is False


@pytest.mark.parametrize("sql, expected", [
    ("CREATE UNIQUE INDEX uq_catalogue ON policies "
     "(department_id, ((definition->'rule'->>'id'))) WHERE kind = 'catalogue' AND status = 'active'", True),
    ("CREATE UNIQUE INDEX uq_catalogue ON policies "
     "(department_id, json_extract(definition, '$.rule.id')) WHERE kind = 'catalogue' AND status = 'active'", False),
    ("CREATE INDEX ix ON items ((remove_notes_primary_key(value)))", False),
    ("CREATE INDEX ix ON items (value) WHERE remove_notes_primary_key()", False),
    ("CREATE INDEX ix ON items (untrusted.json_extract(value, '$.id'))", False),
    ("CREATE INDEX ix ON items (value dangerous_ops)", False),
    ("CREATE INDEX ix ON items (value); ALTER TABLE items DROP COLUMN id", False),
])
def test_only_proven_pure_index_expressions_preserve_schema(sql, expected):
    from migrations import sql_safety
    assert sql_safety.preserves_schema(sql) is expected
    assert (analyze_sql(sql)["class"] == "additive") is expected


@pytest.mark.parametrize("sql, expected", [
    ("ALTER TABLE swap_requests ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'exchange' "
     "CHECK (kind IN ('exchange', 'give')) CHECK (kind != 'give' OR second_assignment_id IS NULL)", "additive"),
    ("ALTER TABLE swap_requests ADD COLUMN open_offer BOOLEAN NOT NULL DEFAULT FALSE "
     "CHECK (open_offer IN (TRUE, FALSE))", "additive"),
    ("ALTER TABLE items ADD COLUMN value TEXT CHECK (prune() = 1)", "destructive"),
    ("ALTER TABLE items ADD COLUMN value TEXT CHECK (value IN ('one', 'two')) "
     "CHECK (remove_notes_primary_key())", "destructive"),
    ("ALTER TABLE items ADD COLUMN value TEXT CHECK (value IS NULL", "destructive"),
])
def test_added_columns_prove_each_check_expression_without_running_it(sql, expected):
    assert analyze_sql(sql)["class"] == expected


def test_insert_cannot_hide_retained_history_deletion_in_a_trigger():
    from contextlib import closing
    import sqlite3

    sql = ("CREATE TRIGGER erase_history AFTER INSERT ON new_items "
           "BEGIN DELETE FROM items; END; "
           "INSERT INTO new_items (id, value) VALUES (1, 'new') ON CONFLICT DO NOTHING")
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT); "
                         "INSERT INTO items VALUES (1, 'retained'); "
                         "CREATE TABLE new_items (id INTEGER PRIMARY KEY, value TEXT)")
        db.executescript(sql)
        assert db.execute("SELECT count(*) FROM items").fetchone() == (0,)
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "destructive"


@pytest.mark.parametrize("sql", [
    "CREATE FUNCTION prune() RETURNS void LANGUAGE plpgsql AS $$ BEGIN DELETE FROM items; END $$",
    "CREATE OR REPLACE FUNCTION prune() RETURNS void AS 'BEGIN DELETE FROM items; END' LANGUAGE plpgsql",
    "CREATE TRIGGER erase_history AFTER INSERT ON new_items FOR EACH ROW EXECUTE FUNCTION prune()",
    "CREATE OR REPLACE FUNCTION rota_immutable_evidence() RETURNS trigger LANGUAGE plpgsql "
    "AS $$ BEGIN RETURN NEW; END $$",
    "CREATE OR REPLACE FUNCTION guard() RETURNS boolean LANGUAGE sql AS $$ SELECT true $$",
    "CREATE OR REPLACE FUNCTION rota_immutable_evidence() RETURNS trigger AS 'BEGIN RETURN NEW; END' LANGUAGE plpgsql",
    "CREATE OR REPLACE FUNCTION guard() RETURNS void AS 'untrusted_library', 'erase_history' LANGUAGE c",
])
def test_unproved_trigger_and_routine_bodies_require_confirmation(sql):
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "destructive"


@pytest.mark.parametrize("sql", [
    "CREATE FUNCTION prune() RETURNS void LANGUAGE sql AS $$ DELETE FROM items $$",
    "CREATE FUNCTION prune() RETURNS integer LANGUAGE sql AS 'SELECT 1; DELETE FROM items'",
    "CREATE FUNCTION prune() RETURNS integer LANGUAGE sql AS $$ SELECT erase_history() $$",
    "CREATE FUNCTION guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN erase_history(); END $$",
    "CREATE FUNCTION guard() RETURNS SETOF integer LANGUAGE plpgsql "
    "AS $$ BEGIN RETURN NEXT 1; SET LOCAL session_replication_role = replica; RETURN; END $$",
    "CREATE FUNCTION guard() RETURNS SETOF integer LANGUAGE plpgsql AS $$ BEGIN RETURN QUERY SELECT 1; END $$",
    "CREATE FUNCTION guard(value integer DEFAULT erase_history()) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$",
    "CREATE TRIGGER guard AFTER INSERT ON new_items WHEN erase_history() "
    "BEGIN SELECT RAISE(ABORT, 'guard'); END",
    "CREATE TRIGGER guard AFTER INSERT ON new_items BEGIN SELECT erase_history(); END",
    "CREATE TRIGGER guard AFTER INSERT ON new_items BEGIN SELECT 1; DELETE FROM items; END",
    "CREATE TRIGGER guard AFTER INSERT ON new_items BEGIN SELECT RAISE(ABORT, erase_history()); END",
])
def test_unknown_deferred_calls_do_not_inherit_read_only_proof(sql):
    assert analyze_sql(sql, schema=SCHEMA)["class"] == "destructive"


@pytest.mark.parametrize('sql', [
    'SELECT current_schema()', 'SELECT pg_catalog.current_schema()',
    'SELECT filter()', 'SELECT over()', 'SELECT pg_catalog.exists()',
    "UPDATE definition_history SET snapshot = json_remove(snapshot, '$.private_reason')",
    "UPDATE items SET value = lower('known') WHERE value IS NULL AND id = 1",
])
def test_sql_function_calls_require_review_and_invalidate_schema(sql):
    from migrations.sql_safety import preserves_schema
    assert analyze_sql(sql, schema=SCHEMA)['class'] == 'destructive'
    assert not preserves_schema(sql)


def test_read_only_aggregate_select_preserves_schema():
    sql = 'SELECT count(*) FROM items'
    assert analyze_sql(sql, schema=SCHEMA) == {'class': 'additive', 'findings': []}
    from migrations.sql_safety import preserves_schema
    assert preserves_schema(sql)
