"""Store template skip reasons and editable rank codes without rewriting existing data."""
from alembic import op
import sqlalchemy as sa

revision = '0066_ux_audit_b1'
down_revision = '0065_leave_types'
branch_labels = depends_on = None


def upgrade():
    op.add_column('shifts', sa.Column('template_skipped', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('categories', sa.Column('short_code', sa.String(6), nullable=False, server_default=''))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
