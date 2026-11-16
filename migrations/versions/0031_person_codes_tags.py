"""Person display short code and free-form tags for dense rota views."""
from alembic import op
import sqlalchemy as sa

revision = '0031_person_codes_tags'
down_revision = '0030_clinic_closure'
branch_labels = depends_on = None


def upgrade():
    op.add_column('people', sa.Column('short_code', sa.String(6), nullable=False, server_default=''))
    op.add_column('people', sa.Column('tags', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    op.drop_column('people', 'tags')
    op.drop_column('people', 'short_code')
