"""Each worker heartbeat names the release the worker runs, so About can show an API/worker mismatch during a deploy.

Expand-only: one nullable column, added in place; existing rows stay NULL until their worker checks in again.

Revision ID: 0064_worker_release
Revises: 0063_catalogue_v2_facts
"""
from alembic import op
import sqlalchemy as sa

revision = "0064_worker_release"
down_revision = "0063_catalogue_v2_facts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("worker_heartbeats", sa.Column("release", sa.String(80), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
