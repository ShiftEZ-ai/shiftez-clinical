"""Initial event types for explicit department creation; existing departments opt in."""
from zoneinfo import ZoneInfo

from apps.api import models as m
from packages.contracts.schemas import EVENT_TYPE_DEFAULTS, EventTypeInput


def seed_department(db, department):
    zone = db.get(m.Organisation, department.organisation_id).timezone
    starts_on = m.now().astimezone(ZoneInfo(zone)).date()
    db.add_all(m.EventType(department_id=department.id,
        **EventTypeInput(starts_on=starts_on, **definition).model_dump(mode="python"))
        for definition in EVENT_TYPE_DEFAULTS)
