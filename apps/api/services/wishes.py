"""Staff wishes: soft date, duty-type and working-pattern requests paid from a points budget per period.

Staff add their own (a coordinator may add for anyone) until a rota covering those days is submitted. No approval:
wishes are soft and budgeted. On publication each wish is marked granted, partly or declined, and declined points
go into the wish ledger, which raises that person's later wishes (up to the configured cap) until they are granted.
The solver and the score read wishes from the rota snapshot (packages/domain/patterns.py, cp_sat_patterns.py).
"""
from calendar import day_name

from packages.domain.recurrence import inactive
from datetime import date, datetime, time, timedelta
from collections import defaultdict
from math import isfinite
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from sqlalchemy import and_, delete, func, select, or_
from packages.contracts import schemas as s
from packages.domain.time import aware
from apps.api import models as m
from apps.api.auth import require_role, person_access
from apps.api.errors import require
from apps.api.serialization import record
from .common import scoped, expected, bump_inputs
from .person_record import require_person
from apps.api.services.common import neutral_rota_name

LEAVE = ("recorded_leave", "approved_leave", "actual_absence")
SETTLED = ("submitted", "approved", "published", "superseded")  # a rota in these states closes wishes on its days


def reservation_input(row):
    """Only retained settled wishes participate in other rotas' budget reservations."""
    return row.validation if row.archived_at is None and row.status in SETTLED and row.validation and 'wishes' in row.validation else None
MAX_POINTS = s.MAX_WISH_POINTS


def outcome_reasons(actor, reasons):
    """Planning evidence stays private; staff get the explanation without another person's wish or rule details."""
    if actor.permits("coordinator", "approver", "programme_lead", "auditor"):
        return reasons
    out = []
    for reason in reasons or []:
        item = {key: reason[key] for key in ("day", "kind", "code") if key in reason}
        kind = reason["kind"]
        item["message"] = {
            "rule": "A mandatory scheduling rule prevents this preference.",
            "competing_wishes": "A colleague's higher-priority preference was honoured.",
            "cover": "Your duty was needed to cover this session.",
            "tradeoff": "The run selected another combination of duties and preferences; no single blocker was established.",
        }[kind]
        if kind == "cover":
            item.update({key: reason[key] for key in ("shift_id", "session_label", "start", "end") if key in reason})
        out.append(item)
    return out if reasons is not None else None


def wish_record(row, actor):
    value = record(row)
    value["outcome_reasons"] = outcome_reasons(actor, row.outcome_reasons)
    return value


def scoring(db, department_id):
    department = db.get(m.Department, department_id)
    return s.GenerationOptions.model_validate(department.generation_defaults or {}).scoring


def require_default_points(kind, points, budget):
    require(not budget or points <= budget, 'WISH_DEFAULT_POINTS',
            f'Default points for {kind.replace("_", " ")} ({points}) exceed the department preference budget ({budget}); '
            'lower the default points or increase the budget')


def require_supported_approval(settings):
    require(settings.approval == 'none' and not settings.approver_roles,
            'WISH_APPROVAL_UNSUPPORTED',
            'Preference approval routing is unavailable; choose No approval and clear reviewer roles', 422)


def period_of(day: date, settings, week_start: int):
    """[start, end) of the wish period that contains `day`."""
    if settings.wish_period == 'week':
        start = day - timedelta(days=(day.weekday() - week_start) % 7)
        return start, start + timedelta(days=7)
    months = 3 if settings.wish_period == 'quarter' else 1
    first = date(day.year, (day.month - 1) // months * months + 1, 1)
    month = first.month - 1 + months
    return first, date(first.year + month // 12, month % 12 + 1, 1)


def locked(db, department_id, first: date, last: date, *, standing=False):
    """Return the name of a submitted (or approved, published) rota settling preferences on these days, if any."""
    return neutral_rota_name(db.scalar(select(m.Workspace.name).where(
        m.Workspace.department_id == department_id, m.Workspace.archived_at.is_(None),
        m.Workspace.status.in_(("submitted", "approved") if standing else SETTLED),
        m.Workspace.starts_on <= last, m.Workspace.ends_on > first).limit(1)))


def require_wish_person(actor, person_id):
    require(actor.permits("coordinator") or actor.person_id == person_id, "FORBIDDEN", "Staff may add only their own preferences", 403)


def save(db, actor, command):
    require_role(actor, "staff", "coordinator")
    restoring = command.kind == "wish.restore"
    if restoring:
        s.Decision.model_validate(command.payload)
    row = own_wish(db, actor, command, status="withdrawn" if restoring else "open") if restoring or command.kind == "wish.update" else None
    value = s.StaffWishInput.model_validate({key: getattr(row, key) for key in s.StaffWishInput.model_fields}
                                           if restoring else command.payload)
    person_access(db, actor, value.person_id)
    require_wish_person(actor, value.person_id)
    settings = scoring(db, actor.department_id)
    if value.assignment_type_id:
        scoped(db, m.AssignmentType, value.assignment_type_id, actor)
    if db.info.get("bulk"):
        # Wish overlap, budget and leave checks must include preceding writes in this transaction.
        db.flush()
    if row is not None:
        require(not wish_locked(db, row), "WISH_LOCKED",
                "A rota for these days has been submitted; this preference can no longer change", 409)
    types = s.WishSettings.model_validate(db.get(m.Department, actor.department_id).wish_settings or {}).types
    require_supported_approval(types[value.kind])
    if restoring or row is None or value.kind != row.kind:
        require(types[value.kind].allowed, 'WISH_KIND_DISABLED', 'This preference kind is not allowed by the department', 422)
    if value.repeat is not None and (restoring or row is None or row.repeat is None or value.kind != row.kind):
        require(types[value.kind].may_repeat, 'WISH_REPEAT_DISABLED',
                'This preference kind cannot repeat under the department settings', 422)
    if 'points' not in value.model_fields_set:
        if row is None:
            require_default_points(value.kind, types[value.kind].default_points, settings.wish_budget_points)
        value.points = types[value.kind].default_points if row is None else row.points
    require(settings.wish_budget_points > 0, "WISHES_OFF", "Preferences are switched off for this department", 409)
    name = locked(db, actor.department_id, value.first_day,
                  (value.repeat.ends_on or date.max) if value.repeat else value.last_day, standing=bool(value.repeat))
    require(not name, "WISH_LOCKED",
            f"The rota for these days ({name}) has been submitted; preferences for them are closed", 409)
    organisation = db.get(m.Organisation, actor.organisation_id)
    # Leave already takes a date wish's days; pattern wishes still describe the working/off pattern around leave.
    zone = ZoneInfo(organisation.timezone)
    window = (datetime.combine(value.first_day, time(), zone), datetime.combine(value.last_day + timedelta(days=1), time(), zone))
    require(value.repeat is not None or value.kind in s.PATTERN_WISH_KINDS or not db.scalar(select(m.Availability.id).where(
        m.Availability.person_id == value.person_id, m.Availability.retracted.is_(False), m.Availability.kind.in_(LEAVE),
        m.Availability.counts_as_unavailable.is_(True),
        m.Availability.start < window[1], m.Availability.end > window[0]).limit(1)),
        "WISH_ON_LEAVE", "There is leave on one of these days already; preferences are for days you could work", 409)
    week_start = organisation.week_start
    start, end = period_of(value.first_day, settings, week_start)
    if value.repeat is None:
        _, end = period_of(value.last_day, settings, week_start)
    others = list(db.scalars(select(m.StaffWish).where(
        m.StaffWish.department_id == actor.department_id, m.StaffWish.person_id == value.person_id,
        m.StaffWish.repeat.is_not(None) if value.repeat else m.StaffWish.repeat.is_(None),
        or_(m.StaffWish.repeat.is_not(None), and_(m.StaffWish.last_day >= value.first_day, m.StaffWish.first_day <= value.last_day)),
        m.StaffWish.status == "open")))
    others = [w for w in others if row is None or w.id != row.id]
    if value.repeat:
        for wish in others:
            repeat = s.WishRepeat.model_validate(wish.repeat)
            shared = set(value.repeat.weekdays) & set(repeat.weekdays)
            if shared and repeat.starts_on <= (value.repeat.ends_on or date.max) and value.repeat.starts_on <= (repeat.ends_on or date.max):
                require(False, "WISH_OVERLAP",
                        f"There is already a standing preference on {day_name[min(shared)]}s; change that one instead", 409)
    else:
        require(not any(w.first_day <= value.last_day and value.first_day <= w.last_day and
                        (w.kind == value.kind if value.kind in s.PATTERN_WISH_KINDS else w.kind not in s.PATTERN_WISH_KINDS)
                        for w in others), "WISH_OVERLAP",
                "There is already a preference" + (" of this kind" if value.kind in s.PATTERN_WISH_KINDS else "") +
                (" on these days" if value.kind in s.PATTERN_WISH_KINDS else " on one of these days") + "; change that one instead", 409)
    fields = value.model_dump()
    fields['repeat'] = value.repeat.model_dump(mode='json') if value.repeat else None
    previous = None
    if row is None:
        row = m.StaffWish(department_id=actor.department_id, created_by=actor.identity_id, **fields)
        db.add(row)
    else:
        previous = {key: record(row)[key] for key in s.StaffWishInput.model_fields}  # for Undo
        for key, v in fields.items():
            setattr(row, key, v)
        row.status = 'open'
        row.revision += 1
    db.flush()
    report = []
    snapshot_wishes(db, [actor.department_id], SimpleNamespace(
        department_id=actor.department_id, starts_on=start, ends_on=end), person_id=value.person_id, report=report)
    if value.repeat is None:
        for period in report:
            used = period['used'] + period['unfunded_one_off_points'] - value.points
            require(used + value.points <= settings.wish_budget_points, "WISH_BUDGET",
                    f"This needs {value.points} point{'s' if value.points > 1 else ''}; {settings.wish_budget_points - used} of "
                    f"{settings.wish_budget_points} left for this {settings.wish_period}", 409,
                    {"used": used, "budget": settings.wish_budget_points, "period_start": period['start']})
    bump_inputs(db, actor)
    return {"wish": wish_record(row, actor), "previous": previous, "usage": report, "message": "Preference saved."}


def wish_locked(db, wish):
    return locked(db, wish.department_id, wish.first_day,
                  date.fromisoformat(wish.repeat['ends_on']) if wish.repeat and wish.repeat.get('ends_on') else
                  date.max if wish.repeat else wish.last_day, standing=bool(wish.repeat))


def require_no_override_overlap(db, workspace, starts_on, ends_on, *, wish_id=None):
    modified = or_(m.WishOverride.skip.is_(True), m.WishOverride.points.is_not(None), m.WishOverride.number.is_not(None))
    wish_ids = [wish_id] if wish_id is not None else select(m.WishOverride.wish_id).where(
        m.WishOverride.workspace_id == workspace.id, modified)
    require(not db.scalar(select(m.WishOverride.id).join(m.Workspace,
        m.Workspace.id == m.WishOverride.workspace_id).outerjoin(m.Publication,
        m.Publication.workspace_id == m.Workspace.id).where(m.WishOverride.wish_id.in_(wish_ids),
        m.WishOverride.workspace_id != workspace.id, m.Workspace.archived_at.is_(None),
        m.Workspace.starts_on < ends_on,
        func.coalesce(m.Publication.effective_until, m.Workspace.ends_on) > starts_on,
        or_(m.Publication.id.is_(None), and_(m.Publication.current.is_(True),
            m.Publication.id.not_in([x for x in (workspace.base_publication_id, workspace.replaces_publication_id) if x]))),
        modified).limit(1)), "WISH_OVERRIDE_OVERLAP",
        "Another open rota already changes this preference on these days; change or remove that one first", 409)


def override(db, actor, command):
    require_role(actor, "staff", "coordinator")
    value = s.WishOverrideInput.model_validate(command.payload)
    wish = own_wish(db, actor, command)
    require(wish.repeat is not None, "WISH_NOT_STANDING", "Only a standing preference has period overrides", 409)
    if value.workspace_id:
        workspace = scoped(db, m.Workspace, value.workspace_id, actor)
        require(workspace.status == 'draft' and workspace.archived_at is None, "WISH_LOCKED",
                "Only an open rota's preferences can change", 409)
        require(not locked(db, wish.department_id, workspace.starts_on,
                           workspace.ends_on - timedelta(days=1), standing=True),
                "WISH_LOCKED", "A rota for these days has been submitted; this preference can no longer change", 409)
        if value.skip or value.points is not None or value.number is not None:
            require_no_override_overlap(db, workspace, workspace.starts_on, workspace.ends_on, wish_id=wish.id)
    else:
        settings = scoring(db, actor.department_id)
        week_start = db.get(m.Organisation, actor.organisation_id).week_start
        a, b = period_of(value.period_start, settings, week_start)
        require(a == value.period_start, "WISH_PERIOD", "Use the budget period's start date")
        require(db.scalar(select(m.Workspace.id).where(m.Workspace.department_id == actor.department_id,
                m.Workspace.archived_at.is_(None), m.Workspace.starts_on < b, m.Workspace.ends_on > a).limit(1)),
                "NOT_FOUND", "No rota exists for this period", 404)
        require(not locked(db, actor.department_id, a, b - timedelta(days=1)), "WISH_LOCKED",
                "A rota for this period has been submitted; its override can no longer change", 409)
    row = db.scalar(select(m.WishOverride).where(m.WishOverride.wish_id == wish.id,
        m.WishOverride.period_start == value.period_start, m.WishOverride.workspace_id == value.workspace_id))
    previous = {key: record(row)[key] for key in s.WishOverrideInput.model_fields} if row else {
        **value.model_dump(mode='json'), 'skip': False, 'points': None, 'number': None}
    if row is None:
        row = m.WishOverride(department_id=actor.department_id, wish_id=wish.id, **value.model_dump())
        db.add(row)
    else:
        for key, v in value.model_dump().items():
            setattr(row, key, v)
        row.revision += 1
    wish.revision += 1
    bump_inputs(db, actor)
    return {'wish': record(wish), 'override': record(row), 'previous': previous}


def require_wish_change(actor, row):
    require(actor.permits("coordinator") or row.person_id == actor.person_id, "FORBIDDEN", "Only the person or a coordinator can change this preference", 403)


def own_wish(db, actor, command, *, status="open"):
    row = scoped(db, m.StaffWish, command.target_id, actor)
    require_wish_change(actor, row)
    expected(row, command.expected_revision)
    require(row.status == status, "WORKFLOW_STATE",
            "Only a withdrawn preference can be restored" if status == "withdrawn" else "This preference was withdrawn", 409)
    return row


def withdraw(db, actor, command):
    require_role(actor, "staff", "coordinator")
    s.Decision.model_validate(command.payload)
    row = own_wish(db, actor, command)
    require(not wish_locked(db, row), "WISH_LOCKED",
            "A rota for these days has been submitted; this preference can no longer change", 409)
    row.status = "withdrawn"
    row.revision += 1
    bump_inputs(db, actor)
    return {"wish": wish_record(row, actor)}


def list_wishes(db, actor, start: date | None, end: date | None, person_id: str | None, include_history=False, workspace_id=None):
    """Staff see their own; planners see everyone's. Includes the budget and this person's catch-up multiplier."""
    require_role(actor, "staff", "coordinator", "approver", "programme_lead", "auditor")
    if start and end:
        require((end - start).days <= s.WISH_RANGE_MAX_DAYS, 'WISH_RANGE_LIMIT',
                f'Preview at most {s.WISH_RANGE_MAX_DAYS} days at a time', 422)
    query = select(m.StaffWish).where(m.StaffWish.department_id == actor.department_id)
    if not include_history:
        query = query.where(m.StaffWish.status == "open")
    if person_id:
        require_person(db, actor, person_id, ("coordinator", "approver", "programme_lead", "auditor"))
    if not actor.permits("coordinator", "approver", "programme_lead", "auditor"):
        person_id = actor.person_id
        require(person_id is not None, "FORBIDDEN", "Your account is not linked to a person", 403)
    if person_id:
        query = query.where(m.StaffWish.person_id == person_id)
    if start:
        query = query.where(or_(m.StaffWish.last_day >= start, and_(m.StaffWish.repeat.is_not(None), or_(
            m.StaffWish.repeat['ends_on'].as_string().is_(None), m.StaffWish.repeat['ends_on'].as_string() >= start.isoformat()))))
    if end:
        query = query.where(m.StaffWish.first_day < end)
    department = db.get(m.Department, actor.department_id)
    settings = scoring(db, actor.department_id)
    # Keep the row alive so usage's snapshot lookup reuses the session identity map.
    organisation = db.get(m.Organisation, actor.organisation_id)
    week_start = organisation.week_start
    rows = list(db.scalars(query.order_by(m.StaffWish.first_day, m.StaffWish.created_at, m.StaffWish.seq).limit(2001)))
    partial = len(rows) > 2000
    rows = rows[:2000]
    people = {person_id} if person_id else {w.person_id for w in rows}
    # Days of submitted (or later) rotas: wishes there are settled and the screen offers no change.
    workspace = scoped(db, m.Workspace, workspace_id, actor) if workspace_id else None
    closed = db.execute(select(m.Workspace.starts_on, m.Workspace.ends_on, m.Workspace.status, m.Workspace.name).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None),
        m.Workspace.status.in_(SETTLED))).all()
    return {"wishes": [{**wish_record(w, actor), "locked": any(a <= (date.fromisoformat(w.repeat['ends_on']) if w.repeat and w.repeat.get('ends_on') else
                    date.max if w.repeat else w.last_day) and w.first_day < b and
                    (not w.repeat or status in ('submitted', 'approved')) for a, b, status, _ in closed)} for w in rows],
            # The settled rotas over the days read, by name: the form says a day is closed before a wish is sent, by the
            # same overlap `locked` refuses (a rota's ends_on is exclusive).
            "settled": [{"name": name, "starts_on": a.isoformat(), "ends_on": b.isoformat()} for a, b, _, name in closed
                        if start and end and a < end and b > start],
            "overrides": [record(o) for o in db.scalars(select(m.WishOverride).where(
                m.WishOverride.wish_id.in_([w.id for w in rows])))],
            "budget_points": settings.wish_budget_points, "period": settings.wish_period, "week_start": week_start,
            "max_points": MAX_POINTS, "partial": partial,
            "type_settings": s.WishSettings.model_validate(
                department.wish_settings or {}).model_dump(mode='json')['types'],
            "usage": usage(db, actor.department_id, person_id, start, end, settings, week_start, workspace) if person_id and start and end else [],
            "multipliers": multipliers(db, actor.department_id, people, settings),
            # Staff have no catalogue access: the duty types a wish can name travel with the list.
            "duty_types": [{"id": t.id, "name": t.name} for t in db.scalars(select(m.AssignmentType).where(
                m.AssignmentType.department_id == actor.department_id, m.AssignmentType.active.is_(True)).order_by(m.AssignmentType.name))]}


def usage(db, department_id, person_id, start: date, end: date, settings, week_start, workspace=None):
    """Points used in each budget period overlapping [start, end): the same periods and sums save() enforces."""
    report = []
    snapshot_wishes(db, [department_id], SimpleNamespace(department_id=department_id, starts_on=start, ends_on=end,
                    id=workspace.id if workspace else None,
                    base_publication_id=workspace.base_publication_id if workspace else None,
                    replaces_publication_id=workspace.replaces_publication_id if workspace else None,
                    replacement_starts_on=workspace.starts_on if workspace else start), person_id=person_id, report=report)
    return report


def multipliers(db, department_id, people, settings, before=None):
    """person -> weight multiplier from declined wishes (1 = none owed; at most the configured cap).
    `before`: only publications of earlier periods count (a rota's own outcome never boosts itself)."""
    budget, cap = settings.wish_budget_points, settings.wish_debt_cap
    out = {pid: 1.0 for pid in people}
    if not people or not budget or cap <= 1:
        return out
    ledger = m.WishLedger
    ranked = select(ledger.person_id, ledger.metric, ledger.value, ledger.period_start,
        func.dense_rank().over(partition_by=ledger.person_id, order_by=ledger.period_start.desc()).label('period_rank')).where(
            ledger.department_id == department_id, ledger.person_id.in_(list(people)),
            ledger.metric.in_(("wishes_granted", "wishes_declined")),
            ledger.period_start < (before or date.max)).subquery()
    rows = db.execute(select(ranked.c.person_id, ranked.c.metric, ranked.c.value, ranked.c.period_start)
                      .where(ranked.c.period_rank <= settings.wish_history_periods)
                      .order_by(ranked.c.period_start.desc()))
    periods = {}
    for pid, metric, value, start in rows:
        seen = periods.setdefault(pid, {})
        seen.setdefault(start, 0.0)
        seen[start] += value if metric == "wishes_declined" else -settings.wish_granted_credit * value
    for pid, seen in periods.items():
        debt = sum(settings.wish_priority_decay ** age * net for age, (_, net) in enumerate(sorted(seen.items(), reverse=True)))
        out[pid] = round(1 + min(cap - 1, max(0.0, debt / budget)), 3)
    return out


def snapshot_wishes(db, department_ids, workspace, until=None, *, wish_debt_cap=None, person_id=None, report=None,
                    scoring_settings=None):
    """The only standing expansion and budget allocation point, shared by all evaluators and usage.

    Allocate complete budget periods before clipping to the rota: standing occurrences first, then one-offs.
    An occurrence costs its points; an unfunded wish never enters a score or solver objective.
    """
    end = min(until or workspace.ends_on, workspace.ends_on)
    if workspace.starts_on >= end:
        return []
    # Published preferences are evidence, even after their standing defaults are edited or withdrawn.
    legacy = False
    if getattr(workspace, 'status', None) in ('published', 'superseded') and report is None:
        publication = db.scalar(select(m.Publication).where(m.Publication.workspace_id == workspace.id))
        if publication and 'wishes' in publication.snapshot:
            from .schedule import in_force_snapshot
            return [dict(w) for w in in_force_snapshot(db, publication)['wishes'] if w['first_day'] < end.isoformat()
                    and w['last_day'] >= workspace.starts_on.isoformat()]
        legacy = publication is not None
    department = db.get(m.Department, workspace.department_id)
    settings = scoring_settings if scoring_settings is not None else scoring(db, workspace.department_id)
    if wish_debt_cap is not None:
        settings = settings.model_copy(update={'wish_debt_cap': wish_debt_cap})
    if not settings.wish_budget_points:
        return []
    organisation = db.get(m.Organisation, department.organisation_id)
    lo = period_of(workspace.starts_on, settings, organisation.week_start)[0]
    hi = period_of(end - timedelta(days=1), settings, organisation.week_start)[1]
    base_id = getattr(workspace, 'base_publication_id', None)
    replaces_id = getattr(workspace, 'replaces_publication_id', None)
    replaces_on = getattr(workspace, 'replacement_starts_on', workspace.starts_on)
    rows = list(db.scalars(select(m.StaffWish).where(
        m.StaffWish.department_id.in_(department_ids), m.StaffWish.status == "open",
        *([m.StaffWish.repeat.is_(None)] if legacy else []),
        *([m.StaffWish.person_id == person_id] if person_id else []),
        or_(m.StaffWish.last_day >= lo, m.StaffWish.repeat.is_not(None)), m.StaffWish.first_day < hi)
        .order_by(m.StaffWish.first_day, m.StaffWish.created_at, m.StaffWish.seq)))
    types = s.WishSettings.model_validate(department.wish_settings or {}).types
    # Legacy unsupported settings fail closed too: no unreviewed wish enters a score or solver.
    rows = [wish for wish in rows if types[wish.kind].approval == 'none' and not types[wish.kind].approver_roles]
    standing = [w for w in rows if w.repeat]
    overrides = db.execute(select(m.WishOverride, m.Workspace.starts_on,
        func.coalesce(m.Publication.effective_until, m.Workspace.ends_on), m.Publication.id).outerjoin(
        m.Workspace, m.Workspace.id == m.WishOverride.workspace_id).outerjoin(m.Publication,
        m.Publication.workspace_id == m.Workspace.id).where(m.WishOverride.wish_id.in_([w.id for w in standing]),
        or_(m.WishOverride.period_start.between(lo, hi - timedelta(days=1)), and_(
            m.Workspace.archived_at.is_(None), m.Workspace.starts_on < hi, m.Workspace.ends_on > lo,
            or_(m.Publication.id.is_(None), m.Publication.current.is_(True)))))) if standing else ()
    by_period, by_day, rota_starts = {}, {}, {}
    for change, first, last, publication_id in overrides:
        if not (change.skip or change.points is not None or change.number is not None):
            continue  # Reset rows remain audited, but inherit their defaults.
        if change.period_start is not None:
            by_period[change.wish_id, change.period_start] = change
        else:
            if base_id and publication_id == base_id:
                continue
            if replaces_id and publication_id == replaces_id:
                last = min(last, replaces_on)
            if last <= lo:
                continue
            if change.number is not None:
                rota_starts[change.wish_id] = min(rota_starts.get(change.wish_id, lo), first)
            else:
                first = max(first, lo)
            for i in range((min(last, hi) - first).days):
                by_day[change.wish_id, first + timedelta(days=i)] = change
    # Rota caps consume earlier matches even when only a suffix of that rota is requested.
    scan_lo = min(rota_starts.values(), default=lo)
    # Index leave once. A standing default must not spend points on dates already unavailable.
    leaves = defaultdict(list)
    zone = ZoneInfo(organisation.timezone)
    leave_rows = db.execute(select(m.Availability.person_id, m.Availability.start, m.Availability.end).where(
            m.Availability.person_id.in_({w.person_id for w in standing}),
            m.Availability.retracted.is_(False), m.Availability.kind.in_(LEAVE),
            m.Availability.counts_as_unavailable.is_(True),
            m.Availability.start < datetime.combine(hi, time(), zone),
            m.Availability.end > datetime.combine(scan_lo, time(), zone))) if standing else ()
    for pid, a, b in leave_rows:
        a, b = aware(a).astimezone(zone).date(), (aware(b).astimezone(zone) - timedelta(microseconds=1)).date()
        for i in range((min(b, hi - timedelta(days=1)) - max(a, scan_lo)).days + 1):
            leaves[pid].append(max(a, scan_lo) + timedelta(days=i))
    leaves = {pid: set(days) for pid, days in leaves.items()}
    candidates = defaultdict(list)
    frozen_keys, frozen_output_ids, closed_days = set(), set(), set()
    # Reserve settled charges once; edits elsewhere cannot fund or reclaim occurrences on closed days.
    from .schedule import current_publications, in_force_snapshot, until as publication_until
    reservations = current_publications(db, department_ids, lo, hi)
    for settled in db.scalars(select(m.Workspace).outerjoin(m.Publication,
            m.Publication.workspace_id == m.Workspace.id).where(
            m.Workspace.department_id.in_(department_ids), m.Workspace.archived_at.is_(None),
            m.Workspace.status.in_(SETTLED), m.Publication.id.is_(None),
            m.Workspace.starts_on < hi, m.Workspace.ends_on > lo)):
        reservation = reservation_input(settled)
        if reservation is None:
            continue  # shortcut: older checks expand live until revalidation records their allocation.
        reservations.append(SimpleNamespace(id=None, snapshot=reservation,
            starts_on=settled.starts_on, ends_on=settled.ends_on, effective_until=None,
            created_at=settled.submitted_at or settled.created_at))
    for publication in reservations:
        if base_id and publication.id == base_id:
            continue
        if replaces_id and publication.id == replaces_id:
            publication = SimpleNamespace(id=publication.id, snapshot=publication.snapshot,
                department_id=publication.department_id, starts_on=publication.starts_on, ends_on=publication.ends_on,
                effective_until=min(publication_until(publication), replaces_on), created_at=publication.created_at)
        first, last = max(lo, publication.starts_on), min(hi, publication_until(publication))
        closed_days.update(first + timedelta(days=i) for i in range((last - first).days))
        for wish in in_force_snapshot(db, publication).get('wishes', []):
            if person_id and wish['person_id'] != person_id:
                continue
            wish_first = max(first, date.fromisoformat(wish['first_day']))
            wish_end = min(last, date.fromisoformat(wish['last_day']) + timedelta(days=1))
            if wish_first >= wish_end:
                continue
            a, _ = period_of(wish_first, settings, organisation.week_start)
            while a < wish_end:
                key = a, wish['id']
                if key not in frozen_keys:
                    frozen_keys.add(key)
                    candidates[a].append((-1, date.min, aware(publication.created_at), wish['id'], wish))
                _, a = period_of(a, settings, organisation.week_start)
    records = {w.id: record(w, exclude=('note', 'outcome_reasons')) for w in rows}
    one_off_days = defaultdict(set)
    for w in rows:
        if not w.repeat and w.kind not in s.PATTERN_WISH_KINDS:
            first, last = max(w.first_day, scan_lo), min(w.last_day, hi - timedelta(days=1))
            one_off_days[w.person_id].update(first + timedelta(days=i) for i in range((last - first).days + 1))
    for w in rows:
        if not w.repeat:
            a = max(lo, period_of(w.first_day, settings, organisation.week_start)[0])
            while a < hi and a <= w.last_day:
                if (a, w.id) not in frozen_keys:
                    candidates[a].append((1, date.min, aware(w.created_at), w.id, records[w.id]))
                _, a = period_of(a, settings, organisation.week_start)
            continue
        repeat = s.WishRepeat.model_validate(w.repeat)
        a = max(period_of(rota_starts.get(w.id, lo), settings, organisation.week_start)[0],
                period_of(repeat.starts_on, settings, organisation.week_start)[0])
        rota_counts = defaultdict(int)
        while a < hi and (repeat.ends_on is None or a <= repeat.ends_on):
            _, b = period_of(a, settings, organisation.week_start)
            default = by_period.get((w.id, a))
            points = default.points if default and default.points is not None else w.points
            number = default.number if default and default.number is not None else repeat.number
            count = 0
            first, last = max(a, repeat.starts_on), min(b - timedelta(days=1), repeat.ends_on or b)
            for i in range((last - first).days + 1):
                day = first + timedelta(days=i)
                if day.weekday() not in repeat.weekdays or day in leaves.get(w.person_id, ()):
                    continue
                count += 1
                change = by_day.get((w.id, day))
                if change:
                    rota_counts[change.id] += 1
                selected = rota_counts[change.id] <= change.number if change and change.number is not None else number is None or count <= number
                if not selected or day < lo or day in one_off_days[w.person_id] or day in closed_days or (change.skip if change else default and default.skip):
                    continue
                occurrence = {**records[w.id], 'id': f'standing:{w.id}:{day.isoformat()}', 'standing_wish_id': w.id,
                              'first_day': day.isoformat(), 'last_day': day.isoformat(),
                              'points': change.points if change and change.points is not None else points}
                candidates[a].append((0, day, aware(w.created_at), w.id, occurrence))
            a = b
    allocated, totals = {}, {}
    for a, entries in sorted(candidates.items()):
        for priority, _, _, _, wish in sorted(entries, key=lambda x: x[:4]):
            t = totals.setdefault((a, wish['person_id']), dict(used=0, standing_points=0, one_off_points=0,
                                      unfunded_standing_points=0, unfunded_one_off_points=0))
            source = 'standing_points' if wish.get('standing_wish_id') else 'one_off_points'
            if priority != -1 and t['used'] + wish['points'] > settings.wish_budget_points:
                t['unfunded_' + source] += wish['points']
                continue
            t['used'] += wish['points']
            t[source] += wish['points']
            if date.fromisoformat(wish['first_day']) < end and date.fromisoformat(wish['last_day']) >= workspace.starts_on:
                if wish.get('standing_wish_id'):
                    allocated[wish['id']] = wish
                    if priority == -1:
                        frozen_output_ids.add(wish['id'])
                else:
                    # Neighboring publications reserve charges; this live rota evaluates the current one-off once.
                    allocated[wish['id']] = records.get(wish['id'], wish)
    if report is not None:
        a = period_of(workspace.starts_on, settings, organisation.week_start)[0]
        while a < end:
            _, b = period_of(a, settings, organisation.week_start)
            report.append({'start': a.isoformat(), 'end': b.isoformat(), **totals.get((a, person_id), dict(
                used=0, standing_points=0, one_off_points=0, unfunded_standing_points=0, unfunded_one_off_points=0))})
            a = b
    boost = multipliers(db, workspace.department_id, {w['person_id'] for w in allocated.values()}, settings, workspace.starts_on)
    return [w if w['id'] in frozen_output_ids else {**w, "multiplier": boost[w['person_id']],
             'priority_policy': {key: getattr(settings, key) for key in
                 ('wish_priority_decay', 'wish_granted_credit', 'wish_history_periods')},
             "weight": min(w['points'], settings.wish_budget_points) / settings.wish_budget_points * boost[w['person_id']]} for w in allocated.values()]


def _applied_reasons(db, workspace, data, wishes):
    """Reuse the immutable receipt only for its full, unchanged application, including its effective wish weights."""
    from packages.domain.time import aware
    job = db.scalar(select(m.Job).where(m.Job.workspace_id == workspace.id, m.Job.kind == "generation",
        m.Job.status == "applied", m.Job.result["applied_content_revision"].as_integer() == workspace.content_revision)
        .order_by(m.Job.completed_at.desc(), m.Job.id).limit(1))
    if job is None or "declined_wishes" not in job.result:
        return None
    reconciliation = job.result.get('reconciliation')
    if reconciliation is not None:
        # Reconciled work gets freshly computed reasons; original full-proposal reasons stay historical.
        return None
    fields = tuple(key for key in s.StaffWishInput.model_fields if key != "note") + ('weight', 'multiplier', 'standing_wish_id')
    wished = lambda rows: {row["id"]: {key: row.get(key) for key in fields} for row in rows}
    if wished(job.input_snapshot.get("wishes", [])) != wished(wishes):
        return None
    if any(not isinstance(wish.get(key), (int, float)) or isinstance(wish[key], bool) or not isfinite(wish[key])
           for wish in wishes for key in ('weight', 'multiplier')):
        return None
    by_id = {wish['id']: wish for wish in wishes}
    receipt = job.result['declined_wishes']
    if any(row['wish_id'] not in by_id or row.get('weight') != by_id[row['wish_id']]['weight']
           or row.get('person_id') != by_id[row['wish_id']]['person_id']
           or row.get('kind') != by_id[row['wish_id']]['kind'] for row in receipt):
        return None
    removed = {row["assignment_id"] for row in job.result.get("removals", [])}
    focus = set(job.input_snapshot.get("focus_ids", []))

    def signature(participant):
        value = s.ParticipantInput.model_validate({key: participant[key] for key in s.ParticipantInput.model_fields
                                                   if key in participant}).model_dump(mode="json")
        value.update(start=aware(value["start"]).isoformat(), end=aware(value["end"]).isoformat())
        value["segments"] = [{"start": aware(part["start"]).isoformat(), "end": aware(part["end"]).isoformat()}
                             for part in value["segments"]]
        return value

    expected = {shift["id"]: [signature(p) for p in shift["participants"] if p["id"] not in removed]
                for shift in job.input_snapshot["shifts"] if shift["id"] in focus}
    for addition in job.result.get("additions", []):
        expected.setdefault(addition["shift_id"], []).append(signature(addition["participant"]))
    actual = {shift["id"]: [signature(p) for p in shift["participants"]]
              for shift in data["shifts"] if shift["id"] in focus}
    ordered = lambda rows: {sid: sorted(values, key=lambda p: (p["person_id"], p["start"], p["end"]))
                            for sid, values in rows.items()}
    def external(snapshot):
        return {shift["id"]: {**{key: value for key, value in shift.items() if key != "participants"},
                              "participants": ordered({shift["id"]: [signature(p) for p in shift["participants"]]})[shift["id"]]}
                for shift in snapshot["shifts"] if shift["id"] not in focus}
    return (receipt
            if ordered(expected) == ordered(actual) and external(job.input_snapshot) == external(data) else None)


def planner_effects(data, zone, *, external_shifts=()):
    """Annotate the planner response, without scoring or changing stored wishes/assignments."""
    from collections import defaultdict
    from packages.domain.patterns import day_of, wish_day_effect, wish_days, wish_outcomes
    data['wishes'] = [dict(wish) for wish in data['wishes']]
    context = {**data, 'organisation': {'timezone': zone}, 'shifts': [*data['shifts'], *external_shifts]}
    outcomes = wish_outcomes(context)
    by_day = defaultdict(list)
    for wish in data['wishes']:
        honoured, n = outcomes[wish['id']]
        wish['draft_outcome'] = {
            'outcome': 'granted' if honoured == 1 else 'declined' if honoured == 0 else 'partly',
            'honoured_days': round(honoured * n), 'total_days': n}
        for day in wish_days(wish, context):
            by_day[wish['person_id'], day].append(wish)
    for shift in data['shifts']:
        types = {shift['assignment_type_id']}
        for participant in shift['participants']:
            participant['wish_effects'] = []
            if inactive(shift):
                continue
            for wish in by_day.get((participant['person_id'], day_of(participant['start'], zone)), ()):
                effect = wish_day_effect(wish, types)
                if effect is not None:
                    participant['wish_effects'].append({'wish_id': wish['id'], 'effect': effect})


def published(db, actor, workspace, publication, zone):
    """Mark each wish on the rota's days in force granted / partly / declined, and replace the ledger rows.
    Run again for a rota a later one replaced from a date: its rows then cover only the days it is still in force."""
    from packages.domain.patterns import wish_outcomes
    from .schedule import frozen_wish_worked, in_force_snapshot
    snapshot = in_force_snapshot(db, publication)
    wishes = snapshot.get('wishes')
    if wishes is None:
        # Pre-P6 publications contain only original one-offs, never newly added standing defaults.
        wishes = [record(w, exclude=('note',)) for w in db.scalars(select(m.StaffWish).where(
            m.StaffWish.department_id == workspace.department_id, m.StaffWish.status == 'open',
            m.StaffWish.first_day < (publication.effective_until or workspace.ends_on),
            m.StaffWish.last_day >= workspace.starts_on, m.StaffWish.repeat.is_(None)))]
    superseded = [publication.supersedes_id] if publication.supersedes_id else []
    db.execute(delete(m.WishLedger).where(m.WishLedger.publication_id.in_([publication.id, *superseded])))
    if not wishes:
        return
    period_end = publication.effective_until or workspace.ends_on
    data = {"organisation": {"timezone": zone},
            "workspace": {**record(workspace), "ends_on": period_end.isoformat()},
            "shifts": snapshot['shifts'],
            "wishes": wishes}
    outcomes = wish_outcomes(data, frozen_wish_worked(snapshot))
    ids = [wish['id'] for wish in wishes if wish['id'] in outcomes and not wish.get('standing_wish_id')]
    rows = {row.id: row for row in db.scalars(select(m.StaffWish).where(m.StaffWish.id.in_(ids)))}
    totals = {}
    for wish in wishes:
        if wish["id"] not in outcomes:
            continue
        honoured, _ = outcomes[wish["id"]]
        if not wish.get('standing_wish_id'):
            row = rows[wish['id']]
            row.outcome = "granted" if honoured == 1 else "declined" if honoured == 0 else "partly"
            row.outcome_reasons = wish.get('outcome_reasons')
            row.publication_id = publication.id
            row.revision += 1
        t = totals.setdefault(wish["person_id"], {"wishes_submitted": 0.0, "wishes_granted": 0.0, "wishes_declined": 0.0})
        t["wishes_submitted"] += wish["points"]
        t["wishes_granted"] += wish["points"] * honoured
        t["wishes_declined"] += wish["points"] * (1 - honoured)
    for pid, metrics in totals.items():
        for metric, value in metrics.items():
            db.add(m.WishLedger(publication_id=publication.id, person_id=pid, metric=metric, department_id=workspace.department_id,
                                period_start=workspace.starts_on, period_end=period_end, value=round(value, 4)))
