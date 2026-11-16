"""Add give-away swaps without rebuilding tables or rewriting existing consent evidence.

Existing swaps retain the exchange kind through the new column's default. Only the swap material guard changes:
give-away consent uses the same forward-only stages without requiring a duty in return.
Follows dev's 0083_swap_approval_settings (re-chained to keep a single head).
"""
from contextlib import nullcontext
from alembic import op
import sqlalchemy as sa

revision = "0084_swap_give"
down_revision = "0083_swap_approval_settings"
branch_labels = depends_on = None

DESTRUCTIVE_REVIEWED = (
    "Schema-only SQLite swap material guard replacement for the owner-requested give-away consent path; "
    "no rows or retained evidence are rewritten, and the delete guard remains intact. PostgreSQL replaces "
    "the function in place. Before deployment verify the backup; rollback uses a reviewed forward fix or "
    "restores that backup into an empty database with its compatible image."
)


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database dialect")
    # SQLite's legacy driver does not begin a transaction for DDL; a savepoint makes guard replacement atomic.
    with bind.begin_nested() if dialect == "sqlite" else nullcontext():
        op.add_column("departments", sa.Column("swap_kinds_allowed", sa.JSON(), nullable=False,
                                             server_default='["exchange", "give"]'))
        # Inline checks allow SQLite to add the column in place, retaining all existing tables and triggers.
        op.execute("""ALTER TABLE swap_requests ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'exchange'
            CHECK (kind IN ('exchange', 'give')) CHECK (kind != 'give' OR second_assignment_id IS NULL)""")

        def changed(*fields):
            comparison = "IS NOT" if dialect == "sqlite" else "IS DISTINCT FROM"
            return "(" + " OR ".join(f"OLD.{field} {comparison} NEW.{field}" for field in fields) + ")"

        swap_edges = " OR ".join([
            "(OLD.status='offered' AND NEW.status IN ('counteroffered','withdrawn','rejected'))",
            "(OLD.status='counteroffered' AND NEW.status IN ('accepted','withdrawn','rejected'))",
            "(OLD.status='accepted' AND NEW.status IN ('reviewing','withdrawn','rejected'))",
            "(OLD.status='reviewing' AND NEW.status IN ('published','withdrawn','rejected'))",
        ])
        condition = " OR ".join([
            changed("id", "department_id", "created_at", "publication_id", "proposer_id", "proposer_person_id",
                    "recipient_person_id", "first_assignment_id", "expires_at", "reason", "kind"),
            "NOT (" + swap_edges + ")",
            "(" + changed("recipient_id", "second_assignment_id", "counteroffered_at")
                + " AND NOT (OLD.status='offered' AND NEW.status='counteroffered'))",
            "(" + changed("confirmed_at") + " AND NOT (OLD.status='counteroffered' AND NEW.status='accepted'))",
            "(" + changed("workspace_id", "expected_content_hash") + " AND NOT (OLD.status='accepted' AND NEW.status='reviewing'))",
            "(" + changed("result_publication_id") + " AND NOT (OLD.status='reviewing' AND NEW.status='published'))",
            "(NEW.status IN ('counteroffered','accepted','reviewing','published') AND (NEW.recipient_id IS NULL "
                "OR (NEW.kind='exchange' AND NEW.second_assignment_id IS NULL) OR NEW.counteroffered_at IS NULL))",
            "(NEW.status IN ('accepted','reviewing','published') AND NEW.confirmed_at IS NULL)",
            "(NEW.status IN ('reviewing','published') AND (NEW.workspace_id IS NULL OR NEW.expected_content_hash IS NULL))",
            "(NEW.status='published' AND NEW.result_publication_id IS NULL)",
        ])
        if dialect == "sqlite":
            op.execute("DROP TRIGGER swap_requests_material_guard")
            op.execute(f"""CREATE TRIGGER swap_requests_material_guard BEFORE UPDATE ON swap_requests
                WHEN {condition} BEGIN SELECT RAISE(ABORT, 'immutable authority or consent material'); END""")
        else:
            op.execute(f"""CREATE OR REPLACE FUNCTION rota_swap_requests_material_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retained authority or consent evidence'; END IF;
                  IF {condition} THEN RAISE EXCEPTION 'immutable authority or consent material'; END IF;
                  RETURN NEW;
                END $$""")


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
