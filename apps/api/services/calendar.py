"""Bounded calendar projection; staff selection is enforced before any rows leave the API."""
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import workspace_shifts, project_series_shifts
from packages.contracts.schemas import ABSENCE_CATEGORY_LABELS, ABSENCE_REPORT_LABEL
from packages.domain.time import aware, overlaps
from packages.domain.recurrence import inactive
from .common import scoped, local_date_span
from .leave_disclosure import display, leave_wording, with_leave
from .schedule import current_publications
from .schedule_view import published_shifts, date_markers
from . import leave_types
from .catalogue import membership_covers
from .training import catalogue_scope, led_programmes, span_scope


def calendar(db, actor, start, end, workspace_id=None, programme_id=None, category_id=None, person_id=None,
             service_id=None, location_id=None, mode=None):
    require_role(actor, "staff", "admin", "coordinator", "approver", "auditor", "programme_lead")
    require(0 <= (end - start).days <= 31 and start < date.max,
            "CALENDAR_RANGE", "Choose a period of 1–31 days")
    if start == end:
        end += timedelta(days=1)
    require(mode in {None, 'physical', 'virtual', 'unspecified'}, 'CALENDAR_MODE', 'Choose a supported consultation mode')
    team = actor.permits("admin", "coordinator", "approver", "auditor", "programme_lead")
    restricted = led_programmes(db, actor) is not None
    if not team:
        require(not workspace_id and (not person_id or person_id == actor.person_id), "FORBIDDEN", "Staff can view only their own published schedule", 403)
        require(bool(actor.person_id), "PERSON_REQUIRED", "Your account is not linked to a person", 403)
        person_id = actor.person_id
    org = db.get(m.Organisation, actor.organisation_id)
    lo, hi = local_date_span(start, end, org.timezone)
    memberships = list(db.scalars(select(m.Membership).where(m.Membership.department_id == actor.department_id,
        m.Membership.person_id == person_id if person_id else True,
        m.Membership.starts_on < end, (m.Membership.ends_on.is_(None) | (m.Membership.ends_on > start)))))
    ids = {r.person_id for r in memberships if not category_id or r.category_id == category_id}
    programme_members = list(db.scalars(select(m.ProgrammeMembership).where(m.ProgrammeMembership.department_id == actor.department_id,
        m.ProgrammeMembership.person_id == person_id if person_id else True,
        m.ProgrammeMembership.starts_on < end, m.ProgrammeMembership.ends_on > start,
        catalogue_scope(db, actor, "programme_memberships", m.ProgrammeMembership))))
    if restricted:
        ids &= {r.person_id for r in programme_members}
    if programme_id:
        ids &= {r.person_id for r in programme_members if r.programme_id == programme_id}
    if person_id:
        ids &= {person_id}
    require(len(ids) <= 2000, "CALENDAR_LIMIT", "Filter the calendar to at most 2,000 people", 409)
    people = [{"id": p.id, "display_name": p.display_name} for p in db.scalars(select(m.Person).where(m.Person.id.in_(ids)).order_by(m.Person.display_name, m.Person.id))]
    events, sessions = [], []

    def append(event):
        if event['kind'] == 'duty' and ((service_id and service_id not in event['service_ids'])
                or (location_id and location_id != event['location_id'])
                or (mode and not any(s['mode'] == mode for s in event['services']))):
            return
        if event["person_id"] in ids and overlaps(lo, hi, aware(event["start"]), aware(event["end"])):
            events.append(event)
            require(len(events) <= 10000, "CALENDAR_LIMIT", "Narrow the calendar period or people filter", 409)

    draft = scoped(db, m.Workspace, workspace_id, actor) if workspace_id else None
    # Each rota on the days it is in force, read for this window only (Shift rows, not snapshots: design §9); one being
    # replaced from the shown draft's first day ends there.
    pubs = [p for p in current_publications(db, [actor.department_id], start, end, light=True)
            if not draft or p.id != draft.base_publication_id and p.workspace_id != draft.id]
    window = published_shifts(db, pubs, org.timezone, start, end,
                              {draft.replaces_publication_id: draft.starts_on} if draft and draft.replaces_publication_id else None,
                              person_id=person_id if not team else None)
    # Clinic and place names as issued with each rota: only those two small parts of the snapshot are read.
    P = m.Publication
    issued = {i: (sv or {}, lc or {}) for i, sv, lc in db.execute(select(P.id, P.snapshot["services"], P.snapshot["locations"]).where(
        P.id.in_([p.id for p in pubs])))} if pubs else {}
    sources = [([s for s in window if s['publication_id'] == p.id], "published", p.id, *issued.get(p.id, ({}, {}))) for p in pubs]
    if draft:
        from .leave_disclosure import overlap_labels
        shifts = overlap_labels(db, actor, project_series_shifts(db, draft, workspace_shifts(db, draft.id)))
        services = {r.id: record(r) for r in db.scalars(select(m.Service).where(m.Service.id.in_(
            {sid for shift in shifts for sid in shift['service_ids']})))}
        locations = {r.id: record(r) for r in db.scalars(select(m.Location).where(m.Location.id.in_(
            {shift['location_id'] for shift in shifts})))}
        sources.append((shifts, draft.status, draft.id, services, locations))
    from .people import duty_ranks
    from packages.domain.supervision_teams import team_facts
    ranked = duty_ranks(db, actor.department_id, [s for shifts, *_ in sources for s in shifts], org.timezone)
    ranked_by_id = {s['id']: s for s in ranked}
    for shifts, state, source, services, locations in sources:
        shifts = [ranked_by_id[s['id']] for s in shifts]
        for shift in shifts:
            service_labels = [{'id': sid, 'name': services.get(sid, {}).get('name', 'Service not recorded'),
                               'mode': services.get(sid, {}).get('mode', 'unspecified')} for sid in shift['service_ids']]
            if (team and overlaps(lo, hi, aware(shift['start']), aware(shift['end']))
                    and (not service_id or service_id in shift['service_ids'])
                    and (not location_id or location_id == shift['location_id'])
                    and (not mode or any(s['mode'] == mode for s in service_labels))
                    and (not (person_id or programme_id or category_id or restricted)
                         or any(p['person_id'] in ids for p in shift['participants']))):
                sessions.append({'shift_id': shift['id'], 'source_id': source, 'state': state,
                    'label': shift['type_snapshot']['name'], 'start': shift['start'], 'end': shift['end'],
                    'location_id': shift['location_id'], 'location_name': locations.get(shift['location_id'], {}).get('name', 'Location not recorded'),
                    'services': service_labels, 'assigned_people': 0 if inactive(shift) else len(shift['participants']),
                    'cancelled': shift['cancelled'], 'cancellation_reason': shift.get('cancellation_reason'),
                    'closed': shift.get('closed', False), 'closure_reason': shift.get('closure_reason')})
                if shift.get('double_booking_conflicts'):
                    sessions[-1]['double_booking_conflicts'] = shift['double_booking_conflicts']
                require(len(sessions) <= 5000, 'CALENDAR_LIMIT', 'Narrow the session date range', 409)
            if inactive(shift):
                continue  # A closure is visible to coordinators, never scheduled work.
            teams = team_facts(shift["participants"])
            for participant in shift["participants"]:
                append({"id": participant["id"], "person_id": participant["person_id"], "kind": "duty",
                    "label": shift["type_snapshot"]["name"], "start": participant["start"], "end": participant["end"],
                    "colour": shift["type_snapshot"].get("definition", {}).get("colour", ""),
                    "state": state, "source_id": source, "shift_id": shift["id"],
                    "service_ids": shift["service_ids"], "location_id": shift["location_id"],
                    "location_name": locations.get(shift['location_id'], {}).get('name', 'Location not recorded'),
                    "services": service_labels,
                    "participation": participant.get("participation", "staff"),
                    "supervisor_person_id": participant.get("supervisor_person_id"),
                    **teams[participant["id"]],
                    "team_lead": participant.get("team_lead", False),
                    "overtime": participant.get("overtime", False)})
    rotations = list(db.scalars(select(m.Rotation).where(m.Rotation.department_id == actor.department_id,
        m.Rotation.person_id.in_(ids), m.Rotation.starts_on < end, m.Rotation.ends_on > start,
        catalogue_scope(db, actor, "rotations", m.Rotation))))
    services = dict(db.execute(select(m.Service.id, m.Service.name).where(m.Service.id.in_(
        {r.service_id for r in rotations if r.service_id}))).all()) if rotations else {}
    rotation_types = dict(db.execute(select(m.RotationType.id, m.RotationType.name).where(m.RotationType.id.in_(
        {r.rotation_type_id for r in rotations if r.rotation_type_id}))).all()) if rotations else {}
    for r in rotations:
        rotation_start, rotation_end = local_date_span(r.starts_on, r.ends_on, org.timezone)
        append({"id": r.id, "person_id": r.person_id, "kind": "rotation", "label": rotation_types.get(r.rotation_type_id) or services.get(r.service_id) or "Rotation",
            "start": rotation_start.isoformat(), "end": rotation_end.isoformat(),
            "state": "Assignable" if r.department_available else "Away · unavailable"})
    unavailable = list(db.scalars(select(m.Availability).where(m.Availability.department_id == actor.department_id,
        m.Availability.person_id.in_(ids), m.Availability.start < hi, m.Availability.end > lo, m.Availability.retracted.is_(False),
        span_scope(db, actor, m.Availability) if restricted else True)))
    report_ids = {row.request_id for row in unavailable if row.request_id}
    reports = {row.id: row for row in db.scalars(select(m.RequestRecord).where(
        m.RequestRecord.department_id == actor.department_id, m.RequestRecord.id.in_(report_ids),
        m.RequestRecord.kind == 'absence_report'))} if report_ids else {}
    names = {r['id']: r['leave_type_name'] for r in leave_types.names(db, actor.department_id, [record(r) for r in unavailable])}
    department = db.get(m.Department, actor.department_id)
    zone = ZoneInfo(org.timezone)
    for r in unavailable:
        report = reports.get(r.request_id)
        wording = leave_wording(department, actor, report or r, names[r.id])
        if report:
            label = wording.get('label') or ABSENCE_CATEGORY_LABELS.get(
                wording.get('absence_reason'), wording.get('absence_reason', ABSENCE_REPORT_LABEL))
            shown = display('details' if wording.get('note') else 'type' if wording else 'none', label,
                            aware(r.start), aware(r.end), wording.get('note'), zone)
            wording = {**wording, 'label': label}
        else:
            shown = display('details' if wording['leave_type_name'] else 'none', names[r.id], aware(r.start), aware(r.end),
                            wording.get('note'), zone)
        append(with_leave({"id": r.id, "person_id": r.person_id, "kind": "availability", **wording,
            'counts_as_unavailable': r.counts_as_unavailable,
            "start": aware(r.start).isoformat(), "end": aware(r.end).isoformat(), "state": "Recorded"}, shown))
    academic = []
    entry_query = select(m.ProgrammeCalendarEntry).where(
        m.ProgrammeCalendarEntry.department_id == actor.department_id,
        m.ProgrammeCalendarEntry.starts_on < end, m.ProgrammeCalendarEntry.ends_on > start,
        catalogue_scope(db, actor, "programme_calendar_entries", m.ProgrammeCalendarEntry))
    if programme_id:
        entry_query = entry_query.where(m.ProgrammeCalendarEntry.programme_id == programme_id)
    if not team or person_id or category_id:
        entry_query = entry_query.where(m.ProgrammeCalendarEntry.programme_id.in_(
            {r.programme_id for r in programme_members if r.person_id in ids}))
    entries = list(db.scalars(entry_query.order_by(m.ProgrammeCalendarEntry.starts_on, m.ProgrammeCalendarEntry.id).limit(501)))
    require(len(entries) <= 500, 'CALENDAR_LIMIT', 'Narrow the programme calendar period', 409)
    programmes = dict(db.execute(select(m.Programme.id, m.Programme.name).where(m.Programme.id.in_(
        {entry.programme_id for entry in entries}))).all()) if entries else {}
    members_by_programme = defaultdict(list)
    for member in programme_members:
        if member.person_id in ids:
            members_by_programme[member.programme_id].append(member)
    full_members = defaultdict(lambda: defaultdict(list))
    if team and entries:
        # A narrow calendar window must still assess membership over each whole academic entry.
        for member in db.scalars(select(m.ProgrammeMembership).where(
                m.ProgrammeMembership.department_id == actor.department_id,
                m.ProgrammeMembership.programme_id.in_(programmes), m.ProgrammeMembership.person_id.in_(ids),
                m.ProgrammeMembership.starts_on < max(e.ends_on for e in entries),
                m.ProgrammeMembership.ends_on > min(e.starts_on for e in entries),
                catalogue_scope(db, actor, "programme_memberships", m.ProgrammeMembership))):
            full_members[member.programme_id][member.person_id].append((member.starts_on, member.ends_on))
    for entry in entries:
        if programme_id and entry.programme_id != programme_id:
            continue
        members = [r for r in members_by_programme[entry.programme_id]
                   if r.starts_on < min(end, entry.ends_on) and r.ends_on > max(start, entry.starts_on)]
        if (not team or person_id or category_id) and not members:
            continue
        item = record(entry)
        item['programme_name'] = programmes[entry.programme_id]
        if team:
            item['confirmed_members'] = sum(membership_covers(intervals, entry.starts_on, entry.ends_on)
                for intervals in full_members[entry.programme_id].values())
        academic.append(item)
        require(len(academic) <= 500, 'CALENDAR_LIMIT', 'Narrow the programme calendar period', 409)
    # Names of the people paired with a shown duty (supervisor or trainees), also for staff: only their own pairs.
    paired = {q for e in events if e["kind"] == "duty" for q in [e["supervisor_person_id"], *e["trainee_ids"]] if q}
    pair_names = {i: n for i, n in db.execute(select(m.Person.id, m.Person.display_name).where(m.Person.id.in_(paired)))} if paired else {}
    return {"starts_on": start.isoformat(), "ends_on": end.isoformat(), "timezone": org.timezone,
        "people": people, "pair_names": pair_names, "events": sorted(events, key=lambda e: (e["start"], e["person_id"], e["id"])),
        "sessions": sessions,
        "date_set_markers": date_markers(db, actor.department_id, start, end),
        "programme_calendar": academic,
        "team_view": team, "input_revision": org.input_revision, "availability_revision": org.availability_revision}
