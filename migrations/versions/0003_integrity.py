"""Close published-row reassignment loopholes without removing evidence guards.

Revision ID: 0003_integrity
Revises: 0002_guards
"""
from alembic import op
revision = "0003_integrity"
down_revision = "0002_guards"
branch_labels = None
depends_on = None


def upgrade():
    dialect=op.get_bind().dialect.name
    if dialect=="sqlite":
        op.execute("""CREATE TRIGGER shifts_old_owner_guard BEFORE UPDATE ON shifts
            WHEN (SELECT status FROM workspaces WHERE id=OLD.workspace_id) IN ('published','superseded')
            BEGIN SELECT RAISE(ABORT, 'published duty ownership is immutable'); END""")
        for table in ("assignments","shift_services"):
            op.execute(f"""CREATE TRIGGER {table}_old_owner_guard BEFORE UPDATE ON {table}
                WHEN (SELECT w.status FROM workspaces w JOIN shifts s ON s.workspace_id=w.id WHERE s.id=OLD.shift_id) IN ('published','superseded')
                BEGIN SELECT RAISE(ABORT, 'published duty ownership is immutable'); END""")
        op.execute("""CREATE TRIGGER workspaces_published_state_guard BEFORE UPDATE ON workspaces
            WHEN OLD.status IN ('published','superseded') AND NEW.status NOT IN ('published','superseded')
            BEGIN SELECT RAISE(ABORT, 'published workspace cannot become a draft'); END""")
        op.execute("""CREATE TRIGGER publications_identity_guard BEFORE UPDATE ON publications
            WHEN NEW.id != OLD.id OR NEW.revision != OLD.revision
            BEGIN SELECT RAISE(ABORT, 'publication identity is immutable'); END""")
    elif dialect=="postgresql":
        op.execute("""CREATE OR REPLACE FUNCTION rota_duty_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE workspace_status text; row_data jsonb; images jsonb[];
            BEGIN
              IF TG_OP = 'INSERT' THEN images := ARRAY[to_jsonb(NEW)];
              ELSIF TG_OP = 'DELETE' THEN images := ARRAY[to_jsonb(OLD)];
              ELSE images := ARRAY[to_jsonb(OLD),to_jsonb(NEW)]; END IF;
              FOREACH row_data IN ARRAY images LOOP
                IF TG_TABLE_NAME = 'shifts' THEN
                  SELECT status INTO workspace_status FROM workspaces WHERE id=row_data->>'workspace_id';
                ELSE
                  SELECT w.status INTO workspace_status FROM workspaces w JOIN shifts s ON s.workspace_id=w.id WHERE s.id=row_data->>'shift_id';
                END IF;
                IF workspace_status IN ('published','superseded') THEN RAISE EXCEPTION 'published duties are immutable'; END IF;
              END LOOP;
              IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
            END $$""")
        op.execute("""CREATE FUNCTION rota_workspace_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF OLD.status IN ('published','superseded') AND NEW.status NOT IN ('published','superseded') THEN
                RAISE EXCEPTION 'published workspace cannot become a draft'; END IF;
              RETURN NEW;
            END $$""")
        op.execute("CREATE TRIGGER workspaces_published_state_guard BEFORE UPDATE ON workspaces FOR EACH ROW EXECUTE FUNCTION rota_workspace_state_guard()")
    else:
        raise RuntimeError("Unsupported database")


def downgrade():
    raise RuntimeError("Use a reviewed forward repair or restore; never erase evidence guards")
