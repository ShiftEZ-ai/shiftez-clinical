"""Retain the email integration checkpoint in the linear migration batch."""

revision = '0096_meetings_email_merge'
down_revision = "0095_meetings_reminder_merge"
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError('Forward-only migration')
