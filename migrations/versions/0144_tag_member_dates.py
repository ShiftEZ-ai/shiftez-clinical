"""Add optional date bounds to person tag memberships.

Additive: existing tag membership rows remain open-ended (NULL dates) and unchanged.
The check is declared on the added column, not with create_check_constraint: migrations/source_safety.py
classifies every table-level form on an existing table as destructive (batch copy or dialect branch) and
SQLite cannot ALTER constraints; PostgreSQL stores this multi-column column check as a table check anyway.
"""
from alembic import op
import sqlalchemy as sa


revision = "0144_tag_member_dates"
down_revision = "0143_merge_heads"
branch_labels = None
depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column("tag_members", sa.Column("starts_on", sa.Date(), nullable=True))
    op.add_column("tag_members", sa.Column(
        "ends_on", sa.Date(),
        sa.CheckConstraint("(ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on)"
                           " AND (kind = 'people' OR (starts_on IS NULL AND ends_on IS NULL))",
                           name="ck_tag_members_dates"),
        nullable=True,
    ))


def downgrade():
    raise RuntimeError("Forward-only migration: retain tag membership date history")
