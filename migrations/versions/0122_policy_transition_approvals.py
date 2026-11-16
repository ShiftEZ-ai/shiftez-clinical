"""Add configurable independent rule lifecycle decisions and manual assignment reasons."""
from alembic import op
import sqlalchemy as sa

revision = "0122_policy_transition_approvals"
down_revision = "0121_identifier_lookup"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column("departments", sa.Column("policy_retire_approvals_required", sa.Integer(),
        sa.CheckConstraint("policy_retire_approvals_required BETWEEN 1 AND 3",
                           name="ck_departments_policy_retire_approvals"), nullable=False, server_default="1"))
    op.add_column("assignments", sa.Column("override_reason", sa.String(500), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: retain rule approval evidence and assignment reasons")
