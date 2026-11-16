"""Saved planner views join the one saved-views system (table_views, list "planner"); the old rows move across.

Revision ID: 0036_planner_saved_views
Revises: 0035_table_views
"""
import json
from alembic import op
import sqlalchemy as sa

revision = "0036_planner_saved_views"
down_revision = "0035_table_views"
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_bind()
    insert = sa.text("INSERT INTO table_views (id, revision, created_at, department_id, owner_id, table_key, name, definition) "
                     "VALUES (:id, 1, :created_at, :department_id, :owner_id, 'planner', :name, :definition)").bindparams(sa.bindparam("definition", type_=sa.JSON))
    for row in db.execute(sa.text("SELECT id, created_at, department_id, owner_id, name, definition FROM planner_views")).mappings().all():
        old = json.loads(row["definition"]) if isinstance(row["definition"], str) else dict(row["definition"] or {})
        planner = {k: old[k] for k in ("mode", "search", "location_id", "assignment_type_id", "person_id") if k in old}
        db.execute(insert, {"id": row["id"], "created_at": row["created_at"], "department_id": row["department_id"], "owner_id": row["owner_id"], "name": row["name"],
                            "definition": {"schema_version": 1, "layout": "table", "hidden": [], "filters": {}, "group": "", "sort": None, "q": "", "planner": planner}})
    db.execute(sa.text("DELETE FROM planner_views"))


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
