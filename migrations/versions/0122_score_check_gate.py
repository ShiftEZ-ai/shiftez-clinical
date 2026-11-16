"""Keep score checking dark until the department explicitly enables it."""
from alembic import op
import sqlalchemy as sa

revision = '0122_score_check_gate'
down_revision = '0121_score_check_period'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('departments', sa.Column('score_check_enabled', sa.Boolean(),
                                         nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError('Forward-only: retain department feature settings')
