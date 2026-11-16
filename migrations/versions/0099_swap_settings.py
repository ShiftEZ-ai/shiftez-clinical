"""Department swap limits; expand-only, preserving existing consent and publication evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0099_swap_settings"
down_revision = "0097_wish_type_settings"
branch_labels = depends_on = None


def upgrade():
    for name, default in (("swap_offer_window_days", "7"), ("swap_open_offer_cap", "25")):
        # A column check also works on SQLite without rebuilding the department table.
        op.add_column("departments", sa.Column(name, sa.Integer(),
                      sa.CheckConstraint(f"{name} BETWEEN 1 AND 2147483647", name=f"ck_departments_{name}"),
                      nullable=False, server_default=default))


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
