"""Reusable rotation definitions; requirements remain shared scoped policies."""
from alembic import op
import sqlalchemy as sa

revision = '0026_rotation_types'
down_revision = '0025_target_overrides'
branch_labels = depends_on = None


def upgrade():
    op.create_table('rotation_types',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('starts_on', sa.Date(), nullable=False),
        sa.Column('ends_on', sa.Date(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE rotations ADD COLUMN rotation_type_id VARCHAR(36) REFERENCES rotation_types(id)')
    else:
        op.add_column('rotations', sa.Column('rotation_type_id', sa.String(36), sa.ForeignKey('rotation_types.id'), nullable=True))
    with op.batch_alter_table('rotations') as batch:
        batch.alter_column('service_id', existing_type=sa.String(36), nullable=True)
        batch.create_check_constraint('rotation_has_definition', 'service_id IS NOT NULL OR rotation_type_id IS NOT NULL')
    op.create_index('ix_rotations_rotation_type_id', 'rotations', ['rotation_type_id'])


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
