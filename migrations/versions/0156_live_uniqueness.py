"""Enforce the live calendar and job admission invariants."""
from alembic import op

revision = '0156_live_uniqueness'
down_revision = '0155_installation_admin'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'
DUPLICATE_PRECHECKS = (
    """
    SELECT 1 FROM calendar_feeds WHERE revoked IS FALSE
    GROUP BY identity_id, department_id HAVING count(*) > 1
    """,
    """
    SELECT 1 FROM jobs
    WHERE kind = 'validation' AND status IN ('queued', 'running')
      AND workspace_id IS NOT NULL AND input_snapshot ->> 'validation_request_key' IS NOT NULL
    GROUP BY workspace_id, input_snapshot ->> 'validation_request_key' HAVING count(*) > 1
    """,
    """
    SELECT 1 FROM jobs WHERE kind = 'generation' AND status IN ('queued', 'running') AND workspace_id IS NOT NULL
    GROUP BY workspace_id HAVING count(*) > 1
    """,
)


def upgrade():
    op.execute("""
        CREATE UNIQUE INDEX uq_calendar_feeds_active
        ON calendar_feeds (identity_id, department_id) WHERE revoked IS FALSE
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_jobs_live_validation
        ON jobs (workspace_id, (input_snapshot ->> 'validation_request_key'))
        WHERE kind = 'validation' AND status IN ('queued', 'running')
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_jobs_live_generation
        ON jobs (workspace_id) WHERE kind = 'generation' AND status IN ('queued', 'running')
    """)


def downgrade():
    raise RuntimeError('Forward-only migration: retain uniqueness guarantees')
