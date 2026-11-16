"""Record assignment provenance prospectively; existing rows remain unknown.

Follows dev's 0086_swap_open (re-chained to keep a single head)."""
from alembic import op
import sqlalchemy as sa

revision = "0070_assignment_source"
down_revision = "0086_swap_open"
branch_labels = depends_on = None


def upgrade():
    op.add_column("assignments", sa.Column("source", sa.String(20), nullable=True))
    op.add_column("assignments", sa.Column("source_run_id", sa.String(36), nullable=True))
    op.add_column("assignments", sa.Column("source_actor_id", sa.String(36), nullable=True))
    op.add_column("assignments", sa.Column("source_at", sa.DateTime(timezone=True), nullable=True))
    # SQLite cannot add foreign keys without rebuilding the table; PostgreSQL enforces these references.
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key("fk_assignment_source_run", "assignments", "jobs", ["source_run_id"], ["id"])
        op.create_foreign_key("fk_assignment_source_actor", "assignments", "identities", ["source_actor_id"], ["id"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
