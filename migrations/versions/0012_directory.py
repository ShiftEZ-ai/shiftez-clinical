"""Work directory metadata; no clinical authority inferred from these fields."""
from alembic import op
import sqlalchemy as sa

revision = "0012_directory"
down_revision = "0011_alpha_programmes"
branch_labels = depends_on = None


def upgrade():
    for name, length in [('title', 160), ('work_phone', 80), ('work_extension', 80),
                         ('staff_number', 80), ('directory_section', 160)]:
        op.add_column('people', sa.Column(name, sa.String(length), nullable=False, server_default=''))
    op.add_column('people', sa.Column('directory_order', sa.Integer(), nullable=False, server_default='1000'))
    def common():
        return [sa.Column('id', sa.String(36), primary_key=True),
                sa.Column('revision', sa.Integer(), nullable=False),
                sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
                sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
                sa.Column('source_name', sa.String(160), nullable=False)]
    op.create_table('reference_imports', *common(),
                    sa.Column('archive_sha256', sa.String(64), nullable=False),
                    sa.Column('manifest', sa.JSON(), nullable=False),
                    sa.UniqueConstraint('department_id', 'archive_sha256'))
    op.create_table('source_mappings', *common(),
                    sa.Column('source_key', sa.String(300), nullable=False),
                    sa.Column('target_kind', sa.String(40), nullable=False),
                    sa.Column('target_id', sa.String(36), nullable=False),
                    sa.UniqueConstraint('department_id', 'source_name', 'source_key'))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
