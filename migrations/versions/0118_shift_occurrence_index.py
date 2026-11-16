"""Additive index for recurring-session occurrence date windows; no stored data changes."""
from alembic import op

revision = "0118_shift_occurrence_index"
down_revision = "0117_backend_read_indexes"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.create_index("ix_shifts_occurrence_on", "shifts", ["occurrence_on"])


def downgrade():
    raise RuntimeError("Forward-only: retain the occurrence date index")
