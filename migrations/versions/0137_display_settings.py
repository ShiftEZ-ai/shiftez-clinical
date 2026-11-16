"""Append-only display configuration and optional exact person name parts."""
from alembic import op
import sqlalchemy as sa

revision = '0137_display_settings'
down_revision = '0136_person_contacts'
branch_labels = depends_on = None
SAFETY_CLASS = 'fill_only'


def upgrade():
    op.add_column('people', sa.Column('given_name', sa.String(160), nullable=False, server_default=''))
    op.add_column('people', sa.Column('family_name', sa.String(160), nullable=False, server_default=''))
    op.create_table('display_settings_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id')),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('display_settings_versions.id')),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('department_id', 'revision'))
    # A null actor denotes the migration, including departments without an administrator.
    op.execute("INSERT INTO display_settings_versions (id, department_id, revision, created_at, definition, actor_id, reason, previous_id, effective_from) SELECT id, id, 1, created_at, display_settings, NULL, 'Recorded existing display settings', NULL, created_at FROM departments WHERE true ON CONFLICT (department_id, revision) DO NOTHING")


def downgrade():
    raise RuntimeError('Forward-only: retain display history and exact names')
