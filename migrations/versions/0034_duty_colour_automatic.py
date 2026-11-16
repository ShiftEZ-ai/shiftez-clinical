"""Duty types saved with the old default colour (teal) become automatic, so each type gets its own colour.

Colour is display only: validation ignores colour-only differences between a session snapshot and its type.
"""
import json
from alembic import op
import sqlalchemy as sa

revision = '0034_duty_colour_automatic'
down_revision = '0033_catalogue_colours'
branch_labels = depends_on = None


def upgrade():
    db = op.get_bind()
    for id_, definition in db.execute(sa.text('SELECT id, definition FROM assignment_types')).fetchall():
        value = json.loads(definition) if isinstance(definition, str) else dict(definition or {})
        if value.get('colour') == 'teal':
            value['colour'] = ''
            update = sa.text('UPDATE assignment_types SET definition = :d WHERE id = :i').bindparams(sa.bindparam('d', type_=sa.JSON))
            db.execute(update, {'d': value, 'i': id_})


def downgrade():
    pass  # Automatic and teal render the same class of result; nothing to restore.
