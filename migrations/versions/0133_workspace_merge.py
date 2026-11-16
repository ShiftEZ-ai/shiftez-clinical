"""Retain immutable private-fork bases and server-owned merge proposals."""
from alembic import op
import sqlalchemy as sa

revision = '0133_workspace_merge'
down_revision = '0132_pwa_push'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    op.create_table('workspace_fork_bases',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=False, unique=True),
        sa.Column('source_workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=False),
        sa.Column('source_revision', sa.Integer(), nullable=False),
        sa.Column('source_content_revision', sa.Integer(), nullable=False),
        sa.Column('base_content', sa.JSON(), nullable=False),
        sa.Column('fork_content', sa.JSON(), nullable=False),
        sa.Column('session_map', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.CheckConstraint('source_revision >= 1'),
        sa.CheckConstraint('source_content_revision >= 1'))
    op.create_table('workspace_merge_proposals',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('source_workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=False),
        sa.Column('target_workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=False),
        sa.Column('source_revision', sa.Integer(), nullable=False),
        sa.Column('source_content_revision', sa.Integer(), nullable=False),
        sa.Column('target_revision', sa.Integer(), nullable=False),
        sa.Column('target_content_revision', sa.Integer(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='preview'),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.CheckConstraint("status IN ('preview','applied','stale','cancelled')"))
    op.create_index('ix_workspace_fork_bases_source', 'workspace_fork_bases', ['source_workspace_id'])
    op.create_index('ix_workspace_merge_proposals_source_status', 'workspace_merge_proposals', ['source_workspace_id', 'status'])
    op.create_index('ix_workspace_merge_proposals_target_status', 'workspace_merge_proposals', ['target_workspace_id', 'status'])
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER workspace_fork_no_update BEFORE UPDATE ON workspace_fork_bases "
                   "BEGIN SELECT RAISE(ABORT, 'immutable fork base'); END")
        op.execute("CREATE TRIGGER workspace_fork_no_delete BEFORE DELETE ON workspace_fork_bases "
                   "BEGIN SELECT RAISE(ABORT, 'immutable fork base'); END")
        op.execute("CREATE TRIGGER workspace_fork_version_guard BEFORE INSERT ON workspace_fork_bases "
                   "WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) "
                   "BEGIN SELECT RAISE(ABORT, 'invalid fork revision'); END")
    else:
        schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()
        if '$' in schema:
            raise RuntimeError('Schema dollar delimiters are unsupported')
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.workspace_fork_immutable_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable fork base'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1)
              THEN RAISE EXCEPTION 'invalid fork revision'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER workspace_fork_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.workspace_fork_bases "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.workspace_fork_immutable_guard()")


def downgrade():
    raise RuntimeError('Forward-only: retain private fork and merge evidence')
