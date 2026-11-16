"""Residency: external rotators on programme memberships, rotation change requests, rotation templates and trainee notes.

Additive only: new nullable or defaulted columns and two new tables. Existing rows keep their values.
"""
from alembic import op
import sqlalchemy as sa

revision = "0090_residency"
down_revision = "0071_overtime_volunteer"
branch_labels = depends_on = None


def upgrade():
    op.add_column("programme_memberships", sa.Column("external", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("programme_memberships", sa.Column("home_programme", sa.String(160), nullable=False, server_default=""))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE requests ADD COLUMN rotation_id VARCHAR(36) REFERENCES rotations(id)")
    else:
        op.add_column("requests", sa.Column("rotation_id", sa.String(36), sa.ForeignKey("rotations.id"), nullable=True))
    op.create_index("ix_requests_rotation_id", "requests", ["rotation_id"])
    op.add_column("requests", sa.Column("details", sa.JSON(), nullable=True))
    op.create_table("rotation_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("programme_id", sa.String(36), sa.ForeignKey("programmes.id"), nullable=True, index=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("blocks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table("trainee_notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), nullable=False, index=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
