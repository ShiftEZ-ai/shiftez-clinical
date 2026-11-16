"""A person's leave and weekly availability, under the existing absence-reading permissions."""
from types import SimpleNamespace

from sqlalchemy import or_, select

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from packages.domain.time import local_midnight
from .leave_disclosure import leave_wording
from . import leave_types
from .person_record import require_person


def list_availability(db, actor, person_id, start, end):
    require_role(actor, "staff", "coordinator", "approver")
    require_person(db, actor, person_id, ("coordinator", "approver"))
    require(start < end and (end - start).days <= 366, "VALUE_INVALID",
            "Ask for 1 to 366 days (end is the day after the last)", 422)
    department = db.get(m.Department, actor.department_id)
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    lo, hi = local_midnight(start, zone), local_midnight(end, zone)
    from .availability import visible_record_clause
    rows = list(db.scalars(select(m.Availability).where(
        m.Availability.department_id == actor.department_id, m.Availability.person_id == person_id,
        visible_record_clause(), m.Availability.start < hi, m.Availability.end > lo)
        .order_by(m.Availability.start, m.Availability.id).limit(2001)))
    report_ids = {row.request_id for row in rows if row.request_id}
    reports = {row.id: row for row in db.scalars(select(m.RequestRecord).where(
        m.RequestRecord.department_id == actor.department_id, m.RequestRecord.id.in_(report_ids),
        m.RequestRecord.kind == 'absence_report'))} if report_ids else {}
    requests = list(db.scalars(select(m.RequestRecord).where(
        m.RequestRecord.department_id == actor.department_id, m.RequestRecord.person_id == person_id,
        m.RequestRecord.kind == "leave", m.RequestRecord.start < hi, m.RequestRecord.end > lo)
        .order_by(m.RequestRecord.start, m.RequestRecord.id).limit(2001)))
    patterns = list(db.scalars(select(m.AvailabilityPattern).where(
        m.AvailabilityPattern.department_id == actor.department_id, m.AvailabilityPattern.person_id == person_id,
        m.AvailabilityPattern.starts_on < end,
        or_(m.AvailabilityPattern.ends_on.is_(None), m.AvailabilityPattern.ends_on > start))
        .order_by(m.AvailabilityPattern.starts_on, m.AvailabilityPattern.id).limit(2001)))
    team = actor.permits("coordinator", "approver")
    names = {row['id']: row['leave_type_name'] for row in leave_types.names(db, actor.department_id,
             [record(row) for row in rows[:2000] + requests[:2000]])}
    return {"person_id": person_id, "start": start.isoformat(), "end": end.isoformat(),
            "leave_reason_display": department.leave_reason_display,
            "availability": [{**record(row, exclude=("private_reason", "leave_type")),
                              **leave_wording(department, actor, reports.get(row.request_id) or row, names[row.id])}
                             for row in rows[:2000]],
            "requests": [{**record(row, exclude=("private_reason", "decision_reason", *(() if team else ("workspace_id",)))),
                          **leave_wording(department, actor, SimpleNamespace(
                              person_id=row.person_id, kind="approved_leave", leave_type="", leave_type_id=row.leave_type_id,
                              private_reason=row.private_reason), names[row.id])}
                         for row in requests[:2000]],
            "patterns": [{**record(row, exclude=("note",)), **leave_wording(department, actor, SimpleNamespace(
                person_id=row.person_id, kind="weekly_unavailable", leave_type="", private_reason=row.note))}
                         for row in patterns[:2000]],
            "availability_partial": len(rows) > 2000, "requests_partial": len(requests) > 2000,
            "patterns_partial": len(patterns) > 2000, "balances_supported": False}
