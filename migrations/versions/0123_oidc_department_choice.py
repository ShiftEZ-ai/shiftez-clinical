"""Retain the verified identity while a browser chooses an already-authorized department."""
from alembic import op
import sqlalchemy as sa

revision = '0123_oidc_department_choice'
down_revision = '0122_score_check_gate'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('oidc_flows', sa.Column('verified_identity_id', sa.String(36), nullable=True))


def downgrade():
    raise RuntimeError('Forward-only: retain verified sign-in flow state')
