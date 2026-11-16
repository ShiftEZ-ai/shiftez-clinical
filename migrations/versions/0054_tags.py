"""Tags for everything: one flat list of department tags (name, colour, description, which kinds each labels) and
their members by reference (people, clinics, places, duty types, programmes, rules).

Expand-only: two new tables, then each person's free-text tags copied into them (one tag per department and name,
case-insensitive, labelling people; one member row per person and tag). people.tags stays as it is: it is still read
and kept in step by the server until a later, reviewed cleanup.

Revision ID: 0054_tags
Revises: 0053_person_pager
"""
from datetime import datetime, timezone
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0054_tags"
down_revision = "0053_person_pager"
branch_labels = None
depends_on = None

KINDS = ("people", "services", "locations", "assignment_types", "programmes", "policies")


def upgrade():
    op.create_table(
        "tags",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("colour", sa.String(16), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("labels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("department_id", "name"),
    )
    op.create_table(
        "tag_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("tag_id", sa.String(36), sa.ForeignKey("tags.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("member_id", sa.String(36), nullable=False, index=True),
        sa.UniqueConstraint("tag_id", "kind", "member_id"),
        sa.CheckConstraint("kind IN (" + ",".join(f"'{k}'" for k in KINDS) + ")"),
    )
    backfill(op.get_bind())


def backfill(bind):
    """A person's tags go to the departments they hold a rank in (any dates), or to every department of their
    organisation when they hold none."""
    departments = {}
    for department, organisation in bind.execute(sa.text("SELECT id, organisation_id FROM departments")).fetchall():
        departments.setdefault(organisation, []).append(department)
    ranked = {}
    for person, department in bind.execute(sa.text("SELECT DISTINCT person_id, department_id FROM memberships")).fetchall():
        ranked.setdefault(person, set()).add(department)
    now = datetime.now(timezone.utc)
    tags = {}  # (department, lower name) -> id
    people = bind.execute(sa.text("SELECT id, organisation_id, tags FROM people ORDER BY id")).fetchall()
    for person, organisation, raw in people:
        names = json.loads(raw) if isinstance(raw, str) else raw or []
        first = {}  # one tag per name whatever its case: the first spelling names it
        for n in names:
            if n and n.strip():
                first.setdefault(n.strip().lower(), n.strip())
        names = list(first.values())
        for department in sorted(ranked.get(person) or departments.get(organisation) or []):
            for name in names:
                key = (department, name.lower())
                if key not in tags:
                    tags[key] = str(uuid.uuid4())
                    bind.execute(sa.text("INSERT INTO tags (id, revision, created_at, department_id, name, colour, description, labels, active) "
                                         "VALUES (:id, 1, :now, :d, :name, '', '', :labels, :on)"),
                                 {"id": tags[key], "now": now, "d": department, "name": name[:40], "labels": '["people"]', "on": True})
                bind.execute(sa.text("INSERT INTO tag_members (id, revision, created_at, department_id, tag_id, kind, member_id) "
                                     "VALUES (:id, 1, :now, :d, :t, 'people', :p)"),
                             {"id": str(uuid.uuid4()), "now": now, "d": department, "t": tags[key], "p": person})


def downgrade():
    raise RuntimeError("Forward-only: restore a compatible database/image pair")
