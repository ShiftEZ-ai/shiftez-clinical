"""Department contact types and lazy contact values; retain all legacy columns and evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0136_person_contacts'
down_revision = '0135_applied_population_index'
branch_labels = depends_on = None
SAFETY_CLASS = 'fill_only'


def upgrade():
    # Default type identifiers are scoped by department, so seeding needs no procedural UUID backfill.
    op.create_table('contact_types',
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), primary_key=True),
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('icon', sa.String(40), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('required', sa.Boolean(), nullable=False),
        sa.Column('one_tap', sa.Boolean(), nullable=False),
        sa.Column('dial', sa.JSON(), nullable=False),
        sa.Column('visibility', sa.JSON(), nullable=False),
        sa.Column('validation_format', sa.JSON(), nullable=False),
        sa.Column('person_may_change', sa.Boolean(), nullable=False),
        sa.Column('edited_by', sa.String(24), nullable=False),
        sa.Column('legacy_field', sa.String(32)),
        sa.Column('starts_on', sa.Date(), nullable=False),
        sa.Column('ends_on', sa.Date()),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.UniqueConstraint('department_id', 'id'),
        sa.UniqueConstraint('department_id', 'legacy_field'))
    op.create_table('person_contacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False),
        sa.Column('person_id', sa.String(36), sa.ForeignKey('people.id'), nullable=False),
        sa.Column('contact_type_id', sa.String(36), nullable=False),
        sa.Column('value', sa.String(254), nullable=False),
        sa.Column('visibility_override', sa.JSON()),
        sa.ForeignKeyConstraint(['department_id', 'contact_type_id'], ['contact_types.department_id', 'contact_types.id']),
        sa.UniqueConstraint('department_id', 'person_id', 'contact_type_id'))
    op.create_index('ix_person_contacts_department_id', 'person_contacts', ['department_id'])
    op.create_index('ix_person_contacts_person_id', 'person_contacts', ['person_id'])
    op.execute('INSERT INTO contact_types (department_id, id, revision, created_at, name, kind, icon, sort_order, required, one_tap, dial, visibility, validation_format, person_may_change, edited_by, legacy_field, starts_on, active) SELECT id, \'f0136000-0000-4000-8000-000000000001\', 1, created_at, \'Mobile\', \'phone\', \'\', 20, false, true, \'{"line":"","join":"wait"}\', \'{"mode":"everyone","roles":["staff","coordinator","approver","credentialer","admin","auditor","programme_lead","operator"]}\', \'{"pattern": null,"max_length": 254}\', false, \'admins\', \'work_phone\', \'0001-01-01\', true FROM departments WHERE true ON CONFLICT (department_id, id) DO NOTHING')
    op.execute('INSERT INTO contact_types (department_id, id, revision, created_at, name, kind, icon, sort_order, required, one_tap, dial, visibility, validation_format, person_may_change, edited_by, legacy_field, starts_on, active) SELECT id, \'f0136000-0000-4000-8000-000000000002\', 1, created_at, \'Extension\', \'extension\', \'\', 30, false, false, \'{"line":"","join":"wait"}\', \'{"mode":"roles","roles":["admin","coordinator"]}\', \'{"pattern": null,"max_length": 254}\', false, \'admins\', \'work_extension\', \'0001-01-01\', true FROM departments WHERE true ON CONFLICT (department_id, id) DO NOTHING')
    op.execute('INSERT INTO contact_types (department_id, id, revision, created_at, name, kind, icon, sort_order, required, one_tap, dial, visibility, validation_format, person_may_change, edited_by, legacy_field, starts_on, active) SELECT id, \'f0136000-0000-4000-8000-000000000003\', 1, created_at, \'MCD pager\', \'pager\', \'\', 10, false, false, \'{"line":"","join":"wait"}\', \'{"mode":"everyone","roles":["staff","coordinator","approver","credentialer","admin","auditor","programme_lead","operator"]}\', \'{"pattern": null,"max_length": 254}\', false, \'admins\', \'pager\', \'0001-01-01\', true FROM departments WHERE true ON CONFLICT (department_id, id) DO NOTHING')
    op.execute('INSERT INTO contact_types (department_id, id, revision, created_at, name, kind, icon, sort_order, required, one_tap, dial, visibility, validation_format, person_may_change, edited_by, legacy_field, starts_on, active) SELECT id, \'f0136000-0000-4000-8000-000000000004\', 1, created_at, \'Work email\', \'email\', \'\', 40, false, true, \'{"line":"","join":"wait"}\', \'{"mode":"roles","roles":["admin","coordinator"]}\', \'{"pattern": null,"max_length": 254}\', false, \'admins\', \'work_email\', \'0001-01-01\', true FROM departments WHERE true ON CONFLICT (department_id, id) DO NOTHING')


def downgrade():
    raise RuntimeError('Forward-only: retain contact configuration and values')
