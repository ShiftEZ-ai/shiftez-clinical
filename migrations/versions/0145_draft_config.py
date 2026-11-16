"""Add draft configuration evidence and governed department settings.

Forward-only: retain all existing baselines and custom scopes without backfill.
Option values are frozen here; current ORM choices come from contracts.
"""
from alembic import op
import sqlalchemy as sa


revision = "0145_draft_config"
down_revision = "0144_tag_member_dates"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column("workspace_baselines", sa.Column("followed", sa.JSON(), nullable=True))
    op.add_column("workspace_baselines", sa.Column("safety_receipts", sa.JSON(), nullable=True))
    op.create_table(
        "workspace_config_keeps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("item_key", sa.String(160), nullable=False),
        sa.Column("department_item_hash", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.CheckConstraint("revision >= 1"),
    )
    op.create_index("ix_workspace_config_keeps_department_id", "workspace_config_keeps", ["department_id"])
    op.create_index("ix_workspace_config_keeps_workspace_id", "workspace_config_keeps", ["workspace_id"])
    op.create_index("ix_workspace_config_keeps_item_created", "workspace_config_keeps", ["workspace_id", "item_key", "created_at"])
    op.create_table(
        "department_draft_settings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("safety_change", sa.String(32), nullable=False, server_default="stricter_only"),
        sa.Column("safety_use_default", sa.String(32), nullable=False, server_default="allowed"),
        sa.Column("editors", sa.String(32), nullable=False, server_default="department_roles"),
        sa.Column("non_safety_approval", sa.String(32), nullable=False, server_default="free"),
        sa.Column("rules_source", sa.String(32), nullable=False, server_default="own_snapshot"),
        sa.Column("duties_source", sa.String(32), nullable=False, server_default="own_snapshot"),
        sa.Column("score_source", sa.String(32), nullable=False, server_default="own_snapshot"),
        sa.Column("auto_source", sa.String(32), nullable=False, server_default="own_snapshot"),
        sa.Column("display_source", sa.String(32), nullable=False, server_default="own_snapshot"),
        sa.Column("default_change", sa.String(32), nullable=False, server_default="notify"),
        sa.Column("reason_required", sa.String(32), nullable=False, server_default="safety_only"),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("proposed_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("previous_id", sa.String(36), sa.ForeignKey("department_draft_settings.id"), nullable=True),
        sa.UniqueConstraint("department_id", "revision"),
        sa.CheckConstraint("revision >= 1"),
        sa.CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"),
        sa.CheckConstraint("safety_change IN ('stricter_only','approved_loosening','any_with_reason')", name="ck_draft_safety_change"),
        sa.CheckConstraint("safety_use_default IN ('allowed','needs_approval')", name="ck_draft_safety_use_default"),
        sa.CheckConstraint("editors IN ('department_roles','draft_editors','administrators')", name="ck_draft_config_editors"),
        sa.CheckConstraint("non_safety_approval IN ('free','needs_approval')", name="ck_draft_config_approval"),
        sa.CheckConstraint("rules_source IN ('own_snapshot','follow_department')", name="ck_draft_rules_source"),
        sa.CheckConstraint("duties_source IN ('own_snapshot','follow_department')", name="ck_draft_duties_source"),
        sa.CheckConstraint("score_source IN ('own_snapshot','follow_department')", name="ck_draft_score_source"),
        sa.CheckConstraint("auto_source IN ('own_snapshot','follow_department')", name="ck_draft_auto_source"),
        sa.CheckConstraint("display_source IN ('own_snapshot','follow_department')", name="ck_draft_display_source"),
        sa.CheckConstraint("default_change IN ('notify','use_unchanged')", name="ck_draft_default_change"),
        sa.CheckConstraint("reason_required IN ('safety_only','all','optional')", name="ck_draft_config_reason"),
        sa.CheckConstraint("status IN ('proposed','active','declined','retired')", name="ck_draft_settings_status"),
        sa.CheckConstraint("status <> 'active' OR approved_by IS NOT NULL", name="ck_draft_settings_active_approval"),
    )
    op.create_index("ix_department_draft_settings_department_id", "department_draft_settings", ["department_id"])
    op.create_index("ix_department_draft_settings_status", "department_draft_settings", ["department_id", "status", "revision"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER workspace_config_keeps_no_update BEFORE UPDATE ON workspace_config_keeps "
                   "BEGIN SELECT RAISE(ABORT, 'immutable draft configuration'); END")
        op.execute("CREATE TRIGGER workspace_config_keeps_no_delete BEFORE DELETE ON workspace_config_keeps "
                   "BEGIN SELECT RAISE(ABORT, 'immutable draft configuration'); END")
        op.execute("CREATE TRIGGER department_draft_settings_no_update BEFORE UPDATE ON department_draft_settings "
                   "BEGIN SELECT RAISE(ABORT, 'immutable draft configuration'); END")
        op.execute("CREATE TRIGGER department_draft_settings_no_delete BEFORE DELETE ON department_draft_settings "
                   "BEGIN SELECT RAISE(ABORT, 'immutable draft configuration'); END")
        op.execute("""CREATE TRIGGER workspace_config_keeps_insert BEFORE INSERT ON workspace_config_keeps
            WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1
              AND NOT EXISTS (SELECT 1 FROM workspace_config_keeps existing WHERE existing.id = NEW.id))
            BEGIN SELECT RAISE(ABORT, 'invalid draft configuration keep'); END""")
        op.execute("""CREATE TRIGGER department_draft_settings_insert BEFORE INSERT ON department_draft_settings
            WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1
              AND NOT EXISTS (SELECT 1 FROM department_draft_settings existing WHERE existing.id = NEW.id
                OR (existing.department_id = NEW.department_id AND existing.revision = NEW.revision)))
            BEGIN SELECT RAISE(ABORT, 'invalid draft configuration settings'); END""")
    else:
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        if "$" in schema:
            raise RuntimeError("Schema dollar delimiters are unsupported")
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.workspace_config_keeps_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable draft configuration'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1
                AND NOT EXISTS (SELECT 1 FROM {q}.workspace_config_keeps existing WHERE existing.id = NEW.id))
              THEN RAISE EXCEPTION 'invalid draft configuration keep'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"""CREATE FUNCTION {q}.department_draft_settings_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable draft configuration'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1
                AND NOT EXISTS (SELECT 1 FROM {q}.department_draft_settings existing WHERE existing.id = NEW.id
                  OR (existing.department_id = NEW.department_id AND existing.revision = NEW.revision)))
              THEN RAISE EXCEPTION 'invalid draft configuration settings'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER workspace_config_keeps_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.workspace_config_keeps "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.workspace_config_keeps_guard()")
        op.execute(f"CREATE TRIGGER department_draft_settings_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.department_draft_settings "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.department_draft_settings_guard()")


def downgrade():
    raise RuntimeError("Forward-only: retain draft configuration and governance evidence")
