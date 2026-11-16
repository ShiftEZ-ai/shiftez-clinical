"""Fairness ledger: per person, metric and local day, what each current publication gave them.

Expand-only: one new table (filled on publish; `scripts/manage.py rebuild-fairness` backfills it).

Revision ID: 0047_fairness_ledger
Revises: 0046_staff_wishes
"""
from alembic import op
import sqlalchemy as sa

revision = "0047_fairness_ledger"
down_revision = "0046_staff_wishes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("fairness_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("publications.id"), nullable=False),
        sa.Column("person_id", sa.String(36), nullable=True),
        sa.Column("other_person_id", sa.String(36), nullable=True),
        sa.Column("metric_key", sa.String(96), nullable=False),
        sa.Column("bucket", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("expected", sa.Float(), nullable=True),
        sa.Column("wish_points_submitted", sa.Float(), nullable=True),
        sa.Column("wish_points_granted", sa.Float(), nullable=True))
    op.create_index("ix_fairness_ledger_publication_id", "fairness_ledger", ["publication_id"])
    op.create_index("ix_fairness_metric_period", "fairness_ledger", ["department_id", "metric_key", "bucket"])
    op.create_index("ix_fairness_person_period", "fairness_ledger", ["department_id", "person_id", "bucket"])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; the ledger is rebuilt from publications, never dropped here")
