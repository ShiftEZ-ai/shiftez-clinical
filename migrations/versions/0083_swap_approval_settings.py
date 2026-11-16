"""Add department swap automation, explicit swap routes and preparation refusal evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0083_swap_approval_settings"
down_revision = "0077_session_limits"
branch_labels = depends_on = None


def upgrade():
    op.add_column("review_routes", sa.Column("purpose", sa.String(16),
                                            sa.CheckConstraint("purpose IN ('rota','swap')", name="ck_review_routes_purpose"),
                                            nullable=False, server_default="rota"))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE departments ADD COLUMN swap_review_route_id VARCHAR(36) REFERENCES review_routes(id)")
    else:
        op.add_column("departments", sa.Column("swap_review_route_id", sa.String(36),
                                              sa.ForeignKey("review_routes.id"), nullable=True))
    op.add_column("departments", sa.Column("swap_auto_prepare", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("departments", sa.Column("swap_auto_publish", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("swap_events", sa.Column("detail", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("""CREATE TRIGGER review_routes_purpose_guard BEFORE UPDATE ON review_routes
            WHEN OLD.status != 'proposed' AND OLD.purpose IS NOT NEW.purpose
            BEGIN SELECT RAISE(ABORT, 'immutable review route purpose'); END""")
    else:
        op.execute("""CREATE FUNCTION rota_review_route_purpose_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.status != 'proposed' AND OLD.purpose IS DISTINCT FROM NEW.purpose THEN
                    RAISE EXCEPTION 'immutable review route purpose';
                END IF;
                RETURN NEW;
            END $$""")
        op.execute("""CREATE TRIGGER review_routes_purpose_guard BEFORE UPDATE ON review_routes
            FOR EACH ROW EXECUTE FUNCTION rota_review_route_purpose_guard()""")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
