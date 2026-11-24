"""Enforce the live calendar, person-code and job admission invariants."""
from alembic import op

revision = '0153_live_uniqueness'
down_revision = '0152_feasibility_results'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.execute("""
        CREATE UNIQUE INDEX uq_calendar_feeds_active
        ON calendar_feeds (identity_id, department_id) WHERE revoked IS FALSE
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_people_active_short_code
        ON people (organisation_id, short_code) WHERE active IS TRUE AND short_code <> ''
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
    op.execute("""
        CREATE UNIQUE INDEX uq_jobs_running_heavy
        ON jobs ((1)) WHERE status = 'running' AND kind IN (
            'generation', 'feasibility', 'catalogue_proof', 'swap_audience',
            'merge_preview', 'merge_apply', 'fill_options'
        )
    """)


def downgrade():
    raise RuntimeError('Forward-only migration: retain uniqueness guarantees')
