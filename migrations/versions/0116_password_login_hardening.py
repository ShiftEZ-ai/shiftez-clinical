"""Strengthen authentication retention without deleting existing evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0116_password_login_hardening"
down_revision = "0115_worker_poll_indexes"
branch_labels = depends_on = None
DESTRUCTIVE_REVIEWED = (
    "Strengthen the already authorized authentication-event retention function to a 30-day minimum and worker-only "
    "execution; no migration-time deletion or evidence rewrite. Keep the existing guards, add a stricter floor, "
    "verify a backup before deployment and restore the compatible database/image pair for rollback.")


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER auth_login_events_retention_floor BEFORE DELETE ON auth_login_events "
                   "WHEN julianday(OLD.created_at) IS NULL OR julianday(OLD.created_at) >= julianday('now', '-30 days') "
                   "BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
        return
    schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
    qualified = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
    table = qualified + ".auth_login_events"
    guard = qualified + ".auth_login_retention_floor"
    prune = qualified + ".prune_auth_login_events"
    op.execute(f"""CREATE FUNCTION {guard}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF OLD.created_at < statement_timestamp() - interval '30 days' THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'immutable evidence';
        END $$""")
    op.execute(f"CREATE TRIGGER auth_login_events_retention_floor BEFORE DELETE ON {table} "
               f"FOR EACH ROW EXECUTE FUNCTION {guard}()")
    op.execute(f"""CREATE OR REPLACE FUNCTION {prune}(retention_days integer, batch_size integer) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE removed integer;
        BEGIN
            IF retention_days IS NULL OR retention_days < 30 OR retention_days > 3650
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
    # Remove previous runtime grants, including installations using a renamed API role.
    grantees = op.get_bind().execute(sa.text("""SELECT DISTINCT r.rolname
        FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
        JOIN pg_roles r ON r.oid = a.grantee
        WHERE p.oid = to_regprocedure(:function) AND a.grantee <> p.proowner"""),
        {"function": prune + "(integer,integer)"}).scalars()
    for name in grantees:
        role = op.get_bind().dialect.identifier_preparer.quote_identifier(name)
        op.execute(f"REVOKE ALL ON FUNCTION {prune}(integer, integer) FROM {role}")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rota_worker') THEN
            GRANT EXECUTE ON FUNCTION {prune}(integer, integer) TO rota_worker;
        END IF;
    END $$""")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
