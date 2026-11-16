"""Trainee-consultant attachments: a mentorship gains its duty types and clinics (empty: all) and a strength
(required: auto-schedule pairs them; preferred: scored). A rotation's unused mentor becomes an attachment for the
rotation's dates, so the one place to see and change it is Programmes › Attachments.

Expand-only: three columns with defaults, plus copied rows (no update or delete).

Revision ID: 0052_attachments
Revises: 0051_swap_reopened_from
"""
from datetime import datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0052_attachments"
down_revision = "0051_swap_reopened_from"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mentorships", sa.Column("assignment_type_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("mentorships", sa.Column("service_ids", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("mentorships", sa.Column("strength", sa.String(12), nullable=False, server_default="preferred"))
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT department_id, person_id, mentor_id, starts_on, ends_on FROM rotations "
                                "WHERE mentor_id IS NOT NULL AND mentor_id <> person_id AND ends_on IS NOT NULL")).fetchall()
    existing = {(r[0], r[1], r[2], str(r[3])) for r in bind.execute(sa.text(
        "SELECT department_id, person_id, mentor_id, starts_on FROM mentorships")).fetchall()}
    now = datetime.now(timezone.utc)
    for department, person, mentor, starts, ends in rows:
        if (department, person, mentor, str(starts)) in existing:
            continue
        existing.add((department, person, mentor, str(starts)))
        bind.execute(sa.text("INSERT INTO mentorships (id, revision, created_at, department_id, person_id, mentor_id, starts_on, ends_on) "
                             "VALUES (:id, 1, :now, :d, :p, :m, :s, :e)"),
                     {"id": str(uuid.uuid4()), "now": now, "d": department, "p": person, "m": mentor, "s": starts, "e": ends})


def downgrade():
    raise RuntimeError("Use a reviewed forward migration; attachments are scheduling inputs")
