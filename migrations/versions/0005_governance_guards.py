"""Guard effective authority and consent material against in-place rewriting.

Revision ID: 0005_governance_guards
Revises: 0004_governance

Both supported dialects use null-safe material comparisons. These are defence in
 depth, not a replacement for authenticated domain commands or owner separation.
"""
from alembic import op

revision = "0005_governance_guards"
down_revision = "0004_governance"
branch_labels = None
depends_on = None


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Unsupported database dialect")

    def changed(*fields):
        def compare(field):
            if dialect == "sqlite":
                return f"OLD.{field} IS NOT NEW.{field}"
            suffix = "::text" if field == "definition" else ""
            return f"OLD.{field}{suffix} IS DISTINCT FROM NEW.{field}{suffix}"
        return "(" + " OR ".join(compare(field) for field in fields) + ")"

    def guard(table, condition):
        if dialect == "sqlite":
            op.execute(f"""CREATE TRIGGER {table}_material_guard BEFORE UPDATE ON {table}
                WHEN {condition} BEGIN SELECT RAISE(ABORT, 'immutable authority or consent material'); END""")
            op.execute(f"""CREATE TRIGGER {table}_delete_guard BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'retained authority or consent evidence'); END""")
        else:
            function = f"rota_{table}_material_guard"
            op.execute(f"""CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retained authority or consent evidence'; END IF;
                  IF {condition} THEN RAISE EXCEPTION 'immutable authority or consent material'; END IF;
                  RETURN NEW;
                END $$""")
            op.execute(f"""CREATE TRIGGER {table}_material_guard BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION {function}()""")

    guard("review_routes", " OR ".join([
        changed("id", "department_id", "created_at"),
        "OLD.status = 'retired'",
        "NOT ((OLD.status='proposed' AND NEW.status IN ('proposed','active')) OR (OLD.status='active' AND NEW.status='retired'))",
        "(OLD.status='active' AND " + changed("name", "starts_on", "ends_on", "definition", "created_by", "activated_by") + ")",
        "(NEW.status='active' AND NEW.activated_by IS NULL)",
        "(NEW.status='proposed' AND NEW.activated_by IS NOT NULL)",
    ]))
    guard("review_delegations", " OR ".join([
        changed("id", "department_id", "created_at", "route_id", "route_revision", "step_key",
                "delegator_id", "delegate_id", "starts_at", "expires_at", "reason"),
        "OLD.revoked AND NOT NEW.revoked",
    ]))
    swap_edges = " OR ".join([
        "(OLD.status='offered' AND NEW.status IN ('counteroffered','withdrawn','rejected'))",
        "(OLD.status='counteroffered' AND NEW.status IN ('accepted','withdrawn','rejected'))",
        "(OLD.status='accepted' AND NEW.status IN ('reviewing','withdrawn','rejected'))",
        "(OLD.status='reviewing' AND NEW.status IN ('published','withdrawn','rejected'))",
    ])
    guard("swap_requests", " OR ".join([
        changed("id", "department_id", "created_at", "publication_id", "proposer_id", "proposer_person_id",
                "recipient_person_id", "first_assignment_id", "expires_at", "reason"),
        "NOT (" + swap_edges + ")",
        "(" + changed("recipient_id", "second_assignment_id", "counteroffered_at")
            + " AND NOT (OLD.status='offered' AND NEW.status='counteroffered'))",
        "(" + changed("confirmed_at") + " AND NOT (OLD.status='counteroffered' AND NEW.status='accepted'))",
        "(" + changed("workspace_id", "expected_content_hash") + " AND NOT (OLD.status='accepted' AND NEW.status='reviewing'))",
        "(" + changed("result_publication_id") + " AND NOT (OLD.status='reviewing' AND NEW.status='published'))",
        "(NEW.status IN ('counteroffered','accepted','reviewing','published') AND (NEW.recipient_id IS NULL OR NEW.second_assignment_id IS NULL OR NEW.counteroffered_at IS NULL))",
        "(NEW.status IN ('accepted','reviewing','published') AND NEW.confirmed_at IS NULL)",
        "(NEW.status IN ('reviewing','published') AND (NEW.workspace_id IS NULL OR NEW.expected_content_hash IS NULL))",
        "(NEW.status='published' AND NEW.result_publication_id IS NULL)",
    ]))


def downgrade():
    raise RuntimeError("Authority and consent guards are retained; use a reviewed forward repair or restore")
