"""Allow a contact type to switch off its kind's natural card action."""
from alembic import op
import sqlalchemy as sa

revision = '0138_contact_action_off'
down_revision = '0137_display_settings'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('contact_types', sa.Column('action_off', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError('Forward-only: retain contact action configuration history')
