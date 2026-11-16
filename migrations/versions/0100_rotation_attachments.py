"""Keep a rotation's mentor in its attachment, retaining the retired column and snapshots.

Expand-only: add the ownership link and copy only missing attachment rows. Existing
attachments, including the broader rows copied by 0052, retain their exact settings.
"""
from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0100_rotation_attachments"
down_revision = "0106_feedback_recent_routes"
branch_labels = depends_on = None


def upgrade():
    op.create_table("rotation_attachments",
        sa.Column("rotation_id", sa.String(36), sa.ForeignKey("rotations.id"), primary_key=True),
        sa.Column("mentorship_id", sa.String(36), sa.ForeignKey("mentorships.id"), nullable=False),
        sa.Column("synced_revision", sa.Integer()))
    op.create_index("ix_rotation_attachments_mentorship_id", "rotation_attachments", ["mentorship_id"])
    convert_rotations(op.get_bind())


def convert_rotations(bind):
    """Copy legacy mentors once; share automatic ownership and preserve independent attachments."""
    rotations = sa.table("rotations", sa.column("id", sa.String()),
        sa.column("department_id", sa.String()), sa.column("person_id", sa.String()),
        sa.column("cohort_id", sa.String()),
        sa.column("mentor_id", sa.String()), sa.column("service_id", sa.String()),
        sa.column("starts_on", sa.Date()), sa.column("ends_on", sa.Date()))
    cohorts = sa.table("cohorts", sa.column("id", sa.String()),
        sa.column("department_id", sa.String()), sa.column("programme_id", sa.String()))
    mentorships = sa.table("mentorships", sa.column("id", sa.String()),
        sa.column("revision", sa.Integer()), sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("department_id", sa.String()), sa.column("person_id", sa.String()),
        sa.column("mentor_id", sa.String()), sa.column("programme_id", sa.String()),
        sa.column("starts_on", sa.Date()), sa.column("ends_on", sa.Date()),
        sa.column("assignment_type_ids", sa.JSON()), sa.column("service_ids", sa.JSON()),
        sa.column("strength", sa.String()))
    links = sa.table("rotation_attachments", sa.column("rotation_id", sa.String()),
        sa.column("mentorship_id", sa.String()), sa.column("synced_revision", sa.Integer()))
    existing_links = list(bind.execute(sa.select(links)).mappings())
    linked = {row["rotation_id"] for row in existing_links}
    keys = ("department_id", "person_id", "mentor_id", "starts_on", "ends_on")
    existing = {}
    revisions = {}
    for row in bind.execute(sa.select(mentorships).order_by(mentorships.c.id)).mappings():
        revisions[row["id"]] = row["revision"]
        if not row["assignment_type_ids"]:
            key = tuple(row[name] for name in keys)
            services = row["service_ids"] or []
            for service_id in services or [None]:
                scope = key, row["programme_id"], service_id
                candidate = len(services), row["id"]
                if scope not in existing or candidate < existing[scope]:
                    existing[scope] = candidate
    owned = {row["mentorship_id"] for row in existing_links
             if row["synced_revision"] == revisions[row["mentorship_id"]]}
    created_at = datetime.now(timezone.utc)
    programme = sa.select(cohorts.c.programme_id).where(cohorts.c.id == rotations.c.cohort_id,
        cohorts.c.department_id == rotations.c.department_id).scalar_subquery()
    for row in bind.execute(sa.select(rotations, programme.label("rotation_programme_id")).where(rotations.c.mentor_id.is_not(None),
            rotations.c.mentor_id != rotations.c.person_id).order_by(rotations.c.id)).mappings():
        if row["id"] in linked:
            continue
        key = tuple(row[name] for name in keys)
        services = [row["service_id"]] if row["service_id"] else []
        candidates = [existing.get((key, programme_id, service_id))
            for programme_id in (row["rotation_programme_id"], None) for service_id in (row["service_id"], None)]
        matching = min((r for r in candidates if r), key=lambda r: (r[0] == 0, r[0], r[1]), default=None)
        mentorship_id = matching[1] if matching else None
        synced_revision = None
        if mentorship_id is None:
            mentorship_id = str(uuid4())
            bind.execute(mentorships.insert(), {**dict(zip(keys, key)), "id": mentorship_id,
                "revision": 1, "created_at": created_at, "programme_id": None,
                "assignment_type_ids": [], "service_ids": services,
                "strength": "preferred"})
            existing[key, None, row["service_id"]] = len(services), mentorship_id
            revisions[mentorship_id] = 1
            owned.add(mentorship_id)
        if mentorship_id in owned:
            synced_revision = revisions[mentorship_id]
        bind.execute(links.insert(), {"rotation_id": row["id"], "mentorship_id": mentorship_id,
                                      "synced_revision": synced_revision})


def downgrade():
    raise RuntimeError("Forward-only: retain attachment records and historical rotation mentors")
