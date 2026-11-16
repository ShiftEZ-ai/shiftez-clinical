"""Bound draft overlap and job reads without changing retained schedule, inputs or results."""
from alembic import op
import sqlalchemy as sa

revision = "0117_backend_read_indexes"
down_revision = "0116_password_login_hardening"
branch_labels = depends_on = None
SAFETY_CLASS = "destructive"  # Current guard conservatively requires exact-ID confirmation for this Python backfill.
DESTRUCTIVE_REVIEWED = (
    "Fill only NULL jobs.read_metadata and jobs.result_summary projections added by this migration; "
    "retained inputs, solver results and publication/audit evidence stay verbatim. Take a verified backup "
    "before deployment; roll forward or restore that backup rather than dropping evidence on downgrade."
)


def upgrade():
    op.create_index("ix_assignments_shift_end", "assignments", ["shift_id", "end"])
    op.create_index("ix_requests_person_status_end", "requests", ["person_id", "status", "end"])
    op.add_column('jobs', sa.Column('read_metadata', sa.JSON(), nullable=True))
    op.add_column('jobs', sa.Column('result_summary', sa.JSON(), nullable=True))
    backfill(op.get_bind())


def backfill(bind):
    from apps.api.serialization import job_result_projection, job_read_metadata
    jobs = sa.table('jobs', sa.column('id', sa.String()), sa.column('kind', sa.String()),
                    sa.column('input_snapshot', sa.JSON()), sa.column('result', sa.JSON()),
                    sa.column('read_metadata', sa.JSON()), sa.column('result_summary', sa.JSON()))
    # Only new projections are written; input snapshots and solver/publication evidence remain verbatim.
    rows = bind.execute(sa.select(jobs.c.id, jobs.c.kind, jobs.c.input_snapshot, jobs.c.result)
                        .execution_options(stream_results=True, yield_per=1))
    metadata_update = sa.text('UPDATE jobs SET read_metadata = :projection WHERE id = :id AND read_metadata IS NULL').bindparams(
        sa.bindparam('projection', type_=sa.JSON()))
    summary_update = sa.text('UPDATE jobs SET result_summary = :projection WHERE id = :id AND result_summary IS NULL').bindparams(
        sa.bindparam('projection', type_=sa.JSON()))
    for row in rows:
        bind.execute(metadata_update, {'id': row.id, 'projection': job_read_metadata(row.input_snapshot or {}, row.result)})
        bind.execute(summary_update, {'id': row.id, 'projection': job_result_projection(row.kind, row.result)})


def downgrade():
    raise RuntimeError("Forward-only: retain read indexes and schedule evidence")
