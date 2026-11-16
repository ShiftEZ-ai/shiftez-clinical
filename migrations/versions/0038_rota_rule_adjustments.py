"""Per-rota adjustments over department rules (switch a rule off, or another number), reviewed with the rota.

Revision ID: 0038_rota_rule_adjustments
Revises: 0037_availability_patterns
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_rota_rule_adjustments"
down_revision = "0037_availability_patterns"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspaces", sa.Column("rule_adjustments", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
