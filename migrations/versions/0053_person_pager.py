"""A person's pager number, shown in the schedule's person label when a view asks for it.

Expand-only: one text column with an empty default, added in place.

Revision ID: 0053_person_pager
Revises: 0052_attachments
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_person_pager"
down_revision = "0052_attachments"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("people", sa.Column("pager", sa.String(40), nullable=False, server_default=""))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
