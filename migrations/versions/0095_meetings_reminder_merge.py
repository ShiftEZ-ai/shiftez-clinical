"""Retain the reminder integration checkpoint in the linear migration batch."""

revision = "0095_meetings_reminder_merge"
down_revision = "0094_announcement_empty_checks"
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Forward-only migration")
