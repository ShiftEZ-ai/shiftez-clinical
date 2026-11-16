"""Add reviewed rota imports, benchmark evidence and opt-in AI profiles without changing retained schedules."""
from alembic import op
import sqlalchemy as sa

revision = "0120_score_check"
down_revision = "0120_table_view_defaults"
branch_labels = depends_on = None
SAFETY_CLASS = "additive"


def upgrade():
    op.add_column("workspaces", sa.Column("origin", sa.String(16),
        sa.CheckConstraint("origin IN ('planner','auto','import')", name="ck_workspaces_origin"),
        nullable=False, server_default="planner"))
    op.create_table("ai_provider_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("reasoning", sa.JSON(), nullable=False, server_default='"none"'),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="4096"),
        sa.Column("timeout_s", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("structured_output", sa.String(16), nullable=False, server_default="json_schema"),
        sa.Column("allow_private_endpoint", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("key_ciphertext", sa.Text()),
        sa.Column("key_saved_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('openai_chat','openai_responses','anthropic','azure')"),
        sa.CheckConstraint("structured_output IN ('json_schema','json_object')"),
        sa.CheckConstraint("max_tokens BETWEEN 1 AND 1000000"),
        sa.CheckConstraint("timeout_s BETWEEN 1 AND 600"))
    op.create_index("ix_ai_provider_profiles_department_id", "ai_provider_profiles", ["department_id"])
    op.execute("CREATE UNIQUE INDEX uq_ai_provider_default ON ai_provider_profiles (department_id) "
               "WHERE is_default = true AND archived_at IS NULL")
    op.create_table("score_check_import_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False, server_default=""),
        sa.Column("starts_on", sa.Date()),
        sa.Column("ends_on", sa.Date()),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id")),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("confidence_threshold", sa.Float(), nullable=False, server_default="0.9"),
        sa.Column("manual_time_hours", sa.Float()),
        sa.Column("manual_time_people", sa.Integer()),
        sa.Column("gaps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("use_ai", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("ai_provider_profiles.id")),
        sa.Column("profile_revision", sa.Integer()),
        sa.Column("provider_snapshot", sa.JSON()),
        sa.Column("privacy_acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("privacy_acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("privacy_acknowledged_by", sa.String(36), sa.ForeignKey("identities.id")),
        sa.CheckConstraint("status IN ('uploaded','reading','check','saved','failed','discarded')"),
        sa.CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"),
        sa.CheckConstraint("confidence_threshold BETWEEN 0 AND 1"),
        sa.CheckConstraint("manual_time_hours IS NULL OR manual_time_hours >= 0"),
        sa.CheckConstraint("manual_time_people IS NULL OR manual_time_people >= 1"),
        sa.CheckConstraint("NOT use_ai OR privacy_acknowledged"))
    op.create_index("ix_score_check_import_batches_department_id", "score_check_import_batches", ["department_id"])
    op.create_index("ix_score_check_import_batches_workspace_id", "score_check_import_batches", ["workspace_id"])
    op.create_index("ix_score_check_batches_department_created", "score_check_import_batches", ["department_id", "created_at"])
    op.create_table("import_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("score_check_import_batches.id"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id")),
        sa.Column("file_name", sa.String(300), nullable=False),
        sa.Column("file_ref", sa.String(300), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("group", sa.String(160), nullable=False, server_default=""),
        sa.Column("starts_on", sa.Date()),
        sa.Column("ends_on", sa.Date()),
        sa.Column("work_hours_each", sa.Float()),
        sa.Column("made_by", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column("content", sa.LargeBinary()),
        sa.Column("extraction", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("extraction_job_id", sa.String(36)),
        sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("batch_id", "file_ref"),
        sa.CheckConstraint("status IN ('uploaded','reading','ready','needs_review','failed')"),
        sa.CheckConstraint("work_hours_each IS NULL OR work_hours_each >= 0"),
        sa.CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"))
    op.create_index("ix_import_sources_department_id", "import_sources", ["department_id"])
    op.create_index("ix_import_sources_batch_id", "import_sources", ["batch_id"])
    op.create_index("ix_import_sources_workspace_id", "import_sources", ["workspace_id"])
    op.create_table("score_check_gap_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("score_check_import_batches.id"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("import_sources.id")),
        sa.Column("gap_id", sa.String(300), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(36)),
        sa.Column("location_id", sa.String(36), sa.ForeignKey("locations.id")),
        sa.Column("replacement", sa.JSON()),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
        sa.Column("undone_by", sa.String(36), sa.ForeignKey("identities.id")),
        sa.CheckConstraint("action IN ('acknowledge','add','match','exclude','confirm','resolve')"))
    op.create_index("ix_score_check_gap_decisions_department_id", "score_check_gap_decisions", ["department_id"])
    op.create_index("ix_score_check_gap_decisions_batch_id", "score_check_gap_decisions", ["batch_id"])
    op.create_index("ix_score_check_decisions_batch_gap", "score_check_gap_decisions", ["batch_id", "gap_id"])
    op.create_table("benchmark_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("baseline_publication_id", sa.String(36), sa.ForeignKey("publications.id")),
        sa.Column("workspace_ids", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.CheckConstraint("ends_on > starts_on"))
    op.create_index("ix_benchmark_results_department_id", "benchmark_results", ["department_id"])
    op.create_index("ix_benchmark_results_period", "benchmark_results", ["department_id", "starts_on", "ends_on", "created_at"])
    op.create_table("score_check_extraction_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("import_sources.id")),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("ai_provider_profiles.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.CheckConstraint("kind IN ('extraction','test')"),
        sa.CheckConstraint("state IN ('queued','running','completed','failed','cancelled')"))
    op.create_index("ix_score_check_extraction_jobs_department_id", "score_check_extraction_jobs", ["department_id"])
    op.create_index("ix_score_check_jobs_queue", "score_check_extraction_jobs", ["state", "created_at"])
    op.create_index("ix_score_check_jobs_lease", "score_check_extraction_jobs", ["state", "lease_expires_at"])
    op.create_table("ai_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("ai_provider_profiles.id")),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("identities.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("score_check_extraction_jobs.id")),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("file_sha256", sa.String(64)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("job_id", "attempt"),
        sa.CheckConstraint("attempt >= 1"),
        sa.CheckConstraint("result IN ('pending','valid','invalid','error')"),
        sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0"),
        sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0"))
    op.create_index("ix_ai_calls_department_id", "ai_calls", ["department_id"])
    op.create_index("ix_ai_calls_job_id", "ai_calls", ["job_id"])
    op.create_index("ix_ai_calls_department_created", "ai_calls", ["department_id", "created_at"])


def downgrade():
    raise RuntimeError("Forward-only: retain import decisions, benchmark evidence and AI audit")
