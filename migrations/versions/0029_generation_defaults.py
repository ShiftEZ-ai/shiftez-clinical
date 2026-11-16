"""Department-owned generation defaults, resolved in immutable job snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '0029_generation_defaults'
down_revision = '0028_job_progress'
branch_labels = depends_on = None


def upgrade():
    op.add_column('departments', sa.Column('generation_defaults', sa.JSON(),
                                         nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('departments', 'generation_defaults')
