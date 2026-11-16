"""Effective review routes and two-sided staff swap evidence.

Revision ID: 0004_governance
Revises: 0003_integrity
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_governance"
down_revision = "0003_integrity"
branch_labels = None
depends_on = None


def common(department=False):
    columns = [sa.Column("id", sa.String(36), primary_key=True),
               sa.Column("revision", sa.Integer(), nullable=False),
               sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]
    if department:
        columns.append(sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False))
    return columns


def fk(name, target, nullable=False):
    return sa.Column(name, sa.String(36), sa.ForeignKey(target + ".id"), nullable=nullable)


def upgrade():
    op.add_column("workspaces", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workspaces", sa.Column("review_snapshot", sa.JSON(), nullable=True))
    op.add_column("workspaces", sa.Column("urgency", sa.String(16), nullable=False, server_default="routine"))
    op.add_column("workspaces", sa.Column("urgency_reason", sa.String(500), nullable=False, server_default=""))
    op.create_table("review_routes", *common(True),
        sa.Column("name", sa.String(160), nullable=False), sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True), sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), fk("created_by", "identities"),
        fk("activated_by", "identities", True),
        sa.CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),
        sa.CheckConstraint("status IN ('proposed','active','retired')"))
    op.create_table("review_delegations", *common(True), fk("route_id", "review_routes"),
        sa.Column("route_revision", sa.Integer(), nullable=False), sa.Column("step_key", sa.String(40), nullable=False),
        fk("delegator_id", "identities"), fk("delegate_id", "identities"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False), sa.Column("reason", sa.String(500), nullable=False),
        sa.CheckConstraint("expires_at > starts_at"), sa.CheckConstraint("delegate_id != delegator_id"))
    op.create_table("review_decisions", *common(), fk("workspace_id", "workspaces"),
        sa.Column("content_revision", sa.Integer(), nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("route_hash", sa.String(64), nullable=False), sa.Column("step_key", sa.String(40), nullable=False),
        fk("approved_by", "identities"), fk("authority_id", "identities"),
        sa.Column("authority_role", sa.String(32), nullable=False), fk("delegation_id", "review_delegations", True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.UniqueConstraint("workspace_id", "content_revision", "approved_by"),
        sa.UniqueConstraint("workspace_id", "content_revision", "authority_id"),
        sa.CheckConstraint("authority_role IN ('approver','programme_lead')"))
    op.create_table("swap_requests", *common(True), fk("publication_id", "publications"),
        fk("proposer_id", "identities"), fk("proposer_person_id", "people"), fk("recipient_person_id", "people"),
        fk("recipient_id", "identities", True), fk("first_assignment_id", "assignments"),
        fk("second_assignment_id", "assignments", True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False), sa.Column("counteroffered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True), fk("workspace_id", "workspaces", True),
        sa.Column("expected_content_hash", sa.String(64), nullable=True), fk("result_publication_id", "publications", True),
        sa.UniqueConstraint("workspace_id"), sa.CheckConstraint("proposer_person_id != recipient_person_id"),
        sa.CheckConstraint("status IN ('offered','counteroffered','accepted','reviewing','published','withdrawn','rejected')"))
    op.create_table("swap_events", *common(), fk("swap_id", "swap_requests"), fk("actor_id", "identities"),
        sa.Column("action", sa.String(24), nullable=False), sa.Column("pair_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False))
    for table, column in (("review_routes", "department_id"), ("review_delegations", "department_id"),
                          ("review_decisions", "workspace_id"), ("swap_requests", "department_id"),
                          ("swap_requests", "publication_id"), ("swap_events", "swap_id")):
        op.create_index(f"ix_{table}_{column}", table, [column])
    dialect = op.get_bind().dialect.name
    for table in ("review_decisions", "swap_events"):
        if dialect == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_{action.lower()}_guard BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable evidence'); END")
        elif dialect == "postgresql":
            op.execute(f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION rota_immutable_evidence()")
        else:
            raise RuntimeError("Unsupported database dialect")


def downgrade():
    raise RuntimeError("Review and consent evidence are retained; use a reviewed forward repair or restore")
