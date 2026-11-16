"""Department display defaults: how names appear, how short codes are made, and the tag list."""
from alembic import op
import sqlalchemy as sa

revision = '0032_department_display'
down_revision = '0031_person_codes_tags'
branch_labels = depends_on = None


def upgrade():
    op.add_column('departments', sa.Column('display_settings', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('departments', 'display_settings')
