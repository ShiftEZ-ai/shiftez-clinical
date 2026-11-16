"""One set of bulk limits, with deployment defaults and audited department overrides."""
from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from .common import expected, history

FIELDS = ("bulk_max_items", "bulk_direct_max", "bulk_staged_days", "rate_bulk_preview_per_minute")


def limits(db, actor, settings):
    department = db.get(m.Department, actor.department_id)
    return s.BulkSettings.model_validate({**{key: getattr(settings, key) for key in FIELDS},
                                         **(department.bulk_settings or {})}).model_dump()


def update(db, actor, command, settings):
    require_role(actor, "admin")
    require(command.target_id == actor.department_id, "NOT_FOUND", "Department not found", 404)
    value = s.BulkSettingsInput.model_validate(command.payload)
    department = db.get(m.Department, actor.department_id)
    expected(department, command.expected_revision)
    previous = limits(db, actor, settings)
    changed = {**previous, **value.model_dump(exclude={"reason"}, exclude_unset=True)}
    changed = s.BulkSettings.model_validate(changed).model_dump()
    if changed != previous:
        department.bulk_settings = changed
        department.revision += 1
        history(db, actor, "department_bulk_settings", department, value.reason)
    return {"department": record(department), "settings": changed, "previous": previous,
            "undo": {"kind": "department.bulk_settings", "target_id": department.id,
                     "expected_revision": department.revision, "payload": {**previous, "reason": value.reason}}}
