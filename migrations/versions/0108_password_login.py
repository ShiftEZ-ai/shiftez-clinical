"""Record authentication methods and durable login evidence without changing existing records."""
from alembic import op
import sqlalchemy as sa

revision = "0108_password_login"
down_revision = "0102_wish_outcome_reasons"
branch_labels = depends_on = None
DESTRUCTIVE_REVIEWED = (
    "Owner-approved authentication-event retention only: install a bounded purge function, with a one-day "
    "minimum age and no migration-time deletion. Clinical, publication and other audit evidence stays immutable. "
    "Verify a backup before deployment; recover retained authentication events from that backup if required.")


def upgrade():
    op.add_column("sessions", sa.Column("auth_method", sa.String(16), nullable=True))
    op.create_table("auth_login_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_hash", sa.String(64), nullable=False),
        sa.Column("peer_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True))
    op.create_index("ix_auth_login_events_created_at", "auth_login_events", ["created_at"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER auth_login_events_update_guard BEFORE UPDATE ON auth_login_events "
                   "BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
        # SQLite is test/development only and has no owner/runtime role separation.
        op.execute("CREATE TRIGGER auth_login_events_delete_guard BEFORE DELETE ON auth_login_events "
                   "WHEN julianday(OLD.created_at) IS NULL OR julianday(OLD.created_at) >= julianday('now', '-1 day') "
                   "BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
    else:
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        qualified = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        table = qualified + '.auth_login_events'
        guard = qualified + '.auth_login_event_guard'
        prune = qualified + '.prune_auth_login_events'
        op.execute(f"""CREATE FUNCTION {guard}() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
                IF TG_OP = 'DELETE' AND OLD.created_at < statement_timestamp() - interval '1 day'
                    AND current_user = (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID) THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'immutable evidence';
            END $$""")
        op.execute("CREATE TRIGGER auth_login_events_guard BEFORE UPDATE OR DELETE ON auth_login_events "
                   f"FOR EACH ROW EXECUTE FUNCTION {guard}()")
        op.execute(f"""CREATE FUNCTION {prune}(retention_days integer, batch_size integer) RETURNS integer
            LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            DECLARE removed integer;
            BEGIN
                IF retention_days IS NULL OR retention_days < 1 OR retention_days > 3650
                    OR batch_size IS NULL OR batch_size < 1 OR batch_size > 500 THEN
                    RAISE EXCEPTION 'Invalid authentication-event retention bounds';
                END IF;
                WITH expired AS (
                    SELECT id FROM {table}
                    WHERE created_at < statement_timestamp() - make_interval(days => retention_days)
                    ORDER BY created_at, id LIMIT batch_size FOR UPDATE SKIP LOCKED
                )
                DELETE FROM {table} WHERE id IN (SELECT id FROM expired);
                GET DIAGNOSTICS removed = ROW_COUNT;
                RETURN removed;
            END $$""")
        op.execute(f"REVOKE ALL ON FUNCTION {guard}() FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {prune}(integer, integer) FROM PUBLIC")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
