"""Server-assigned programme ownership without enrolling a director as a trainee.

Additive only: existing programmes retain a NULL owner and their current authority.
"""
from alembic import op
import sqlalchemy as sa

revision = "0091_programme_ownership"
down_revision = "0090_residency"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE programmes ADD COLUMN owner_identity_id VARCHAR(36) REFERENCES identities(id)")
    else:
        op.add_column("programmes", sa.Column("owner_identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=True))
    op.create_index("ix_programmes_owner_identity_id", "programmes", ["owner_identity_id"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
