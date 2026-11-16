"""Add duty-taking facts without deriving or rewriting existing membership data."""
from alembic import op
import sqlalchemy as sa


revision = "0146_can_take_duty"
down_revision = "0145_draft_config"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column('departments', sa.Column('new_members_take_duties', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('memberships', sa.Column('takes_duties', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    raise RuntimeError('Forward-only migration: retain dated duty-taking facts and history')
