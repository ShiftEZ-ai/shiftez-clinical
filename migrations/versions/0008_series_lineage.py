"""Explicit logical-date/version provenance for newly materialised recurring sessions.

Legacy rows keep unknown (NULL) provenance. No unreviewed history is backfilled.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_series_lineage"
down_revision = "0007_operational_resources"
branch_labels = depends_on = None

INVALID = """(NEW.series_id IS NULL AND
    (NEW.series_revision IS NOT NULL OR NEW.occurrence_on IS NOT NULL OR NEW.series_modified))
    OR ((NEW.series_revision IS NULL) <> (NEW.occurrence_on IS NULL))
    OR NEW.series_revision < 1"""


def upgrade():
    op.add_column("shifts", sa.Column("series_revision", sa.Integer()))
    op.add_column("shifts", sa.Column("occurrence_on", sa.Date()))
    op.add_column("shifts", sa.Column("series_modified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("uq_series_occurrence", "shifts", ["workspace_id", "series_id", "occurrence_on"], unique=True)
    if op.get_bind().dialect.name == "sqlite":
        for action in ("INSERT", "UPDATE"):
            op.execute(f"""CREATE TRIGGER shifts_series_lineage_{action.lower()}
                BEFORE {action} ON shifts WHEN {INVALID}
                BEGIN SELECT RAISE(ABORT, 'invalid series lineage'); END""")
    else:
        op.execute(f"""CREATE FUNCTION rota_series_lineage_guard() RETURNS trigger AS $$
            BEGIN IF {INVALID} THEN RAISE EXCEPTION 'invalid series lineage'; END IF;
            RETURN NEW; END; $$ LANGUAGE plpgsql""")
        op.execute("""CREATE TRIGGER shifts_series_lineage BEFORE INSERT OR UPDATE ON shifts
            FOR EACH ROW EXECUTE FUNCTION rota_series_lineage_guard()""")


def downgrade():
    raise RuntimeError("Forward-only: do not discard recurring-instance provenance or disable guards")
