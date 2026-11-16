"""Capture immutable department-input baselines for draft workspaces.

Rollback note: this is additive and forward-only. A rollback deploy keeps the
table and baseline rows in place; the previous application ignores them. Do
not downgrade or delete the retained evidence.
"""
from alembic import op
import sqlalchemy as sa


revision = "0142_workspace_baselines"
down_revision = "0141_workspace_archive_lifecycle"
branch_labels = None
depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.create_table(
        "workspace_baselines",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("workspace_revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope_mode", sa.String(16), nullable=False, server_default="department"),
        sa.Column("requested_sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("included_sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("omitted_sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("dependency_closure", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_revisions", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("canonical_payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.UniqueConstraint("workspace_id", "workspace_revision"),
        sa.CheckConstraint("workspace_revision >= 1"),
        sa.CheckConstraint("schema_version >= 1"),
        sa.CheckConstraint("scope_mode IN ('department','custom')"),
    )
    op.create_index("ix_workspace_baselines_workspace", "workspace_baselines", ["workspace_id"])
    op.create_index("ix_workspace_baselines_workspace_created", "workspace_baselines", ["workspace_id", "created_at"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER workspace_baselines_no_update BEFORE UPDATE ON workspace_baselines "
                   "BEGIN SELECT RAISE(ABORT, 'immutable workspace baseline'); END")
        op.execute("CREATE TRIGGER workspace_baselines_no_delete BEFORE DELETE ON workspace_baselines "
                   "BEGIN SELECT RAISE(ABORT, 'immutable workspace baseline'); END")
        op.execute("CREATE TRIGGER workspace_baselines_guard BEFORE INSERT ON workspace_baselines "
                   "WHEN NOT EXISTS (SELECT 1 WHERE NEW.workspace_revision >= 1) "
                   "BEGIN SELECT RAISE(ABORT, 'invalid workspace baseline'); END")
    else:
        schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()
        if "$" in schema:
            raise RuntimeError("Schema dollar delimiters are unsupported")
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.workspace_baselines_immutable_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable workspace baseline'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.workspace_revision >= 1)
              THEN RAISE EXCEPTION 'invalid workspace baseline'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER workspace_baselines_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.workspace_baselines "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.workspace_baselines_immutable_guard()")


def downgrade():
    raise RuntimeError("Forward-only: retain immutable workspace baseline evidence")
