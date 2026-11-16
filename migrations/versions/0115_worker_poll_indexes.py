"""Index worker scheduling and sender history without rewriting retained evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0115_worker_poll_indexes"
down_revision = "0114_request_decision_times"
branch_labels = depends_on = None


def upgrade():
    # Status/state remain index keys: parameterized PostgreSQL plans cannot always use partial status predicates.
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])
    op.create_index("ix_jobs_status_lease", "jobs", ["status", sa.text("coalesce(heartbeat_at, started_at)")])
    op.create_index("ix_outbox_state_attempt", "outbox", ["state", "next_attempt_at"])
    op.create_index("ix_email_deliveries_sender_sent", "email_deliveries", ["department_id", "sender_mode", "sent_at"],
                    sqlite_where=sa.text("sent_at IS NOT NULL"), postgresql_where=sa.text("sent_at IS NOT NULL"))
    op.create_index("ix_email_deliveries_identity_sent", "email_deliveries",
                    ["department_id", "sender_mode", "sender_identity_id", "sent_at"],
                    sqlite_where=sa.text("sent_at IS NOT NULL"), postgresql_where=sa.text("sent_at IS NOT NULL"))


def downgrade():
    raise RuntimeError("Forward-only: retain worker scheduling indexes")
