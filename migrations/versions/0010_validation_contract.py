"""Require quota-aware publication validation without rewriting prior evidence.

No new columns are needed for quota JSON, but the semantic compatibility boundary
must not be invisible to readiness, deployment or an older binary's publication.
"""
from alembic import op

revision = "0010_validation_contract"
down_revision = "0009_participation_segments"
branch_labels = depends_on = None


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("""CREATE TRIGGER publications_validation_contract BEFORE INSERT ON publications
            WHEN json_extract(NEW.validation, '$.contracts.block_quota') IS NOT 'block-quota-v1'
            BEGIN SELECT RAISE(ABORT, 'quota-aware publication validation required'); END""")
    elif dialect == "postgresql":
        op.execute("""CREATE FUNCTION rota_publication_validation_contract() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (NEW.validation->'contracts'->>'block_quota') IS DISTINCT FROM 'block-quota-v1' THEN
                RAISE EXCEPTION 'quota-aware publication validation required' USING ERRCODE='23514';
              END IF;
              RETURN NEW;
            END $$""")
        op.execute("""CREATE TRIGGER publications_validation_contract BEFORE INSERT ON publications
            FOR EACH ROW EXECUTE FUNCTION rota_publication_validation_contract()""")
    else:
        raise RuntimeError("Unsupported database dialect")


def downgrade():
    raise RuntimeError("Forward-only: do not re-enable quota-unaware publication; use an approved forward repair or restore")
