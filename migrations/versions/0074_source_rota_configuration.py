"""Add configurable person labels and recurring-session provenance without rewriting records."""
from alembic import op
import sqlalchemy as sa

revision = "0074_source_rota_configuration"
down_revision = "0091_programme_ownership"
branch_labels = depends_on = None


def upgrade():
    op.add_column("people", sa.Column("rota_name", sa.String(160), nullable=False, server_default=""))
    op.add_column("people", sa.Column("rota_aliases", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("shifts", sa.Column("series_snapshot", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
