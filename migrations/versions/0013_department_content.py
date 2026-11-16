"""Dated departmental announcements and links, managed through existing catalogues."""
from alembic import op
import sqlalchemy as sa

revision = '0013_department_content'
down_revision = '0012_directory'
branch_labels = depends_on = None


def upgrade():
    op.create_table('department_content',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('starts_on', sa.Date(), nullable=False),
        sa.Column('ends_on', sa.Date()),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('url', sa.String(2000), nullable=False),
        sa.CheckConstraint("kind IN ('announcement','link')"))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
