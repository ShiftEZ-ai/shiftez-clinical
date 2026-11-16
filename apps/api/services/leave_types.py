"""Department leave types: shared creation validation and name projection, including archived references."""
from sqlalchemy import select
from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import record
from packages.domain.leave_types import DEFAULT_LEAVE_TYPES, matching_type
from .common import scoped

LEAVE_KINDS = ('recorded_leave', 'approved_leave', 'actual_absence')
KIND_LABELS = {'recorded_leave': 'Leave', 'approved_leave': 'Leave', 'actual_absence': 'Absence', 'external_commitment': 'External commitment',
               'protected_time': 'Protected time', 'off_duty': 'Off duty', 'weekly_unavailable': 'Weekly unavailability'}


def seed(db, department_id):
    db.add_all(m.LeaveType(department_id=department_id, name=name, short_code=code, colour=colour, sort_order=order)
               for order, (name, code, colour) in enumerate(DEFAULT_LEAVE_TYPES))


def catalogue(db, department_id, active_only=False):
    query = select(m.LeaveType).where(m.LeaveType.department_id == department_id)
    if active_only:
        query = query.where(m.LeaveType.active.is_(True))
    rows = list(db.scalars(query.order_by(m.LeaveType.sort_order, m.LeaveType.name, m.LeaveType.id).limit(2001)))
    require(len(rows) <= 2000, 'CATALOGUE_LIMIT', 'Too many leave types to display completely', 409)
    return [record(row) for row in rows]


def resolve(db, actor, type_id=None, text='', required=True):
    """Older clients may send text or omit a type; exact matches or Other retain compatibility."""
    if type_id:
        row = scoped(db, m.LeaveType, type_id, actor)
    elif required or text:
        types = catalogue(db, actor.department_id)
        match = matching_type(text, types) or next((t for t in types if t['short_code'] == 'OTHER'), None)
        require(match is not None, 'LEAVE_TYPE_REQUIRED', 'Choose an active leave type')
        row = scoped(db, m.LeaveType, match['id'], actor)
    else:
        return None
    require(row.active, 'LEAVE_TYPE_ARCHIVED', 'Choose an active leave type')
    return row


def names(db, department_id, items):
    """Enrich a page with the same name; the old text remains its own field, never a second catalogue."""
    ids = {i['leave_type_id'] for i in items if i.get('leave_type_id')}
    types = {r.id: r.name for r in db.scalars(select(m.LeaveType).where(
        m.LeaveType.department_id == department_id, m.LeaveType.id.in_(ids)))} if ids else {}
    for item in items:
        kind = item.get('kind', 'leave')
        item['leave_type_name'] = types.get(item.get('leave_type_id')) or item.get('leave_type') or KIND_LABELS.get(kind, kind.replace('_', ' ').capitalize())
    return items
