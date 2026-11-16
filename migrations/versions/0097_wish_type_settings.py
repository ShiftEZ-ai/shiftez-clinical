"""Add department wish settings and expand the points range without changing stored values.

Follows the meetings batch's 0096_meetings_email_merge in one linear chain."""
from alembic import op
import sqlalchemy as sa

revision = '0097_wish_type_settings'
down_revision = '0096_meetings_email_merge'
branch_labels = depends_on = None


def upgrade():
    op.add_column('departments', sa.Column('wish_settings', sa.JSON(), nullable=False, server_default='{}'))
    connection = op.get_bind()
    table = sa.Table('staff_wishes', sa.MetaData(), autoload_with=connection)
    points = next(c for c in table.constraints if isinstance(c, sa.CheckConstraint) and 'points' in str(c.sqltext))
    # Give the old unnamed SQLite check a name; copy_from preserves every other check and index.
    points.name = points.name or 'staff_wishes_points_old'
    triggers = list(connection.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='staff_wishes' AND sql IS NOT NULL"
    )).scalars()) if connection.dialect.name == 'sqlite' else []
    with op.batch_alter_table('staff_wishes', copy_from=table) as batch:
        batch.drop_constraint(points.name, type_='check')
        # Preserve any legacy zero-point rows; PostgreSQL enforces this on subsequent inserts and updates.
        batch.create_check_constraint('staff_wishes_points', 'points BETWEEN 1 AND 2147483647',
                                      postgresql_not_valid=True)
    for trigger in triggers:
        op.execute(trigger)


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
