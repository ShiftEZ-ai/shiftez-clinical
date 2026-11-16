"""The department's weekend days (server weekday numbers, Monday 0 … Sunday 6), shown shaded on every calendar.

Expand-only: one JSON column defaulting to Friday and Saturday, added in place.

Revision ID: 0056_department_weekend
Revises: 0055_catalogue_rule_unique
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_department_weekend"
down_revision = "0055_catalogue_rule_unique"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("departments", sa.Column("weekend_days", sa.JSON(), nullable=False, server_default="[4, 5]"))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
