"""Mark newly issued publications that carry calendar duty lineage."""
from alembic import op
import sqlalchemy as sa


revision = '0153_calendar_lineage'
down_revision = '0152_feasibility_results'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.add_column('publications', sa.Column('calendar_lineage', sa.Boolean(), nullable=True))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("""CREATE TRIGGER publications_calendar_lineage_guard
                      BEFORE UPDATE OF calendar_lineage ON publications
                      WHEN NEW.calendar_lineage IS NOT OLD.calendar_lineage
                      BEGIN SELECT RAISE(ABORT, 'immutable publication'); END""")


def downgrade():
    raise RuntimeError('Forward-only migration: preserve issued calendar identity')
