"""Allow open swap offers without rebuilding tables or rewriting consent evidence."""
from contextlib import nullcontext
import re
from alembic import op
import sqlalchemy as sa

revision = "0086_swap_open"
down_revision = "0084_swap_give"
branch_labels = depends_on = None

DESTRUCTIVE_REVIEWED = (
    "Schema-only SQLite NOT NULL removal and swap material guard replacement for owner-requested open offers; "
    "no operational rows or retained evidence are rewritten, and every table, index, foreign key and delete guard "
    "is retained. PostgreSQL alters nullability and replaces the function in place. Verify the deployment backup; "
    "rollback uses a reviewed forward fix or restores that backup into an empty database with its compatible image."
)


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database dialect")
    # SQLite's legacy driver does not begin a transaction for DDL; include every schema change in one savepoint.
    with bind.begin_nested() if dialect == "sqlite" else nullcontext():
        op.add_column("departments", sa.Column("swap_open_offers", sa.Boolean(), nullable=False,
                                             server_default=sa.true()))
        op.create_table("swap_offer_audience",
            sa.Column("swap_id", sa.String(36), sa.ForeignKey("swap_requests.id"), primary_key=True),
            sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), primary_key=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_swap_offer_audience_person_swap", "swap_offer_audience", ["person_id", "swap_id"])
        op.execute("""ALTER TABLE swap_requests ADD COLUMN open_offer BOOLEAN NOT NULL DEFAULT false
            CHECK (recipient_person_id IS NOT NULL OR
                (open_offer AND status IN ('offered','withdrawn','rejected')))""")
        if dialect == "sqlite":
            # Official schema-only NOT NULL removal: https://www.sqlite.org/lang_altertable.html#otheralter
            original = bind.execute(sa.text("SELECT sql FROM sqlite_schema WHERE type='table' AND name='swap_requests'")).scalar_one()
            replacement, count = re.subn(r"\brecipient_person_id\s+VARCHAR\(36\)\s+NOT NULL\b",
                                         "recipient_person_id VARCHAR(36)", original)
            if count != 1:
                raise RuntimeError("Unexpected swap recipient schema; no nullability change applied")
            version = bind.exec_driver_sql("PRAGMA schema_version").scalar_one()
            bind.exec_driver_sql("PRAGMA writable_schema=ON")
            try:
                bind.execute(sa.text("UPDATE sqlite_schema SET sql=:sql WHERE type='table' AND name='swap_requests'"),
                             {"sql": replacement})
                bind.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
            finally:
                bind.exec_driver_sql("PRAGMA writable_schema=OFF")
            if bind.exec_driver_sql("PRAGMA integrity_check(swap_requests)").scalars().all() != ["ok"]:
                raise RuntimeError("Swap schema integrity check failed")
        else:
            op.alter_column("swap_requests", "recipient_person_id", existing_type=sa.String(36), nullable=True)

        def changed(*fields):
            comparison = "IS NOT" if dialect == "sqlite" else "IS DISTINCT FROM"
            return "(" + " OR ".join(f"OLD.{field} {comparison} NEW.{field}" for field in fields) + ")"

        swap_edges = " OR ".join([
            "(OLD.status='offered' AND NEW.status IN ('counteroffered','withdrawn','rejected'))",
            "(OLD.status='counteroffered' AND NEW.status IN ('accepted','withdrawn','rejected'))",
            "(OLD.status='accepted' AND NEW.status IN ('reviewing','withdrawn','rejected'))",
            "(OLD.status='reviewing' AND NEW.status IN ('published','withdrawn','rejected'))",
        ])
        claim = "(OLD.open_offer AND OLD.status='offered' AND NEW.status='counteroffered' " \
                "AND OLD.recipient_person_id IS NULL AND NEW.recipient_person_id IS NOT NULL)"
        condition = " OR ".join([
            changed("id", "department_id", "created_at", "publication_id", "proposer_id", "proposer_person_id",
                    "first_assignment_id", "expires_at", "reason", "kind", "open_offer"),
            "(" + changed("recipient_person_id") + " AND NOT " + claim + ")",
            "NOT (" + swap_edges + ")",
            "(" + changed("recipient_id", "second_assignment_id", "counteroffered_at")
                + " AND NOT (OLD.status='offered' AND NEW.status='counteroffered'))",
            "(" + changed("confirmed_at") + " AND NOT (OLD.status='counteroffered' AND NEW.status='accepted'))",
            "(" + changed("workspace_id", "expected_content_hash") + " AND NOT (OLD.status='accepted' AND NEW.status='reviewing'))",
            "(" + changed("result_publication_id") + " AND NOT (OLD.status='reviewing' AND NEW.status='published'))",
            "(NEW.status IN ('counteroffered','accepted','reviewing','published') AND (NEW.recipient_person_id IS NULL "
                "OR NEW.recipient_id IS NULL OR (NEW.kind='exchange' AND NEW.second_assignment_id IS NULL) "
                "OR NEW.counteroffered_at IS NULL))",
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
