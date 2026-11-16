"""Opening-window and fungible-resource evaluation, shared by every planner path.

No holiday, equipment quantity or clinical entitlement is inferred from a label.
Calendars use explicit local day windows; resource reservations use elapsed instants.
"""

from .recurrence import inactive
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from .time import aware, local_stamp, overlaps, duration_hours
from .date_sets import DateSetRangeError, matching_date_sets, prepare_date_sets


def date_set_rule(calendar, day, date_sets=None, *, data=None):
    """The first matching set rule wins; opening evaluation applies dated overrides first."""
    definition = calendar['definition']
    if any(override['on'] == day.isoformat() for override in definition.get('overrides', [])):
        return None
    rules = definition.get('date_set_rules', [])
    if not rules:
        return None
    source = data if data is not None else {'date_sets': date_sets or {}}
    for rule in rules:
        if matching_date_sets(source, day, [rule['date_set_id']], rule.get('entry_names')):
            return rule
    return None


def date_set_closes(calendar, start, end, date_sets=None, *, data=None):
    """Skip only closed days; partial opening conflicts remain for validation."""
    if not calendar['definition'].get('date_set_rules'):
        return False
    zone = ZoneInfo(calendar['definition']['timezone'])
    first, last = start.astimezone(zone).date(), (end - timedelta(microseconds=1)).astimezone(zone).date()
    overrides = {override['on']: override for override in calendar['definition'].get('overrides', [])}
    for n in range((last - first).days + 1):
        day = first + timedelta(days=n)
        opening = overrides.get(day.isoformat()) or date_set_rule(calendar, day, date_sets, data=data)
        if opening is not None and not opening['windows']:
            return True
    return False


def closed_intervals(calendar: dict, start, end, date_sets=None, *, data=None):
    """Return exact uncovered instants; a date override replaces that entire weekday.

    Ambiguous/nonexistent local boundaries raise ValueError, to be presented as an
    unresolved blocking finding instead of guessing an offset or approving cover.
    """
    definition = calendar["definition"]
    zone = definition["timezone"]
    first = start.astimezone(ZoneInfo(zone)).date()
    last = (end - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
    if data is None:
        data = {'date_sets': date_sets or {}}
        data['_date_set_cache'] = prepare_date_sets(data, first, last)
    weekly = {d["weekday"]: d["windows"] for d in definition["week"]}
    overrides = {d["on"]: d["windows"] for d in definition["overrides"]}
    windows = []
    for n in range((last - first).days + 1):
        day = first + timedelta(days=n)
        if not calendar.get("active", True) or day < date.fromisoformat(calendar["starts_on"]):
            continue
        if calendar.get("ends_on") and day >= date.fromisoformat(calendar["ends_on"]):
            continue
        rule = date_set_rule(calendar, day, date_sets, data=data)
        opening = overrides.get(day.isoformat(), rule['windows'] if rule is not None else weekly.get(day.weekday(), []))
        for window in opening:
            a = local_stamp(day, window["start_time"], zone)
            b = local_stamp(day, window["end_time"], zone)
            if a < b and overlaps(a, b, start, end):
                windows.append((max(a, start), min(b, end)))
    gaps, cursor = [], start
    for a, b in sorted(windows):
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def check_calendar(data, calendar_id, start, end, shift_id, add, owner):
    calendar = data.get("operating_calendars", {}).get(calendar_id)
    if calendar is None:
        add("CALENDAR_UNKNOWN", "A referenced opening calendar is unavailable; opening is unresolved",
            shift_id, a=start, b=end, extra=owner)
        return
    try:
        gaps = closed_intervals(calendar, start, end, data=data)
    except DateSetRangeError:
        add("CALENDAR_DATE_SET_UNRESOLVED", "An opening date set is outside the supported calendar range; review this calendar",
            shift_id, a=start, b=end, extra={**owner, "calendar_id": calendar_id})
        return
    except (ValueError, KeyError):
        add("CALENDAR_TIME_UNRESOLVED", "Opening boundaries require an unambiguous local time; review this calendar",
            shift_id, a=start, b=end, extra={**owner, "calendar_id": calendar_id})
        return
    for a, b in gaps:
        add("CALENDAR_CLOSED", "The required interval is outside configured opening windows",
            shift_id, a=a, b=b, extra={**owner, "calendar_id": calendar_id})


def resource_intervals(shift, definition):
    demands = definition.get("resources", [])
    if not demands:
        return
    start, end = aware(shift["start"]), aware(shift["end"])
    for demand in demands:
        a = start + timedelta(minutes=demand["from_minute"])
        b = start + timedelta(minutes=demand["to_minute"]) if demand.get("to_minute") is not None else end
        yield demand, a, b, min(b, end)


def resource_timeline(entries):
    """Sweep half-open reservations; repeated demands share one live session key."""
    events = defaultdict(list)
    for sid, start, end, units in entries:
        events[start].append((sid, units))
        events[end].append((sid, -units))
    bounds = sorted(events)
    live, total = {}, 0
    for start, end in zip(bounds, bounds[1:]):
        for sid, change in events[start]:
            total += change
            live[sid] = live.get(sid, 0) + change
            if not live[sid]:
                del live[sid]
        yield start, end, total, live


def evaluate_resources(data, definitions, shifts, focus, add, ancestor_ids, valid_dates):
    pools = data.get("resource_pools", {})
    reservations = defaultdict(list)
    for shift in shifts:
        sid = shift["id"]
        if sid not in focus and not definitions[sid].get("resources"):
            continue
        start, end = aware(shift["start"]), aware(shift["end"])
        if sid in focus:
            for service_id in shift["service_ids"]:
                service = data["services"].get(service_id, {})
                if service.get("calendar_id"):
                    check_calendar(data, service["calendar_id"], start, end, sid, add, {"service_id": service_id})
            for location_id in ancestor_ids(data, shift["location_id"]):
                location = data["locations"][location_id]
                if location.get("calendar_id"):
                    check_calendar(data, location["calendar_id"], start, end, sid, add, {"location_id": location_id})
        for demand, a, requested_end, b in resource_intervals(shift, definitions[sid]):
            pool_id = demand["resource_pool_id"]
            pool = pools.get(pool_id)
            if sid in focus and (a >= end or requested_end > end):
                add("RESOURCE_INTERVAL_OUTSIDE_SHIFT", "A configured resource interval extends outside this session",
                    sid, extra={"resource_pool_id": pool_id})
            if a >= b:
                continue
            reservations[pool_id].append((sid, a, b, demand["units"]))
            if sid not in focus:
                continue
            if pool is None:
                add("RESOURCE_UNKNOWN", "Required resource pool is unavailable; capacity is unresolved", sid,
                    a=a, b=b, extra={"resource_pool_id": pool_id})
                continue
            if not valid_dates(pool, a, b, data["organisation"]["timezone"]):
                add("RESOURCE_INACTIVE", "Required resource pool is inactive or outside its effective interval", sid,
                    a=a, b=b, extra={"resource_pool_id": pool_id})
            if pool.get("location_id") and pool["location_id"] not in ancestor_ids(data, shift["location_id"]):
                add("RESOURCE_LOCATION", "This pool is limited to a different location subtree", sid,
                    a=a, b=b, extra={"resource_pool_id": pool_id})
            if pool.get("calendar_id"):
                check_calendar(data, pool["calendar_id"], a, b, sid, add, {"resource_pool_id": pool_id})
    for pool_id, entries in reservations.items():
        pool = pools.get(pool_id)
        if not pool or not any(sid in focus for sid, *_ in entries):
            continue
        for a, b, units, live in resource_timeline(entries):
            if units > pool["capacity"]:
                for sid in sorted(live.keys() & focus):
                    add("POOLED_RESOURCE_CAPACITY", "Concurrent required units exceed the resource pool capacity",
                        sid, a=a, b=b, extra={"resource_pool_id": pool_id, "required_units": units,
                                              "capacity": pool["capacity"]})


def resource_report(snapshot):
    """Frozen configured reservations, never inventory availability or consumption."""
    pools = snapshot.get("resource_pools", {})
    rows = {}
    for shift in snapshot["shifts"]:
        if inactive(shift):
            continue
        for demand, start, _, end in resource_intervals(shift, shift["type_snapshot"]["definition"]):
            if start >= end:
                continue
            pid = demand["resource_pool_id"]
            pool = pools.get(pid, {})
            row = rows.setdefault(pid, {"resource_pool_id": pid, "name": pool.get("name", "Unresolved resource"),
                "capacity_at_publication": pool.get("capacity"), "unit_hours": 0.0, "peak_required_units": 0,
                "_intervals": [], "_shifts": set()})
            row["unit_hours"] += duration_hours(start, end) * demand["units"]
            row["_intervals"].append((shift["id"], start, end, demand["units"]))
            row["_shifts"].add(shift["id"])
    for row in rows.values():
        intervals = row.pop("_intervals")
        row["peak_required_units"] = max((units for _, _, units, _ in resource_timeline(intervals)), default=0)
        row["unit_hours"] = round(row["unit_hours"], 4)
        row["sessions"] = len(row.pop("_shifts"))
    return {"metric_version": "resource-reservations-v1", "basis": "Frozen configured reservations, not actual equipment use or availability",
            "rows": sorted(rows.values(), key=lambda r: (r["name"], r["resource_pool_id"]))}
