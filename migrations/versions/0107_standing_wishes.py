"""Standing wish definitions and period/rota overrides; existing wishes remain one-off."""
from alembic import op
import sqlalchemy as sa

revision = "0107_standing_wishes"
down_revision = "0100_rotation_attachments"
branch_labels = depends_on = None


def upgrade():
    op.add_column("staff_wishes", sa.Column("repeat", sa.JSON(none_as_null=True)))
    op.create_table("wish_overrides",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("wish_id", sa.String(36), sa.ForeignKey("staff_wishes.id"), nullable=False),
        sa.Column("period_start", sa.Date()),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id")),
        sa.Column("skip", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("points", sa.Integer()),
        sa.Column("number", sa.Integer()),
        sa.UniqueConstraint("wish_id", "period_start"), sa.UniqueConstraint("wish_id", "workspace_id"),
        sa.CheckConstraint("(period_start IS NULL) <> (workspace_id IS NULL)"),
        sa.CheckConstraint("points IS NULL OR points >= 1"),
        sa.CheckConstraint("number IS NULL OR number >= 1"))
    op.create_index("ix_wish_overrides_department", "wish_overrides", ["department_id", "wish_id"])


def downgrade():
    raise RuntimeError("Forward-only: standing preferences and overrides are part of the record")
