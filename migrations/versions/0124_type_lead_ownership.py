"""Retain immutable approved leadership ownership without rewriting old definitions."""
from alembic import op
import sqlalchemy as sa

revision = '0124_type_lead_ownership'
down_revision = '0123_oidc_department_choice'
branch_labels = depends_on = None
SAFETY_CLASS = 'additive'


def upgrade():
    policies = sa.table('policies', sa.column('department_id'), sa.column('definition', sa.JSON()),
                        sa.column('created_at'), sa.column('kind'))
    op.create_index('ix_policy_catalogue_history', 'policies', [policies.c.department_id,
        policies.c.definition['rule']['id'].as_string(),
        sa.func.coalesce(policies.c.definition['rule']['catalogue_version'].as_integer(), 1),
        policies.c.created_at.desc()], sqlite_where=policies.c.kind == 'catalogue',
        postgresql_where=policies.c.kind == 'catalogue')
    op.create_table('type_lead_ownership',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('assignment_type_id', sa.String(36), sa.ForeignKey('assignment_types.id'), nullable=False),
        sa.Column('source_type_revision', sa.Integer(), nullable=False),
        sa.Column('source_history_id', sa.String(36), sa.ForeignKey('definition_history.id'), nullable=False),
        sa.Column('source_lead_hash', sa.String(64), nullable=False),
        sa.Column('initial_policy_id', sa.String(36), sa.ForeignKey('policies.id'), nullable=False),
        sa.Column('initial_line_id', sa.String(36), nullable=False),
        sa.Column('effective_on', sa.Date(), nullable=False),
        sa.UniqueConstraint('department_id', 'assignment_type_id'),
        sa.CheckConstraint('source_type_revision >= 1 AND revision = 1'))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER type_lead_no_update BEFORE UPDATE ON type_lead_ownership "
                   "BEGIN SELECT RAISE(ABORT, 'immutable leadership evidence'); END")
        op.execute("CREATE TRIGGER type_lead_no_delete BEFORE DELETE ON type_lead_ownership "
                   "BEGIN SELECT RAISE(ABORT, 'immutable leadership evidence'); END")
        op.execute("""CREATE TRIGGER type_lead_department BEFORE INSERT ON type_lead_ownership
            WHEN NOT EXISTS (SELECT 1 FROM assignment_types t, policies p, definition_history h
              WHERE t.id = NEW.assignment_type_id AND p.id = NEW.initial_policy_id AND h.id = NEW.source_history_id
              AND t.department_id = NEW.department_id AND p.department_id = NEW.department_id
              AND h.department_id = NEW.department_id AND h.object_id = t.id
              AND h.object_revision = NEW.source_type_revision AND h.kind = 'assignment_types'
              AND NOT EXISTS (SELECT 1 FROM type_lead_ownership existing
                WHERE existing.id = NEW.id OR (existing.department_id = NEW.department_id
                  AND existing.assignment_type_id = NEW.assignment_type_id)))
            BEGIN SELECT RAISE(ABORT, 'invalid leadership provenance'); END""")
    else:
        schema = op.get_bind().execute(sa.text('SELECT current_schema()')).scalar_one()
        if '$' in schema:
            raise RuntimeError('Schema dollar delimiters are unsupported')
        q = op.get_bind().dialect.identifier_preparer.quote_identifier(schema)
        op.execute(f"""CREATE FUNCTION {q}.type_lead_guard() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable leadership evidence'; END IF;
              IF NOT EXISTS (SELECT 1 FROM {q}.assignment_types t, {q}.policies p, {q}.definition_history h
                WHERE t.id = NEW.assignment_type_id AND p.id = NEW.initial_policy_id AND h.id = NEW.source_history_id
                AND t.department_id = NEW.department_id AND p.department_id = NEW.department_id
                AND h.department_id = NEW.department_id AND h.object_id = t.id
                AND h.object_revision = NEW.source_type_revision AND h.kind = 'assignment_types'
                AND NOT EXISTS (SELECT 1 FROM {q}.type_lead_ownership existing
                  WHERE existing.id = NEW.id OR (existing.department_id = NEW.department_id
                    AND existing.assignment_type_id = NEW.assignment_type_id)))
              THEN RAISE EXCEPTION 'invalid leadership provenance'; END IF;
              RETURN NEW;
            END $$""")
        op.execute(f"CREATE TRIGGER type_lead_guard BEFORE INSERT OR UPDATE OR DELETE ON {q}.type_lead_ownership "
                   f"FOR EACH ROW EXECUTE FUNCTION {q}.type_lead_guard()")


def downgrade():
    raise RuntimeError('Forward-only: retain approved leadership ownership evidence')
