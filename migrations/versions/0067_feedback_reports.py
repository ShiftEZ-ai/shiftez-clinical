"""Private in-app feedback reports and screenshots, without changing existing records or evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0067_feedback_reports"
down_revision = "0066_ux_audit_b1"
branch_labels = depends_on = None


def upgrade():
    op.create_table("feedback_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("reporter_person_id", sa.String(36), sa.ForeignKey("people.id"), nullable=True),
        sa.Column("reporter_identity_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("route", sa.String(2000), nullable=False),
        sa.Column("view_state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("page_title", sa.String(160), nullable=False, server_default=""),
        sa.Column("app_version", sa.String(80), nullable=False, server_default=""),
        sa.Column("build_commit", sa.String(80), nullable=False, server_default=""),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("role_names", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("viewport", sa.JSON(), nullable=False),
        sa.Column("theme", sa.String(16), nullable=False, server_default="system"),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
        sa.Column("recent_errors", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("request_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("mark", sa.JSON(), nullable=True),
        sa.Column("screenshot", sa.LargeBinary(), nullable=True),
        sa.Column("screenshot_media_type", sa.String(16), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("duplicate_of", sa.String(36), sa.ForeignKey("feedback_reports.id"), nullable=True),
        sa.Column("resolved_in", sa.String(80), nullable=True),
        sa.Column("triage_note", sa.Text(), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("github_issue_url", sa.String(2000), nullable=True),
        sa.CheckConstraint("kind IN ('problem','suggestion','confusing','question')"),
        sa.CheckConstraint("status IN ('new','triaged','in_progress','fixed','wont_fix','duplicate')"),
        sa.CheckConstraint("environment IN ('dev','production')"),
        sa.CheckConstraint("length(message) BETWEEN 1 AND 4000"),
        sa.CheckConstraint("expected IS NULL OR length(expected) <= 2000"),
        sa.CheckConstraint("length(user_agent) <= 300"),
        sa.CheckConstraint("occurrences >= 1"),
        sa.CheckConstraint("duplicate_of IS NULL OR duplicate_of <> id"),
        sa.CheckConstraint("screenshot IS NULL OR length(screenshot) <= 2097152"),
        sa.CheckConstraint("(screenshot IS NULL AND screenshot_media_type IS NULL) OR "
                           "(screenshot IS NOT NULL AND screenshot_media_type IS NOT NULL "
                           "AND screenshot_media_type IN ('image/png','image/webp'))"))
    op.create_index("ix_feedback_reports_department_status_created", "feedback_reports", ["department_id", "status", "created_at"])
    op.create_index("ix_feedback_reports_department_fingerprint_created", "feedback_reports", ["department_id", "fingerprint", "created_at"])
    op.create_index("ix_feedback_reports_department_reporter_created", "feedback_reports", ["department_id", "reporter_identity_id", "created_at"])
    op.create_index("ix_feedback_reports_person_created", "feedback_reports", ["reporter_person_id", "created_at"])
    op.create_index("ix_feedback_reports_identity_created", "feedback_reports", ["reporter_identity_id", "created_at"])
    op.create_index("ix_feedback_reports_created_at", "feedback_reports", ["created_at"])


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
