"""Add decision times, preserving all existing request and audit evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0114_request_decision_times"
down_revision = "0113_department_quota_calendar"
branch_labels = depends_on = None


def upgrade():
    for name in ("requests", "availability_patterns"):
        op.add_column(name, sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_audit_events_target_action_at", "audit_events",
                    ["department_id", "target_id", "action", "created_at"])
    backfill(op.get_bind())


def backfill(bind):
    """Populate only the new nullable facts, and only from matching decision evidence."""
    events = sa.table("audit_events", sa.column("id"), sa.column("department_id"), sa.column("target_id"),
        sa.column("action"), sa.column("actor_id"), sa.column("reason"), sa.column("detail", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)))
    receipts = sa.table("command_receipts", sa.column("id"), sa.column("result", sa.JSON()))
    for name, prefix, waiting in (("requests", "request", "submitted"),
                                  ("availability_patterns", "availability_pattern", "proposed")):
        columns = [sa.column(key) for key in ("id", "department_id", "status", "decided_by", "revision")]
        columns.append(sa.column("decided_at", sa.DateTime(timezone=True)))
        if name == "requests":
            columns.append(sa.column("decision_reason"))
        rows = sa.table(name, *columns)
        for row in bind.execute(sa.select(rows).where(rows.c.decided_at.is_(None), rows.c.status != waiting)).mappings():
            actions = {prefix + (".end" if row["status"] == "ended" else ".decide")}
            if name == "availability_patterns" and row["status"] == "active":
                actions.add(prefix + ".propose")
            target = events.c.target_id == row["id"]
            if name == "availability_patterns":
                target = sa.or_(target, sa.and_(events.c.action == prefix + ".propose",
                    receipts.c.result["pattern"]["id"].as_string() == row["id"]))
            query = sa.select(events, receipts.c.id.label("receipt_id"), receipts.c.result).select_from(events.outerjoin(receipts,
                receipts.c.id == events.c.detail["receipt_id"].as_string())).where(
                    events.c.department_id == row["department_id"], target,
                    events.c.action.in_([*actions, prefix + ".undo"])).order_by(events.c.created_at.desc(), events.c.id.desc())
            history = list(bind.execute(query).mappings())
            groups, proven = {}, {}
            for event in reversed(history):
                if event["result"] is not None:
                    groups.setdefault((event["receipt_id"], event["action"]), []).append(event)
            for group in groups.values():
                states = [item for item in _records(group[0]["result"], group[0]["action"])
                          if item.get("id") == row["id"]]
                # A receipt's operation order is usable only with complete, strictly ordered audit evidence.
                if len(states) == len(group) == len({event["created_at"] for event in group}):
                    proven.update((event["id"], state) for event, state in zip(group, states))
            undone = False
            for event in history:
                if event["action"] == prefix + ".undo":
                    undone = True
                    continue
                if name == "requests" and row["status"] != "withdrawn" and (
                        event["actor_id"] != row["decided_by"] or event["reason"] != row["decision_reason"]):
                    continue
                if event["result"] is not None:
                    state = proven.get(event["id"], {})
                    if state.get("status") != row["status"] or state.get("revision", row["revision"]) > row["revision"]:
                        continue
                elif undone:
                    # Without a retained receipt, an Undo may have superseded this otherwise matching event.
                    continue
                bind.execute(rows.update().where(rows.c.id == row["id"], rows.c.decided_at.is_(None))
                             .values(decided_at=event["created_at"]))
                break


def _records(reply, action):
    kind = reply.get("authorization_command", {}).get("kind")
    if kind in {None, action} or kind == "request.decide_many" and action in {"request.decide", "availability_pattern.decide"}:
        for key in ("record", "pattern"):
            if isinstance(reply.get(key), dict):
                yield reply[key]
        yield from reply.get("records", [])
    for item in reply.get("items", []):
        for result in item.get("results", []):
            yield from _records(result, action)


def downgrade():
    raise RuntimeError("Forward-only: retain decision and audit evidence")
