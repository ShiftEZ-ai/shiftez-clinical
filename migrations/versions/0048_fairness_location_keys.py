"""Fairness ledger: work per location ('<metric>|location:<id>' keys). No schema change.

The ledger is derived from current publications. Both deploy paths rebuild it (`scripts/manage.py rebuild-fairness`)
after a migration, so this revision is what gives already-published rotas their location keys. Nothing to undo.

Revision ID: 0048_fairness_location_keys
Revises: 0047_fairness_ledger
"""

revision = "0048_fairness_location_keys"
down_revision = "0047_fairness_ledger"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
