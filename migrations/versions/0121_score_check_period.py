"""Distinguish inferred import periods from dates explicitly chosen by the operator."""
from alembic import op
import sqlalchemy as sa

revision = "0121_score_check_period"
down_revision = "0120_score_check"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column("score_check_import_batches",
                  sa.Column("period_inferred", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("import_sources",
                  sa.Column("period_inferred", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("import_sources", sa.Column("people_count_override", sa.Integer()))
    op.add_column("import_sources", sa.Column("removed_at", sa.DateTime(timezone=True)))
    op.add_column("departments", sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("ai_provider_profiles", sa.Column("provider_kind", sa.String(32)))
    op.add_column("ai_provider_profiles", sa.Column("api_style", sa.String(16), nullable=False, server_default="chat"))
    op.add_column("score_check_extraction_jobs", sa.Column("profile_revision", sa.Integer()))
    op.add_column("score_check_extraction_jobs", sa.Column("privacy_acknowledged_at", sa.DateTime(timezone=True)))


def downgrade():
    raise RuntimeError("Forward-only: retain explicit and inferred import period provenance")
