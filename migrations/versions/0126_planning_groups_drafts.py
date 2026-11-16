"""Versioned scheduling responsibility and creator-only sandbox access; retain legacy rows."""
from alembic import op
import sqlalchemy as sa

revision = '0126_planning_groups_drafts'
down_revision = '0125_application_branding'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    # Static DDL lets the shared migration checker prove every new-table guard.
    op.create_table('planning_group_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('planning_group_versions.id')),
        sa.Column('group_id', sa.String(36), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.UniqueConstraint('group_id', 'revision'))
    op.create_table('planning_settings_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('planning_settings_versions.id')),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.UniqueConstraint('department_id', 'revision'))
    op.create_table('workspace_access_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('workspace_access_versions.id')),
        sa.Column('workspace_id', sa.String(36), sa.ForeignKey('workspaces.id'), nullable=False),
        sa.Column('visibility', sa.String(16), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False),
        sa.UniqueConstraint('workspace_id', 'revision'),
        sa.CheckConstraint("visibility IN ('private','shared')"),
        sa.CheckConstraint("purpose IN ('sandbox','alternative','integrated')"),
        sa.CheckConstraint("visibility <> 'private' OR purpose = 'sandbox'"))
    op.add_column('workspaces', sa.Column('access_version_id', sa.String(36), nullable=True))
    op.add_column('command_receipts', sa.Column('private_owner_id', sa.String(36), nullable=True))
    op.create_index('ix_workspace_access_versions_workspace_id', 'workspace_access_versions', ['workspace_id'])
    op.create_index('ix_planning_group_department_version', 'planning_group_versions', ['department_id', 'group_id', 'revision'])
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER planning_group_no_update BEFORE UPDATE ON planning_group_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable planning configuration'); END")
        op.execute("CREATE TRIGGER planning_group_no_delete BEFORE DELETE ON planning_group_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable planning configuration'); END")
        op.execute("CREATE TRIGGER planning_group_version_guard BEFORE INSERT ON planning_group_versions "
                   "WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) "
                   "BEGIN SELECT RAISE(ABORT, 'invalid planning revision'); END")
        op.execute("CREATE TRIGGER planning_settings_no_update BEFORE UPDATE ON planning_settings_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable planning configuration'); END")
        op.execute("CREATE TRIGGER planning_settings_no_delete BEFORE DELETE ON planning_settings_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable planning configuration'); END")
        op.execute("CREATE TRIGGER planning_settings_version_guard BEFORE INSERT ON planning_settings_versions "
                   "WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) "
                   "BEGIN SELECT RAISE(ABORT, 'invalid planning revision'); END")
        op.execute("CREATE TRIGGER workspace_access_no_update BEFORE UPDATE ON workspace_access_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable planning configuration'); END")
        op.execute("CREATE TRIGGER workspace_access_no_delete BEFORE DELETE ON workspace_access_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable planning configuration'); END")
        op.execute("CREATE TRIGGER workspace_access_version_guard BEFORE INSERT ON workspace_access_versions "
                   "WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) "
                   "BEGIN SELECT RAISE(ABORT, 'invalid planning revision'); END")
    else:
        # SQLite cannot add these FKs without rebuilding retained evidence tables.
        op.create_foreign_key('fk_workspace_access_version', 'workspaces', 'workspace_access_versions', ['access_version_id'], ['id'])
        op.create_foreign_key('fk_receipt_private_owner', 'command_receipts', 'identities', ['private_owner_id'], ['id'])
        schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()
        if '$' in schema:
            raise RuntimeError('Schema dollar delimiters are unsupported')
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.planning_configuration_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable planning configuration'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1)
              THEN RAISE EXCEPTION 'invalid planning revision'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER planning_group_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.planning_group_versions "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.planning_configuration_guard()")
        op.execute(f"CREATE TRIGGER planning_settings_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.planning_settings_versions "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.planning_configuration_guard()")
        op.execute(f"CREATE TRIGGER workspace_access_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.workspace_access_versions "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.planning_configuration_guard()")


def downgrade():
    raise RuntimeError('Forward-only: retain planning configuration and private draft evidence')
