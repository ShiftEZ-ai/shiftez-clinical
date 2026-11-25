"""Personal calendar subscriptions: a private link a calendar app refreshes; revoking or a new link ends the old one."""
import hashlib
import secrets
from datetime import timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import JSON, and_, column, func, select, true
from apps.api import models as m
from packages.contracts import schemas as s
from apps.api.errors import require
from apps.api.models import now as utc_now
from packages.domain.reports import calendar_feed, publication_duty_ids
from packages.domain.time import local_midnight
from .common import expected

digest = lambda token: hashlib.sha256(token.encode()).hexdigest()


def active_feed(db, actor):
    return db.scalar(select(m.CalendarFeed).where(m.CalendarFeed.identity_id == actor.identity_id,
        m.CalendarFeed.department_id == actor.department_id, m.CalendarFeed.revoked.is_(False)))


def feed_command(db, actor, command):
    require(bool(actor.person_id), "PERSON_REQUIRED", "Your account is not linked to a person in the rota", 409)
    old = active_feed(db, actor)
    if command.kind == "calendar_feed.settings":
        value = s.CalendarFeedSettingsInput.model_validate(command.payload)
        require(old is not None, "NOT_FOUND", "Create a calendar link before changing its settings", 404)
        expected(old, command.expected_revision)
        ids = set(value.hidden_event_type_ids)
        available = set(db.scalars(select(m.EventType.id).where(m.EventType.department_id == actor.department_id,
            m.EventType.id.in_(ids))))
        require(ids <= available, "NOT_FOUND", "Event type not found", 404)
        old.hidden_event_type_ids = sorted(ids)
        old.revision += 1
        return {"active": True, "revision": old.revision, "hidden_event_type_ids": old.hidden_event_type_ids}
    if old:
        old.revoked, old.revision = True, old.revision + 1
    if command.kind == "calendar_feed.revoke":
        return {"revoked": bool(old)}
    token = secrets.token_urlsafe(32)
    feed = m.CalendarFeed(department_id=actor.department_id, identity_id=actor.identity_id,
        person_id=actor.person_id, token_hash=digest(token), hidden_event_type_ids=list(old.hidden_event_type_ids or []) if old else [])
    db.add(feed)
    db.flush()
    # The secret is shown once; only its hash is stored.
    return {"created": True, "path": f"/calendar/{token}.ics", "revision": feed.revision,
            "hidden_event_type_ids": feed.hidden_event_type_ids}


def feed_calendar(db, token):
    feed = db.scalar(select(m.CalendarFeed).where(m.CalendarFeed.token_hash == digest(token), m.CalendarFeed.revoked.is_(False)))
    identity = db.get(m.Identity, feed.identity_id) if feed else None
    from apps.api.auth import current_roles
    # The link dies with the person's access to the department, not only with their sign-in.
    require(feed is not None and identity is not None and not identity.disabled and identity.person_id == feed.person_id
            and bool(current_roles(db, identity.id, feed.department_id)),
            "NOT_FOUND", "Calendar not found", 404)
    from .schedule import current_publications, zone_of
    zone = zone_of(db, feed.department_id)
    since = (utc_now().astimezone(ZoneInfo(zone)) - timedelta(days=60)).date()
    pubs = current_publications(db, [feed.department_id], since, light=True)
    feed.last_used_at = utc_now()
    department = db.get(m.Department, feed.department_id)
    return calendar_feed(publication_calendars(db, pubs, feed.person_id, zone),
                          feed.person_id, f"{department.name} rota",
                          events=feed_events(db, feed, since))


def publication_calendars(db, pubs, person_id, zone):
    """Only frozen ICS fields and this person's sessions; never decode checks or re-evaluate wishes."""
    from .schedule import in_force_shifts
    if not pubs:
        return []
    P = m.Publication
    ids = [p.id for p in pubs]
    postgres = db.get_bind().dialect.name == 'postgresql'
    fields = ('workspace', 'timezone', 'organisation', 'metric_version', 'date_sets', 'people', 'locations')
    if postgres:
        issued = func.json_to_record(P.snapshot).table_valued(*[column(key, JSON) for key in fields]).render_derived(
            name='feed_metadata', with_types=True)
        query = select(P.id, *issued.c).select_from(P).join(issued, true())
    else:
        query = select(P.id, func.json_extract(P.snapshot, *['$.' + key for key in fields], type_=JSON))
    metadata = {}
    for row in db.execute(query.where(P.id.in_(ids))):
        metadata[row.id] = {key: value for key, value in zip(fields, row[1:] if postgres else row[1]) if value is not None}
    shifts = (func.json_array_elements(P.snapshot['shifts']).table_valued(column('value', JSON), with_ordinality='ordinality', joins_implicitly=True)
              if postgres else func.json_each(P.snapshot, '$.shifts').table_valued(column('value', JSON), 'key', joins_implicitly=True)).alias('feed_shift')
    people = (func.json_array_elements(shifts.c.value['participants']) if postgres else
              func.json_each(shifts.c.value, '$.participants')).table_valued(column('value', JSON), joins_implicitly=True).alias('feed_person')
    subject = people.c.value['person_id'].as_string() if postgres else func.json_extract(people.c.value, '$.person_id')
    mine = select(1).select_from(people).where(subject == person_id).correlate(shifts).exists()
    source_ids = ids
    replacement = P.snapshot[('workspace', 'replaces_publication_id')].as_string()
    predecessor = func.coalesce(P.supersedes_id, replacement).label('predecessor')
    if any(pub.calendar_lineage for pub in pubs):
        # Read same-department lineage only as far back as the first already-issued legacy version.
        columns = (P.id, P.department_id, P.calendar_lineage, predecessor)
        chain = select(*columns).where(P.id.in_(ids)).cte('feed_publications', recursive=True)
        chain = chain.union(select(*columns).join(chain, and_(
            P.id == chain.c.predecessor, P.department_id == chain.c.department_id,
            chain.c.calendar_lineage.is_(True))))
        source_ids = select(chain.c.id)
    grouped, lineage = {}, {}
    shift_rows = select(P.id, predecessor, P.calendar_lineage, P.supersedes_id, P.starts_on,
                        P.snapshot['timezone'].as_string(), shifts.c.value).select_from(P)
    shift_rows = shift_rows.outerjoin(shifts, mine).where(P.id.in_(source_ids))
    for publication_id, previous_id, tracked, supersedes, starts, tz, shift in db.execute(
            shift_rows.order_by(P.id, shifts.c.ordinality if postgres else shifts.c.key)):
        lineage[publication_id] = (previous_id, tracked, starts if not supersedes else None, tz or zone)
        group = grouped.setdefault(publication_id, [])
        if shift is not None:
            group.append(shift)
    result = []
    for pub in pubs:
        snapshot = metadata[pub.id]
        snapshot['shifts'] = in_force_shifts(pub, grouped[pub.id], snapshot.get('timezone') or zone)
        if pub.calendar_lineage:
            history, seen = [], set()
            current_id = pub.id
            while current_id in lineage and current_id not in seen:
                seen.add(current_id)
                previous_id, tracked, replace_from, _ = lineage[current_id]
                cutoff = None
                if tracked and replace_from and previous_id in lineage:
                    cutoff = local_midnight(replace_from, lineage[previous_id][3])
                history.append((grouped[current_id], tracked, cutoff))
                if not tracked:
                    break
                current_id = previous_id
            snapshot['_calendar_duty_ids'] = publication_duty_ids(reversed(history), person_id)
        if pub.effective_until:
            snapshot['effective_until'] = pub.effective_until.isoformat()
        result.append((snapshot, pub.sequence, pub.created_at))
    return result


def feed_events(db, feed, since):
    """Calendar visibility uses the same dated audience evaluator as event delivery."""
    from .events import viewer_occurrences
    department = db.get(m.Department, feed.department_id)
    zone = db.get(m.Organisation, department.organisation_id).timezone
    until = utc_now().astimezone(ZoneInfo(zone)).date() + timedelta(days=department.event_horizon_days)
    rows = viewer_occurrences(db, feed, since, until)
    if not rows:
        return []
    responses = dict(db.execute(select(m.AnnouncementReceipt.event_occurrence_id, m.AnnouncementReceipt.response).where(
        m.AnnouncementReceipt.event_occurrence_id.in_([r.id for r in rows]), m.AnnouncementReceipt.identity_id == feed.identity_id)).all())
    locations = dict(db.execute(select(m.Location.id, m.Location.name).where(m.Location.department_id == feed.department_id)).all())
    hidden, events = set(feed.hidden_event_type_ids or []), []
    for row in rows:
        value = row.data
        settings = value["type"]
        if (value["event_type_id"] in hidden or settings["calendar"] == "none"
                or settings["calendar"] == "going_only" and responses.get(row.id) != "going"):
            continue
        place = ", ".join(dict.fromkeys(p for p in (locations.get(value.get("location_id"), ""), value.get("place")) if p))
        events.append({"id": row.id, "revision": row.revision, "created_at": row.created_at, "start": row.start, "end": row.end,
            "heading": value["heading"], "description": value.get("description", ""),
            "place": place, "attendance": settings["attendance"],
            "online_url": value.get("url") or "", "cancelled": row.cancelled})
    return events
