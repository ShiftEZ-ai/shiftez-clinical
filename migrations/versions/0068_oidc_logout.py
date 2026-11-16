"""Keep the verified ID token server-side and bind forced reauthentication to the sign-in flow."""
from alembic import op
import sqlalchemy as sa

revision = "0068_oidc_logout"
down_revision = "0067_feedback_reports"
branch_labels = depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("oidc_id_token", sa.Text(), nullable=True))
    op.add_column("oidc_flows", sa.Column("force_login", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("oidc_flows", sa.Column("completed_session_id", sa.String(36), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
