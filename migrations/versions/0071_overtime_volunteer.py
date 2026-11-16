"""Add request-linked overtime availability without rewriting existing approved definitions.

Missing volunteer settings retain whenever-free behaviour in the resolver; newly authored lines default to
volunteer-only. Re-chain after any intervening migration before admission.
"""
from alembic import op
import sqlalchemy as sa

revision = "0071_overtime_volunteer"
down_revision = "0070_assignment_source"
branch_labels = depends_on = None


def upgrade():
    op.add_column("requests", sa.Column("overtime", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE scheduling_facts ADD COLUMN request_id VARCHAR(36) REFERENCES requests(id)")
    else:
        op.add_column("scheduling_facts", sa.Column("request_id", sa.String(36), sa.ForeignKey("requests.id"), nullable=True))
    op.create_index("ix_scheduling_facts_request_id", "scheduling_facts", ["request_id"])


def downgrade():
    raise RuntimeError("Forward-only: retain staff requests and their approved offer evidence")
