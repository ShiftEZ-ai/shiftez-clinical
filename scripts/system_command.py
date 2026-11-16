"""Run product commands as the audited system-maintenance actor (operators only, inside the API container).

Same path as the HTTP command bus: `commands.dispatch` validates and applies each command, then a command
receipt and an audit event are written in the same transaction, so definition history and audit stay consistent.
The actor is a disabled, non-user identity (issuer urn:fmuc:maintenance) holding exactly the roles each command
requires for this one call; it has no role binding and can never sign in.

    docker cp scripts/system_command.py <api>:/tmp/
    docker exec -i -w /app -e PYTHONPATH=/app <api> python /tmp/system_command.py < commands.json

commands.json: {"reason": "...", "commands": [{"kind": "catalogue.update", "target_id": ..., "expected_revision": 1,
"payload": {...}}]}. All commands commit together or not at all. Prints the receipts (no secrets are involved).
"""
import json
import sys
import uuid

MAINTENANCE_ISSUER = "urn:fmuc:maintenance"
MAINTENANCE_SUBJECT = "system-maintenance"


def maintenance_identity(db, m, select):
    row = db.scalar(select(m.Identity).where(m.Identity.issuer == MAINTENANCE_ISSUER, m.Identity.subject == MAINTENANCE_SUBJECT))
    if row is None:
        row = m.Identity(issuer=MAINTENANCE_ISSUER, subject=MAINTENANCE_SUBJECT,
                         display_name="System maintenance (not a user)", disabled=True)
        db.add(row)
        db.flush()
    return row


def apply(db, identity, department, commands, correlation_id, settings):
    """Apply product commands and their receipts/audit within the caller's write transaction."""
    from apps.api import models as m, live
    from apps.api.auth import Actor
    from apps.api.commands import dispatch, command_roles
    from apps.api.serialization import json_value
    from apps.api.snapshots import snapshot_hash
    from packages.contracts import schemas as s

    results = []
    for raw in commands:
        command = s.Command.model_validate(raw)
        roles = command_roles(command)
        actor_roles = ([*roles, *s.PERMISSIONS["access.system_roles"]]
                       if command.kind in {"account.invite", "account.reset_link"} else roles)
        actor = Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                      frozenset(actor_roles), None, "maintenance")
        canonical = command.model_dump(mode="json")
        if command.expected_fingerprint is None:
            canonical.pop("expected_fingerprint")
        result = json_value(dispatch(db, actor, command, settings))
        receipt_id = m.uid()
        result["receipt_id"] = receipt_id
        db.add(m.CommandReceipt(id=receipt_id, department_id=department.id, actor_id=identity.id,
                                command_kind=command.kind, required_roles=roles, key="maintenance-" + uuid.uuid4().hex,
                                payload_hash=snapshot_hash(canonical), result=result))
        db.add(m.AuditEvent(department_id=department.id, actor_id=identity.id, action=command.kind,
                            target_id=command.target_id, reason=str(command.payload.get("reason", command.kind))[:500],
                            detail={"receipt_id": receipt_id, "payload_fields": sorted(command.payload), "maintenance": True},
                            correlation_id=correlation_id))
        db.flush()
        link = db.info.pop("one_time_link", None)  # returned once, never stored (see apps/api/commands.py)
        results.append({**result, "one_time_link": link} if link else result)
    live.signal(db, department.id)
    return results


def run(commands, correlation_id, before=None):
    """Apply commands atomically. `before(db, actor_identity, department)` may run extra checks in the same transaction."""
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import make_engine, retry_write
    from apps.api.schema import require_current_schema
    from apps.api.settings import Settings

    settings = Settings.from_env()
    engine = make_engine(settings.database_url, settings=settings)

    def operation(db):
        require_current_schema(db)
        identity = maintenance_identity(db, m, select)
        department = db.scalars(select(m.Department)).one()  # shortcut: single-department install; add --department when a second exists
        if before:
            before(db, identity, department)
        return apply(db, identity, department, commands, correlation_id, settings)
    external = any(command["kind"].startswith("integration.github.") or (
        command["kind"] == "feedback.export_github" and command.get("payload", {}).get("create_issue") is True)
        for command in commands)
    try:
        return retry_write(engine, operation, attempts=1 if external else 3)
    finally:
        engine.dispose()


if __name__ == "__main__":
    request = json.load(sys.stdin)
    out = run(request["commands"], request.get("correlation_id", "maintenance-" + uuid.uuid4().hex[:12]))
    print(json.dumps([{"receipt_id": r["receipt_id"], "record": {k: r.get("record", {}).get(k) for k in ("id", "name", "revision")}}
                      for r in out], indent=1))
