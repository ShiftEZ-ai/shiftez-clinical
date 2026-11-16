"""Retain publication change lists and versioned absence-report settings without altering issued evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0126_published_changes'
down_revision = '0125_application_branding'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table('publication_change_lists',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('publication_id', sa.String(36), sa.ForeignKey('publications.id'), nullable=False, unique=True),
        sa.Column('workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=False, unique=True),
        sa.Column('changes', sa.JSON(), nullable=False))
    op.create_index('ix_publication_change_lists_department_id', 'publication_change_lists', ['department_id'])
    op.create_table('absence_report_settings',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False, unique=True),
        sa.Column('document_required', sa.Boolean(), nullable=False))
    op.create_index('ix_absence_report_settings_department_id', 'absence_report_settings', ['department_id'])


def downgrade():
    raise RuntimeError('Forward-only: retain published change and settings history')
