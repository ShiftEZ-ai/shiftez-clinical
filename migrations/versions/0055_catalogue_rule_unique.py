"""At most one catalogue rule in force per catalogue entry and department (docs/design/rule-catalogue.md §2).

Expand-only: a partial unique index on policies (department, the catalogue entry id inside the definition) for active
catalogue rows. No data changes; existing rules are not catalogue rows, so nothing can conflict.

Revision ID: 0055_catalogue_rule_unique
Revises: 0054_tags
"""
from alembic import op

revision = "0055_catalogue_rule_unique"
down_revision = "0054_tags"
branch_labels = None
depends_on = None

WHERE = "kind = 'catalogue' AND status = 'active'"


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE UNIQUE INDEX uq_catalogue_rule_active ON policies (department_id, ((definition->'rule'->>'id'))) WHERE " + WHERE)
    else:
        op.execute("CREATE UNIQUE INDEX uq_catalogue_rule_active ON policies (department_id, json_extract(definition, '$.rule.id')) WHERE " + WHERE)


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
