"""Department leave catalogue and references, preserving all original leave text and evidence.

Only newly added nullable references are filled. Existing columns and publication/audit snapshots are untouched.
"""
from datetime import datetime, timezone
import uuid
from alembic import op
import sqlalchemy as sa

revision = "0065_leave_types"
down_revision = "0064_worker_release"
branch_labels = None
depends_on = None


# Frozen migration inputs: runtime catalogue changes must not alter historical upgrades.
DEFAULT_LEAVE_TYPES = (
    ('Annual', 'ANNUAL', 'teal'),
    ('Sick', 'SICK', 'coral'),
    ('Study/Conference', 'STUDY', 'blue'),
    ('Emergency', 'EMERG', 'amber'),
    ('Other', 'OTHER', 'slate'),
)

LEGACY_CODES = {
    'annual leave': 'ANNUAL', 'sick leave': 'SICK', 'study': 'STUDY', 'study leave': 'STUDY',
    'conference': 'STUDY', 'conference leave': 'STUDY', 'study/conference leave': 'STUDY',
    'emergency leave': 'EMERG', 'other leave': 'OTHER',
}


def matching_type(text, types):
    """Only a unique exact name/code or known legacy label matches; ambiguity returns None."""
    label = (text or '').strip().casefold()
    matches = [t for t in types if label and label in (t['name'].casefold(), t['short_code'].casefold())]
    if not matches and label in LEGACY_CODES:
        matches = [t for t in types if t['short_code'] == LEGACY_CODES[label]]
    return matches[0] if len(matches) == 1 else None


def upgrade():
    op.create_table('leave_types',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('department_id', sa.String(36), sa.ForeignKey('departments.id'), nullable=False, index=True),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('short_code', sa.String(6), nullable=False),
        sa.Column('colour', sa.String(16), nullable=False, server_default=''),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('counts_as_unavailable', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint('department_id', 'name'), sa.UniqueConstraint('department_id', 'short_code'),
        sa.CheckConstraint('sort_order BETWEEN 0 AND 100000'))
    bind = op.get_bind()
    for table in ('availability', 'requests'):
        if bind.dialect.name == 'sqlite':
            op.execute(f'ALTER TABLE {table} ADD COLUMN leave_type_id VARCHAR(36) REFERENCES leave_types(id)')
        else:
            op.add_column(table, sa.Column('leave_type_id', sa.String(36), sa.ForeignKey('leave_types.id'), nullable=True))
        op.create_index(f'ix_{table}_leave_type_id', table, ['leave_type_id'])
        op.add_column(table, sa.Column('counts_as_unavailable', sa.Boolean(), nullable=False, server_default=sa.true()))
    backfill(bind)


def backfill(bind):
    types = sa.Table('leave_types', sa.MetaData(), autoload_with=bind)
    availability = sa.Table('availability', sa.MetaData(), autoload_with=bind)
    requests = sa.Table('requests', sa.MetaData(), autoload_with=bind)
    linked = {}
    now = datetime.now(timezone.utc)
    for department, in bind.execute(sa.text('SELECT id FROM departments')):
        seeded = [dict(id=str(uuid.uuid4()), revision=1, created_at=now, department_id=department,
                       name=name, short_code=code, colour=colour, active=True, sort_order=order,
                       counts_as_unavailable=True)
                  for order, (name, code, colour) in enumerate(DEFAULT_LEAVE_TYPES)]
        bind.execute(types.insert(), seeded)
        other = next(t for t in seeded if t['short_code'] == 'OTHER')
        for row in bind.execute(sa.select(availability).where(availability.c.department_id == department)).mappings():
            if row['kind'] not in ('recorded_leave', 'approved_leave', 'actual_absence') and not row['leave_type']:
                continue
            typ = matching_type(row['leave_type'], seeded) or other
            # The original columns, including revision and free text, are deliberately not written.
            bind.execute(availability.update().where(availability.c.id == row['id']).values(leave_type_id=typ['id']))
            if row['request_id']:
                linked.setdefault((department, row['request_id']), set()).add(typ['id'])
        for row in bind.execute(sa.select(requests).where(requests.c.department_id == department, requests.c.kind == 'leave')).mappings():
            matches = linked.get((department, row['id']), set())
            typ_id = next(iter(matches)) if len(matches) == 1 else other['id']
            bind.execute(requests.update().where(requests.c.id == row['id']).values(leave_type_id=typ_id))


def downgrade():
    raise RuntimeError('Forward-only: restore a compatible database/image pair')
