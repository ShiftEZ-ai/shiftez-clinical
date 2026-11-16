"""Join retained publication-change and planning-group migration branches."""

revision = '0127_integration_wave1'
down_revision = ('0126_published_changes', '0126_planning_groups_drafts')
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    pass


def downgrade():
    raise RuntimeError('Forward-only: retain both integrated migration branches')
