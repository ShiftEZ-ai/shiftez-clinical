"""Environment-local authentication settings and bounded remembered sessions."""
from alembic import op
import sqlalchemy as sa

revision = "0120_auth_configuration"
down_revision = "0119_backend_hotpath_indexes"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.create_table("auth_configuration",
                    sa.Column("id", sa.String(36), primary_key=True),
                    sa.Column("revision", sa.Integer(), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("data", sa.JSON(), nullable=False),
                    sa.Column("secret_ciphertext", sa.Text(), nullable=True),
                    sa.Column("previous", sa.JSON(), nullable=True),
                    sa.CheckConstraint("id = 'environment'", name="ck_auth_configuration_singleton"))
    op.add_column("sessions", sa.Column("remember_me", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("oidc_flows", sa.Column("remember_me", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("oidc_flows", sa.Column("config_revision", sa.Integer(), nullable=True))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
