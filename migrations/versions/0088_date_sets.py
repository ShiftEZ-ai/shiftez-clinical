"""Department date sets; retain every pre-existing record and publication unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0088_date_sets"
down_revision = "0073_integration_credentials"
branch_labels = depends_on = None


def upgrade():
    op.create_table("date_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("entries", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
