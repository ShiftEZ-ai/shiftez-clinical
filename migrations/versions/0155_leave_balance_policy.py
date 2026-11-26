"""Store editable leave allowances and leave-year policy."""
from alembic import op
import sqlalchemy as sa


revision = '0155_leave_balance_policy'
down_revision = '0154_request_undo_note'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table(
        'leave_balance_policies',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False, unique=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('previous_definition', sa.JSON(), nullable=True),
    )
    op.create_index('ix_leave_balance_policies_department_id', 'leave_balance_policies', ['department_id'])


def downgrade():
    raise RuntimeError('Forward-only migration: retain leave allowance policy')
