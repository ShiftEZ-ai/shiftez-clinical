"""Distinguish source-recorded leave from application-approved requests."""
from alembic import op
import sqlalchemy as sa

revision = '0015_recorded_leave'
down_revision = '0014_supervision_pairing'
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    # Preserve any SQLite guards when widening the existing check constraint.
    triggers = list(connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='availability' AND sql IS NOT NULL")).scalars()) if connection.dialect.name == 'sqlite' else []
    check = next(c for c in sa.inspect(connection).get_check_constraints('availability') if 'kind' in c['sqltext'])
    with op.batch_alter_table('availability') as batch:
        batch.drop_constraint(check['name'], type_='check')
        batch.create_check_constraint('availability_kind_reference', "kind IN ('recorded_leave','approved_leave','actual_absence','external_commitment','protected_time','off_duty')")
        batch.add_column(sa.Column('leave_type', sa.String(80), nullable=False, server_default=''))
    for trigger in triggers:
        op.execute(trigger)


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
