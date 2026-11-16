"""Explicit supervision pairing and shift leadership."""
from alembic import op
import sqlalchemy as sa

revision = '0014_supervision_pairing'
down_revision = '0013_department_content'
branch_labels = depends_on = None


def upgrade():
    # SQLite table rebuilds discard the existing issued-assignment guard triggers.
    # Nullable REFERENCES is supported by additive ALTER TABLE, preserving guards.
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE assignments ADD COLUMN supervisor_person_id VARCHAR(36) REFERENCES people(id)')
    else:
        op.add_column('assignments', sa.Column('supervisor_person_id', sa.String(36), sa.ForeignKey('people.id'), nullable=True))
    op.add_column('assignments', sa.Column('team_lead', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
