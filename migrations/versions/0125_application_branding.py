"""Application-wide branding and retained safe raster assets; no existing data changes."""
from alembic import op
import sqlalchemy as sa

revision = '0125_application_branding'
down_revision = '0124_type_lead_ownership'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table('application_branding',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('previous', sa.JSON(), nullable=True),
        sa.CheckConstraint("id = 'application'"))
    op.create_table('branding_images',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint('sha256'))


def downgrade():
    raise RuntimeError('Forward-only: retain branding versions and image assets')
