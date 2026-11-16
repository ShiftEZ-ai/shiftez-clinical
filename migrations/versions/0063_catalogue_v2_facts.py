"""Storage only for independently approved catalogue conversions and scheduling facts.

Expand-only: no conversion or existing definition rewrite runs in this migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0063_catalogue_v2_facts"
down_revision = "0062_session_devices"
branch_labels = depends_on = None


def record_columns():
    return [sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True)]


def upgrade():
    op.create_table("catalogue_conversions", *record_columns(),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposed_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("identities.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("proof", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('proposed','active','declined','retired')"))
    op.create_index("uq_catalogue_conversion_pending", "catalogue_conversions", ["department_id"], unique=True,
                    sqlite_where=sa.text("status = 'proposed'"), postgresql_where=sa.text("status = 'proposed'"))
    op.create_table("scheduling_facts", *record_columns(),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("person_id", sa.String(36), sa.ForeignKey("people.id"), index=True),
        sa.Column("calendar_entry_id", sa.String(36), sa.ForeignKey("programme_calendar_entries.id"), index=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("on", sa.Boolean(), nullable=False),
        sa.Column("replaces_id", sa.String(36), sa.ForeignKey("scheduling_facts.id"), index=True),
        sa.Column("replaces_revision", sa.Integer()),
        sa.Column("starts_on", sa.Date()), sa.Column("ends_on", sa.Date()),
        sa.Column("proposed_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("identities.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("migration_proposal_id", sa.String(36), sa.ForeignKey("catalogue_conversions.id"), index=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.CheckConstraint("kind IN ('workload','target','overtime_offer','protected_time')"),
        sa.CheckConstraint("status IN ('proposed','active','retired','declined')"),
        sa.CheckConstraint("kind = 'protected_time' OR person_id IS NOT NULL"),
        sa.CheckConstraint("kind <> 'protected_time' OR person_id IS NOT NULL OR calendar_entry_id IS NOT NULL"),
        sa.CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"))
    op.create_index("ix_scheduling_facts_department_status", "scheduling_facts", ["department_id", "status"])
    op.create_table("rule_source_bindings", *record_columns(),
        sa.Column("migration_proposal_id", sa.String(36), sa.ForeignKey("catalogue_conversions.id"), index=True),
        sa.Column("source_policy_id", sa.String(36), sa.ForeignKey("policies.id"), nullable=False, index=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint("migration_proposal_id", "source_policy_id", "source_revision"))


def downgrade():
    raise RuntimeError("Forward-only: retain catalogue versions and their approval evidence")
