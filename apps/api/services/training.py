"""Residency: the rotation board, rotation plans (from a template or blocks), copying a year forward, rotation change
requests and programme leads' notes on trainees.

A trainee is anyone with a programme membership in the department. Programme leads manage only their led programmes."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, false, func, or_, select, true, tuple_
from sqlalchemy.orm import lazyload
from packages.contracts import schemas as s
from packages.domain.time import aware, local_midnight
from apps.api import models as m
from apps.api.auth import CATALOGUE_READ_ROLES, require_catalogue_read, require_role, person_access
from apps.api.errors import Problem, require
from apps.api.serialization import record
from .common import scoped, expected, bump_inputs, history, history_many, outbox, range_overlaps, local_date_span

BOARD_ROLES = ("admin", "coordinator", "approver", "programme_lead", "auditor")
ROTATION_FIELDS = ("rotation_type_id", "service_id", "starts_on", "ends_on")
MAX_BOARD_DAYS = 731


def led_programmes(db, actor):
    """Owned programmes plus existing current membership authority. None: no limit for other reading roles."""
    if not actor.permits("programme_lead") or actor.permits("admin", "coordinator", "approver", "auditor"):
        return None
    return lead_programmes(db, actor)


def lead_programmes(db, actor, *, day=None):
    """Director-only authority; additional reading roles never broaden a director's writes or note access."""
    if not actor.permits("programme_lead"):
        return select(m.Programme.id).where(false())
    from . import independence
    M, day = m.ProgrammeMembership, day or independence.today(db, actor.department_id)
    memberships = select(M.programme_id).where(M.withdrawn.is_(False), M.department_id == actor.department_id, M.person_id == actor.person_id,
                                              M.starts_on <= day, M.ends_on > day, true() if actor.person_id else false())
    return select(m.Programme.id).where(m.Programme.department_id == actor.department_id,
        or_(m.Programme.owner_identity_id == actor.identity_id, m.Programme.id.in_(memberships)))


def trainees(department_id, on=None, until=None, programmes=None):
    """People with a programme membership in the department (overlapping [on, until), in `programmes` when given): a subquery."""
    M = m.ProgrammeMembership
    window = [M.ends_on > on] if on else []
    window += [M.starts_on < until] if until else []
    window += [M.programme_id.in_(programmes)] if programmes is not None else []
    return select(M.person_id).where(M.withdrawn.is_(False), M.department_id == department_id, *window)


def is_trainee(db, department_id, person_id, on=None, until=None, programmes=None):
    return db.scalar(trainees(department_id, on, until, programmes).where(m.ProgrammeMembership.person_id == person_id).limit(1)) is not None


def note_trainees(db, actor):
    from .independence import today
    day = today(db, actor.department_id)
    return trainees(actor.department_id, on=day, programmes=lead_programmes(db, actor)).where(
        m.ProgrammeMembership.starts_on <= day)


def leads(db, actor, person_id, on=None, until=None):
    """This programme lead leads this trainee over [on, until) (availability.may_decide, protected time)."""
    if not actor.permits("programme_lead"):
        return False
    if on is None or until is None:
        return db.scalar(note_trainees(db, actor).where(m.ProgrammeMembership.person_id == person_id).limit(1)) is not None
    return any(pid == person_id and a <= on and until <= b for pid, a, b in _lead_intervals(db, actor))


def _lead_intervals(db, actor):
    from .catalogue import programme_memberships
    allowed = set(db.scalars(lead_programmes(db, actor)))
    rows = sorted((row.person_id, row.starts_on, row.ends_on) for row in programme_memberships(db, actor, allowed))
    spans = []
    for pid, a, b in rows:
        if spans and spans[-1][0] == pid and a <= spans[-1][2]:
            previous = spans[-1]
            spans[-1] = (pid, previous[1], max(previous[2], b))
        else:
            spans.append((pid, a, b))
    return spans


def request_scope(db, actor):
    """The same director request boundary for reads, counts and decisions, using exclusive local-midnight ends."""
    R = m.RequestRecord
    rotations = select(m.Rotation.id).join(m.Cohort, m.Cohort.id == m.Rotation.cohort_id).where(m.Rotation.withdrawn.is_(False),
        m.Rotation.department_id == actor.department_id, m.Cohort.department_id == actor.department_id,
        m.Cohort.programme_id.in_(lead_programmes(db, actor)))
    peer = R.details["swap"]["rotation_id"].as_string()
    peer_rotations = rotations.where(span_scope(db, actor, R, person_id=R.details["swap"]["person_id"].as_string()))
    return and_(span_scope(db, actor, R), or_(R.kind == "leave",
        and_(R.kind == "rotation_change", R.rotation_id.in_(rotations),
             or_(peer.is_(None), peer.in_(peer_rotations)))))


def change_person_scope(person_ids):
    """A rotation swap is about both trainees, while retaining one request and decision."""
    R = m.RequestRecord
    return or_(R.person_id.in_(person_ids), R.details["swap"]["person_id"].as_string().in_(person_ids))


def change_parts(row, rotation_people):
    """Project only visible rotation halves; both keep the same request revision for decisions."""
    details = row.details or {}
    parts = [(row.rotation_id, row.person_id, details.get("proposed", {}))]
    if swap := details.get("swap"):
        parts.append((swap["rotation_id"], swap["person_id"], swap["proposed"]))
    result = []
    for identifier, person_id, proposed in parts:
        if rotation_people.get(identifier) != person_id:
            continue
        part = {"id": row.id, "revision": row.revision, "rotation_id": identifier,
                "person_id": person_id, "proposed": proposed, "created_by": row.created_by}
        if len(parts) == 2:
            other, other_person, _ = next(part for part in parts if part[0] != identifier)
            if rotation_people.get(other) == other_person:
                part["swap_rotation_id"] = other
        result.append(part)
    return result


def change_rotation_people(db, actor, changes, people_ids, programme_id=None):
    """Load authorised request targets even when their current dates fall outside the board window."""
    changes = [row for row in changes if row.kind == "rotation_change"]
    ids = {q.rotation_id for q in changes} | {
        q.details["swap"]["rotation_id"] for q in changes if (q.details or {}).get("swap")}
    R = m.Rotation
    return dict(db.execute(select(R.id, R.person_id).where(R.department_id == actor.department_id,
        R.id.in_(ids), R.person_id.in_(people_ids), catalogue_scope(db, actor, "rotations", R),
        R.cohort_id.in_(select(m.Cohort.id).where(m.Cohort.programme_id == programme_id))
        if programme_id else true())).all()) if ids else {}


def span_scope(db, actor, cls, *, person_id=None):
    """Dated trainee records must be wholly within the director's programme membership intervals."""
    tz = zone(db, actor)
    subject = cls.person_id if person_id is None else person_id
    spans = [and_(subject == pid, cls.start >= lo, cls.end <= hi)
             for pid, a, b in _lead_intervals(db, actor) for lo, hi in [local_date_span(a, b, tz)]]
    return or_(*spans) if spans else false()


def rotation_records_access(db, actor, records, *, scope=None):
    """Reauthorize snapshot history and the original command scope, including skipped rotations."""
    if actor.permits("admin", "coordinator"):
        return
    message = "These rotation snapshots are outside your current programme authority"
    require(scope is None or isinstance(scope, list) and bool(scope) and all(
        isinstance(c, dict) and isinstance(c.get("cohort_id"), str) and isinstance(c.get("programme_id"), str)
        for c in scope), "FORBIDDEN", message, 403)
    if not records and scope is None:
        return
    require_role(actor, "programme_lead")
    allowed = set(db.scalars(lead_programmes(db, actor)))
    cohorts = dict(db.execute(select(m.Cohort.id, m.Cohort.programme_id).where(
        m.Cohort.department_id == actor.department_id,
        m.Cohort.id.in_({r["cohort_id"] for r in records} | {c["cohort_id"] for c in scope or ()}))).all())
    H = m.DefinitionHistory
    original = {(oid, rev): programme for oid, rev, programme in db.execute(select(
        H.object_id, H.object_revision, H.snapshot["programme_id"].as_string()).where(
            H.department_id == actor.department_id, H.kind == "rotations",
            tuple_(H.object_id, H.object_revision).in_({(r["id"], r["revision"]) for r in records})))}
    require(all(cohorts.get(r["cohort_id"]) in allowed and original.get((r["id"], r["revision"])) in allowed for r in records)
            and all(cohorts.get(c["cohort_id"]) in allowed and c["programme_id"] in allowed for c in scope or ()),
            "FORBIDDEN", message, 403)


def rotation_person_scope(person_id):
    return or_(m.Rotation.person_id == person_id, m.Rotation.attachment_mentor_id == person_id)


def catalogue_scope(db, actor, kind, cls):
    """Scope trainee-specific catalogue data; keep the people directory shared for rota references."""
    if kind == "rotations" and not actor.permits(*CATALOGUE_READ_ROLES):
        if not actor.permits("staff") or not actor.person_id:
            return false()
        return rotation_person_scope(actor.person_id)
    programmes = led_programmes(db, actor)
    if programmes is None:
        return true()
    if kind == "programmes":
        return cls.id.in_(programmes)
    if kind == "rotations":
        return cls.cohort_id.in_(select(m.Cohort.id).where(m.Cohort.department_id == actor.department_id,
                                                        m.Cohort.programme_id.in_(programmes)))
    if kind in {"cohorts", "programme_memberships", "programme_calendar_entries", "rotation_templates", "mentorships"}:
        scope = cls.programme_id.in_(programmes)
        if kind == "rotation_templates":
            scope = or_(scope, cls.programme_id.is_(None))  # shared reference templates grant no write authority
        return scope
    return true()


def person_rotations(db, actor, person_id, start, end):
    require_catalogue_read(actor, "rotations")
    try:
        person_access(db, actor, person_id)
    except Problem:
        raise Problem(403, "FORBIDDEN", "You cannot read this person's rotations") from None
    require(start < end and (end - start).days <= 400, "VALUE_INVALID",
            "Ask for 1 to 400 days (end is the day after the last)")
    R = m.Rotation
    rows = db.scalars(select(R).where(R.department_id == actor.department_id,
        rotation_person_scope(person_id), R.starts_on < end, R.ends_on > start,
        catalogue_scope(db, actor, "rotations", R)).order_by(R.starts_on, R.id))
    return {"rotations": [record(r) for r in rows]}


def note_access(db, actor, person_id):
    require_role(actor, "programme_lead")
    person_access(db, actor, person_id)
    require(person_id != actor.person_id, "FORBIDDEN", "You cannot access notes on yourself", 403)
    require(actor.permits("admin") or leads(db, actor, person_id),
            "FORBIDDEN", "Only a director of this trainee's programme can access their notes", 403)


def note_summary(db, actor, people_ids):
    notes, note_person_ids = {}, set()
    if actor.permits("programme_lead") and people_ids:
        note_person_ids = set(people_ids) if actor.permits("admin") else set(db.scalars(note_trainees(db, actor).where(
            m.ProgrammeMembership.person_id.in_(people_ids))))
        note_person_ids -= {actor.person_id}
    if note_person_ids:
        N = m.TraineeNote
        notes = dict(db.execute(select(N.person_id, func.count()).where(N.department_id == actor.department_id,
            N.retracted.is_(False), N.person_id.in_(note_person_ids)).group_by(N.person_id)).all())
    return notes, note_person_ids


def rotation_choices(db, actor):
    return {kind: dict(db.execute(select(cls.id, cls.name).where(cls.department_id == actor.department_id,
        cls.active.is_(True)).order_by(cls.name, cls.id)).all())
        for kind, cls in (("rotation_types", m.RotationType), ("services", m.Service))}


def zone(db, actor):
    return db.get(m.Organisation, actor.organisation_id).timezone


# ---- The rotation board (GET /api/rotation-board) ----

def board(db, actor, start, end, programme_id=None):
    """Trainees, their memberships, rotations, leave and waiting rotation changes overlapping [start, end).
    Staff read only their own; planning roles read the department. Leave carries its type only as the department's
    leave display allows planners (leave_disclosure), and never a reason."""
    require_role(actor, "staff", *BOARD_ROLES)
    require(end > start and (end - start).days <= MAX_BOARD_DAYS, "BOARD_RANGE", "Choose up to two years")
    team = actor.permits(*BOARD_ROLES)
    require(team or actor.person_id, "FORBIDDEN", "Your account is not linked to a person", 403)
    dep = actor.department_id
    M, R = m.ProgrammeMembership, m.Rotation
    led = led_programmes(db, actor)
    if programme_id:
        scoped(db, m.Programme, programme_id, actor)
    mine = [M.programme_id.in_(led), *([M.person_id != actor.person_id] if actor.person_id else [])] if led is not None else [] if team else [M.person_id == actor.person_id]
    memberships = list(db.scalars(select(M).where(M.department_id == dep, M.starts_on < end, M.ends_on > start, *mine,
                                                  *([M.programme_id == programme_id] if programme_id else [])).order_by(M.starts_on)))
    rot_mine = ([R.person_id.in_({x.person_id for x in memberships})] if led is not None else []) if team else [R.person_id == actor.person_id]
    rotations = list(db.scalars(select(R).where(R.department_id == dep, R.starts_on < end, R.ends_on > start, *rot_mine,
                                                catalogue_scope(db, actor, "rotations", R))
                                .order_by(R.starts_on, R.id)))
    cohorts = {c.id: c for c in db.scalars(select(m.Cohort).where(m.Cohort.department_id == dep))}
    if programme_id:
        rotations = [r for r in rotations if getattr(cohorts.get(r.cohort_id), "programme_id", None) == programme_id]
    people_ids = {x.person_id for x in memberships} | {r.person_id for r in rotations}
    people = {p.id: p for p in db.scalars(select(m.Person).where(m.Person.id.in_(people_ids)))} if people_ids else {}
    tz = zone(db, actor)
    lo, hi = local_date_span(start, end, tz)
    leave = []
    if people_ids:
        from . import leave_types
        department = db.get(m.Department, dep)
        typed = not team or (department.leave_reason_display != "none" and actor.permits(*BOARD_ROLES))
        A = m.Availability
        rows = [dict(person_id=a.person_id, start=a.start, end=a.end, leave_type_id=a.leave_type_id, leave_type=a.leave_type, kind=a.kind)
                for a in db.scalars(select(A).where(A.department_id == dep, A.person_id.in_(people_ids), A.retracted.is_(False),
                                                    A.kind.in_(leave_types.LEAVE_KINDS + ("protected_time",)), A.start < hi, A.end > lo,
                                                    span_scope(db, actor, A) if led is not None else true()))]
        for row in leave_types.names(db, dep, rows):
            leave.append(_day_span(row, tz, "approved", row["leave_type_name"] if typed or row["kind"] == "protected_time" else "Leave"))
        Q = m.RequestRecord
        for q in db.scalars(select(Q).where(Q.department_id == dep, Q.person_id.in_(people_ids), Q.kind == "leave",
                                            Q.status == "submitted", Q.start < hi, Q.end > lo,
                                            request_scope(db, actor) if led is not None else true())):
            leave.append({**_day_span({"person_id": q.person_id, "start": q.start, "end": q.end, "kind": "leave"}, tz, "waiting",
                                      "Leave request"), "request_id": q.id})
    changes = list(db.scalars(select(m.RequestRecord).where(m.RequestRecord.department_id == dep, m.RequestRecord.kind == "rotation_change",
                                                                m.RequestRecord.status == "submitted",
                                                                change_person_scope(people_ids),
                                                                request_scope(db, actor) if led is not None else true(),
                                                                or_(and_(m.RequestRecord.start < hi, m.RequestRecord.end > lo),
                                                                    m.RequestRecord.rotation_id.in_({r.id for r in rotations}))))) if people_ids else []
    from .availability import _decidable
    decidable = _decidable(db, actor, [("request", q) for q in changes])
    rotation_people = change_rotation_people(db, actor, changes, people_ids, programme_id)
    changes = [{**part, "can_decide": q.id in decidable}
               for q in changes for part in change_parts(q, rotation_people)]
    notes, note_person_ids = note_summary(db, actor, people_ids)
    refs = {"programmes": (m.Programme, {x.programme_id for x in memberships} | {c.programme_id for c in cohorts.values() if c.id in {r.cohort_id for r in rotations}}),
            "cohorts": (m.Cohort, {r.cohort_id for r in rotations}),
            "rotation_types": (m.RotationType, {r.rotation_type_id for r in rotations} | {(c["proposed"] or {}).get("rotation_type_id") for c in changes}),
            "services": (m.Service, {r.service_id for r in rotations} | {(c["proposed"] or {}).get("service_id") for c in changes})}
    names = {kind: dict(db.execute(select(cls.id, cls.name).where(cls.department_id == dep, cls.id.in_(ids - {None}))).all()) if ids - {None} else {}
             for kind, (cls, ids) in refs.items()}
    choices = rotation_choices(db, actor)
    return {"start": start.isoformat(), "end": end.isoformat(), "names": names, "choices": choices,
            "people": [{"id": p.id, "display_name": p.display_name, "short_code": p.short_code, "notes": notes.get(p.id, 0)}
                       for p in sorted(people.values(), key=lambda p: (p.display_name.casefold(), p.id))],
            "memberships": [record(x) for x in memberships], "rotations": [record(r) for r in rotations],
            "leave": sorted(leave, key=lambda x: (x["starts_on"], x["person_id"])), "changes": changes,
            "can_plan": actor.permits("admin", "coordinator", "programme_lead"), "can_note": actor.permits("programme_lead"),
            "note_person_ids": sorted(note_person_ids)}


def _day_span(row, tz, stage, label):
    zone_info = ZoneInfo(tz)
    try:
        first = aware(row["start"]).astimezone(zone_info).date()
        end = (aware(row["end"]) - timedelta(microseconds=1)).astimezone(zone_info).date() + timedelta(days=1)
    except (ValueError, OverflowError):
        require(False, "INTERVAL_DATES", "The recorded interval exceeds the supported calendar dates")
    return {"person_id": row["person_id"], "starts_on": first.isoformat(), "ends_on": end.isoformat(),
            "stage": stage, "label": label, "kind": "protected_time" if row.get("kind") == "protected_time" else "leave"}


# ---- Plans: create, copy forward, undo, remove ----

def _block_end(start, block):
    try:
        return start + (timedelta(weeks=block.length) if block.unit == "weeks" else relativedelta(months=block.length))
    except (ValueError, OverflowError):
        require(False, "ROTATION_DATES", "The plan exceeds the supported date range")


def _create_rotation(db, actor, value, context, reason):
    from .catalogue import sync_rotation_attachment, validate_references
    value = s.RotationInput.model_validate(value)
    conflicts = validate_references(db, actor, "rotations", value, rotation_context=context)
    context.setdefault('leave_conflicts', []).extend(conflicts or [])
    values = value.model_dump()
    mentor_id = values.pop('mentor_id')
    row = m.Rotation(department_id=actor.department_id, attachment_link=None, **values)
    db.add(row)
    if mentor_id:
        sync_rotation_attachment(db, actor, row, mentor_id, reason)
    return row


def _finish_rotations(db, actor, rows, reason, context=None):
    if rows:
        cohort_programmes = {row.id: row.programme_id for row in (context or {}).get('references', ())
                             if isinstance(row, m.Cohort)}
        history_many(db, actor, [("rotations", row, reason) for row in rows],
                     cohort_programmes=cohort_programmes)
        bump_inputs(db, actor)
    return [record(row) for row in rows]


def _rotation_context(db, actor, people, types, services, cohorts, mentors=(), preloaded=()):
    # Keep references alive: SQLAlchemy's identity map otherwise releases unchanged rows between blocks.
    refs = list(preloaded)
    held = {(type(row), row.id) for row in refs}
    refs.extend(row for cls, ids in ((m.Person, set(people) | set(mentors)), (m.RotationType, types),
            (m.Service, services), (m.Cohort, cohorts)) if ids
            for row in db.scalars(select(cls).where(cls.id.in_(set(ids) - {ident for kind, ident in held if kind is cls}))))
    M = m.ProgrammeMembership
    memberships = {}
    for pid, programme, start, end in db.execute(select(M.person_id, M.programme_id, M.starts_on, M.ends_on).where(
            M.department_id == actor.department_id, M.person_id.in_(people)).order_by(M.starts_on)):
        memberships.setdefault((pid, programme), []).append((start, end))
    return {"references": refs, "memberships": memberships, "taken": _taken(db, actor, people)}


def _taken(db, actor, person_ids):
    taken = {}
    for r in db.scalars(select(m.Rotation).where(m.Rotation.department_id == actor.department_id, m.Rotation.person_id.in_(person_ids))):
        taken.setdefault(r.person_id, []).append((r.starts_on, r.ends_on))
    return taken


def plan(db, actor, command):
    require_role(actor, "admin", "coordinator", "programme_lead")
    value = s.RotationPlanInput.model_validate(command.payload)
    cohort = scoped(db, m.Cohort, value.cohort_id, actor)
    from .catalogue import programme_access
    programme_access(db, actor, cohort.programme_id)
    if value.template_id:
        template = scoped(db, m.RotationTemplate, value.template_id, actor)
        require(template.active and template.programme_id in (None, cohort.programme_id),
                "ROTATION_TEMPLATE", "Choose an active template for this programme")
    require(len(set(value.person_ids)) == len(value.person_ids), "DUPLICATE_PEOPLE", "Each person can be planned once")
    context = _rotation_context(db, actor, value.person_ids, {b.rotation_type_id for b in value.blocks} - {None},
                                {b.service_id for b in value.blocks} - {None}, {cohort.id})
    for pid in value.person_ids:
        person_access(db, actor, pid)
    for block in value.blocks:
        if block.rotation_type_id:
            scoped(db, m.RotationType, block.rotation_type_id, actor)
        if block.service_id:
            scoped(db, m.Service, block.service_id, actor)
    taken = context["taken"]
    created, skipped = [], []
    n = len(value.blocks)
    for i, pid in enumerate(value.person_ids):
        order = value.blocks[i % n:] + value.blocks[:i % n] if value.stagger else value.blocks
        start = value.starts_on
        for block in order:
            end = _block_end(start, block)
            if start < cohort.starts_on or end > cohort.ends_on:
                skipped.append({"person_id": pid, "starts_on": start.isoformat(), "ends_on": end.isoformat(), "why": "outside_cohort"})
            elif any(range_overlaps(a, b, start, end) for a, b in taken.get(pid, ())):
                skipped.append({"person_id": pid, "starts_on": start.isoformat(), "ends_on": end.isoformat(), "why": "overlap"})
            else:
                created.append(_create_rotation(db, actor, {"person_id": pid, "cohort_id": cohort.id, "starts_on": start.isoformat(),
                    "ends_on": end.isoformat(), "rotation_type_id": block.rotation_type_id, "service_id": block.service_id,
                    "department_available": block.department_available}, context, value.reason))
                taken.setdefault(pid, []).append((start, end))
            start = end
    return {"created": _finish_rotations(db, actor, created, value.reason, context), "skipped": skipped,
            "rotation_scope": [{"cohort_id": cohort.id, "programme_id": cohort.programme_id}],
            'conflicts': context.get('leave_conflicts', []),
            'warnings': list(dict.fromkeys(f['message'] for f in context.get('leave_conflicts', []))),
            "message": f"{len(created)} rotation{'s' if len(created) != 1 else ''} planned"
                       + (f"; {len(skipped)} block{'s' if len(skipped) != 1 else ''} skipped (overlap or outside the cohort)" if skipped else "") + "."
                       + (' Warning: ' + ' '.join(dict.fromkeys(f['message'] for f in context.get('leave_conflicts', []))) if context.get('leave_conflicts') else '')}


def copy_forward(db, actor, command):
    require_role(actor, "admin", "coordinator", "programme_lead")
    value = s.RotationCopyInput.model_validate(command.payload)
    target = scoped(db, m.Cohort, value.cohort_id, actor) if value.cohort_id else None
    from .catalogue import programme_access
    if target:
        programme_access(db, actor, target.programme_id)
    if value.programme_id:
        scoped(db, m.Programme, value.programme_id, actor)
        programme_access(db, actor, value.programme_id)
    require(len(set(value.person_ids)) == len(value.person_ids), "DUPLICATE_PEOPLE", "Choose each person once")
    R = m.Rotation
    query = select(R).where(R.department_id == actor.department_id, R.starts_on >= value.starts_on, R.starts_on < value.ends_on)
    if value.person_ids:
        query = query.where(R.person_id.in_(value.person_ids))
    if value.programme_id:
        query = query.where(R.cohort_id.in_(select(m.Cohort.id).where(m.Cohort.programme_id == value.programme_id)))
    if not actor.permits("admin", "coordinator"):
        query = query.where(R.cohort_id.in_(select(m.Cohort.id).where(m.Cohort.department_id == actor.department_id,
            m.Cohort.programme_id.in_(lead_programmes(db, actor)))))
    # The default select-in attachment relationship adds a second query even
    # when no copied rotation has an attachment.  Select the live mentor id in
    # the source query and keep the relationship unloaded; this preserves
    # attached rotations without a per-command relationship read.
    source_rows = list(db.execute(query.options(lazyload(R.attachment_link)).add_columns(
        R.attachment_mentor_id.label("_attachment_mentor_id")).order_by(R.person_id, R.starts_on)))
    source = [row[0] for row in source_rows]
    source_mentors = {row[0].id: row[1] for row in source_rows}
    if not source and value.person_ids:
        known = set(db.scalars(select(m.Person.id).where(
            m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(value.person_ids))))
        require(set(value.person_ids).issubset(known), "NOT_FOUND", "Person not found", 404)
    require(source, "NOTHING_TO_COPY", "No rotations start in that period", 422)
    context = _rotation_context(db, actor, {r.person_id for r in source}, {r.rotation_type_id for r in source} - {None},
        {r.service_id for r in source} - {None}, {r.cohort_id for r in source} | ({target.id} if target else set()),
        {source_mentors.get(r.id) for r in source} - {None}, preloaded=([target] if target else []))
    # The context already holds every person with a source rotation.  Only
    # request a second batch for selected people with no source rows.
    people = {row.id: row for row in context['references'] if isinstance(row, m.Person)}
    missing = set(value.person_ids) - people.keys()
    if missing:
        people.update({row.id: row for row in db.scalars(select(m.Person).where(
            m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(missing)))})
    require(set(value.person_ids).issubset(people), "NOT_FOUND", "Person not found", 404)
    for pid in value.person_ids:
        person_access(db, actor, pid)
    taken = {pid: list(spans) for pid, spans in context["taken"].items()}
    shift = relativedelta(months=value.months)
    # _rotation_context already loaded source and target cohorts and keeps them
    # strongly referenced for the command.  Reuse those rows instead of issuing
    # a second cohort query for the same IDs.
    cohorts = {c.id: c for c in context['references'] if isinstance(c, m.Cohort)}
    scope = {**cohorts, **({target.id: target} if target else {})}
    candidates, skipped = [], []
    for r in source:
        try:
            a, b = r.starts_on + shift, r.ends_on + shift
        except (ValueError, OverflowError):
            require(False, "ROTATION_DATES", "The copied rotations exceed the supported date range")
        cohort = target or cohorts[r.cohort_id]
        why = ("month_end" if b <= a else "outside_cohort" if a < cohort.starts_on or b > cohort.ends_on
               else "overlap" if any(range_overlaps(x, y, a, b) for x, y in taken.get(r.person_id, ())) else "")
        if why:
            skipped.append({"person_id": r.person_id, "starts_on": a.isoformat(), "ends_on": b.isoformat(), "why": why, "source_id": r.id})
            continue
        candidates.append({"person_id": r.person_id, "cohort_id": cohort.id, "starts_on": a.isoformat(),
            "ends_on": b.isoformat(), "rotation_type_id": r.rotation_type_id, "service_id": r.service_id,
            "mentor_id": source_mentors.get(r.id),
            "department_available": r.department_available})
        require(len(candidates) <= s.MAX_ROTATIONS_PER_PLAN, "ROTATION_COPY_LIMIT",
                f"Copy at most {s.MAX_ROTATIONS_PER_PLAN:,} rotations at once; choose fewer people or a shorter period", 422)
        taken.setdefault(r.person_id, []).append((a, b))
    created = []
    for candidate in candidates:
        created.append(_create_rotation(db, actor, candidate, context, value.reason))
        context["taken"].setdefault(candidate["person_id"], []).append((date.fromisoformat(candidate["starts_on"]), date.fromisoformat(candidate["ends_on"])))
    return {"created": _finish_rotations(db, actor, created, value.reason, context), "skipped": skipped,
            "rotation_scope": [{"cohort_id": c.id, "programme_id": c.programme_id} for c in scope.values()],
            'conflicts': context.get('leave_conflicts', []),
            'warnings': list(dict.fromkeys(f['message'] for f in context.get('leave_conflicts', []))),
            "message": f"{len(created)} rotation{'s' if len(created) != 1 else ''} copied"
                       + (f"; {len(skipped)} skipped (overlap, outside the cohort or shortened to nothing at month end)" if skipped else "") + "."
                       + (' Warning: ' + ' '.join(dict.fromkeys(f['message'] for f in context.get('leave_conflicts', []))) if context.get('leave_conflicts') else '')}


def remove(db, actor, command, *, own=False):
    """Remove rotations (Undo of a plan or copy: own=True, only ones this person created). A rotation a change request
    refers to, or one changed since it was read, is kept and counted. The removed records come back so Undo can restore them."""
    require_role(actor, "admin", "coordinator", "programme_lead")
    from .catalogue import remove_created, withdraw_records
    value = s.RotationRemoveInput.model_validate(command.payload)
    require(len({i.id for i in value.items}) == len(value.items), "DUPLICATE_ROTATIONS", "Choose each rotation once")
    rows = list(db.scalars(select(m.Rotation).where(m.Rotation.id.in_({i.id for i in value.items}))))
    cohorts = list(db.scalars(select(m.Cohort).where(m.Cohort.id.in_({r.cohort_id for r in rows}))))
    from .catalogue import programme_write_access
    for item in value.items:
        programme_write_access(db, actor, "rotations", scoped(db, m.Rotation, item.id, actor))
    targets = {i.id for i in value.items}
    R = m.RequestRecord
    peer = R.details["swap"]["rotation_id"].as_string()
    referenced = set(db.scalars(select(R.rotation_id).where(R.rotation_id.in_(targets))))
    referenced.update(db.scalars(select(peer).where(peer.in_(targets))))
    items = [i.model_dump() for i in value.items if i.id not in referenced]
    removed_records = [record(r) for r in (db.get(m.Rotation, i["id"]) for i in items) if r is not None and r.department_id == actor.department_id]
    removed_ids = []
    if own:
        removed, kept = remove_created(db, actor, m.Rotation, "rotations", items, value.reason, removed_ids=removed_ids)
    else:
        revisions = {i['id']: i['revision'] for i in items}
        removed_rows = [r for r in rows if revisions.get(r.id) == r.revision]
        withdraw_records(db, actor, 'rotations', removed_rows, value.reason, withdrawn=True)
        removed_ids = [r.id for r in removed_rows]
        removed, kept = len(removed_rows), len(items) - len(removed_rows)
    kept += len(value.items) - len(items)
    return {"removed": removed, "kept": kept, "removed_ids": removed_ids,
            'kept_records': [{'id': item.id,
                'code': 'ROTATION_HAS_REQUEST' if item.id in referenced else 'STALE_REVISION',
                'message': 'This rotation is referenced by a change request; review that request before removing it'
                           if item.id in referenced else 'This rotation changed; reload its current revision'}
                for item in value.items if item.id not in removed_ids],
            "records": [r for r in removed_records if r["id"] in removed_ids],
            **({'undo': {'kind': 'rotation.restore', 'payload': {'items': [
                {'id': r.id, 'revision': r.revision} for r in removed_rows], 'reason': value.reason}}}
               if not own and removed_rows else {}),
            "message": f"{removed} rotation{'s' if removed != 1 else ''} removed" + (f"; {kept} kept (changed since or requested)" if kept else "") + "."}


def restore(db, actor, command):
    """Restore the same rotations and attachment links, rechecking current contracts atomically."""
    require_role(actor, *s.PERMISSIONS['rotation.remove'])
    from .catalogue import programme_write_access, validate_references, withdraw_records
    value = s.RotationRemoveInput.model_validate(command.payload)
    require(len({i.id for i in value.items}) == len(value.items), 'DUPLICATE_ROTATIONS', 'Choose each rotation once')
    db.flush()  # Undo may first delete replacement rotations in this same bulk transaction.
    rows = []
    for item in value.items:
        row = scoped(db, m.Rotation, item.id, actor, include_withdrawn=True)
        programme_write_access(db, actor, 'rotations', row)
        expected(row, item.revision)
        require(row.withdrawn, 'ROTATION_NOT_REMOVED', 'This rotation is already present; reload its current revision', 409)
        validate_references(db, actor, 'rotations', _rotation_value(row), row.id)
        link = row.attachment_link
        mentor = db.get(m.Mentorship, link.mentorship_id, execution_options={'include_withdrawn': True}) if link else None
        if mentor and link.synced_revision == mentor.revision:
            validate_references(db, actor, 'mentorships', s.MentorshipInput.model_validate(
                {key: getattr(mentor, key) for key in s.MentorshipInput.model_fields}), mentor.id)
        row.withdrawn = False
        db.flush()  # Later rows see earlier restorations; any refusal rolls back the whole command.
        rows.append(row)
    withdraw_records(db, actor, 'rotations', rows, value.reason, withdrawn=False)
    return {'restored': len(rows), 'restored_ids': [r.id for r in rows], 'records': [record(r) for r in rows],
            'undo': {'kind': 'rotation.remove', 'payload': {
                'items': [{'id': r.id, 'revision': r.revision} for r in rows], 'reason': value.reason}}}


# ---- Rotation change requests (RequestRecord kind 'rotation_change') ----

def _rotation_value(rotation, **changes):
    values = {k: getattr(rotation, k) for k in ("rotation_type_id", "person_id", "cohort_id", "service_id",
                                                "department_available", "starts_on", "ends_on")}
    values['mentor_id'] = rotation.attachment_mentor_id
    values.update(changes)
    return s.RotationInput.model_validate({k: v.isoformat() if isinstance(v, date) else v for k, v in values.items()})


def _snapshot(rotation):
    return {k: getattr(rotation, k).isoformat() if isinstance(getattr(rotation, k), date) else getattr(rotation, k) for k in ROTATION_FIELDS}


def _waiting_change(db, rotation_ids):
    R = m.RequestRecord
    return db.scalar(select(R.id).where(R.status == "submitted", or_(
        R.rotation_id.in_(rotation_ids), R.details["swap"]["rotation_id"].as_string().in_(rotation_ids))).limit(1))


def _validate_rotation_pair(db, actor, rotations, proposed):
    from .catalogue import validate_references
    values = [_rotation_value(row, **change) for row, change in zip(rotations, proposed)]
    context = _rotation_context(db, actor, {r.person_id for r in rotations},
        {v.rotation_type_id for v in values} - {None}, {v.service_id for v in values} - {None},
        {v.cohort_id for v in values}, {v.mentor_id for v in values} - {None})
    for row in rotations:
        context["taken"][row.person_id].remove((row.starts_on, row.ends_on))
    for row, value in zip(rotations, values):
        validate_references(db, actor, "rotations", value, row.id, rotation_context=context)
        context["taken"].setdefault(value.person_id, []).append((value.starts_on, value.ends_on))
    return values


def request_swap(db, actor, command):
    """One governed request owns both halves; no rotation changes before independent approval."""
    from packages.domain.rotation_swap import swapped_dates
    from .catalogue import programme_access
    require_role(actor, "staff", "coordinator", "programme_lead")
    value = s.RotationSwapInput.model_validate(command.payload)
    rotations = [scoped(db, m.Rotation, identifier, actor)
                 for identifier in (value.rotation_id, value.other_rotation_id)]
    for row, revision in zip(rotations, (value.expected_revision, value.other_expected_revision)):
        expected(row, revision)
        require(actor.permits("coordinator", "programme_lead") or actor.person_id == row.person_id,
                "FORBIDDEN", "Staff may swap only their own rotations", 403)
    before = [_snapshot(row) for row in rotations]
    proposed = swapped_dates(*before)
    require(proposed != tuple(before), "NOTHING_TO_CHANGE", "Choose rotations with different dates")
    for row, change in zip(rotations, proposed):
        if actor.permits("programme_lead") and not actor.permits("admin", "coordinator"):
            programme_access(db, actor, scoped(db, m.Cohort, row.cohort_id, actor).programme_id)
            require(leads(db, actor, row.person_id, min(row.starts_on, date.fromisoformat(change["starts_on"])),
                          max(row.ends_on, date.fromisoformat(change["ends_on"]))),
                    "FORBIDDEN", "This trainee is outside your programme for these dates", 403)
    _validate_rotation_pair(db, actor, rotations, proposed)
    require(_waiting_change(db, [row.id for row in rotations]) is None,
            "CHANGE_WAITING", "A change to one of these rotations is already waiting for a decision", 409)
    first, second = rotations
    lo, hi = local_date_span(min(r.starts_on for r in rotations), max(r.ends_on for r in rotations), zone(db, actor))
    row = m.RequestRecord(department_id=actor.department_id, created_by=actor.identity_id,
        person_id=first.person_id, kind="rotation_change", counts_as_unavailable=False,
        rotation_id=first.id, start=lo, end=hi, private_reason=value.reason,
        details={"before": before[0], "proposed": proposed[0], "rotation_revision": first.revision,
                 "swap": {"rotation_id": second.id, "person_id": second.person_id,
                           "before": before[1], "proposed": proposed[1],
                          "rotation_revision": second.revision}})
    db.add(row)
    db.flush()
    outbox(db, actor, "request:" + row.id, "request", {"request_id": row.id, "person_id": row.person_id,
           "request_status": row.status, "request_kind": row.kind})
    return {"record": record(row, exclude=("private_reason",)),
            "undo": {"kind": "request.decide", "target_id": row.id, "expected_revision": row.revision,
                     "payload": {"decision": "withdraw", "reason": "Withdraw rotation swap"}},
            "message": "Rotation swap requested."}


def request_change(db, actor, command):
    require_role(actor, "staff", "coordinator", "programme_lead")
    value = s.RotationChangeInput.model_validate(command.payload)
    rotation = scoped(db, m.Rotation, value.rotation_id, actor)
    require(actor.permits("coordinator", "programme_lead") or actor.person_id == rotation.person_id,
            "FORBIDDEN", "Staff may ask to change only their own rotations", 403)
    if actor.permits("programme_lead") and not actor.permits("admin", "coordinator") and rotation.person_id != actor.person_id:
        from .catalogue import programme_access
        programme_access(db, actor, scoped(db, m.Cohort, rotation.cohort_id, actor).programme_id)
        require(leads(db, actor, rotation.person_id, value.starts_on, value.ends_on),
                "FORBIDDEN", "This trainee is outside your programme for these dates", 403)
    proposed = {"rotation_type_id": value.rotation_type_id, "service_id": value.service_id,
                "starts_on": value.starts_on.isoformat(), "ends_on": value.ends_on.isoformat()}
    require(proposed != _snapshot(rotation), "NOTHING_TO_CHANGE", "Change the type, service or dates to ask for a change")
    from .catalogue import validate_references
    validate_references(db, actor, "rotations", _rotation_value(rotation, **proposed), rotation.id)
    waiting = _waiting_change(db, [rotation.id])
    require(waiting is None, "CHANGE_WAITING", "A change to this rotation is already waiting for a decision", 409)
    tz = zone(db, actor)
    lo, hi = local_date_span(value.starts_on, value.ends_on, tz)
    row = m.RequestRecord(department_id=actor.department_id, created_by=actor.identity_id, person_id=rotation.person_id,
                          kind="rotation_change", counts_as_unavailable=False, rotation_id=rotation.id,
                          start=lo, end=hi,
                          private_reason=value.private_reason,
                          details={"proposed": proposed, "before": _snapshot(rotation), "rotation_revision": rotation.revision})
    db.add(row)
    db.flush()
    outbox(db, actor, "request:" + row.id, "request", {"request_id": row.id, "person_id": row.person_id,
           "request_status": row.status, "request_kind": row.kind})
    return {"record": record(row, exclude=("private_reason",)),
            "message": "Change requested. The rotation stays as it is until a programme lead approves."}


def change_summary(db, row, names=None):
    """A rotation change in words for the Requests list: "Emergency · Paediatrics clinic → 1 Oct – 19 Oct 2026"."""
    proposed = (row.details or {}).get("proposed", {})
    named = lambda cls, ident: (names.get("rotation_types" if cls is m.RotationType else "services", {}).get(ident, "") if names is not None else
                               getattr(db.get(cls, ident), "name", "")) if ident else ""
    what = " · ".join(x for x in (named(m.RotationType, proposed.get("rotation_type_id")), named(m.Service, proposed.get("service_id"))) if x)
    last = date.fromisoformat(proposed["ends_on"]) - timedelta(days=1) if proposed.get("ends_on") else None
    first = date.fromisoformat(proposed['starts_on']) if last else None
    when = f"{first.day} {first:%b} – {last.day} {last:%b %Y}" if last else ""
    return ("Swap dates: " if (row.details or {}).get("swap") else "Change to ") + " → ".join(x for x in (what, when) if x)


def _write_rotation(db, actor, rotation, values, reason):
    from .catalogue import sync_rotation_attachment, validate_references
    value = _rotation_value(rotation, **values)
    validate_references(db, actor, "rotations", value, rotation.id)
    for k in ROTATION_FIELDS:
        setattr(rotation, k, date.fromisoformat(values[k]) if k in ("starts_on", "ends_on") else values[k])
    rotation.revision += 1
    affected = 1 + sync_rotation_attachment(db, actor, rotation, value.mentor_id, reason)
    history(db, actor, "rotations", rotation, reason)
    bump_inputs(db, actor)
    return affected


def _write_rotation_pair(db, actor, rotations, proposed, reason):
    from .catalogue import sync_rotation_attachments
    values = _validate_rotation_pair(db, actor, rotations, proposed)
    # Stage both final states before any flush, including attachment/history writers.
    for rotation, value in zip(rotations, values):
        for key in ROTATION_FIELDS:
            setattr(rotation, key, getattr(value, key))
        rotation.revision += 1
    db.flush()
    affected = len(rotations) + sync_rotation_attachments(db, actor,
        [(rotation, value.mentor_id) for rotation, value in zip(rotations, values)], reason)
    _finish_rotations(db, actor, rotations, reason)
    return affected


def change_people(db, row):
    peer = (row.details or {}).get("swap", {}).get("person_id") if row.kind == "rotation_change" else None
    return {row.person_id} | ({peer} if peer else set())


def apply_change(db, actor, row, reason):
    """Approving a rotation change writes it to the rotation, unless the rotation changed since the request."""
    rotation = db.get(m.Rotation, row.rotation_id)
    details = dict(row.details or {})
    require(rotation is not None and rotation.revision == details.get("rotation_revision"), "ROTATION_CHANGED",
            "The rotation changed since this request; decline it and ask the trainee to request again", 409)
    if details.get("swap"):
        swap = details["swap"]
        other = scoped(db, m.Rotation, swap["rotation_id"], actor)
        require(other.revision == swap["rotation_revision"], "ROTATION_CHANGED",
                "The other rotation changed since this request; request the swap again", 409)
        affected = _write_rotation_pair(db, actor, [rotation, other],
                                       [details["proposed"], swap["proposed"]], reason)
        details["swap"] = {**swap, "applied_revision": other.revision}
    else:
        affected = _write_rotation(db, actor, rotation, details["proposed"], reason)
    row.details = {**details, "applied_revision": rotation.revision}
    return affected


def undo_change(db, actor, row):
    rotation = db.get(m.Rotation, row.rotation_id)
    details = dict(row.details or {})
    require(rotation is not None and rotation.revision == details.get("applied_revision"), "ROTATION_CHANGED",
            "The rotation changed after this approval; change it directly instead", 409)
    reason = "Undo of an approved rotation change"
    if details.get("swap"):
        swap = details["swap"]
        other = scoped(db, m.Rotation, swap["rotation_id"], actor)
        require(other.revision == swap.get("applied_revision"), "ROTATION_CHANGED",
                "The other rotation changed after this approval; change it directly instead", 409)
        affected = _write_rotation_pair(db, actor, [rotation, other], [details["before"], swap["before"]], reason)
        details["swap"] = {key: val for key, val in swap.items() if key != "applied_revision"} | {
            "rotation_revision": other.revision}
    else:
        affected = _write_rotation(db, actor, rotation, details["before"], reason)
    row.details = {k: v for k, v in details.items() if k != "applied_revision"} | {"rotation_revision": rotation.revision}
    return affected


# ---- Notes on trainees (programme leads only) ----

def notes(db, actor, person_id):
    note_access(db, actor, person_id)
    N = m.TraineeNote
    rows = db.execute(select(N, m.Identity.display_name).join(m.Identity, m.Identity.id == N.author_id)
        .where(N.department_id == actor.department_id, N.person_id == person_id, N.retracted.is_(False))
        .order_by(N.created_at.desc(), N.seq.desc()))
    out = [{**record(n), "author_name": author, "mine": n.author_id == actor.identity_id} for n, author in rows]
    return {"notes": out}


def note_command(db, actor, command):
    require_role(actor, "programme_lead")
    kind = command.kind
    if kind == "trainee_note.create":
        value = s.TraineeNoteInput.model_validate(command.payload)
        note_access(db, actor, value.person_id)
        row = m.TraineeNote(department_id=actor.department_id, person_id=value.person_id, body=value.body, author_id=actor.identity_id)
        db.add(row)
        db.flush()
        return {"record": record(row), "message": "Note saved. Only programme leads can read it."}
    row = scoped(db, m.TraineeNote, command.target_id, actor)
    note_access(db, actor, row.person_id)
    expected(row, command.expected_revision)
    require(row.author_id == actor.identity_id, "FORBIDDEN", "Only the author can change this note", 403)
    previous = record(row)
    if kind == "trainee_note.update":
        require(not row.retracted, "WORKFLOW_STATE", "This note was deleted", 409)
        row.body = s.TraineeNoteEdit.model_validate(command.payload).body
    else:
        require(row.retracted == (kind == "trainee_note.restore"), "WORKFLOW_STATE", "This note is already in that state", 409)
        row.retracted = kind == "trainee_note.delete"
    row.updated_at = m.now()
    row.revision += 1
    db.flush()
    return {"record": record(row), "previous_value": {"body": previous["body"]},
            "message": {"trainee_note.update": "Note saved.", "trainee_note.delete": "Note deleted.", "trainee_note.restore": "Note restored."}[kind]}
