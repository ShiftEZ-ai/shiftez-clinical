"""Require line- and level-aware quota validation for new publications only.

Issued snapshots and validation receipts remain immutable and readable. The two
existing INSERT guards change together; no publication or other data is rewritten.
"""
from alembic import op

revision = '0060_quota_line_contract'
down_revision = '0059_rule_acknowledgements'
branch_labels = depends_on = None

DESTRUCTIVE_REVIEWED = (
    'SQLite replaces only the two publication INSERT contract triggers to require quota v6 and programme v1; '
    'PostgreSQL replaces their functions. No issued rows or evidence guards are removed. Take a verified '
    'pre-deploy backup; recover by forward repair or restoring the compatible database/image pair, never downgrade.'
)


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == 'sqlite':
        # sqlite3 legacy mode does not begin a transaction for DDL. Join Alembic's
        # logical transaction explicitly so both guards AND its later version write
        # roll back together. Alembic alone commits after the version write.
        if not op.get_context().as_sql and not bind.connection.driver_connection.in_transaction:
            bind.exec_driver_sql('BEGIN')
        op.execute('DROP TRIGGER publications_validation_contract')
        op.execute('DROP TRIGGER publications_alpha_contract')
        op.execute("""CREATE TRIGGER publications_validation_contract BEFORE INSERT ON publications
            WHEN json_extract(NEW.validation, '$.contracts.block_quota') IS NOT 'block-quota-v6'
            BEGIN SELECT RAISE(ABORT, 'quota-aware publication validation required'); END""")
        op.execute("""CREATE TRIGGER publications_alpha_contract BEFORE INSERT ON publications
            WHEN json_extract(NEW.validation, '$.contracts.block_quota') IS 'block-quota-v6'
             AND json_extract(NEW.validation, '$.contracts.programmes') IS NOT 'programmes-v1'
            BEGIN SELECT RAISE(ABORT, 'programme-aware publication validation required'); END""")
    elif dialect == 'postgresql':
        op.execute("""CREATE OR REPLACE FUNCTION rota_publication_validation_contract() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (NEW.validation->'contracts'->>'block_quota') IS DISTINCT FROM 'block-quota-v6' THEN
                RAISE EXCEPTION 'quota-aware publication validation required' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$""")
        op.execute("""CREATE OR REPLACE FUNCTION rota_publication_alpha_contract() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (NEW.validation->'contracts'->>'block_quota') = 'block-quota-v6'
                 AND (NEW.validation->'contracts'->>'programmes') IS DISTINCT FROM 'programmes-v1' THEN
                RAISE EXCEPTION 'programme-aware publication validation required' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$""")
    else:
        raise RuntimeError('Unsupported database dialect')


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
