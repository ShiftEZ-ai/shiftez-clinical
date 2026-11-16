"""Permit only removal of accidentally retained private leave text from definition history."""
from contextlib import nullcontext

from alembic import op
import sqlalchemy as sa

revision = "0098_bulk_privacy"
down_revision = "0093_bulk_changes"
branch_labels = depends_on = None

DESTRUCTIVE_REVIEWED = (
    "Owner-authorized privacy repair: replace only the definition-history update guard to permit removal of "
    "the private_reason key from availability snapshots, preserving every other value and all delete guards. "
    "Remove existing accidental copies once as owner and add a person-scoped definer function; runtime retains "
    "no history UPDATE grant. Verify the deployment backup; rollback uses a reviewed "
    "forward fix or restores the backup into an empty database with a compatible image."
)

KINDS = "('availability','availability.update','availability.undo_update','availability.restore')"


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database dialect")
    with bind.begin_nested() if dialect == "sqlite" else nullcontext():
        if dialect == "sqlite":
            unchanged = " AND ".join(f'OLD."{column["name"]}" IS NEW."{column["name"]}"'
                for column in sa.inspect(bind).get_columns("definition_history") if column["name"] != "snapshot")
            permitted = (f"OLD.kind IN {KINDS} AND {unchanged} AND json_type(OLD.snapshot) = 'object' "
                "AND json_type(OLD.snapshot,'$.private_reason') IS NOT NULL "
                "AND json_type(NEW.snapshot,'$.private_reason') IS NULL "
                "AND json(NEW.snapshot) = json_remove(OLD.snapshot,'$.private_reason')")
            op.execute("DROP TRIGGER definition_history_update_guard")
            op.execute(f"""CREATE TRIGGER definition_history_update_guard BEFORE UPDATE ON definition_history
                WHEN NOT ({permitted}) BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END""")
            op.execute(f"""UPDATE definition_history SET snapshot = json_remove(snapshot,'$.private_reason')
                WHERE kind IN {KINDS} AND json_type(snapshot) = 'object'
                  AND json_type(snapshot,'$.private_reason') IS NOT NULL""")
        else:
            op.execute(f"""CREATE FUNCTION rota_history_privacy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'immutable evidence'; END IF;
                  IF OLD.kind NOT IN {KINDS} OR jsonb_typeof(OLD.snapshot::jsonb) <> 'object'
                    OR NOT (OLD.snapshot::jsonb ? 'private_reason')
                    OR (to_jsonb(OLD) - 'snapshot') IS DISTINCT FROM (to_jsonb(NEW) - 'snapshot')
                    OR NEW.snapshot::jsonb IS DISTINCT FROM (OLD.snapshot::jsonb - 'private_reason')
                  THEN RAISE EXCEPTION 'immutable evidence'; END IF;
                  RETURN NEW;
                END $$""")
            op.execute("DROP TRIGGER definition_history_guard ON definition_history")
            op.execute("""CREATE TRIGGER definition_history_guard BEFORE UPDATE OR DELETE ON definition_history
                FOR EACH ROW EXECUTE FUNCTION rota_history_privacy_guard()""")
            schema = bind.dialect.identifier_preparer.quote_schema(bind.scalar(sa.text("SELECT current_schema()")))
            # Any inactive person is intentional: callers check admin/org scope; SQL only strips private_reason from their guarded availability history.
            op.execute(f"""CREATE FUNCTION {schema}.rota_redact_person_history(person_id text)
                RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
                SET search_path = pg_catalog, pg_temp AS $privacy$
                DECLARE changed integer;
                BEGIN
                  UPDATE {schema}.definition_history AS h SET snapshot = (h.snapshot::jsonb - 'private_reason')::json
                  WHERE h.kind IN {KINDS} AND jsonb_typeof(h.snapshot::jsonb) = 'object'
                    AND h.snapshot::jsonb ? 'private_reason'
                    AND EXISTS (SELECT 1 FROM {schema}.availability AS a
                      JOIN {schema}.people AS p ON p.id = a.person_id
                      WHERE a.id = h.object_id AND a.department_id = h.department_id
                        AND a.person_id = $1 AND p.active IS FALSE);
                  GET DIAGNOSTICS changed = ROW_COUNT;
                  RETURN changed;
                END $privacy$""")
            op.execute(f"REVOKE ALL ON FUNCTION {schema}.rota_redact_person_history(text) FROM PUBLIC")
            # The grants template also handles a runtime role provisioned after migration (including tests).
            if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rota_app')")):
                op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.rota_redact_person_history(text) TO rota_app")
            op.execute(f"""UPDATE definition_history SET snapshot = (snapshot::jsonb - 'private_reason')::json
                WHERE kind IN {KINDS} AND jsonb_typeof(snapshot::jsonb) = 'object'
                  AND snapshot::jsonb ? 'private_reason'""")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
