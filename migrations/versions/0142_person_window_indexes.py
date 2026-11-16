"""Index dated person rows used by schedule and timeline reads.

Expand-only: these indexes accelerate bounded availability and rotation reads;
they do not alter rows, evidence or scheduling decisions.
"""
from alembic import op

revision = "0142_person_window_indexes"
SAFETY_CLASS = "additive"
down_revision = "0128_job_supersessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_availability_department_person_window",
        "availability",
        ["department_id", "person_id", "start", "end", "retracted"],
    )
    op.create_index(
        "ix_rotations_department_person_window",
        "rotations",
        ["department_id", "person_id", "starts_on", "ends_on"],
    )
    op.create_index(
        "ix_availability_patterns_department_person_window",
        "availability_patterns",
        ["department_id", "person_id", "starts_on", "ends_on", "status"],
    )


def downgrade():
    raise RuntimeError("Forward-only migration: retain person-window read indexes")
