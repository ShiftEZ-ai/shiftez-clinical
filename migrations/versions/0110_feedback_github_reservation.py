"""Reserve one external issue attempt per feedback report before contacting GitHub."""
from alembic import op
import sqlalchemy as sa

revision = "0110_feedback_github_reservation"
down_revision = "0098_bulk_privacy"
branch_labels = depends_on = None


def upgrade():
    op.add_column("feedback_reports", sa.Column("github_export_repo", sa.String(300), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: retain external issue reservations")
