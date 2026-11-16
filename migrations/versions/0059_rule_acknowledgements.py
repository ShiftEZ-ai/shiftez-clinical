"""Rules › Needs attention acknowledgements shared by the department (who, when, note), keyed by the issue's fingerprint.

Expand-only: one new table.

Revision ID: 0059_rule_acknowledgements
Revises: 0058_workspace_accepted_issues
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_rule_acknowledgements"
down_revision = "0058_workspace_accepted_issues"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rule_acknowledgements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("acknowledged_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("department_id", "fingerprint"),
    )


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
