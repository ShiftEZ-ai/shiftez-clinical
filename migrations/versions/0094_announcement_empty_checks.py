"""Bound empty-audience reminder checks without consuming the real reminder cooldown."""
from alembic import op
import sqlalchemy as sa

revision = "0094_announcement_empty_checks"
down_revision = "0087_email_delivery"
branch_labels = depends_on = None


def upgrade():
    op.add_column("department_content", sa.Column("reminder_empty_checked_on", sa.Date()))


def downgrade():
    raise RuntimeError("Forward-only migration")
