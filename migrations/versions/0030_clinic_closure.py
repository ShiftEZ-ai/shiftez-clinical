"""Keep the reviewed closure explanation with the session and its snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '0030_clinic_closure'
down_revision = '0029_generation_defaults'
branch_labels = depends_on = None


def upgrade():
    op.add_column('shifts', sa.Column('cancellation_reason', sa.String(500), nullable=True))


def downgrade():
    op.drop_column('shifts', 'cancellation_reason')
