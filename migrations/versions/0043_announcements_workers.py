"""Announcement audiences and importance, worker heartbeats, and an index of who is on each publication.

Revision ID: 0043_announcements_workers
Revises: 0042_rate_limits
"""
import json
from alembic import op
import sqlalchemy as sa

revision = "0043_announcements_workers"
down_revision = "0042_rate_limits"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("department_content", sa.Column("audience", sa.JSON(), nullable=False, server_default='{"everyone": true}'))
    op.add_column("department_content", sa.Column("importance", sa.String(16), nullable=False, server_default="normal"))
    op.create_table("worker_heartbeats",
        sa.Column("worker_id", sa.String(120), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("passes", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_worker_heartbeats_last_seen_at", "worker_heartbeats", ["last_seen_at"])
    op.create_table("publication_people",
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("publications.id"), primary_key=True),
        sa.Column("person_id", sa.String(36), primary_key=True))
    op.create_index("ix_publication_people_person_id", "publication_people", ["person_id"])
    # Backfill from the immutable snapshots once, so staff lists never need to read them again.
    bind = op.get_bind()
    table = sa.table("publication_people", sa.column("publication_id"), sa.column("person_id"))
    # One snapshot at a time: they can be megabytes each.
    for pid in [row[0] for row in bind.execute(sa.text("SELECT id FROM publications"))]:
        snapshot = bind.execute(sa.text("SELECT snapshot FROM publications WHERE id = :id"), {"id": pid}).scalar()
        data = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        people = {a["person_id"] for s in data.get("shifts", []) for a in s.get("participants", []) if a.get("person_id")}
        if people:
            op.bulk_insert(table, [{"publication_id": pid, "person_id": person} for person in sorted(people)])


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; keep the normal command audit history")
