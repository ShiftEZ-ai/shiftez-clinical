"""Retain withdrawn dated relationships and their original identities."""
from alembic import op
import sqlalchemy as sa


revision = '0147_relationship_withdrawal'
down_revision = '0146_can_take_duty'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('memberships', sa.Column('withdrawn', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('eligibilities', sa.Column('withdrawn', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('programme_memberships', sa.Column('withdrawn', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('rotations', sa.Column('withdrawn', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError('Forward-only migration: retain withdrawn relationships and history')
