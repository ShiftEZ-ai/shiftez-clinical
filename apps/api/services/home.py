"""Home: what needs this person, as counts and short lists.

Every figure is a COUNT or a LIMITed read scoped to the department and to the roles that may act on it, so Home costs
the same with one rota or years of publications. Rotas are read only while live (draft, submitted, approved).
The client owns the wording; each item is {key, count,...} in priority order (most urgent first). A few items carry a
`sample` (the first one or two, names and times only) so a row can say which without opening a list."""
from datetime import timedelta
from collections import OrderedDict
from copy import deepcopy
from threading import Lock
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import load_only
from apps.api import models as m
from apps.api.serialization import record
from apps.api.snapshots import cheap_fingerprints, inactive_series_shift_ids
from packages.contracts.schemas import PERMISSIONS
from packages.domain.time import aware
from packages.domain.finding_causes import DUTIES_DURING_APPROVED_LEAVE
from . import availability, reviews, schedule, scheduling_facts, published_operational
from .independence import person_key, my_actions, active_permissions
from .common import neutral_rota_name, rota_names
from apps.api.settings import Settings
from packages.domain.input_manifest import digest

_HOME_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_HOME_CACHE_LOCK = Lock()
_HOME_CACHE_LIMIT = 128


def _home_stamp(db, actor, now):
    """Return a bounded invalidation stamp without aggregating every history row."""
    latest_audit = db.scalar(select(func.max(m.AuditEvent.created_at)).where(
        m.AuditEvent.department_id == actor.department_id))
    row = db.execute(select(m.Department.revision, m.Department.catalogue_revision,
                            m.Organisation.revision, m.Organisation.input_revision,
                            m.Organisation.availability_revision)
                    .join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
                    .where(m.Department.id == actor.department_id)
                    ).one()
    # The audit timestamp closes the commit-to-notification gap.  Keep this
    # stamp process-independent so the durable Home projection is shared by
    # every API worker after a deploy.
    from .rota_summary import validation_job_stamp
    return (*row, latest_audit, now.date(), validation_job_stamp(db, department_id=actor.department_id))


def rota_strip(db, actor, start, end, limit, offset):
    """The Home timeline needs dates and health, without saved edit/approval payloads."""
    from apps.api.auth import require_role
    from apps.api.errors import require
    require_role(actor, 'coordinator', 'approver', 'programme_lead', 'auditor')
    require(0 < (end - start).days <= 366, 'INVALID_RANGE', 'Choose a period of at most one year')
    W = m.Workspace
    rows = list(db.scalars(select(W).options(load_only(W.id, W.name, W.status, W.starts_on, W.ends_on,
        W.revision, W.content_revision, W.base_publication_id, W.department_id, W.accepted_issues)).where(
        W.department_id == actor.department_id, W.archived_at.is_(None),
        W.status.in_((*schedule.LIVE, 'published')), W.starts_on < end, W.ends_on > start)
        .order_by(W.starts_on, W.id).offset(offset).limit(limit + 1)))
    workspaces = []
    from .job_reads import proposals
    proposal_by_workspace = proposals(db, actor, rows[:limit])
    from .workspace_reads import stored_checks
    checks = stored_checks(db, rows[:limit])
    current_inputs = cheap_fingerprints(db, rows[:limit], checks=checks)
    from .rota_summary import checking_workspaces
    checking = checking_workspaces(db, [row.id for row in rows[:limit]])
    names = rota_names(db, actor.roles, actor.person_id, {row.id: row.name for row in rows[:limit]})
    for row in rows[:limit]:
        value = checks[row.id]['health']
        summary = value.get('summary', {})
        if summary:
            check_job = checking.get(row.id)
            summary['fix_list']['summary'].update(
                checking=bool(check_job and check_job['checking']),
                check_status=check_job['status'] if check_job else None,
                check_queued_at=check_job['queued_at'] if check_job else None)
            value = {**value, 'summary': {key: summary[key] for key in (
                'places_needed', 'places_filled', 'places_open', 'sessions_with_gaps', 'other_gaps', 'issues', 'repair_counts', 'fix_list')}}
        value['fresh'] = bool(checks[row.id]['checked'] and checks[row.id]['fingerprint'] == current_inputs.get(row.id))
        workspaces.append({**{key: getattr(row, key) for key in ('id', 'status', 'starts_on', 'ends_on', 'base_publication_id')},
                           'name': names[row.id],
                           'health': value, **({'proposal': proposal_by_workspace[row.id]} if row.id in proposal_by_workspace else {})})
    return {'workspaces': workspaces, 'limit': limit, 'offset': offset, 'has_more': len(rows) > limit}

def _home_uncached(db, actor, *, max_days):
    org = db.get(m.Organisation, actor.organisation_id)
    now = m.now()
    today = now.astimezone(ZoneInfo(org.timezone)).date()
    dept = actor.department_id
    can = actor.permits
    items = []

    def add(priority, key, count, **extra):
        if count:
            items.append((priority, {"key": key, "count": count, **extra}))

    def count(cls, *where):
        return db.scalar(select(func.count()).select_from(cls).where(cls.department_id == dept, *where)) or 0

    rotas = None
    if can("coordinator", "approver", "programme_lead", "auditor"):
        W = m.Workspace
        rotas = count(W, W.archived_at.is_(None))
        # Live rotas only (a handful); their stored check says what is left without loading any session.
        near_since = today - timedelta(days=max_days)
        near_until = today + timedelta(days=max_days)
        live = list(db.scalars(select(W).options(load_only(W.id, W.name, W.status, W.starts_on, W.ends_on,
            W.revision, W.content_revision, W.base_publication_id, W.submitted_fingerprint, W.submitted_at, W.review_snapshot,
            W.return_note,
            W.archived_at, W.department_id, W.accepted_issues)).where(W.department_id == dept, W.archived_at.is_(None),
            W.status.in_(schedule.LIVE), W.ends_on > near_since, W.starts_on < near_until)
            .order_by(W.starts_on, W.id)))
        drafts = [w for w in live if w.status == "draft"]
        names = rota_names(db, actor.roles, actor.person_id, {w.id: w.name for w in live})
        from .rota_summary import checking_workspaces
        checking = checking_workspaces(db, [w.id for w in live])
        from .workspace_reads import stored_checks
        checks = stored_checks(db, live)
        # A landing read must not rebuild every live rota. Unknown currency is shown
        # conservatively and the planner resolves it on open.
        current_inputs = cheap_fingerprints(db, live, checks=checks)
        outdated = {w.id for w in live if w.status != "draft" and (
            w.id not in current_inputs or w.submitted_fingerprint != current_inputs[w.id])}
        fresh = {w.id for w in drafts if checks[w.id]['checked'] and checks[w.id]['fingerprint'] == current_inputs.get(w.id)}

        summaries = {}

        def health(w):
            stored = checks[w.id]
            summary = stored.get('health', {}).get('summary')
            if not summary:
                return False, 0, 0, False
            summaries[w.id] = summary
            check_job = checking.get(w.id)
            summary['fix_list']['summary'].update(
                checking=bool(check_job and check_job['checking']),
                check_status=check_job['status'] if check_job else None,
                check_queued_at=(check_job['queued_at'].isoformat() if check_job else None))
            return (w.id in fresh, summary['fix_list']['summary']['must'], summary['places_open'],
                    bool(summary['session_count']))

        def first(rows):
            if not rows:
                return {}
            row = rows[0]
            stored = checks[row.id]
            summary = summaries.get(row.id) or stored.get('health', {}).get('summary')
            current = row.id in fresh
            return {"rota_id": rows[0].id, "rota_name": names[rows[0].id], "rotas": len(rows),
                    **({'checked': current} if row.status == 'draft' else {}),
                    "checked_at": stored['evaluated_at'],
                    **({'fix_list': {'summary': summary['fix_list']['summary']}}
                       if summary else {})}

        if can("coordinator"):
            checked = {w.id: health(w) for w in drafts}
            # Stored blocking counts are actionable only while their check is
            # current; stale drafts belong in the explicit unchecked bucket.
            blocking = [w for w in drafts if checked[w.id][0] and checked[w.id][1]]
            for w in blocking:
                add(10, "blocking", checked[w.id][1], **first([w]))
            A, S, R, P = m.Assignment, m.Shift, m.RequestRecord, m.Person
            conflicts = availability.impact_duties(dept, R.start, R.end).join(R, R.person_id == A.person_id).where(
                S.workspace_id.in_([w.id for w in drafts]), W.status == "draft", R.department_id == dept, R.status == "approved",
                R.counts_as_unavailable.is_(True), R.end > now, A.end > now,
                ~published_operational.amendment_duplicate(db, dept, now))
            inactive_ids = inactive_series_shift_ids(db, select(S.id).where(S.workspace_id.in_([w.id for w in drafts]), S.end > now))
            if inactive_ids:
                conflicts = conflicts.where(S.id.not_in(inactive_ids))
            n = db.scalar(conflicts.with_only_columns(func.count(A.id.distinct()))) or 0
            add(15, "leave_conflicts", n, classification=DUTIES_DURING_APPROVED_LEAVE, sample=[{
                "person": person, "person_id": person_id, "assignment_id": assignment_id,
                "start": aware(start).isoformat(), "end": aware(end).isoformat(),
                "rota_id": rota_id, "rota_name": neutral_rota_name(rota_name), "shift_id": shift_id, "request_id": request_id,
            } for person, person_id, assignment_id, start, end, rota_id, rota_name, shift_id, request_id in db.execute(
                conflicts.with_only_columns(P.display_name, A.person_id, A.id, A.start, A.end, W.id, W.name, S.id, func.min(R.id))
                .join(P, P.id == A.person_id).group_by(A.id, P.display_name, A.start, A.end, W.id, W.name, S.id, S.start)
                .order_by(S.start, A.id).limit(5))] if n else [])
            stale = [w for w in live if w.id in outdated]
            add(30, "resubmit", len(stale), **first(stale))
            flags = schedule.flags(db, dept, live)
            overlapping = [w for w in live if (flags.get(w.id) or {}).get("drafts") or (flags.get(w.id) or {}).get("publications")]
            add(35, "overlap", len(overlapping), **first(overlapping))
            ready = [w for w in drafts if checked[w.id][0] and not checked[w.id][1] and not checked[w.id][2] and checked[w.id][3]]
            add(70, "ready_to_submit", len(ready), **first(ready))
            unchecked = [w for w in drafts if not checked[w.id][0]]
            for w in unchecked:
                add(75, "unchecked", 1, **first([w]))
            returned = [w for w in drafts if w.return_note]
            add(12, "returned", len(returned), sample=[{
                "rota_id": w.id, "rota_name": names[w.id],
                "returned_at": (w.return_note or {}).get("at"),
                "by": (w.return_note or {}).get("by_name"),
                "reason": (w.return_note or {}).get("reason")
            } for w in returned[:2]])
        if can("approver", "programme_lead"):
            # Route seats and the shared independence predicate both govern actionable reviews.
            mine = []
            from apps.api.auth import held_roles
            context = {'held': held_roles(db, actor.identity_id, actor.department_id),
                       'permissions': active_permissions(db, actor.department_id)}
            for w in live:
                if w.status == 'submitted' and w.id not in outdated and reviews.viewer_progress(
                        db, actor, w, current_fingerprint=current_inputs[w.id], context=context)['can_decide']:
                    mine.append(w)
            # ME-28: an amendment says what it is (its changes and their reasons), read from its one change list.
            amendment = {}
            if mine and mine[0].base_publication_id:
                from .published_changes import _disclosed_change, change_list
                lst = change_list(db, mine[0].base_publication_id)
                sent = [c for c in (lst.changes if lst else []) if c.get('workspace_id') == mine[0].id and c['status'] != 'withdrawn']
                reasons = {_disclosed_change(c, db, actor).get('absence_reason') for c in sent}
                amendment = {'amendment': {'changes': len(sent), 'reasons': sorted(reason for reason in reasons if reason)}}
            add(20, "reviews", len(mine), **first(mine), **amendment)
        if can("coordinator", "approver"):
            approved = [w for w in live if w.status == "approved" and w.id not in outdated]
            add(60, "to_publish", len(approved), **first(approved))
        if can("auditor") and not can("coordinator", "approver", "programme_lead"):
            submitted = [w for w in live if w.status == "submitted"]
            add(80, "in_review", len(submitted), **first(submitted))

    from .published_changes import absence_attention
    absence = absence_attention(db, actor)
    if absence:
        # One approved absence already explains the operational overlap for its
        # session; keep one server-owned Home cause and Replace action.
        absence_sessions = absence.pop('session_filter')
        operational = published_operational.home_attention(db, actor, exclude_sessions=absence_sessions)
        if operational:
            items.append((15, operational))
        items.append((15, absence))
    else:
        operational = published_operational.home_attention(db, actor)
        if operational:
            items.append((15, operational))

    P, I = m.Person, m.Identity
    # Own decisions appear only with the same explicit permission enforced by the command.
    my_ids = {actor.identity_id, *(db.scalars(select(I.id).where(I.person_id == actor.person_id)) if actor.person_id else ())}
    R = m.RequestRecord
    pending_requests = availability.pending_request_decisions(db, actor)
    waiting = db.scalar(select(func.count()).select_from(pending_requests.subquery())) or 0
    add(25, "requests", waiting, sample=[{"person": n, "kind": k, "start": aware(a).isoformat(), "end": aware(b).isoformat()}
        for k, a, b, n in db.execute(pending_requests.with_only_columns(R.kind, R.start, R.end, P.display_name)
            .join(P, P.id == R.person_id).order_by(R.start, R.id).limit(2))] if waiting else [])
    if can("approver"):
        pending = scheduling_facts.pending_query(db, actor, decidable=True)
        waiting_facts = db.scalar(select(func.count()).select_from(pending.subquery())) or 0
        add(26, "scheduling_facts", waiting_facts, sample=[scheduling_facts.approval_item(db, actor, row,
            lambda pid: db.get(P, pid).display_name) for row in db.scalars(pending.order_by(
                m.SchedulingFact.created_at, m.SchedulingFact.seq).limit(2))] if waiting_facts else [])
        Po, Pe = m.Policy, m.PolicyException
        self_actions = my_actions(db, actor)
        others = (Po.status == "proposed", *(() if 'rule.activate' in self_actions else (Po.created_by.not_in(my_ids),)))
        proposed = count(Po, *others)
        proposal = db.execute(select(Po.id, Po.name, I.display_name).outerjoin(I, I.id == Po.created_by).where(
            Po.department_id == dept, *others).order_by(Po.created_at, Po.seq).limit(1)).first() if proposed else None
        add(65, "rules", proposed, sample=[{"name": proposal.name, "by": proposal.display_name}] if proposal else [],
            link_target="#rules?source=" + proposal.id if proposal else None)
        exceptions = (Pe.department_id == dept, Pe.status == "proposed",
                      *(() if 'exception.approve' in self_actions else (Pe.created_by.not_in(my_ids),)))
        exception = db.execute(select(Pe.id, Pe.policy_id).where(*exceptions)
                               .order_by(Pe.created_at, Pe.seq).limit(1)).first()
        add(66, "exceptions", count(Pe, *exceptions[1:]),
            link_target=f"#rules?source={exception.policy_id}&exception={exception.id}" if exception else None)
    if can("coordinator"):
        AP = m.AvailabilityPattern
        add(45, "patterns", count(AP, AP.status == "proposed", *availability.not_own(AP, actor)))
        S = m.SwapRequest
        agreed = (S.status.in_(("accepted", "reviewing")), S.expires_at > now)
        add(50, "swaps_to_apply", count(S, *agreed), sample=swap_sample(db, dept, agreed))
    if actor.person_id:
        # shortcut: open, unexpired swaps by stored status; a swap made stale by a newer publication still counts until opened.
        S = m.SwapRequest
        for_me = (S.expires_at > now, or_(and_(S.status == "offered", S.recipient_person_id == actor.person_id),
                                          and_(S.status == "counteroffered", S.proposer_person_id == actor.person_id)))
        add(15, "my_swaps", count(S, *for_me), sample=swap_sample(db, dept, for_me))

    from . import audiences
    C, Receipt = m.DepartmentContent, m.AnnouncementReceipt
    acknowledgements = select(C).outerjoin(Receipt,
        and_(Receipt.content_id == C.id, Receipt.identity_id == actor.identity_id)).where(
            C.department_id == dept, C.kind == "announcement", C.active.is_(True), C.starts_on <= today,
            or_(C.ends_on.is_(None), C.ends_on > today), C.importance == "must_acknowledge",
            Receipt.acknowledged_at.is_(None))
    pending_acknowledgements = 0
    if db.scalar(select(acknowledgements.exists())):
        acknowledgements = audiences.visible_content(db, actor, today, acknowledgements)
        pending_acknowledgements = db.scalar(select(func.count()).select_from(acknowledgements.subquery())) or 0
    add(12, "acknowledgements", pending_acknowledgements, sample=[{"content_id": content_id, "name": name}
        for content_id, name in db.execute(acknowledgements.with_only_columns(C.id, C.name)
            .order_by(C.name, C.id).limit(2))] if pending_acknowledgements else [])

    current = lambda cls: and_(cls.department_id == dept, cls.starts_on <= today, or_(cls.ends_on.is_(None), cls.ends_on > today))
    people = select(func.count()).select_from(m.Person).where(m.Person.organisation_id == actor.organisation_id, m.Person.active.is_(True))
    if can("admin", "credentialer"):
        E = m.Eligibility
        add(55, "no_eligibility", db.scalar(people.where(~m.Person.id.in_(select(E.person_id).where(current(E))))) or 0)
    if can("admin", "coordinator"):
        from .people import current_people
        add(56, "no_rank", current_people(db, actor)['people_without_current_rank'])
    if can("admin"):
        add(57, "no_hours", count(m.Service, m.Service.active.is_(True), m.Service.calendar_id.is_(None)))
    if can(*PERMISSIONS["feedback.triage"]):
        add(58, "feedback", count(m.FeedbackReport, m.FeedbackReport.status == "new"))
    if can("programme_lead"):
        R = m.Rotation
        from .training import catalogue_scope
        add(85, "rotations_soon", count(R, or_(R.starts_on.between(today, today + timedelta(days=13)),
                                               R.ends_on.between(today + timedelta(days=1), today + timedelta(days=14))),
                                       catalogue_scope(db, actor, "rotations", R)))
    if can(*PERMISSIONS["system.operations"]):
        add(5, "failed_notifications", count(m.Outbox, m.Outbox.state == "failed"))
        add(6, "failed_runs", count(m.Job, m.Job.status == "failed", m.Job.completed_at > now - timedelta(days=1)))
    recent = []
    if can(*PERMISSIONS["audit.read"]):
        E = m.AuditEvent
        add(90, "self_approved", count(E, E.created_at > now - timedelta(days=30), E.detail["self_approved"].as_string().is_not(None)))
        # Each command is one change, even when a batch writes one event per request. Separate later Undos of the
        # same batch by correlation; single-record events keep their own row. Count before taking the latest 40.
        group = (func.coalesce(E.detail["batch_id"].as_string(), E.id), E.correlation_id, E.actor_id)
        grouped = select(E.id,
            func.count().over(partition_by=group).label("count"),
            func.row_number().over(partition_by=group, order_by=(E.created_at.desc(), E.seq.desc())).label("position"))\
            .where(E.department_id == dept, E.action.not_like('auth.%'),
                   or_(E.detail["batch_id"].as_string().is_(None), E.detail["count"].as_integer().is_(None))).subquery()
        recent = [{**record(e, exclude=("detail",)), "actor_display_name": name, "count": count,
                   "action": ('request.undo' if e.action.endswith('.undo') else 'request.decide')
                             if e.detail.get('batch_id') else e.action} for e, name, count in db.execute(
            select(E, m.Identity.display_name, grouped.c.count).join(grouped, grouped.c.id == E.id)
            .outerjoin(m.Identity, m.Identity.id == E.actor_id).where(grouped.c.position == 1)
            .order_by(E.created_at.desc(), E.seq.desc()).limit(40))]

    mine = []
    if actor.person_id and can("staff", "coordinator", "approver"):
        # Personal rows and authorized open offers, with Requests' waiting-first order and every request kind.
        # Home only needs public summary words and dates: reasons, rota IDs and workflow metadata stay out.
        fields = {"id", "person_id", "source", "type", "stage", "status", "kind", "start", "end", "starts_on",
                  "ends_on", "first_day", "last_day", "duty_type", "proposer_name", "recipient_name",
                  "leave_type_id", "leave_type_name", "counts_as_unavailable", "effective_status", "decided_at",
                  "audience_pending", "audience_failed", "audience_count", "no_eligible_recipients",
                  "no_eligible_recipients_reason"}
        for row in availability.list_all(db, actor, personal=True, limit=5, include_details=False)["items"]:
            if row["source"] == "scheduling_fact":
                mine.append(row)  # Already the safe shared approval projection, including its structured fields and link.
                continue
            summary = {key: value for key, value in row.items() if key in fields}
            if row['source'] == 'request' and row.get('kind') == 'absence_report' and row.get('person_id') == actor.person_id:
                # Only the reporter gets this typed duty binding and explicitly submitted category.
                summary.update({key: row[key] for key in ('assignment_id', 'absence_category', 'created_at') if key in row})
            if row.get("offered_assignment"):
                summary["offered_assignment"] = {key: row["offered_assignment"][key] for key in ("start", "end")}
            mine.append(summary)

    return {"attention": [item for _, item in sorted(items, key=lambda x: x[0])], "rotas": rotas,
            "my_requests": mine, "recent_changes": recent}


def home(db, actor, *, settings=None):
    """Render Home once per actor and revision stamp, shared across API workers.

    The stamp is cheap enough to compute on every request.  The existing
    workspace projection table supplies the durable cross-process cache; the
    bounded local cache remains only for a department that has no workspace yet.
    """
    now = m.now()
    settings = settings or Settings()
    max_days = settings.workspace_read_max_days
    from .notification_audience import is_test_identity
    stamp = (_home_stamp(db, actor, now), settings.publish_test_people_only, is_test_identity(actor))
    from .read_projections import cached_department
    if db is not None:
        return cached_department(db, actor, 'home', digest([stamp, max_days]),
                                 lambda: _home_uncached(db, actor, max_days=max_days),
                                 audience_values=('home', max_days))
    key = (actor.department_id, actor.identity_id, tuple(sorted(actor.roles)), max_days, stamp)
    with _HOME_CACHE_LOCK:
        cached = _HOME_CACHE.get(key)
        if cached is not None:
            _HOME_CACHE.move_to_end(key)
            return deepcopy(cached)
    value = _home_uncached(db, actor, max_days=max_days)
    with _HOME_CACHE_LOCK:
        _HOME_CACHE[key] = deepcopy(value)
        _HOME_CACHE.move_to_end(key)
        while len(_HOME_CACHE) > _HOME_CACHE_LIMIT:
            _HOME_CACHE.popitem(last=False)
    return value


def swap_sample(db, dept, where):
    """The swap expiring first among `where`: both names and the offered duty's time (nothing from its message)."""
    S, A, P = m.SwapRequest, m.Assignment, m.Person
    rows = db.execute(select(S.proposer_person_id, S.recipient_person_id, A.start, A.end).join(A, A.id == S.first_assignment_id)
                      .where(S.department_id == dept, *where).order_by(S.expires_at, S.seq).limit(1)).all()
    if not rows:
        return []
    proposer, recipient, start, end = rows[0]
    names = dict(db.execute(select(P.id, P.display_name).where(P.id.in_((proposer, recipient)))).all())
    return [{"proposer": names.get(proposer), "recipient": names.get(recipient), "proposer_id": proposer,
             "start": aware(start).isoformat(), "end": aware(end).isoformat()}]
