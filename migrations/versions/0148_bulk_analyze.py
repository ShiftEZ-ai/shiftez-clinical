"""Refresh planner statistics sooner after bulk changes."""
from alembic import op


revision = '0148_bulk_analyze'
down_revision = '0147_relationship_withdrawal'
branch_labels = depends_on = None
SAFETY_CLASS = 'destructive'

TABLES = (
    'approvals',
    'assignment_types',
    'assignments',
    'audit_events',
    'availability',
    'availability_patterns',
    'capabilities',
    'capability_grants',
    'categories',
    'change_sets',
    'definition_history',
    'eligibilities',
    'fairness_ledger',
    'jobs',
    'locations',
    'memberships',
    'people',
    'policies',
    'policy_exceptions',
    'programme_memberships',
    'publication_change_lists',
    'publication_people',
    'publications',
    'record_insertions',
    'recurring_series',
    'requests',
    'review_decisions',
    'rotations',
    'scheduling_facts',
    'services',
    'shift_services',
    'shifts',
    'staff_wishes',
    'swap_events',
    'swap_requests',
    'tag_members',
    'tags',
    'wish_ledger',
    'workspace_access_versions',
    'workspace_baselines',
    'workspace_fork_bases',
    'workspace_read_projections',
    'workspaces',
)


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        for table in TABLES:
            op.execute(f'ALTER TABLE {table} SET (autovacuum_analyze_scale_factor = 0.02, '
                       'autovacuum_analyze_threshold = 50)')


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        for table in TABLES:
            op.execute(f'ALTER TABLE {table} RESET (autovacuum_analyze_scale_factor, '
                       'autovacuum_analyze_threshold)')
