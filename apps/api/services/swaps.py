"""Staff consent is separate from rota review and atomic official publication."""

import copy
from collections import Counter
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, defer
from packages.contracts import schemas as s
from packages.domain.participation import presence_intervals, replaced_participants
from packages.domain.recurrence import inactive
from packages.domain.time import aware, overlaps
from packages.domain.validation import (authority_intervals, effective_type_definition, evaluate,
                                        participation_start_day, qualified_supervisor, resource_sessions,
                                        workload_predecessors, workload_selected)
from apps.api import models as m
from apps.api.auth import Actor, current_roles, department_roles, require_role
from apps.api.errors import Problem, require
from apps.api.serialization import record
from apps.api.snapshots import (domain_snapshot, snapshot_hash, workspace_shifts, frozen_series_states,
                                inactive_series_shift_ids)
from .access import SWAP_AUTOMATION_ISSUER
from .leave_disclosure import staff_overlap_labels
from .common import scoped, expected, outbox, supersede_outbox, neutral_rota_name
from .person_record import require_person

OPEN_STATES = {"offered", "counteroffered", "accepted", "reviewing"}
# Validator findings that depend only on the person and the duty they would take, never on other duties.
RECIPIENT_DUTY_CODES = {"NOT_ELIGIBLE", "UNAVAILABLE", "RESTRICTION", "EXTERNAL_ROTATION"}
PRIVATE_TIME_WORDS = {
    "UNAVAILABLE": "is unavailable at that time.",
    "RESTRICTION": "has protected time then.",
}
COLLEAGUE_NOT_FREE = "COLLEAGUE_NOT_FREE"
COLLEAGUE_NOT_FREE_MESSAGE = "This colleague is not free at that time."
NO_OTHER_STAFF = "There are no other active staff sign-ins in this department."
UNKNOWN_AUDIENCE_REASON = "Nobody can take this duty under the current rules."
AUDIENCE_NOTICE_TITLE = "Nobody eligible can take this duty"
_AUDIENCE_NOT_FREE = "Colleagues are not free at that time."
AUDIENCE_WORDS = {
    **dict.fromkeys((*PRIVATE_TIME_WORDS, COLLEAGUE_NOT_FREE), _AUDIENCE_NOT_FREE),
    "ATTACHMENT_PAIRING": "Colleagues cannot take this duty while keeping trainee pairings.",
    "CALENDAR_CLOSED": "The clinic or location is closed for this duty.",
    "CALENDAR_DATE_SET_UNRESOLVED": "The opening dates for this duty need review.",
    "CALENDAR_TIME_UNRESOLVED": "The opening times for this duty need review.",
    "CALENDAR_UNKNOWN": "The opening calendar for this duty is unavailable.",
    "CAPABILITY_GAP": "Taking this duty would leave required skills uncovered.",
    "CAPACITY_OVERFLOW": "Taking this duty would use extra places.",
    "CATALOGUE_VALIDITY_CHANGED": "The setup for this duty has changed.",
    "CONFIGURED_DURATION_REQUIRED": "This duty must be booked for its full configured duration.",
    "CONSECUTIVE_DAYS": "Taking this duty would exceed the consecutive-day limit.",
    "CONSECUTIVE_WEEKENDS": "Taking this duty would exceed the consecutive-weekend limit.",
    "COVERAGE_GAP": "Taking this duty would leave required cover short.",
    "COVERAGE_TARGET": "Taking this duty would leave cover below its target.",
    "CUSTOM_FIELD_INVALID": "This duty has an invalid configured value.",
    "DISTINCT_PERSON_GAP": "Taking this duty would leave too few distinct qualified people.",
    "DUPLICATE_PARTICIPATION": "Colleagues cannot be counted twice on the same duty.",
    "DURATION_LIMIT": "This duty exceeds its configured duration limit.",
    "EXTERNAL_CLASSIFICATION_UNKNOWN": "External duties need classification before this transfer can be checked.",
    "EXTERNAL_LOCATION_UNKNOWN": "External duty locations need review before this transfer can be checked.",
    "EXTERNAL_ROTATION": _AUDIENCE_NOT_FREE,
    "EXTRA_COVER_NOT_OVERTIME": "This extra-cover duty requires overtime.",
    "FIXED_SESSION_UNITS_REQUIRED": "This duty needs a complete-assignment counting setup.",
    "FORBIDDEN_TRANSITION": "Taking this duty would create a forbidden next-day combination.",
    "FULL_ASSIGNMENT_REQUIRED": "Colleagues must be able to take the full duty.",
    "LEAD_GAP": "Taking this duty would leave its team-lead requirement unmet.",
    "LEAD_NOT_QUALIFIED": "The team-lead requirements for this duty are unmet.",
    "LOCATION_CLOSED": "The place for this duty is closed.",
    "LOCATION_INACTIVE": "The place for this duty is inactive at that time.",
    "LOCATION_TYPE": "This duty cannot use its configured place type.",
    "LOCATION_TYPE_INACTIVE": "The place type for this duty is inactive at that time.",
    "MAXIMUM_PARTICIPANTS": "Taking this duty would exceed the number of people allowed.",
    "MAX_DUTIES": "Taking this duty would exceed duty limits.",
    "MAX_HOURS": "Taking this duty would exceed working-hours limits.",
    "MIN_REST": "Taking this duty would leave too little rest between duties.",
    "NOT_ELIGIBLE": "Colleagues do not meet the eligibility requirements for the full duty.",
    "OPEN_EXTRA_COVER": "This duty still needs extra cover.",
    "PARTICIPATION_INVALID": "The planned attendance for this duty needs review.",
    "POOLED_RESOURCE_CAPACITY": "Taking this duty would exceed shared resource capacity.",
    "QUOTA_ABOVE_MAXIMUM": "Taking this duty would exceed duty quotas.",
    "QUOTA_ABOVE_TARGET": "Taking this duty would exceed quota targets.",
    "QUOTA_BELOW_MINIMUM": "Taking this duty would leave required quotas unmet.",
    "QUOTA_BELOW_TARGET": "Taking this duty would leave quota targets unmet.",
    "QUOTA_CONTEXT_MISSING": "The complete quota period is needed to check this transfer.",
    "QUOTA_PRESENCE_INVALID": "Attendance data needs review before quotas can be checked.",
    "QUOTA_PRORATION_UNRESOLVED": "The adjusted quotas for this duty need review.",
    "QUOTA_SCOPE_LIMIT": "This duty's quota scope is too large to check.",
    "QUOTA_TIME_AMBIGUOUS": "The quota period boundaries need review.",
    "REQUIREMENT_OUTSIDE_SHIFT": "A requirement for this duty falls outside its scheduled time.",
    "RESOURCE_CAPACITY": "There is too little space for concurrent duties at that time.",
    "RESOURCE_INACTIVE": "A resource required for this duty is inactive.",
    "RESOURCE_INTERVAL_OUTSIDE_SHIFT": "A resource is required outside this duty's scheduled time.",
    "RESOURCE_LOCATION": "A required resource cannot be used at this duty's location.",
    "RESOURCE_UNKNOWN": "A resource required for this duty is unavailable.",
    "ROTATION_LEAVE_NEEDS_APPROVAL": _AUDIENCE_NOT_FREE,
    "ROTATION_LEAVE_NOT_ALLOWED": _AUDIENCE_NOT_FREE,
    "RULE_EXCEPTION": "An approved exception applies to the duty check.",
    "SCOPED_COVERAGE_GAP": "Taking this duty would leave a required group without enough cover.",
    "SCOPED_PARTICIPANT_LIMIT": "Taking this duty would exceed a group staffing limit.",
    "SERVICE_INACTIVE": "A service for this duty is inactive at that time.",
    "SERVICE_REQUIRED": "This duty needs a service before it can be transferred.",
    "SHARED_DUTY_UNAPPROVED": "This duty is not configured for shared cover.",
    "SUPERVISION_GAP": "Taking this duty would leave too little qualified supervision.",
    "SUPERVISION_LEVEL": "Taking this duty would leave supervision unevenly distributed.",
    "SUPERVISION_PAIRING": "Taking this duty would leave required supervision pairings unmet.",
    "SUPERVISION_RANK": "Taking this duty would leave supervision rank requirements unmet.",
    "SUPERVISION_UNCONFIGURED": "Trainee supervision requirements need to be configured for this duty.",
    "SUPERVISOR_CAPACITY": "Taking this duty would exceed supervision capacity.",
    "SUPERVISOR_REQUIRED": "Duties need assigned supervisors before this limit can be checked.",
    "SUPERVISOR_SCOPED_CAPACITY": "Taking this duty would exceed supervision capacity for a group.",
    "SURPLUS_COVER": "Taking this duty would leave more cover than needed.",
    "SWAP_EXPIRY": "Consent would expire after a duty starts.",
    "SWAP_RETURN_REQUIRED": "Colleagues have no future duty to offer in return on this rota.",
    "TARGET_OVERRIDE_STALE": "A quota adjustment needs review before this duty can be transferred.",
    "TIME_OVERLAP": "Taking this duty would overlap other duties.",
    "TRAVEL_BUFFER": "Taking this duty would leave too little travel time.",
    "TYPE_INACTIVE": "This duty type is inactive at that time.",
    "TYPE_VERSION_CHANGED": "The duty type has changed and needs review.",
}
SAFE_AUDIENCE_REASONS = frozenset(AUDIENCE_WORDS.values()) | {NO_OTHER_STAFF, UNKNOWN_AUDIENCE_REASON}
# One source: a code the audience table words as "not free" is also neutral on a staff reader's row.
NOT_FREE_CODES = frozenset(code for code, words in AUDIENCE_WORDS.items() if words == _AUDIENCE_NOT_FREE)
OPTIONAL_NOTE_COMMANDS = {"swap.offer", "swap.accept", "swap.counteroffer", "swap.confirm", "swap.withdraw"}


def optional_swap_note(kind, reason):
    reason = reason.strip()
    return "" if kind == "swap.withdraw" and reason == "Routine update" else reason


def can_read_private_time(actor):
    # The pair that reads every swap in the department and a colleague's availability on their card.
    return actor is not None and actor.permits("coordinator", "approver")


def staff_refusal(value):
    """Project machine-generated refusal evidence without touching the stored event."""
    if isinstance(value, dict):
        if value.get("code") in NOT_FREE_CODES or value.get("error_code") in NOT_FREE_CODES:
            return {"code": COLLEAGUE_NOT_FREE, "message": COLLEAGUE_NOT_FREE_MESSAGE}
        return {key: staff_refusal(child) for key, child in value.items()}
    if isinstance(value, list):
        result = []
        for child in value:
            projected = staff_refusal(child)
            if not isinstance(projected, dict) or projected.get("code") != COLLEAGUE_NOT_FREE or projected not in result:
                result.append(projected)
        return result
    if isinstance(value, str):
        if value in NOT_FREE_CODES:
            return COLLEAGUE_NOT_FREE
        if value.startswith("The person taking ") and value.endswith(tuple(PRIVATE_TIME_WORDS.values())):
            return COLLEAGUE_NOT_FREE_MESSAGE
    return value


def eligibility_for_reader(actor, result):
    if result["eligible"] or can_read_private_time(actor):
        return result
    shown = staff_refusal(result)
    # The headline is the first finding's sentence, so it cannot say more than the row does.
    return {**shown, "message": shown["findings"][0]["message"]} if shown["findings"] else shown


def stored_audience_reason(message):
    if message is None:
        return None
    # Historical notices repeated the title before the explanation.
    reason = message.removeprefix(AUDIENCE_NOTICE_TITLE + ". ")
    return reason if reason in SAFE_AUDIENCE_REASONS else UNKNOWN_AUDIENCE_REASON


def read_swap_result(actor, result):
    """Apply current reader permissions to projections, including old command receipts."""
    if not can_read_private_time(actor):
        result = {**result, "prepare_refusal": staff_refusal(result.get("prepare_refusal")),
                  "publish_refusal": staff_refusal(result.get("publish_refusal"))}
    if "no_eligible_recipients_reason" in result:
        result = {**result, "no_eligible_recipients_reason": stored_audience_reason(result["no_eligible_recipients_reason"])}
    return result


def read_command_result(actor, result):
    return {key: read_swap_result(actor, value) if key in {"swap", "undone"} and isinstance(value, dict) else value
            for key, value in result.items()}


def staff_identity(db, identity_id, person_id, department_id, cache=None):
    if cache is None:
        rows = db.execute(select(m.Identity, m.Person, m.Department, m.Organisation)
            .join(m.Person, m.Person.id == m.Identity.person_id)
            .join(m.Department, m.Department.id == department_id)
            .join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
            .where(m.Identity.id == identity_id, m.Person.id == person_id)).first()
        if rows is None:
            return False
        # Keep the role-calendar rows in the identity map until current_roles has evaluated them.
        identity, person, department, _organisation = rows
    else:
        identity = cache[m.Identity].get(identity_id)
        person = cache[m.Person].get(person_id)
        department = cache[m.Department][department_id]
    return bool(identity and not identity.disabled and identity.person_id == person_id
                and person and person.active and person.organisation_id == department.organisation_id
                and "staff" in (cache["roles"].get(identity_id, ()) if cache is not None
                                else current_roles(db, identity_id, department_id)))


def own_person(db, actor):
    require_role(actor, "staff")
    require(actor.person_id is not None and staff_identity(db, actor.identity_id, actor.person_id, actor.department_id),
            "STAFF_BINDING_REQUIRED", "A current staff role and active person binding are required", 403)
    return actor.person_id


def available_staff(db, person_id, department_id):
    return any(staff_identity(db, identity_id, person_id, department_id) for identity_id in db.scalars(
        select(m.Identity.id).where(m.Identity.person_id == person_id, m.Identity.disabled.is_(False))))


def assignment(db, publication, assignment_id, person_id):
    participant = db.get(m.Assignment, assignment_id)
    shift = db.get(m.Shift, participant.shift_id) if participant else None
    require(participant is not None and shift is not None and shift.workspace_id == publication.workspace_id
            and participant.person_id == person_id,
            "SWAP_ASSIGNMENT_NOT_FOUND", "Choose your own assignment from this publication", 404)
    require(not shift.cancelled and not shift.locked, "SWAP_ASSIGNMENT_UNAVAILABLE", "Closed or locked sessions cannot be offered", 409)
    require(not inactive(frozen_series_states([record(shift)], publication)[0]),
            'SWAP_ASSIGNMENT_UNAVAILABLE', 'Closed sessions cannot be offered', 409)
    require(aware(participant.start) > m.now(), "SWAP_ALREADY_STARTED", "Only future assignments can be offered", 409)
    require(in_force(db, publication, shift), "SWAP_STALE", "A later rota replaced this duty's day; start from the current rota", 409)
    return participant, shift


def in_force(db, publication, row, zone=None):
    """The duty (a Shift or Assignment row) is still on the published rota: not on a day a later rota replaced."""
    from .schedule import in_force_shifts, zone_of
    return not publication.effective_until or bool(in_force_shifts(publication, [row], zone or zone_of(db, publication.department_id)))


def effective_status(db, row, cache=None):
    if row.status not in OPEN_STATES:
        return row.status
    if aware(row.expires_at) <= m.now():
        return "expired"
    pub = cache[m.Publication][row.publication_id] if cache is not None else db.get(m.Publication, row.publication_id)
    if not pub.current:
        return "stale"
    if pub.effective_until and not all(in_force(db, pub, cache[m.Assignment].get(a) if cache is not None else db.get(m.Assignment, a),
                                              cache["zone"] if cache is not None else None)
                                       for a in (row.first_assignment_id, row.second_assignment_id) if a):
        return "stale"
    if not staff_identity(db, row.proposer_id, row.proposer_person_id, row.department_id, cache):
        return "access_revoked"
    if row.recipient_id and not staff_identity(db, row.recipient_id, row.recipient_person_id, row.department_id, cache):
        return "access_revoked"
    return row.status


def assert_live(db, row):
    state = effective_status(db, row)
    require(state in OPEN_STATES, "SWAP_" + state.upper(), "This consent is no longer actionable: " + state.replace("_", " "), 409)
    pub = db.get(m.Publication, row.publication_id)
    first, one = assignment(db, pub, row.first_assignment_id, row.proposer_person_id)
    require(aware(row.expires_at) <= aware(first.start), "SWAP_EXPIRY", "Consent cannot extend beyond the offered assignment")
    if row.second_assignment_id:
        second, two = assignment(db, pub, row.second_assignment_id, row.recipient_person_id)
        require(aware(row.expires_at) <= aware(second.start), "SWAP_EXPIRY", "Consent expires before either assignment begins")
        require(one.id != two.id, "INVALID_SWAP", "Select duties from two different sessions")
    return pub


def require_unique_offer(db, department_id, assignment_id, recipient_person_id, exclude_id=None):
    query = select(m.SwapRequest).where(
        m.SwapRequest.department_id == department_id, m.SwapRequest.first_assignment_id == assignment_id,
        m.SwapRequest.recipient_person_id == recipient_person_id,
        m.SwapRequest.status.in_(OPEN_STATES), m.SwapRequest.expires_at > m.now())
    if exclude_id:
        query = query.where(m.SwapRequest.id != exclude_id)
    require(not any(effective_status(db, row) in OPEN_STATES for row in db.scalars(query)),
            "SWAP_DUPLICATE", "This duty is already offered to this colleague", 409)


def require_offer_capacity(db, department_id, proposer_id):
    query = select(func.count()).select_from(m.SwapRequest).where(
        m.SwapRequest.department_id == department_id, m.SwapRequest.proposer_id == proposer_id,
        m.SwapRequest.status.in_(OPEN_STATES), m.SwapRequest.expires_at > m.now())
    require(db.scalar(query) < db.get(m.Department, department_id).swap_open_offer_cap,
            "SWAP_LIMIT", "Close existing invitations before opening more", 409)


def public_offer(actor, row):
    return (row.open_offer and not actor.permits("coordinator", "approver")
            and actor.person_id not in {row.proposer_person_id, row.recipient_person_id})


def unclaimed_open():
    row = m.SwapRequest
    return and_(row.open_offer.is_(True), row.recipient_person_id.is_(None),
                row.status == "offered", row.expires_at > m.now())


def get_scoped(db, actor, object_id, responding=False):
    row = scoped(db, m.SwapRequest, object_id, actor)
    if not actor.permits("coordinator", "approver"):
        own_person(db, actor)
    require(actor.permits("coordinator", "approver") or (actor.permits("staff")
            and (actor.person_id in {row.proposer_person_id, row.recipient_person_id}
                 # Replies validate the exact transfer below; racing replies get a revision conflict, never thread data.
                 or (responding and row.open_offer)
                 or row.id in visible_open_ids(db, actor, row.id))),
            "NOT_FOUND", "Swap request not found", 404)
    return row


def latest_thread_id(db, actor, object_id):
    """Resolve an old Requests link; callers still apply the current row's full visibility query.

    Command targets and receipts retain their original IDs. Every traversal step stays in this department.
    """
    S = m.SwapRequest
    thread = select(S.id).where(S.id == object_id, S.department_id == actor.department_id).cte(
        'swap_thread', recursive=True)
    child = aliased(S)
    thread = thread.union(select(child.id).join(thread, child.reopened_from_id == thread.c.id).where(
        child.department_id == actor.department_id))
    later = aliased(S)
    return db.scalar(select(thread.c.id).where(~select(later.id).where(
        later.reopened_from_id == thread.c.id, later.department_id == actor.department_id).exists()).limit(1))


def summary(publication, assignment_id, index=None):
    if not assignment_id:
        return None
    match = index.get(assignment_id) if index is not None else next(
        ((shift, p) for shift in publication.snapshot["shifts"] for p in shift["participants"] if p["id"] == assignment_id), None)
    if match is None:
        return None
    shift, participant = match
    return {"assignment_id": assignment_id, "person_id": participant["person_id"],
            "person": publication.snapshot["people"][participant["person_id"]]["display_name"],
            "type": shift["type_snapshot"]["name"], "location": publication.snapshot["locations"][shift["location_id"]]["name"],
            "start": participant["start"], "end": participant["end"],
            "participation": participant["participation"], "overtime": participant["overtime"],
            "segments": participant.get("segments", []),
            "publication_id": publication.id, "publication_sequence": publication.sequence}


def projection(db, row, cache=None, actor=None):
    publication = cache[m.Publication][row.publication_id] if cache is not None else db.get(m.Publication, row.publication_id)
    person = lambda pid: cache[m.Person][pid] if cache is not None else db.get(m.Person, pid)
    index = cache["assignments"][row.publication_id] if cache is not None else None
    audience = cache["audience_states"] if cache is not None else audience_states(db, [row])
    refusal = None
    if row.status == "accepted" and row.workspace_id is None:
        refusal = cache["prepare_refusals"].get(row.id) if cache is not None else db.scalar(
            select(m.SwapEvent).where(m.SwapEvent.swap_id == row.id, m.SwapEvent.action == "prepare_refused")
            .order_by(m.SwapEvent.created_at.desc(), m.SwapEvent.seq.desc()).limit(1))
    publish_refusal = None
    if row.workspace_id is not None and row.status != "published":
        publish_refusal = cache["publish_refusals"].get(row.id) if cache is not None else db.scalar(
            select(m.SwapEvent).where(m.SwapEvent.swap_id == row.id, m.SwapEvent.action == "publish_refused")
            .order_by(m.SwapEvent.created_at.desc(), m.SwapEvent.seq.desc()).limit(1))
    audience_facts = audience.get(row.id, {"audience_pending": False, "audience_failed": False})
    delivery_status = ('audience_pending' if audience_facts.get('audience_pending') else
                       'audience_failed' if audience_facts.get('audience_failed') else
                       'no_recipients' if audience_facts.get('no_eligible_recipients') else
                       'notified')
    result = {**record(row, exclude=("expected_content_hash",)), "effective_status": effective_status(db, row, cache),
            "prepare_refusal": refusal.detail if refusal else None,
            "publish_refusal": publish_refusal.detail if publish_refusal else None,
            **audience_facts, "delivery_status": delivery_status,
            "proposer_name": person(row.proposer_person_id).display_name,
            "recipient_name": person(row.recipient_person_id).display_name if row.recipient_person_id else None,
            "offered_assignment": summary(publication, row.first_assignment_id, index),
            "counteroffered_assignment": summary(publication, row.second_assignment_id, index)}
    if actor is not None:
        result = read_swap_result(actor, result)
    if actor is not None and public_offer(actor, row):
        return {**{key: result[key] for key in ("id", "revision", "kind", "open_offer", "status", "effective_status",
            "expires_at", "created_at", "publication_id", "proposer_person_id", "proposer_name", "offered_assignment")},
            "audience_pending": result.get("audience_pending", False), "audience_count": result.get("audience_count"),
            "no_eligible_recipients": result.get("no_eligible_recipients", False),
            "no_eligible_recipients_reason": result.get("no_eligible_recipients_reason"),
            "delivery_status": result.get("delivery_status"),
            "recipient_person_id": None, "recipient_name": None, "second_assignment_id": None, "counteroffered_assignment": None}
    return result


def events_for(db, actor, row, thread=False):
    """Consent history belongs to its participants, including when Undo leads to a different recipient."""
    if public_offer(actor, row):
        return []
    reviewer = actor.permits("coordinator", "approver")
    involved = lambda item: reviewer or actor.person_id in {item.proposer_person_id, item.recipient_person_id}
    rows, at = {}, row
    while at is not None:
        if involved(at):
            rows[at.id] = at
        at = db.get(m.SwapRequest, at.reopened_from_id) if thread and at.reopened_from_id else None
    events = db.scalars(select(m.SwapEvent).where(m.SwapEvent.swap_id.in_(rows))
                       .order_by(m.SwapEvent.created_at, m.SwapEvent.seq))
    result = [record(event) for event in events if event.action != "reopened" or not rows[event.swap_id].reopened_from_id
              or involved(db.get(m.SwapRequest, rows[event.swap_id].reopened_from_id))]
    if not can_read_private_time(actor):
        for event in result:
            if event["action"] in {"prepare_refused", "publish_refused"}:
                event["detail"] = staff_refusal(event["detail"])
                event["reason"] = staff_refusal(event["reason"])
    return result


def list_cache(db, actor, rows, summaries=True):
    """The same projections and live-consent checks as commands, with bounded reads for a whole list page."""
    ids = {m.Person: {pid for r in rows for pid in (r.proposer_person_id, r.recipient_person_id) if pid},
           m.Identity: {pid for r in rows for pid in (r.proposer_id, r.recipient_id) if pid},
           m.Publication: {r.publication_id for r in rows},
           m.Assignment: {aid for r in rows for aid in (r.first_assignment_id, r.second_assignment_id) if aid}}
    cache = {model: {row.id: row for row in db.scalars(select(model).where(model.id.in_(keys)).options(
                *([defer(m.Publication.snapshot), defer(m.Publication.validation)] if model is m.Publication and not summaries else [])))}
             for model, keys in ids.items()}
    department = db.get(m.Department, actor.department_id)
    cache[m.Department] = {department.id: department}
    cache["zone"] = db.get(m.Organisation, actor.organisation_id).timezone
    cache["roles"] = department_roles(db, actor.department_id)
    cache["prepare_refusals"] = {event.swap_id: event for event in db.scalars(select(m.SwapEvent).where(
        m.SwapEvent.swap_id.in_([r.id for r in rows if r.status == "accepted" and r.workspace_id is None]),
        m.SwapEvent.action == "prepare_refused").order_by(m.SwapEvent.created_at, m.SwapEvent.seq))}
    cache["publish_refusals"] = {event.swap_id: event for event in db.scalars(select(m.SwapEvent).where(
        m.SwapEvent.swap_id.in_([r.id for r in rows if r.workspace_id is not None and r.status != "published"]),
        m.SwapEvent.action == "publish_refused").order_by(m.SwapEvent.created_at, m.SwapEvent.seq))}
    if summaries:
        cache["audience_states"] = audience_states(db, rows)
        cache["assignments"] = {pub.id: {p["id"]: (shift, p) for shift in pub.snapshot["shifts"] for p in shift["participants"]}
                                for pub in cache[m.Publication].values()}
    return cache


def rule_neighbours(data, shifts, changed, people, rules):
    """Focus rest on either affected endpoint and next-day rules on adjacent duties."""
    neighbours = set()
    zone = data["organisation"]["timezone"]
    rest = [p for p in rules if p["definition"]["rule"]["kind"] == "min_rest" and p["status"] == "active"]
    transitions = any(p["definition"]["rule"]["kind"] == "forbidden_transition" and p["status"] == "active" for p in rules)
    if not rest and not transitions:
        return neighbours
    for pid in people:
        duties = [(shift, p, aware(p["start"]), aware(p["end"])) for shift in shifts if not shift["cancelled"]
                  for p in shift["participants"] if p["person_id"] == pid]
        for policy in rest:
            selected, _ = workload_selected(data, policy, pid, duties)
            for previous, current in workload_predecessors(selected):
                if previous[0]["id"] in changed or current[0]["id"] in changed:
                    neighbours.update((previous[0]["id"], current[0]["id"]))
        if transitions:
            days = {participation_start_day(p, zone) for s, p, _, _ in duties if s["id"] in changed}
            neighbours.update(s["id"] for s, p, _, _ in duties
                              if any(abs((participation_start_day(p, zone) - day).days) <= 1 for day in days))
    return neighbours


class Eligibility:
    """One current, in-memory amendment context per lookup; trials use the shared incremental validator."""

    def __init__(self, db, publication, *, audience=False):
        from .schedule import in_force_snapshot
        preview = m.Workspace(id=m.uid(), department_id=publication.department_id, status="draft",
            starts_on=publication.starts_on, ends_on=publication.ends_on, base_publication_id=publication.id,
            revision=1, content_revision=1, rule_adjustments=[], target_overrides=[])
        # Quotas count these copied duties as the candidate amendment's own work.
        own = [{**shift, 'workspace_id': preview.id} for shift in in_force_snapshot(db, publication)["shifts"]]
        self.data = domain_snapshot(db, preview, own_shifts=own, include_fairness=False)
        original_focus = self.data['focus_ids']
        from .published_changes import validation_scope
        validation_scope(db, preview, self.data, own=own)
        self.data['focus_ids'] = original_focus
        self.data.pop("_published_session_checks", None)
        self.data.pop("person_rule_subjects", None)
        self.data.pop("quota_subjects", None)
        from .staffing import people_index
        self.index = people_index(self.data)
        self.audience = audience
        self.data = {**self.index['fast'], '_person_dates_cache': self.index['dates'], '_participation_cache': {}}
        self.supervision_qualification = {**self.data, '_supervision_commitments': {}}
        from packages.domain.quotas import applicable_policies
        self.data['_quota_policies'] = applicable_policies(self.data)
        self.focus = set(self.data['focus_ids'])
        self.shifts = {shift["id"]: shift for shift in self.index["live"]}
        self.assignments = {p["id"]: (shift, p) for shift in self.index["live"] for p in shift["participants"]}
        self.spans = self.index["times"]
        # Swaps never change session envelopes or locations; prepare their shared location/time index once.
        self.data['_resource_shifts'] = self.index['live']
        if self.shifts:
            first = next(iter(self.shifts.values()))
            resource_sessions(self.data, self.index['live'], first['location_id'], *self.spans[first['id']])
        self.by_span = {}
        for sid, span in self.spans.items():
            self.by_span.setdefault(span, set()).add(sid)
        self.by_person = {pid: {s["id"] for s, _, _, _ in duties} for pid, duties in self.index["duties"].items()}
        self.near = {}
        self.row_contexts = {}

    def pair(self, first_id, second_id, expires_at=None, recipient_id=None, *, public_only=False):
        self.offered_blocked = False  # early refusals (e.g. expiry) never evaluate the offered duty
        one, first = self.assignments[first_id]
        two, second = self.assignments[second_id] if second_id else (None, None)
        if second is not None and expires_at is not None and aware(expires_at) > aware(second["start"]):
            message = "The proposed expiry must precede both assignments"
            return s.SwapEligibility(eligible=False, message=message,
                findings=[{"code": "SWAP_EXPIRY", "message": message}]).model_dump()
        recipient_id = second["person_id"] if second is not None else recipient_id
        people = {first["person_id"], recipient_id}
        replacements = {first_id: recipient_id}
        changed = {one["id"]}
        if second is not None:
            replacements[second_id] = first["person_id"]
            changed.add(two["id"])
        # Peers remain visible to pooled cover/limits; only the exchanged sessions change their participants.
        def touching(ids):
            spans = {self.spans[sid] for sid in ids}
            for span in spans - self.near.keys():
                self.near[span] = {sid for other, keys in self.by_span.items() if overlaps(*span, *other) for sid in keys}
            return set().union(*(self.near[span] for span in spans))
        adjacent = touching(changed) & self.focus
        touched = (changed & self.focus) if self.audience else adjacent
        # Keep whole person histories and every overlapping peer needed by cover/supervision/session checks.
        context = set().union(*(self.by_person.get(pid, set()) for pid in people))
        context.update(touching(adjacent))
        shifts = []
        for sid in sorted(context):
            shift = self.shifts[sid]
            if sid in changed:
                original = shift["participants"]
                candidate = {**shift, "participants": replaced_participants(
                    original, replacements, redirect_supervision=False)}
                definition = effective_type_definition(self.data, candidate)

                def qualified_link(participant, old_person, new_person):
                    trainees = [p for p in original if p.get('supervisor_person_id') == old_person]
                    if not trainees or not definition.get('supervision'):
                        return True
                    incoming = next(p for p in candidate['participants'] if p['id'] == participant['id'])
                    return all(qualified_supervisor(self.supervision_qualification, candidate, incoming, a, b,
                                                     definition=definition)
                               for trainee in trainees for lo, hi in presence_intervals(trainee)
                               for a, b in authority_intervals(self.data, lo, hi))

                shift = {**shift, "participants": replaced_participants(
                    original, replacements, redirect_supervision=qualified_link)}
            shifts.append(shift)
        neighbours = rule_neighbours(self.data, shifts, changed, people, self.index["fast"]["policies"]) & self.focus
        zone = ZoneInfo(self.data["organisation"]["timezone"])
        focus = touched | neighbours
        present = frozenset(people | {p["person_id"] for shift in shifts for p in shift["participants"]})
        if present not in self.row_contexts:
            self.row_contexts[present] = {name: [r for pid in sorted(present) for r in by_person.get(pid, ())]
                for name, by_person in self.index["rows_of"].items() if name in self.data}
        trial = {**self.data, "shifts": shifts, "focus_ids": sorted(focus),
            "_partial": {"shifts": touched, "people": people, "include_resources": True,
                         **({'coverage_focus': touched} if self.audience else {})}, "quota_subjects": people,
            **self.row_contexts[present],
            "quota_dates": (min(self.spans[s][0] for s in changed).astimezone(zone).date().isoformat(),
                            max(self.spans[s][1] for s in changed).astimezone(zone).date().isoformat())}
        result = evaluate(trial, explain_findings=False)
        # Keep existing adjacent-session blockers, but only inspect those peers when the swap itself can pass.
        if adjacent - touched and not any(f['severity'] == 'error' for f in result['session_findings'] + result['person_findings']):
            result = evaluate({**trial, 'focus_ids': sorted(adjacent | neighbours),
                '_partial': {'shifts': adjacent, 'people': people, 'include_resources': True}}, explain_findings=False)
        # Staff receive the validator's messages, never private input rows or unrelated duty/person identifiers.
        errors = [f for f in result["session_findings"] + result["person_findings"] if f["severity"] == "error"]
        # The recipient cannot take the offered duty whatever they give back (see `recipient`).
        self.offered_blocked = any(f["code"] in RECIPIENT_DUTY_CODES and f.get("shift_id") == one["id"]
                                   and f.get("person_id") == recipient_id for f in errors)
        findings = staff_overlap_labels(errors)
        findings = [{"code": f["code"], "message": self.explain(f, {one["id"]} if public_only else changed)}
                    for f in findings]
        return s.SwapEligibility(eligible=not findings, message=findings[0]["message"] if findings else None,
                                 findings=findings).model_dump()

    def explain(self, finding, public_ids):
        """Name only the offered duty or duties explicitly selected by the participants."""
        pid, sid = finding.get('person_id'), finding.get('shift_id')
        shift = self.shifts.get(sid) if sid in public_ids else None
        typ = shift['type_snapshot']['name'] if shift else 'a duty in this swap'
        if finding['code'] == 'NOT_ELIGIBLE':
            from packages.domain.validation import effective_type_definition, qualification_failures
            failures = qualification_failures(self.data, pid, shift, effective_type_definition(self.data, shift),
                aware(finding.get('start') or shift['start']), aware(finding.get('end') or shift['end'])) if shift else []
            clinic = next((self.data['services'][f['service_id']]['name'] for f in failures if f['code'] == 'ELIGIBILITY_MISSING'), typ)
            return f'The person taking this duty is not eligible for {clinic} for the full duty.'
        if finding['code'] in PRIVATE_TIME_WORDS:
            return f"The person taking {typ} {PRIVATE_TIME_WORDS[finding['code']]}"
        if finding['code'] == 'TIME_OVERLAP':
            start = (finding.get('start') or shift.get('start')) if shift else None
            when = aware(start).astimezone(ZoneInfo(self.data['organisation']['timezone'])).strftime('%a %d %b %H:%M') if start else ''
            return f'A person in this swap already has another duty {when}. Offer the overlapping duty in return or choose a different duty.'
        reasons = {
            'MIN_REST': 'This swap leaves too little rest between duties.',
            'FORBIDDEN_TRANSITION': 'These duties cannot be worked on consecutive days.',
            'QUOTA_ABOVE_MAXIMUM': 'This swap would exceed a person’s duty limit.',
            'MAX_DUTIES': 'This swap would exceed a person’s duty limit.',
            'QUOTA_BELOW_MINIMUM': 'This swap would leave a person below their required duty count.',
            'CAPABILITY_GAP': 'This swap would leave a duty without the required skills.',
            'SUPERVISION_PAIRING': 'This swap would leave a duty without the required supervision.',
            'RESOURCE_CAPACITY': 'Too many people would work at the same place at that time.',
            'CALENDAR_CLOSED': 'The clinic or location is closed for this duty.',
        }
        return reasons.get(finding['code'], 'This swap fails a required scheduling check. Ask the coordinator to arrange cover.')

    def recipient(self, first_id, person_id, expires_at=None, kind="exchange"):
        if kind == "give":
            return self.pair(first_id, None, expires_at, recipient_id=person_id, public_only=True)
        one, _ = self.assignments[first_id]
        failures = []
        for shift_id in sorted(self.by_person.get(person_id, set()) & self.focus):
            shift = self.shifts[shift_id]
            if shift_id == one["id"] or inactive(shift) or shift["locked"]:
                continue
            for p in shift["participants"]:
                if p["person_id"] == person_id and aware(p["start"]) > m.now():
                    result = self.pair(first_id, p["id"], expires_at, public_only=True)
                    if result["eligible"]:
                        return result
                    failures.extend(result["findings"])
                    if self.offered_blocked:
                        # One trial per such colleague instead of one per return duty they hold.
                        return s.SwapEligibility(eligible=False, message=result["message"],
                                                 findings=result["findings"]).model_dump()
        if not failures:
            failures = [{"code": "SWAP_RETURN_REQUIRED", "message": "This colleague has no future duty available to offer in return on this rota."}]
        findings = list({(f["code"], f["message"]): f for f in failures}.values())
        return s.SwapEligibility(eligible=False, message=findings[0]["message"], findings=findings).model_dump()


def require_eligible(actor, result):
    if not result["eligible"]:
        shown = eligibility_for_reader(actor, result)
        raise Problem(422, "SWAP_INELIGIBLE", shown["message"] or "This swap fails mandatory validation",
                      {"findings": shown["findings"]})


def nobody_reason(failures):
    """Use the commonest audience explanation, with no text from individual findings."""
    reasons = Counter(AUDIENCE_WORDS.get(f["code"], UNKNOWN_AUDIENCE_REASON) for f in failures)
    return max(reasons, key=lambda reason: (reasons[reason], reason)) if reasons else NO_OTHER_STAFF


def eligible_staff(db, publication, assignment_id, expires_at, kind, *, failures=None, stop_after_first=False):
    """Freeze the invitation audience once, sharing one validator context across all current staff."""
    department = db.get(m.Department, publication.department_id)
    roles = department_roles(db, department.id)
    pairs = list(db.execute(select(m.Identity, m.Person).join(m.Person, m.Person.id == m.Identity.person_id).where(
        m.Identity.id.in_([iid for iid, names in roles.items() if "staff" in names]),
        m.Identity.disabled.is_(False), m.Person.active.is_(True),
        m.Person.organisation_id == department.organisation_id)))
    cache = {m.Department: {department.id: department}, m.Identity: {i.id: i for i, _ in pairs},
             m.Person: {p.id: p for _, p in pairs}, "roles": roles}
    proposer = db.get(m.Assignment, assignment_id).person_id
    people = {p.id for i, p in pairs if p.id != proposer and staff_identity(db, i.id, p.id, department.id, cache)}
    if not people:
        return set()
    preview = Eligibility(db, publication, audience=True)
    eligible = set()
    for pid in sorted(people):
        result = preview.recipient(assignment_id, pid, expires_at, kind)
        if result['eligible']:
            eligible.add(pid)
            if stop_after_first:
                break
        elif failures is not None:
            failures.extend(staff_refusal(result['findings']))
    return eligible


def audience_states(db, rows):
    keys = [f"swap:{r.id}:1:{'reopened' if r.reopened_from_id else 'offered'}" for r in rows
            if r.open_offer and r.status == "offered" and r.recipient_person_id is None and aware(r.expires_at) > m.now()]
    return {payload["swap_id"]: {"audience_pending": bool(payload.get("audience_pending")),
                                 "audience_failed": bool(payload.get("audience_failed")),
                                 "audience_count": payload.get("audience_count"),
                                 'no_eligible_recipients_reason': payload.get('no_eligible_recipients_reason'),
                                 "no_eligible_recipients": payload.get("audience_count") == 0 and not payload.get("audience_pending") and not payload.get("audience_failed")}
            for payload in db.scalars(select(m.Outbox.payload).where(m.Outbox.event_key.in_(keys)))} if keys else {}


def audience_live(db, row):
    if not row or not row.open_offer or row.recipient_person_id is not None or effective_status(db, row) != "offered":
        return False
    try:
        assert_live(db, row)
    except Problem:
        return False
    return True


def compute_audience(db, inputs):
    """Read/validate outside the shared writer lock; responses always validate current facts again."""
    row = db.get(m.SwapRequest, inputs["swap_id"])
    if not audience_live(db, row):
        return {"person_ids": []}
    publication = db.get(m.Publication, row.publication_id)
    failures = []
    people = eligible_staff(db, publication, row.first_assignment_id, aware(row.expires_at), row.kind, failures=failures)
    return {'person_ids': sorted(people), 'no_eligible_recipients_reason': nobody_reason(failures) if not people else None}


def store_audience(db, inputs, result, error=None):
    """Commit membership, completion (including an empty audience), and notification recipients together."""
    row = db.get(m.SwapRequest, inputs["swap_id"])
    notice = db.scalar(select(m.Outbox).where(m.Outbox.event_key == inputs["event_key"]))
    if not notice.payload.get("audience_pending"):
        return {"audience_count": 0}
    live = not error and audience_live(db, row)
    people = set(result["person_ids"]) if live else set()
    computed_at = m.now()
    db.add_all(m.SwapOfferAudience(swap_id=row.id, person_id=pid, computed_at=computed_at) for pid in sorted(people))
    notified = (people | set(notice.payload["person_ids"])) - {inputs.get("exclude_person_id")}
    notice.payload = {**notice.payload, "audience_pending": False, "audience_failed": bool(error),
                      'no_eligible_recipients_reason': result.get('no_eligible_recipients_reason') if live else None,
                      "person_ids": sorted(notified), "audience_count": len(people),
                      **({"audience_computed_at": computed_at.isoformat()} if not error else {})}
    if live and not people and not error:
        outbox(db, system_actor(db, row.department_id), f'swap:{row.id}:{row.revision}:no_recipients', 'swap',
               {'swap_id': row.id, 'person_ids': [row.proposer_person_id], 'notice': 'no_recipients',
                'no_eligible_recipients_reason': result.get('no_eligible_recipients_reason')})
    if not live:
        notice.state = "superseded"
    notice.revision += 1
    return {"audience_count": len(people)}


def visible_open_ids(db, actor, swap_id=None):
    """Indexed invitation membership, filtered for live consent before pagination; no eligibility trials."""
    if not actor.person_id or not actor.permits("staff") or not staff_identity(
            db, actor.identity_id, actor.person_id, actor.department_id):
        return set()
    query = select(m.SwapRequest).join(m.SwapOfferAudience, m.SwapOfferAudience.swap_id == m.SwapRequest.id)
    query = query.join(m.Assignment, m.Assignment.id == m.SwapRequest.first_assignment_id)
    query = query.join(m.Shift, m.Shift.id == m.Assignment.shift_id).join(m.Publication, m.Publication.id == m.SwapRequest.publication_id)
    query = query.where(m.SwapOfferAudience.person_id == actor.person_id,
               m.SwapRequest.department_id == actor.department_id, unclaimed_open(),
               m.SwapRequest.proposer_person_id != actor.person_id, m.Assignment.start > m.now(),
               m.Shift.cancelled.is_(False), m.Shift.locked.is_(False), m.Publication.current.is_(True))
    if swap_id:
        query = query.where(m.SwapRequest.id == swap_id)
    rows = list(db.scalars(query))
    cache = list_cache(db, actor, rows, summaries=False) if rows else None
    return {row.id for row in rows if effective_status(db, row, cache) == "offered"}


def options(db, actor, assignment_id=None, swap_id=None, recipient_id=None, kind=None):
    person_id = own_person(db, actor)
    require(not (assignment_id and swap_id), "INVALID_SWAP", "Choose an offered duty or a received offer, not both")
    require(not recipient_id or (assignment_id and not swap_id), "INVALID_SWAP", "Choose your offered duty before a recipient")
    department = db.get(m.Department, actor.department_id)
    row = get_scoped(db, actor, swap_id) if swap_id else None
    if row:
        require(person_id == row.recipient_person_id or (row.open_offer and row.recipient_person_id is None
                and person_id != row.proposer_person_id),
                "FORBIDDEN", "Only an eligible recipient can choose a return duty", 403)
        publication = assert_live(db, row)
        require(kind is None or kind == row.kind, "INVALID_SWAP", "Use the offer's swap kind")
    kind = row.kind if row else kind or next(iter(department.swap_kinds_allowed), None)
    if not row:
        require(kind is None or kind in department.swap_kinds_allowed,
                "SWAP_KIND_DISABLED", "This swap kind is not allowed by the department", 409)
        if kind is None:
            return {"assignments": [], "recipients": [], "assignment_id": assignment_id, "swap_id": swap_id,
                    "recipient_id": recipient_id, "kind": None, "allowed_kinds": [],
                    "assignments_partial": False, "recipients_partial": False,
                    "basis": "The department does not currently allow swap offers"}
    # Fetch each issued snapshot once, not once per offered assignment in the join.
    query = select(m.Assignment, m.Publication.id).join(m.Shift, m.Shift.id == m.Assignment.shift_id)
    query = query.join(m.Publication, m.Publication.workspace_id == m.Shift.workspace_id).where(
                m.Publication.department_id == actor.department_id, m.Publication.current.is_(True),
                m.Assignment.person_id == person_id, m.Assignment.start > m.now(),
                m.Shift.cancelled.is_(False), m.Shift.locked.is_(False))
    if row:
        first = db.get(m.Assignment, row.first_assignment_id)
        query = query.where(m.Publication.id == publication.id, m.Shift.id != first.shift_id)
    inactive_ids = inactive_series_shift_ids(db, query.with_only_columns(m.Shift.id, maintain_column_froms=True))
    if inactive_ids:
        query = query.where(m.Shift.id.not_in(inactive_ids))
    selected = list(db.execute(query.order_by(m.Assignment.start, m.Assignment.id).limit(201)))
    publications = {pub.id: pub for pub in db.scalars(select(m.Publication).where(
        m.Publication.id.in_({pid for _, pid in selected})).options(defer(m.Publication.validation)))} if selected else {}
    entries = [(assignment, publications[pid]) for assignment, pid in selected]
    indexes = {pid: {p['id']: (shift, p) for shift in pub.snapshot['shifts'] for p in shift['participants']}
               for pid, pub in publications.items()}
    roles = department_roles(db, actor.department_id)
    bindings = list(db.execute(select(m.Identity, m.Person).join(m.Person, m.Person.id == m.Identity.person_id)
                    .where(m.Identity.id.in_(roles), m.Person.id != person_id)
                    .order_by(m.Person.display_name, m.Person.id)))
    cache = {m.Identity: {identity.id: identity for identity, _ in bindings},
             m.Person: {person.id: person for _, person in bindings},
             m.Department: {actor.department_id: department}, "roles": roles}
    # Apply the command's eligibility predicate before limiting; one role/identity read for the whole department.
    people = list({person.id: person for identity, person in bindings
                   if staff_identity(db, identity.id, person.id, actor.department_id, cache)}.values())[:501]
    entries = [(a, pub) for a, pub in entries if in_force(db, pub, a)]
    # The unqualified read is used by Home and to open the offer sheet. It only needs the
    # person's duties and the linked staff directory; eligibility is specific to a selected
    # duty and is checked by the command, so do not run one validator trial per colleague here.
    if not row and assignment_id is None:
        return {"assignments": [summary(pub, a.id, indexes[pub.id]) for a, pub in entries[:200]],
                "recipients": [{"id": person.id, "display_name": person.display_name,
                                "eligible": None, "message": None, "findings": []}
                               for person in people[:500] if entries],
                "assignment_id": entries[0][0].id if entries else None, "swap_id": swap_id,
                "recipient_id": recipient_id, "kind": kind,
                "allowed_kinds": department.swap_kinds_allowed,
                "assignments_partial": len(entries) > 200, "recipients_partial": bool(entries) and len(people) > 500,
                "basis": "Own future published duties and linked staff names only; choose a duty to check eligibility"}
    if row:
        preview = Eligibility(db, publication)
        assignments = ([{**summary(pub, a.id, indexes[pub.id]), **eligibility_for_reader(
                            actor, preview.pair(row.first_assignment_id, a.id, row.expires_at))}
                       for a, pub in entries[:200]] if kind == "exchange" else [])
        recipients = []
    else:
        if assignment_id:
            participant = db.get(m.Assignment, assignment_id)
            shift = db.get(m.Shift, participant.shift_id) if participant else None
            publication = db.scalar(select(m.Publication).where(m.Publication.workspace_id == shift.workspace_id,
                m.Publication.department_id == actor.department_id, m.Publication.current.is_(True))) if shift else None
            require(publication is not None, "SWAP_ASSIGNMENT_NOT_FOUND", "Choose your own current published duty", 404)
            assignment(db, publication, assignment_id, person_id)
        elif entries:
            participant, publication = entries[0]
            assignment_id = participant.id
        assignments = [summary(pub, a.id, indexes[pub.id]) for a, pub in entries[:200]]
        preview = Eligibility(db, publication, audience=True) if assignment_id else None
        recipients = [{"id": person.id, "display_name": person.display_name,
                       **eligibility_for_reader(actor, preview.recipient(assignment_id, person.id, kind=kind))}
                      for person in people[:500] if assignment_id]
        if recipient_id:
            recipient = next((person for person in recipients if person["id"] == recipient_id), None)
            require(recipient is not None, "NOT_FOUND", "Choose an active departmental staff recipient", 404)
    return {"assignments": assignments, "recipients": recipients, "assignment_id": assignment_id, "swap_id": swap_id,
            "recipient_id": recipient_id, "kind": kind, "allowed_kinds": department.swap_kinds_allowed,
            **(eligibility_for_reader(actor, preview.recipient(row.first_assignment_id, person_id, row.expires_at, kind))
               if row and kind == "give" else {}),
            # Why nobody can take the chosen duty, from the checks above (never a second rule in the client).
            "no_eligible_recipients_reason": nobody_reason([f for r in recipients for f in staff_refusal(r["findings"])])
                if assignment_id and not row and not any(r["eligible"] for r in recipients) else None,
            "assignments_partial": len(entries) > 200 and not (row and kind == "give"),
            "recipients_partial": not row and len(people) > 500,
            "basis": "Own future published duties and active staff names only; no colleague schedules or private attributes"}


def list_requests(db, actor, limit=50, offset=0, person_id=None):
    require_role(actor, "staff", "coordinator", "approver")
    query = select(m.SwapRequest).where(m.SwapRequest.department_id == actor.department_id)
    if person_id:
        require_person(db, actor, person_id, ("coordinator", "approver"))
        query = query.where(or_(m.SwapRequest.proposer_person_id == person_id,
                                m.SwapRequest.recipient_person_id == person_id))
    if not actor.permits("coordinator", "approver"):
        own_person(db, actor)
        query = query.where(or_(m.SwapRequest.proposer_person_id == actor.person_id,
                                m.SwapRequest.recipient_person_id == actor.person_id,
                                and_(m.SwapRequest.id.in_(visible_open_ids(db, actor)), unclaimed_open())))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(db.scalars(query.order_by(m.SwapRequest.created_at.desc(), m.SwapRequest.seq.desc()).offset(offset).limit(limit)))
    cache = list_cache(db, actor, rows) if rows else None
    return {"swaps": [{**projection(db, row, cache, actor),
                       **({"direction": "offered" if row.proposer_person_id == person_id else "received"} if person_id else {})}
                      for row in rows], "total": total, "limit": limit,
            "offset": offset, "has_more": offset + len(rows) < total}


def pair_hash(row):
    return snapshot_hash({"publication_id": row.publication_id, "first": row.first_assignment_id,
                          "second": row.second_assignment_id, "proposer": row.proposer_id,
                          "recipient": row.recipient_id, "expires_at": aware(row.expires_at).isoformat(),
                          **({"kind": row.kind} if row.kind == "give" else {})})


def event(db, actor, row, action, reason, detail=None, *, notify=True):
    db.flush()
    event_id = m.uid()
    db.add(m.SwapEvent(id=event_id, swap_id=row.id, actor_id=actor.identity_id, action=action,
                       pair_hash=pair_hash(row), reason=reason, detail=detail))
    payload = {"swap_id": row.id, "person_ids": [pid for pid in (row.proposer_person_id, row.recipient_person_id) if pid]}
    pending = row.open_offer and row.recipient_person_id is None and action in {"offered", "reopened"}
    # Publication can be refused again after reapproval without changing protected consent.
    key = f"swap:{row.id}:{event_id if action == 'publish_refused' else row.revision}:{action}"
    if pending:
        payload.update(person_ids=[row.proposer_person_id] if action == "reopened" else [],
                       open_offer_step="offered", audience_pending=True)
        inputs = {"swap_id": row.id, "event_key": key,
                  "exclude_person_id": actor.person_id if action == "reopened" else None}
        db.add(m.Job(department_id=row.department_id,
            workspace_id=db.get(m.Publication, row.publication_id).workspace_id, requested_by=actor.identity_id,
            kind="swap_audience", input_snapshot=inputs, input_fingerprint=snapshot_hash(inputs)))
    elif row.open_offer and action in {"accept", "counteroffer"}:
        audience = set(db.scalars(select(m.SwapOfferAudience.person_id).where(m.SwapOfferAudience.swap_id == row.id)))
        payload.update(person_ids=sorted(set(payload["person_ids"]) | audience),
                       open_offer_step="taken")
    if action == "reopened":
        payload["person_ids"] = [pid for pid in payload["person_ids"] if pid != actor.person_id]
    notice = outbox(db, actor, key, "swap", payload, undoable=action in UNDOABLE)
    if not notify:
        notice.state = "superseded"


def semantic_content(shifts):
    """Ignore clone IDs/revisions only, never times, people, locks or type/attribute values."""
    cleaned = []
    for shift in shifts:
        # Only hashed, never mutated: no copy.
        item = {key: shift[key] for key in ("assignment_type_id", "type_revision", "type_snapshot",
                "location_id", "cancelled", "locked", "attributes", "series_id")}
        item.update({key: aware(shift[key]).isoformat() for key in ("start", "end")})
        item["service_ids"] = sorted(shift["service_ids"])
        item["participants"] = sorted([{
            "person_id": p["person_id"], "start": aware(p["start"]).isoformat(), "end": aware(p["end"]).isoformat(),
            "participation": p["participation"], "overtime": p["overtime"],
            "supervisor_person_id": p.get('supervisor_person_id'), "team_lead": p.get('team_lead', False),
            **({"segments": [{key: aware(part[key]).isoformat() for key in ("start", "end")}
                             for part in p["segments"]]} if p.get("segments") else {})}
            for p in shift["participants"]], key=snapshot_hash)
        cleaned.append(item)
    return snapshot_hash(sorted(cleaned, key=snapshot_hash))


def expected_content(db, publication, row):
    from .schedule import in_force_snapshot
    shifts = copy.deepcopy(in_force_snapshot(db, publication)["shifts"])
    replacements = {row.first_assignment_id: row.recipient_person_id}
    if row.kind == 'exchange':
        replacements[row.second_assignment_id] = row.proposer_person_id
    for shift in shifts:
        shift['participants'] = replaced_participants(shift['participants'], replacements)
    return semantic_content(shifts)


def swap_rota_name(db, publication, row):
    """Keep swap identities in the scoped request/audit, never in a shared rota label."""
    base = db.get(m.Workspace, publication.workspace_id)
    return neutral_rota_name(base.name if base else f"Rota version {publication.sequence}")[:160]


def prepare(db, actor, row, value):
    from . import planning
    publication = assert_live(db, row)
    require(row.status == "accepted", "SWAP_STATE", "Both staff confirmations are required before preparation", 409)
    workspace = m.Workspace(department_id=actor.department_id, created_by=actor.identity_id,
                            last_edited_by=actor.identity_id, name=swap_rota_name(db, publication, row),
                            starts_on=publication.starts_on, ends_on=publication.ends_on, base_publication_id=publication.id,
                            urgency=value.urgency, urgency_reason=value.reason if value.urgency == "urgent" else "")
    db.add(workspace)
    db.flush()
    from apps.api.workspace_baselines import capture_draft_baseline
    capture_draft_baseline(db, workspace, actor.identity_id,
                           reason="Captured draft baseline for swap workspace")
    desired = expected_content(db, publication, row)
    row.workspace_id, row.expected_content_hash, row.status = workspace.id, desired, "reviewing"
    row.revision += 1
    db.flush()
    # Reject unconfigured urgency immediately; ordinary submit will pin the route again.
    if value.urgency == "urgent":
        from .reviews import resolve_route
        resolve_route(db, workspace)
    from .schedule import in_force_snapshot
    mapping = planning.copy_snapshot_into(db, actor, workspace, in_force_snapshot(db, publication))
    if row.kind == "give":
        before = planning.workspace_content(db, workspace)
        participant = db.get(m.Assignment, mapping[row.first_assignment_id])
        participant.person_id = row.recipient_person_id
        for key, field in planning.assignment_source(actor, 'swap').items():
            setattr(participant, key, field)
        participant.revision += 1
        shift = db.get(m.Shift, participant.shift_id)
        if shift.series_id:
            shift.series_modified = True
        result = planning.changed(db, actor, workspace, before, value.reason)
        require(result["validation"]["valid"], "SWAP_INVALID", "The complete duty transfer failed validation; no change was saved",
                422, result["validation"])
    else:
        result = planning.swap(db, actor, s.Command(kind="draft.swap", expected_revision=workspace.revision,
            payload={"workspace_id": workspace.id, "first_assignment_id": mapping[row.first_assignment_id],
                     "second_assignment_id": mapping[row.second_assignment_id], "reason": value.reason}))
    require(semantic_content(workspace_shifts(db, workspace.id)) == desired,
            "SWAP_CONTENT_CHANGED", "Prepared amendment differs from the consented duties", 409)
    return result


def system_actor(db, department_id):
    """An internal department principal with no sign-in, staff binding or review authority."""
    department = db.get(m.Department, department_id)
    query = select(m.Identity).where(m.Identity.issuer == SWAP_AUTOMATION_ISSUER,
                                     m.Identity.subject == department_id)
    identity = db.scalar(query)
    if identity is None:
        try:
            with db.begin_nested():
                identity = m.Identity(issuer=SWAP_AUTOMATION_ISSUER, subject=department_id,
                                      display_name="Department swap automation", disabled=True)
                db.add(identity)
                db.flush()
        except IntegrityError:
            identity = db.scalar(query)
            if identity is None:
                raise
    return Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                 frozenset({"coordinator"}), None, "")


def automatic_lifecycle(db, actor, row, action):
    from . import planning
    workspace = db.get(m.Workspace, row.workspace_id)
    reason = f"Automatic {action} of the consented swap amendment on behalf of the department"
    result = planning.lifecycle(db, actor, s.Command(kind="workspace." + action, target_id=workspace.id,
                                expected_revision=workspace.revision, payload={"reason": reason}))
    if action == "submit" and workspace.review_snapshot["route_id"]:
        outbox(db, actor, f"review:{workspace.id}:{workspace.content_revision}:submitted", "review",
               {"workspace_id": workspace.id, "action": "submitted"})
    db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                       action="workspace." + action, target_id=workspace.id, reason=reason,
                       detail={"swap_id": row.id, "automatic": True},
                       correlation_id=db.info.get("correlation_id", "swap-automation")))
    return result


def automatically_prepare(db, row):
    actor = system_actor(db, row.department_id)
    reason = "Department automatically prepares the agreed swap for independent review"
    try:
        # Consent is already saved; a refused preparation must leave no partial amendment or notifications.
        with db.begin_nested():
            prepare(db, actor, row, s.SwapPrepareInput(reason=reason))
            event(db, actor, row, "prepare", reason)
            result = automatic_lifecycle(db, actor, row, "submit")
    except Problem as exc:
        event(db, actor, row, "prepare_refused", exc.message, {"code": exc.code, "message": exc.message})
        return {}
    return result


def automatically_publish(db, row):
    actor = system_actor(db, row.department_id)
    try:
        # Keep the human approval even when automatic publication is refused.
        with db.begin_nested():
            return automatic_lifecycle(db, actor, row, "publish")
    except Problem as exc:
        detail = {"code": exc.code, "message": exc.message}
        if isinstance(exc.details, dict) and exc.details.get('stale_approval'):
            detail['stale_approval'] = True
        event(db, actor, row, "publish_refused", exc.message, detail)
        return {}


def swap_command(db, actor, command):
    kind = command.kind
    if kind == "swap.offer":
        person_id = own_person(db, actor)
        value = s.SwapOfferInput.model_validate(command.payload)
        department = db.get(m.Department, actor.department_id)
        require(value.kind in department.swap_kinds_allowed,
                "SWAP_KIND_DISABLED", "This swap kind is not allowed by the department", 409)
        require(value.recipient_person_id is not None or department.swap_open_offers,
                "SWAP_OPEN_DISABLED", "Open swap offers are not allowed by the department", 409)
        publication = scoped(db, m.Publication, value.publication_id, actor, options=[defer(m.Publication.validation)])
        require(publication.current, "SWAP_STALE", "Start from the current publication", 409)
        first, _ = assignment(db, publication, value.assignment_id, person_id)
        require(value.recipient_person_id != person_id, "INVALID_SWAP", "Choose another person")
        require(value.recipient_person_id is None or available_staff(db, value.recipient_person_id, actor.department_id),
                "RECIPIENT_UNAVAILABLE", "Recipient is not an active staff member in this department", 404)
        department, moment = db.get(m.Department, actor.department_id), m.now()
        # Compare durations instead of adding a potentially huge configured window to a date.
        require(moment < value.expires_at <= aware(first.start)
                and (value.expires_at - moment).total_seconds() <= department.swap_offer_window_days * 86400,
                "SWAP_EXPIRY", f"Consent expires within {department.swap_offer_window_days} days and before the offered assignment begins")
        require_unique_offer(db, actor.department_id, first.id, value.recipient_person_id)
        if value.recipient_person_id:
            require_eligible(actor, Eligibility(db, publication).recipient(first.id, value.recipient_person_id, value.expires_at, value.kind))
        else:
            failures = []
            recipients = eligible_staff(db, publication, first.id, value.expires_at, value.kind,
                                        failures=failures, stop_after_first=True)
            if not recipients:
                reason = 'Nobody can take this duty — ask your coordinator. ' + nobody_reason(failures)
                require(False, 'SWAP_NO_RECIPIENTS', reason, 422,
                        {'audience_count': 0, 'no_eligible_recipients': True,
                         'no_eligible_recipients_reason': reason})
        require_offer_capacity(db, actor.department_id, actor.identity_id)
        row = m.SwapRequest(kind=value.kind, open_offer=value.recipient_person_id is None,
                            department_id=actor.department_id, publication_id=publication.id,
                            proposer_id=actor.identity_id, proposer_person_id=person_id,
                            recipient_person_id=value.recipient_person_id, first_assignment_id=first.id,
                            expires_at=value.expires_at, reason=value.reason)
        db.add(row)
        event(db, actor, row, "offered", value.reason)
        return {"swap": projection(db, row)}
    row = get_scoped(db, actor, command.target_id, responding=kind in {"swap.accept", "swap.counteroffer"})
    expected(row, command.expected_revision)
    result = {}
    if kind == "swap.undo":
        value = s.Decision.model_validate(command.payload)
        if not actor.permits("coordinator", "approver"):
            own_person(db, actor)
        again = undo(db, actor, row, value.reason)
        return {"swap": projection(db, again), "undone": projection(db, row)}
    if kind in {"swap.withdraw", "swap.reject"}:
        # The one required reason: a coordinator's or approver's refusal of a swap both people agreed to.
        value = (s.Decision if kind == "swap.reject" else s.SwapNote).model_validate(command.payload)
        require(row.status in OPEN_STATES, "SWAP_STATE", "This swap is already closed", 409)
        if kind == "swap.withdraw":
            own_person(db, actor)
            require(actor.identity_id == row.proposer_id or
                    (actor.person_id == row.recipient_person_id and row.recipient_id in {None, actor.identity_id}),
                    "FORBIDDEN", "Only the bound participants can withdraw consent", 403)
            row.status = "withdrawn"
        else:
            require_role(actor, "coordinator", "approver")
            row.status = "rejected"
    else:
        if kind in {"swap.accept", "swap.counteroffer"}:
            person_id = own_person(db, actor)
            require(person_id != row.proposer_person_id, "FORBIDDEN", "You cannot respond to your own offer", 403)
            require(row.recipient_person_id in {None, person_id}, "FORBIDDEN", "Only an eligible recipient can respond to this offer", 403)
        publication = assert_live(db, row)
        if kind == "swap.accept":
            value = s.SwapNote.model_validate(command.payload)
            require(row.kind == "give", "SWAP_KIND", "An exchange requires a duty in return", 409)
            require(row.status == "offered", "SWAP_STATE", "Only an unanswered give-away can be accepted", 409)
            require_eligible(actor, Eligibility(db, publication).recipient(row.first_assignment_id, person_id, row.expires_at, row.kind))
            require_unique_offer(db, row.department_id, row.first_assignment_id, person_id, row.id)
            row.recipient_person_id, row.recipient_id = person_id, actor.identity_id
            row.counteroffered_at, row.status = m.now(), "counteroffered"
        elif kind == "swap.counteroffer":
            value = s.SwapCounterofferInput.model_validate(command.payload)
            require(row.kind == "exchange", "SWAP_KIND", "A give-away is accepted without a return duty", 409)
            require(row.status == "offered", "SWAP_STATE", "A counteroffer can be made only once; otherwise start a new request", 409)
            second, shift = assignment(db, publication, value.assignment_id, person_id)
            first = db.get(m.Assignment, row.first_assignment_id)
            require(shift.id != first.shift_id, "INVALID_SWAP", "Choose a different session")
            require(aware(row.expires_at) <= aware(second.start), "SWAP_EXPIRY", "The proposed expiry must precede both assignments")
            require_eligible(actor, Eligibility(db, publication).pair(row.first_assignment_id, second.id))
            require_unique_offer(db, row.department_id, row.first_assignment_id, person_id, row.id)
            row.second_assignment_id, row.recipient_id, row.recipient_person_id = second.id, actor.identity_id, person_id
            row.counteroffered_at, row.status = m.now(), "counteroffered"
        elif kind == "swap.confirm":
            own_person(db, actor)
            value = s.SwapNote.model_validate(command.payload)
            require(row.status == "counteroffered", "SWAP_STATE", "The recipient must acknowledge the offer before confirmation", 409)
            require(actor.identity_id == row.proposer_id, "FORBIDDEN", "Only the original proposer can confirm this swap", 403)
            require_eligible(actor, Eligibility(db, publication).pair(row.first_assignment_id, row.second_assignment_id,
                                                           recipient_id=row.recipient_person_id))
            row.confirmed_at, row.status = m.now(), "accepted"
        elif kind == "swap.prepare":
            require_role(actor, "coordinator")
            value = s.SwapPrepareInput.model_validate(command.payload)
            result = prepare(db, actor, row, value)
        else:
            require(False, "UNKNOWN_COMMAND", "Unsupported swap command")
    if kind != "swap.prepare":
        row.revision += 1
    reason = optional_swap_note(kind, value.reason) if kind in OPTIONAL_NOTE_COMMANDS else value.reason
    event(db, actor, row, kind.removeprefix("swap."), reason)
    if kind == "swap.confirm" and db.get(m.Department, actor.department_id).swap_auto_prepare:
        automatically_prepare(db, row)
    return {**result, "swap": projection(db, row)}


UNDOABLE = {"reject", "withdraw", "counteroffer", "accept", "confirm"}


def undo(db, actor, row, reason):
    """Undo this person's latest step (decline, withdraw, offer in return, confirm) as a compensating, forward-only
    action: the swap is closed (already, for decline and withdraw; withdrawn otherwise) and a NEW swap is inserted at the
    earlier stage, carrying exactly the consent that was valid then and linked to this one. Nothing is moved backwards
    (the 0005 guard). Only before the swap was prepared into a rota change, and only while it could still go ahead."""
    last = db.scalar(select(m.SwapEvent).where(m.SwapEvent.swap_id == row.id)
                     .where(m.SwapEvent.action != "prepare_refused")
                     .order_by(m.SwapEvent.created_at.desc(), m.SwapEvent.seq.desc()).limit(1))
    require(last is not None and last.action in UNDOABLE and last.actor_id == actor.identity_id, "SWAP_UNDO_UNAVAILABLE",
            "Only your own latest step on this swap can be undone", 409)
    require(row.workspace_id is None, "SWAP_UNDO_UNAVAILABLE", "The rota change is already prepared; withdraw or decline it instead", 409)
    require(not db.scalar(select(m.SwapRequest.id).where(m.SwapRequest.reopened_from_id == row.id)), "SWAP_UNDO_UNAVAILABLE",
            "This step was already undone", 409)
    if last.action in {"reject", "withdraw"}:
        require(row.status in {"rejected", "withdrawn"}, "SWAP_UNDO_UNAVAILABLE", "This swap has moved on since", 409)
        stage = "accepted" if row.confirmed_at else "counteroffered" if row.counteroffered_at else "offered"
    elif last.action == "confirm":
        require(row.status == "accepted", "SWAP_UNDO_UNAVAILABLE", "This swap has moved on since", 409)
        stage = "counteroffered"
    else:
        require(row.status == "counteroffered", "SWAP_UNDO_UNAVAILABLE", "The duty transfer has already been confirmed"
                if row.kind == "give" else "The pair has already been confirmed", 409)
        stage = "offered"
    later = stage in {"counteroffered", "accepted"}
    recipient_person_id = None if row.open_offer and not later else row.recipient_person_id
    again = m.SwapRequest(kind=row.kind, open_offer=row.open_offer,
                          department_id=row.department_id, publication_id=row.publication_id, proposer_id=row.proposer_id,
                          proposer_person_id=row.proposer_person_id, recipient_person_id=recipient_person_id,
                          first_assignment_id=row.first_assignment_id, expires_at=row.expires_at, reason=row.reason, status=stage,
                          recipient_id=row.recipient_id if later else None,
                          second_assignment_id=row.second_assignment_id if later else None,
                          counteroffered_at=row.counteroffered_at if later else None,
                          confirmed_at=row.confirmed_at if stage == "accepted" else None, reopened_from_id=row.id)
    publication = assert_live(db, again)  # unexpired, current publication, both people still on staff, both duties still offerable
    require_unique_offer(db, row.department_id, row.first_assignment_id, recipient_person_id, row.id)
    if row.status not in OPEN_STATES:
        require_offer_capacity(db, row.department_id, row.proposer_id)
    if recipient_person_id is not None:
        preview = Eligibility(db, publication)
        require_eligible(actor, preview.pair(again.first_assignment_id, again.second_assignment_id,
                                    recipient_id=recipient_person_id) if later else
                         preview.recipient(again.first_assignment_id, recipient_person_id, aware(again.expires_at), again.kind))
    notice_key = f"swap:{row.id}:{row.revision}:{last.action}"
    if row.status in OPEN_STATES:  # an offer in return or a confirmation: this swap closes forward
        row.status = "withdrawn"
        row.revision += 1
    db.flush()
    db.add(m.SwapEvent(swap_id=row.id, actor_id=actor.identity_id, action="undo", pair_hash=pair_hash(row), reason=reason))
    db.add(again)
    db.flush()
    event(db, actor, again, "reopened", reason, notify=not supersede_outbox(db, actor, notice_key))
    return again


def validate_publication(db, workspace):
    row = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id))
    if row is None:
        return None
    publication = assert_live(db, row)
    require(row.status == "reviewing" and row.confirmed_at is not None and row.counteroffered_at is not None,
            "SWAP_CONSENT_REQUIRED", "Both unexpired staff consents must remain active through publication", 409)
    require(workspace.base_publication_id == publication.id and row.expected_content_hash == expected_content(db, publication, row)
            and row.expected_content_hash == semantic_content(workspace_shifts(db, workspace.id)),
            "SWAP_CONTENT_CHANGED", "Candidate no longer contains only the exact consented duties; start a new request", 409)
    return row


def published(db, actor, row, publication_id, reason):
    if row is not None:
        row.status, row.result_publication_id = "published", publication_id
        row.revision += 1
        event(db, actor, row, "published", reason)
