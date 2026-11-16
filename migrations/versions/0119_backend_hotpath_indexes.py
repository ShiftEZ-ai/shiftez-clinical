"""Index recurring occurrence windows and stable audit ordering without changing retained data."""
from alembic import op
import sqlalchemy as sa

revision = "0119_backend_hotpath_indexes"
down_revision = "0118_shift_occurrence_index"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column("departments", sa.Column("catalogue_revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_shifts_series_occurrence", "shifts", ["series_id", "occurrence_on"])
    op.create_index("ix_shifts_occurrence_series", "shifts", ["occurrence_on", "series_id"])
    op.execute("CREATE INDEX ix_audit_events_department_at ON audit_events (department_id, created_at DESC, id DESC)")
    op.create_index("ix_audit_events_department_actor", "audit_events", ["department_id", "actor_id"])


def downgrade():
    raise RuntimeError("Forward-only: retain backend hot-path indexes")
