"""Event catalogues, retained occurrences, RSVP receipts and private calendar settings.

Adds new storage and widens nullable receipt sources without changing M1 evidence.
Editable defaults belong only to new departments/imports, never existing departments at upgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = "0082_events"
down_revision = "0080_announcement_receipts"
branch_labels = depends_on = None


def record_columns():
    return [sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=False, index=True)]


def upgrade():
    op.add_column("departments", sa.Column("event_horizon_days", sa.Integer(),
        sa.CheckConstraint("event_horizon_days BETWEEN 7 AND 3660", name="ck_department_event_horizon"),
        nullable=False, server_default="365"))
    op.add_column("departments", sa.Column("event_receipt_window_days", sa.Integer(),
        sa.CheckConstraint("event_receipt_window_days BETWEEN 1 AND 3660", name="ck_department_event_receipt_window"),
        nullable=False, server_default="14"))
    op.add_column("departments", sa.Column("events_checked_on", sa.Date()))
    op.create_table("event_types", *record_columns(),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("colour", sa.String(16), nullable=False, server_default=""),
        sa.Column("default_start_time", sa.String(5), nullable=False, server_default="09:00"),
        sa.Column("default_length_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("default_audience", sa.JSON(), nullable=False, server_default='{"everyone": true}'),
        sa.Column("attendance", sa.String(20), nullable=False, server_default="expected"),
        sa.Column("blocks_duties", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("calendar", sa.String(16), nullable=False, server_default="everyone"),
        sa.Column("notify_inbox", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rsvp", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_tag_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.CheckConstraint("default_length_minutes BETWEEN 1 AND 10080"),
        sa.CheckConstraint("attendance IN ('optional','expected','must_acknowledge')"),
        sa.CheckConstraint("calendar IN ('everyone','going_only','none')"))
    op.create_table("events", *record_columns(),
        sa.Column("event_type_id", sa.String(36), sa.ForeignKey("event_types.id"), nullable=False, index=True),
        sa.Column("heading", sa.String(160), nullable=False),
        sa.Column("occurrence_data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, server_default="in_person"),
        sa.Column("location_id", sa.String(36), sa.ForeignKey("locations.id")),
        sa.Column("place", sa.String(500), nullable=False, server_default=""),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("organiser_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("presenter_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("audience", sa.JSON(), nullable=False, server_default='{"everyone": true}'),
        sa.Column("repeat", sa.JSON()),
        sa.Column("repeat_until", sa.Date()),
        sa.Column("skip_date_set_id", sa.String(36)),
        sa.Column("cancelled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_attendees", sa.Integer()),
        sa.CheckConstraint('"end" > start'),
        sa.CheckConstraint("format IN ('in_person','online','hybrid')"),
        sa.CheckConstraint("max_attendees IS NULL OR max_attendees > 0"))
    op.create_index("ix_events_department_start", "events", ["department_id", "start"])
    op.create_table("event_occurrences", *record_columns(),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.id"), nullable=False, index=True),
        sa.Column("original_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("overridden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cancelled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("event_id", "original_start"), sa.CheckConstraint('"end" > start'))
    op.create_index("ix_event_occurrences_department_start", "event_occurrences", ["department_id", "start"])
    op.add_column("calendar_feeds", sa.Column("hidden_event_type_ids", sa.JSON(), nullable=False, server_default="[]"))

    # SQLite needs Alembic's preserving table-copy operation for nullable/constraint changes.
    with op.batch_alter_table("announcement_receipts") as batch:
        batch.alter_column("content_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("event_occurrence_id", sa.String(36)))
        batch.add_column(sa.Column("response", sa.String(16)))
        batch.add_column(sa.Column("responded_at", sa.DateTime(timezone=True)))
        batch.create_foreign_key("fk_receipt_event_occurrence", "event_occurrences", ["event_occurrence_id"], ["id"])
        batch.create_unique_constraint("uq_receipt_event_identity", ["event_occurrence_id", "identity_id"])
        batch.create_check_constraint("ck_receipt_source", "(content_id IS NOT NULL AND event_occurrence_id IS NULL) OR "
                                      "(content_id IS NULL AND event_occurrence_id IS NOT NULL)")
        batch.create_check_constraint("ck_receipt_response", "response IS NULL OR response IN ('going','not_going')")
        batch.create_index("ix_announcement_receipts_event_occurrence_id", ["event_occurrence_id"])

    kinds = ("people", "services", "locations", "assignment_types", "programmes", "policies", "events", "department_content")
    checks = sa.inspect(op.get_bind()).get_check_constraints("tag_members")
    with op.batch_alter_table("tag_members", naming_convention={"ck": "ck_tag_members_kind"}) as batch:
        for check in checks:
            if "kind" in check["sqltext"]:
                batch.drop_constraint(check["name"] or "ck_tag_members_kind", type_="check")
        batch.create_check_constraint("ck_tag_members_kind", "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")")


def downgrade():
    raise RuntimeError("Forward-only: retain event, occurrence, RSVP and receipt evidence")
