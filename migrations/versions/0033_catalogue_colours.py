"""Configurable colours for services, staff roles and people ('' means automatic)."""
from alembic import op
import sqlalchemy as sa

revision = '0033_catalogue_colours'
down_revision = '0032_department_display'
branch_labels = depends_on = None


def upgrade():
    for table in ('services', 'categories', 'people'):
        op.add_column(table, sa.Column('colour', sa.String(16), nullable=False, server_default=''))


def downgrade():
    for table in ('people', 'categories', 'services'):
        op.drop_column(table, 'colour')
