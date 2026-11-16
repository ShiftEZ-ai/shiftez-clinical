"""Immutable evidence and published duty database guards.

Revision ID: 0002_guards
Revises: 0001_core
"""
from alembic import op

revision = "0002_guards"
down_revision = "0001_core"
branch_labels = None
depends_on = None
IMMUTABLE = ("audit_events", "definition_history", "approvals", "command_receipts")


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in IMMUTABLE:
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_{action.lower()}_guard BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
        op.execute("""CREATE TRIGGER publications_update_guard BEFORE UPDATE ON publications
                      WHEN NEW.snapshot != OLD.snapshot OR NEW.validation != OLD.validation OR
                      NEW.fingerprint != OLD.fingerprint OR NEW.workspace_id != OLD.workspace_id OR
                      NEW.sequence != OLD.sequence OR NEW.starts_on != OLD.starts_on OR
                      NEW.ends_on != OLD.ends_on OR NEW.published_by != OLD.published_by OR
                      NEW.department_id != OLD.department_id OR NEW.created_at != OLD.created_at OR
                      NEW.supersedes_id IS NOT OLD.supersedes_id
                      BEGIN SELECT RAISE(ABORT, 'immutable publication'); END""")
        op.execute("CREATE TRIGGER publications_delete_guard BEFORE DELETE ON publications BEGIN SELECT RAISE(ABORT, 'immutable publication'); END")
        for action in ("INSERT", "UPDATE", "DELETE"):
            source = "OLD" if action == "DELETE" else "NEW"
            op.execute(f"""CREATE TRIGGER shifts_{action.lower()}_guard BEFORE {action} ON shifts
                          WHEN (SELECT status FROM workspaces WHERE id={source}.workspace_id) IN ('published','superseded')
                          BEGIN SELECT RAISE(ABORT, 'published duties are immutable'); END""")
            op.execute(f"""CREATE TRIGGER assignments_{action.lower()}_guard BEFORE {action} ON assignments
                          WHEN (SELECT w.status FROM workspaces w JOIN shifts s ON s.workspace_id=w.id WHERE s.id={source}.shift_id) IN ('published','superseded')
                          BEGIN SELECT RAISE(ABORT, 'published assignments are immutable'); END""")
            op.execute(f"""CREATE TRIGGER shift_services_{action.lower()}_guard BEFORE {action} ON shift_services
                          WHEN (SELECT w.status FROM workspaces w JOIN shifts s ON s.workspace_id=w.id WHERE s.id={source}.shift_id) IN ('published','superseded')
                          BEGIN SELECT RAISE(ABORT, 'published service support is immutable'); END""")
    elif dialect == "postgresql":
        op.execute("""CREATE FUNCTION rota_immutable_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
                      BEGIN RAISE EXCEPTION 'immutable evidence'; END $$""")
        for table in IMMUTABLE:
            op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION rota_immutable_evidence()")
        op.execute("""CREATE FUNCTION rota_publication_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                      BEGIN
                        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable publication'; END IF;
                        IF (to_jsonb(NEW) - 'current') IS DISTINCT FROM (to_jsonb(OLD) - 'current') THEN
                          RAISE EXCEPTION 'immutable publication';
                        END IF;
                        RETURN NEW;
                      END $$""")
        op.execute("CREATE TRIGGER publications_guard BEFORE UPDATE OR DELETE ON publications FOR EACH ROW EXECUTE FUNCTION rota_publication_guard()")
        op.execute("""CREATE FUNCTION rota_duty_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                      DECLARE workspace_status text; row_data jsonb;
                      BEGIN
                        IF TG_OP = 'DELETE' THEN row_data := to_jsonb(OLD); ELSE row_data := to_jsonb(NEW); END IF;
                        IF TG_TABLE_NAME = 'shifts' THEN
                          SELECT status INTO workspace_status FROM workspaces WHERE id = row_data->>'workspace_id';
                        ELSE
                          SELECT w.status INTO workspace_status FROM workspaces w JOIN shifts s ON s.workspace_id=w.id WHERE s.id=row_data->>'shift_id';
                        END IF;
                        IF workspace_status IN ('published','superseded') THEN RAISE EXCEPTION 'published duties are immutable'; END IF;
                        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
                      END $$""")
        for table in ("shifts", "assignments", "shift_services"):
            op.execute(f"CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION rota_duty_guard()")
    else:
        raise RuntimeError("Unsupported database dialect")


def downgrade():
    raise RuntimeError("Evidence guards are not removed by automatic downgrade. Use a reviewed forward repair or restore.")
