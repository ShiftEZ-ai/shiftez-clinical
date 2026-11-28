"""Give services and locations an optional short code for dense grids and print."""
from alembic import op
import sqlalchemy as sa


revision = '0159_service_location_codes'
down_revision = '0158_live_uniqueness'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('services', sa.Column('short_code', sa.String(6), nullable=False, server_default=''))
    op.add_column('locations', sa.Column('short_code', sa.String(6), nullable=False, server_default=''))


def downgrade():
    raise RuntimeError('Forward-only migration: retain configured service and location codes')
