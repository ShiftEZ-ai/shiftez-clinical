"""Refuse catalogue date edits that exclude existing dated uses."""
from sqlalchemy import Text, cast, func, or_, select

from apps.api import models as m
from apps.api.errors import require
from packages.domain.time import local_midnight


def guard_dates(db, actor, kind, row, value):
    if kind not in {'assignment_types', 'locations', 'location_types'}:
        return
    if not (value.starts_on > row.starts_on or
            value.ends_on is not None and (row.ends_on is None or value.ends_on < row.ends_on)):
        return

    zone = db.get(m.Organisation, actor.organisation_id).timezone
    start = local_midnight(value.starts_on, zone)
    end = local_midnight(value.ends_on, zone) if value.ends_on else None

    def outside(first, last, lo=value.starts_on, hi=value.ends_on):
        return or_(first < lo, *([or_(last.is_(None), last > hi)] if hi is not None else []))

    def count(cls, reference, dates):
        return select(func.count()).select_from(cls).where(
            cls.department_id == actor.department_id, reference, dates)

    if kind == 'assignment_types':
        shift_ref = m.Shift.assignment_type_id == row.id
        series_ref = m.RecurringSeries.assignment_type_id == row.id
        queries = {
            'wishes': count(m.StaffWish, m.StaffWish.assignment_type_id == row.id,
                or_(m.StaffWish.first_day < value.starts_on,
                    *([m.StaffWish.last_day >= value.ends_on,
                       m.StaffWish.repeat.is_not(None) & or_(m.StaffWish.repeat['ends_on'].as_string().is_(None),
                           m.StaffWish.repeat['ends_on'].as_string() >= value.ends_on.isoformat())]
                      if value.ends_on is not None else []))),
            'mentorships': count(m.Mentorship, cast(m.Mentorship.assignment_type_ids, Text).contains(row.id),
                outside(m.Mentorship.starts_on, m.Mentorship.ends_on)),
        }
    else:
        locations = select(m.Location.id).where(m.Location.department_id == actor.department_id)
        if kind == 'location_types':
            locations = locations.where(m.Location.location_type_id == row.id).cte()
        else:
            locations = locations.where(m.Location.id == row.id).cte(recursive=True)
            locations = locations.union(select(m.Location.id).join(locations, m.Location.parent_id == locations.c.id)
                .where(m.Location.department_id == actor.department_id))
        location_ids = select(locations.c.id)
        shift_ref = m.Shift.location_id.in_(location_ids)
        series_ref = m.RecurringSeries.location_id.in_(location_ids)
        queries = {
            'locations': count(m.Location, m.Location.id.in_(location_ids) & (m.Location.id != row.id),
                outside(m.Location.starts_on, m.Location.ends_on)),
            'resource pools': count(m.ResourcePool, m.ResourcePool.location_id.in_(location_ids),
                outside(m.ResourcePool.starts_on, m.ResourcePool.ends_on)),
            'closures': count(m.LocationClosure, m.LocationClosure.location_id.in_(location_ids),
                outside(m.LocationClosure.start, m.LocationClosure.end, start, end)),
            'events': count(m.Event, m.Event.location_id.in_(location_ids),
                or_(outside(m.Event.start, m.Event.end, start, end),
                    *([(m.Event.repeat['anchor'].as_string().is_not(None) &
                        (m.Event.repeat_until.is_(None) | (m.Event.repeat_until > value.ends_on)))]
                      if value.ends_on is not None else []))),
            'event occurrences': count(m.EventOccurrence,
                m.EventOccurrence.data['location_id'].as_string().in_(location_ids),
                outside(m.EventOccurrence.start, m.EventOccurrence.end, start, end)),
        }
    # Do not filter workspace status: published and archived sessions retain their meaning too.
    queries = {
        'sessions': select(func.count()).select_from(m.Shift).join(m.Workspace).where(
            m.Workspace.department_id == actor.department_id, shift_ref,
            outside(m.Shift.start, m.Shift.end, start, end)),
        'series': count(m.RecurringSeries, series_ref, outside(m.RecurringSeries.starts_on, m.RecurringSeries.ends_on)),
        **queries,
    }
    counts = dict(db.execute(select(*(query.scalar_subquery().label(name) for name, query in queries.items()))).one()._mapping)
    uses = [f'{number} {name if number != 1 or name == "series" else "wish" if name == "wishes" else name[:-1]}'
            for name, number in counts.items() if number]
    label = {'assignment_types': 'duty type', 'locations': 'location', 'location_types': 'location type'}[kind]
    require(not uses, 'CATALOGUE_DATES_EXCLUDE_USE',
        f'{" and ".join(uses)} use this {label} outside {value.starts_on:%d %b %Y}'
        + (f' to {value.ends_on:%d %b %Y}' if value.ends_on else ' onwards'), 409,
        {'counts': counts, 'starts_on': value.starts_on.isoformat(),
         'ends_on': value.ends_on.isoformat() if value.ends_on else None})
