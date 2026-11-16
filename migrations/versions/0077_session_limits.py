"""Add explicit unlimited choices without changing existing duration settings or session deadlines.

Revision ID: 0077_session_limits
Revises: 0088_date_sets
"""
from alembic import op
import sqlalchemy as sa

revision = "0077_session_limits"
down_revision = "0088_date_sets"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("session_idle_unlimited", "session_max_unlimited"):
        op.add_column("departments", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("sessions", sa.Column("expiry_unlimited", sa.Boolean(), nullable=False, server_default=sa.false()))
    if op.get_bind().dialect.name == "postgresql":  # 0044 created these checks only on PostgreSQL.
        op.drop_constraint("ck_departments_session_idle", "departments", type_="check")
        op.drop_constraint("ck_departments_session_max", "departments", type_="check")
        op.create_check_constraint("ck_departments_session_idle", "departments",
                                   "session_idle_minutes IS NULL OR session_idle_minutes >= 5")
        op.create_check_constraint("ck_departments_session_max", "departments",
                                   "session_max_hours IS NULL OR session_max_hours >= 1")


def downgrade():
    raise RuntimeError("Forward-only: restoring the old caps would reject existing session settings")
