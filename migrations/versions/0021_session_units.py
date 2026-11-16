"""Require weighted-session-aware validation for new publications only."""
from alembic import op

revision = '0021_session_units'
down_revision = '0020_programme_calendar'
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('DROP TRIGGER publications_validation_contract')
        op.execute('DROP TRIGGER publications_alpha_contract')
        op.execute("""CREATE TRIGGER publications_validation_contract BEFORE INSERT ON publications
            WHEN json_extract(NEW.validation, '$.contracts.block_quota') IS NOT 'block-quota-v2'
            BEGIN SELECT RAISE(ABORT, 'quota-aware publication validation required'); END""")
        op.execute("""CREATE TRIGGER publications_alpha_contract BEFORE INSERT ON publications
            WHEN json_extract(NEW.validation, '$.contracts.block_quota') IS 'block-quota-v2'
             AND json_extract(NEW.validation, '$.contracts.programmes') IS NOT 'programmes-v1'
            BEGIN SELECT RAISE(ABORT, 'programme-aware publication validation required'); END""")
    else:
        op.execute("""CREATE OR REPLACE FUNCTION rota_publication_validation_contract() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (NEW.validation->'contracts'->>'block_quota') IS DISTINCT FROM 'block-quota-v2' THEN
                RAISE EXCEPTION 'quota-aware publication validation required' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$""")
        op.execute("""CREATE OR REPLACE FUNCTION rota_publication_alpha_contract() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (NEW.validation->'contracts'->>'block_quota') = 'block-quota-v2'
                 AND (NEW.validation->'contracts'->>'programmes') IS DISTINCT FROM 'programmes-v1' THEN
                RAISE EXCEPTION 'programme-aware publication validation required' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$""")


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
