"""Keep department score weights and advisory tier values in immutable rows."""
from alembic import op
import sqlalchemy as sa


revision = '0140_score_settings_versions'
down_revision = '0139_duty_short_code'
branch_labels = depends_on = None
SAFETY_CLASS = 'fill_only'


def upgrade():
    op.create_table('score_settings_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('identities.id'), nullable=True),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_id', sa.String(36), sa.ForeignKey('score_settings_versions.id')),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('department_id', 'revision'))
    op.create_index('ix_score_settings_department_revision', 'score_settings_versions', ['department_id', 'revision'])

    op.execute("INSERT INTO score_settings_versions (id, department_id, revision, created_at, definition, actor_id, reason, previous_id, effective_from) "
               "SELECT id, id, 1, COALESCE(created_at, CURRENT_TIMESTAMP), generation_defaults, NULL, "
               "'Recorded existing score settings', NULL, COALESCE(created_at, CURRENT_TIMESTAMP) "
               "FROM departments WHERE true ON CONFLICT (department_id, revision) DO NOTHING")

    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER score_settings_no_update BEFORE UPDATE ON score_settings_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable score settings'); END")
        op.execute("CREATE TRIGGER score_settings_no_delete BEFORE DELETE ON score_settings_versions "
                   "BEGIN SELECT RAISE(ABORT, 'immutable score settings'); END")
        op.execute("CREATE TRIGGER score_settings_version_guard BEFORE INSERT ON score_settings_versions "
                   "WHEN NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1) "
                   "BEGIN SELECT RAISE(ABORT, 'invalid score settings revision'); END")
    else:
        schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()
        if '$' in schema:
            raise RuntimeError('Schema dollar delimiters are unsupported')
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.score_settings_immutable_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable score settings'; END IF;
              IF NOT EXISTS (SELECT 1 WHERE NEW.revision >= 1)
              THEN RAISE EXCEPTION 'invalid score settings revision'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER score_settings_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.score_settings_versions "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.score_settings_immutable_guard()")


def downgrade():
    raise RuntimeError('Forward-only: retain score configuration history and audit evidence')
