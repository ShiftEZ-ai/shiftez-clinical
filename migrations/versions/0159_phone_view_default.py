"""Keep a personal default view for the phone beside the one for a desktop, without rewriting existing choices."""
from alembic import op
import sqlalchemy as sa

revision = '0159_phone_view_default'
down_revision = '0158_live_uniqueness'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('table_view_defaults', sa.Column('phone_view_id', sa.String(36), nullable=True))
    op.add_column('table_view_defaults', sa.Column('phone_built_in', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('table_view_defaults', sa.Column('phone_split', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError('Forward-only: retain saved-view default choices and command audit history')
