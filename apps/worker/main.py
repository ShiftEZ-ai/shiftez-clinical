"""Durable inbox/email outboxes and bounded validation/generation worker."""
import argparse
import copy
import json
import os
import sys
import socket
import re
from datetime import timedelta
import logging
import time
import threading
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo
import httpx
import apps.api.services.catalogue_proof_identity as proof_identity
from sqlalchemy import delete, func, literal, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer, load_only
from apps.api import models as m
from apps.api.auth import Actor, current_roles, department_roles
from apps.api.services.common import may_name_swap
from apps.api import live
from apps.api.db import (DatabaseWriteDeferred, DatabaseWriteTimeout, allow_long_idle, is_transient_database_error,
                         make_engine, retry_write, transaction)
from apps.api.logging_setup import configure_logging
from apps.api.settings import Settings
from apps.api.schema import require_current_schema
from apps.api.serialization import job_progress_record
from apps.api.snapshots import currency_hint, fingerprint
from apps.api.version import build
from apps.api.services.generation_resume import (clear_generation_resume, prune_generation_resume, resume_current,
                                                 resume_expired, has_generation_checkpoint, generation_resume_available, may_run)
from apps.api.services.generation_scope import require_generation_sessions_in_scope
from packages.domain.generation import generate
from packages.domain.generation_outcome import DIAGNOSTIC_PROPOSAL_MESSAGE
from packages.domain.validation import evaluate
from packages.domain.time import aware
from packages.domain.worker_contract import JOB_LEASE_SECONDS
from packages.contracts import schemas as s
from packages.contracts.schemas import AutoscheduleBestScore, FEEDBACK_LIMITS, REQUEST_STAGE_LABELS
from apps.process_memory import memory_probe as _memory_probe, release_memory

log = logging.getLogger("rota.worker")

_HEAVY_JOB_KINDS = ('generation', 'feasibility', 'catalogue_proof', 'swap_audience',
                    'merge_preview', 'merge_apply', 'fill_options')
_GENERATION_STAGE_KEYS = tuple(s.GENERATION_PROGRESS_STAGES)
_GENERATION_SEARCH_STAGE_START = _GENERATION_STAGE_KEYS.index('finding')
_GENERATION_SCORING_STAGE_START = _GENERATION_STAGE_KEYS.index('scoring')


def _generation_stage_index(stage):
    try:
        index = _GENERATION_STAGE_KEYS.index(stage)
    except ValueError:
        return None
    return (_GENERATION_SEARCH_STAGE_START
            if _GENERATION_SEARCH_STAGE_START <= index < _GENERATION_SCORING_STAGE_START else index)


def _preserve_generation_stage(previous, progress):
    previous_stage = (previous or {}).get('stage')
    current_stage = (progress or {}).get('stage', previous_stage)
    previous_index = _generation_stage_index(previous_stage)
    current_index = _generation_stage_index(current_stage)
    if previous_index is not None and current_index is not None and current_index < previous_index:
        return {**progress, 'stage': previous_stage}
    return progress


def _release_job_memory(claim=None):
    release_memory()
    _memory_probe('after_job', claim)


def _name(db, person_id):
    person = db.get(m.Person, person_id) if person_id else None
    return person.display_name if person else "someone"


SWAP_STEP = {"exchange": {"offered": ("Swap offered", "{a} offered a duty to {b}. {b} can offer a duty in return."),
             "counteroffer": ("Swap: duty offered in return", "{b} offered a duty in return. {a} confirms the exact pair."),
             "confirm": ("Swap agreed", "{a} and {b} agreed the pair. The published duties change only after review and publication."),
             "prepare": ("Swap rota change in review", "The swap between {a} and {b} is waiting for approval."),
             "prepare_refused": ("Swap needs coordinator attention", "The agreed swap between {a} and {b} could not be prepared automatically. A coordinator can read the reason and arrange the amendment."),
             "publish_refused": ("Swap needs coordinator attention", "The swap between {a} and {b} was approved but could not be published automatically. A coordinator can read the reason; published duties have not changed."),
             "published": ("Swap published", "The swap between {a} and {b} is now in the published rota."),
             "withdraw": ("Swap withdrawn", "The swap between {a} and {b} was withdrawn."),
             "reject": ("Swap declined", "The swap between {a} and {b} was closed without changes."),
             "reopened": ("Swap step undone", "A step on the swap between {a} and {b} was undone; the swap is open again where it was before.")},
             "give": {"offered": ("Give-away offered", "{a} offered a duty to {b}. {b} can accept without offering a duty in return."),
             "accept": ("Give-away accepted by recipient", "{b} accepted the duty from {a}. Confirmation from {a} is still needed."),
             "confirm": ("Give-away agreed", "{a} and {b} agreed the duty transfer. The published duty changes only after review and publication."),
             "prepare": ("Give-away rota change in review", "The duty transfer from {a} to {b} is waiting for approval."),
             "prepare_refused": ("Give-away needs coordinator attention", "The agreed duty transfer from {a} to {b} could not be prepared automatically. A coordinator can read the reason and arrange the amendment."),
             "publish_refused": ("Give-away needs coordinator attention", "The duty transfer from {a} to {b} was approved but could not be published automatically. A coordinator can read the reason; the published duty has not changed."),
             "published": ("Give-away published", "The duty transfer from {a} to {b} is now in the published rota."),
             "withdraw": ("Give-away withdrawn", "The give-away from {a} to {b} was withdrawn."),
             "reject": ("Give-away declined", "The give-away from {a} to {b} was closed without changes."),
             "reopened": ("Give-away step undone", "A step on the give-away from {a} to {b} was undone; the give-away is open again where it was before.")}}


def publication_wishes(db, publication):
    """Index the publication's in-force wish decisions once for the whole recipient batch."""
    from collections import defaultdict
    outcomes = defaultdict(list)
    if 'wishes' in publication.snapshot:
        from apps.api.services.schedule import in_force_snapshot
        rows = ((w['person_id'], w['outcome']) for w in in_force_snapshot(db, publication)['wishes'])
    else:
        rows = db.execute(select(m.StaffWish.person_id, m.StaffWish.outcome).where(m.StaffWish.publication_id == publication.id))
    for person_id, outcome in rows:
        outcomes[person_id].append(outcome)
    return outcomes


def wish_line(db, publication, identity, outcomes_by_person=None):
    """How this publication decided the reader's own wishes (the words the Requests page uses), or nothing."""
    if not identity.person_id:
        return ""
    outcomes = (publication_wishes(db, publication) if outcomes_by_person is None else outcomes_by_person).get(identity.person_id, [])
    if not outcomes:
        return ""
    words = [f"{outcomes.count(k)} {label}" for k, label in (("granted", "granted"), ("partly", "partly granted"), ("declined", "not granted")) if k in outcomes]
    return f" Your preferences: {', '.join(words)}. Open Requests for each one."


def describe(db, event, publication, identity, *, wishes=None, rota_label=None):
    """Plain notification text: what happened, to whom, and where to look. Never private reasons."""
    own = identity.person_id and identity.person_id == event.payload.get("person_id")
    if event.kind == "rule":
        return "Rule proposal or decision", "Open Rules to review the current version."
    if event.kind == "bulk_review":
        return "Bulk changes need an independent reviewer", "Open Bulk editor to review the pending changes."
    if event.kind == "generation_finished":
        title = {
            'complete': 'Auto-schedule result ready',
            'partial': 'Auto-schedule finished with gaps',
            'needs_more_time': 'Auto-schedule needs more time',
            'infeasible': 'Auto-schedule found conflicting requirements',
            'failed': 'Auto-schedule could not finish',
        }[event.payload['outcome']]
        return title, 'Open Auto-schedule to review the run.'
    if event.kind == "feedback":
        status = event.payload["status"]
        message = f"Your report {event.payload['feedback_id']} is {status.replace('_', ' ')}"
        if status == "fixed" and event.payload.get("resolved_in"):
            message += f" in {event.payload['resolved_in']}"
        return "Your feedback was updated", message + ". Open My feedback for the details."
    if publication:
        changes = (event.payload.get('person_changes') or {}).get(identity.person_id)
        if changes:
            zone = publication.snapshot.get('timezone', 'UTC')
            was = '; '.join(duty_text(d, zone) for d in changes['removed']) or 'No duty'
            now = '; '.join(duty_text(d, zone) for d in changes['added']) or 'No duty'
            suffix = ' A newer rota has since replaced this version.' if not publication.current else ''
            # SW3-B-08: the rota is named (a person can be on several).
            from apps.api.services.common import neutral_rota_name
            workspace = db.get(m.Workspace, publication.workspace_id)
            name = rota_label or (neutral_rota_name(workspace.name) if workspace else f"Version {publication.sequence}")
            return f'Your duties in {name} changed', notification_summary('', [('Was: ', was), ('Now: ', now)],
                suffix + ' Open Schedule for the full change.')
        workspace = db.get(m.Workspace, publication.workspace_id)
        from apps.api.services.common import neutral_rota_name
        name = rota_label or (neutral_rota_name(workspace.name) if workspace else f"Version {publication.sequence}")
        if not publication.current:
            return f"{name} was replaced", "A newer version of this rota is published. Open Schedule for the current duties."
        return f"{name} published", "The rota is final. Open Schedule or Home to see your duties." + wish_line(db, publication, identity, wishes)
    if event.kind == 'credential_impact':
        name = _name(db, event.payload.get('person_id'))
        zone = db.get(m.Organisation, db.get(m.Department, event.department_id).organisation_id).timezone
        duties = '; '.join(duty_text(d, zone) for d in event.payload.get('sessions', []))
        return 'Published duties need an eligibility review', notification_summary(
            f"{event.payload['count']} published duties affected for ", [('', name), ('', duties)],
            ' Open Schedule to arrange cover.')
    if event.kind == 'absence_approved':
        return ('Your absence was approved' if own else f"Absence approved for {_name(db, event.payload.get('person_id'))}"), spans_text(db, event) + 'The approved rota change is ready to publish. Published duties change when it is published.'
    if event.kind == "swap" and event.payload.get('notice'):
        if event.payload['notice'] == 'no_recipients':
            return 'Nobody eligible can take this duty', (event.payload.get('no_eligible_recipients_reason') or 'Open Requests to withdraw the offer or ask the coordinator for cover.')
        return 'Your swap offer needs a new duty', 'The offered duty changed in the published rota. Open Requests to choose a current duty.'
    if event.kind == "swap":
        row = db.get(m.SwapRequest, event.payload.get("swap_id"))
        open_step = event.payload.get("open_offer_step")
        if open_step:
            label = "give-away" if row and row.kind == "give" else "swap"
            if open_step == "taken":
                return f"Open {label} offer taken", "A colleague responded first. Confirmation from the proposer is still needed."
            response = ("accept without offering a duty in return" if label == "give-away" else
                        "offer one of their own duties in return")
            return f"Open {label} offer", (f"An open {label} offer is available. Eligible colleagues can {response}; "
                                          "the proposer then confirms.")
        step = event.event_key.rsplit(":", 1)[-1]
        title, text = SWAP_STEP[row.kind if row else "exchange"].get(step, ("Swap updated", "A swap between {a} and {b} changed."))
        a, b = (_name(db, row.proposer_person_id), _name(db, row.recipient_person_id)) if row else ("someone", "a colleague")
        # Speak to the reader: "You offered…" rather than their own name.
        if row and identity.person_id == row.proposer_person_id:
            a = "you"
        elif row and identity.person_id == row.recipient_person_id:
            b = "you"
        text = re.sub(r"(^|\. )you\b", lambda x: x.group(1) + "You", text.format(a=a, b=b))
        return title, text
    if event.kind == "announcement":
        from apps.api.services.announcements import notification_text
        content = db.get(m.DepartmentContent, event.payload.get("content_id"))
        return notification_text(content)
    if event.kind == "self_approval":
        from apps.api.services.independence import LABELS, SELF_GRANT_ACTION, SELF_GRANT_LABEL
        actor = db.get(m.Identity, event.payload.get("actor_id"))
        action = event.payload.get("action")
        label = SELF_GRANT_LABEL if action == SELF_GRANT_ACTION else LABELS.get(action, "A decision")
        allowed = "because no other admin is available" if action == SELF_GRANT_ACTION else "by a self-approval permission"
        return (f"{actor.display_name if actor else 'Someone'} decided their own work",
                f"{label} · allowed {allowed}. "
                "Open the Audit log for the reason and the record.")
    if event.kind == "returned":
        workspace = db.get(m.Workspace, event.payload.get("workspace_id"))
        note = (workspace.return_note or {}) if workspace else {}
        return (f"{workspace.name if workspace else 'A rota'} returned to the coordinator",
                f"{note.get('by_name') or 'An approver'} sent it back to draft"
                + (f": “{note['reason'][:300]}”" if note.get('reason') else '') + ". Open Schedule to change it, check it and submit it again.")
    if event.kind == "review":
        workspace = db.get(m.Workspace, event.payload.get("workspace_id"))
        if event.payload.get("action") == "submitted":
            return f"{workspace.name if workspace else 'A rota'}: ready for review", "Open Schedule to review the pending step."
        return f"{workspace.name if workspace else 'A rota'}: review step done", "Open Schedule to see the next step."
    request = db.get(m.RequestRecord, event.payload.get("request_id")) if event.payload.get("request_id") else None
    if request:
        from apps.api.services.availability import REQUEST_STAGE
        from apps.api.services.training import change_people
        own = identity.person_id is not None and identity.person_id in change_people(db, request)
        request_kind = event.payload.get("request_kind", request.kind)
        status = event.payload.get("request_status", "submitted" if event.event_key.startswith("request:") else "updated")
        kind = "availability for overtime" if request_kind == "overtime_availability" else request_kind.replace("_", " ")
        who = "Your" if own else f"{_name(db, request.person_id)}'s"
        if request_kind == 'absence_report':
            zone = db.get(m.Organisation, db.get(m.Department, event.department_id).organisation_id).timezone
            action = (request.details or {}).get('replace_action') or {}
            issued = db.get(m.Publication, action.get('publication_id')) if action else None
            duty = swaps_summary(issued, action.get('assignment_id')) if issued else None
            label = duty_text(duty or {'start': request.start, 'end': request.end, 'type': 'Duty'}, zone)
            if status == 'submitted':
                return ('Your absence report was sent' if own else f"{_name(db, request.person_id)} can't make this duty"), f'{label}. Waiting for an approver; the coordinator can arrange cover with Replace in Schedule.'
            if status == 'approved':
                return ('Your absence report was approved' if own else f"Absence approved for {_name(db, request.person_id)}"), f'{label}. The absence is recorded. ' + ('Your coordinator arranges cover.' if own else 'Open Schedule and choose Replace for this duty.')
        if status == "submitted":
            decider = "A coordinator" if request_kind == "overtime_availability" else "An approver"
            return (f"Your {kind} request was sent" if own else f"New {kind} request from {_name(db, request.person_id)}"), f"{decider} decides it under Requests."
        status = REQUEST_STAGE_LABELS.get(REQUEST_STAGE.get(status), status).lower()
        return f"{who} {kind} request was {status}", ""
    if event.kind == "availability":
        if getattr(event, 'event_key', '').startswith('availability-retracted:'):
            return ("Your leave or absence was removed" if own else f"Leave or absence removed for {_name(db, event.payload.get('person_id'))}"), "It no longer marks the person unavailable. Published duties have not changed."
        return ("Your leave or absence was recorded" if own else f"Leave or absence recorded for {_name(db, event.payload.get('person_id'))}"), spans_text(db, event) + ("Your coordinator arranges cover if you are on a published rota then." if own else "Check coverage in Schedule; the published rota is not changed automatically.")
    status = event.payload.get("pattern_status", "proposed")
    if status in {"active", "declined"}:
        who = "Your" if own else f"{_name(db, event.payload.get('person_id'))}'s"
        return f"{who} weekly unavailability was {'approved' if status == 'active' else 'declined'}", ""
    if getattr(event, "event_key", "").endswith(":undone"):
        return "Weekly unavailability decision undone", "The pattern is waiting for a decision again under Requests."
    return f"Weekly unavailability to approve for {_name(db, event.payload.get('person_id'))}", "Open Requests to approve or decline it."


def notification_summary(prefix, parts, suffix):
    """Reserve labels and action within the existing inbox storage contract; full lists stay structured."""
    room = m.Notification.__table__.c.message.type.length - len(prefix + suffix) - sum(len(label) + 2 for label, _ in parts)
    words = []
    for index, (label, value) in enumerate(parts):
        limit = room // (len(parts) - index)
        clipped = value if len(value) <= limit else value[:max(0, limit - 1)] + '…'
        words.append(label + clipped + '. ')
        room -= len(clipped)
    return prefix + ''.join(words).rstrip() + suffix


def duty_text(duty, zone):
    from zoneinfo import ZoneInfo
    start, end = aware(duty['start']).astimezone(ZoneInfo(zone)), aware(duty['end']).astimezone(ZoneInfo(zone))
    place = f" · {duty['location']}" if duty.get('location') else ''
    return f"{duty['type']}{place} · {start:%a %d %b %Y %H:%M}–{end:%H:%M}"


def spans_text(db, event, limit=3):
    """An amendment's absences for one person, each once with its year ("Tue 07 Sep 2027 08:00–12:00. "), or ''."""
    from zoneinfo import ZoneInfo
    spans = event.payload.get('spans') or []
    if not spans:
        return ''
    zone = ZoneInfo(db.get(m.Organisation, db.get(m.Department, event.department_id).organisation_id).timezone)
    def one(span):
        a, b = aware(span['start']).astimezone(zone), aware(span['end']).astimezone(zone)
        return f"{a:%a %d %b %Y %H:%M}–{b:%H:%M}" if a.date() == b.date() else f"{a:%a %d %b %Y %H:%M} – {b:%a %d %b %Y %H:%M}"
    words = '; '.join(one(x) for x in spans[:limit]) + (f" and {len(spans) - limit} more" if len(spans) > limit else '')
    return words + '. '


def swaps_summary(publication, assignment_id):
    from apps.api.services.swaps import summary
    return summary(publication, assignment_id)


def recipients_of(db, event, publication, roles, identities=None):
    """Who is told about an event. Planners hear about everything; staff only about what involves them."""
    if event.kind == "bulk_review":
        from apps.api.services.bulk_staged import reviewers
        row = db.get(m.BulkStaged, event.payload.get("staged_id"))
        if (row is None or row.department_id != event.department_id or row.status != "waiting"
                or aware(row.expires_at) <= m.now()):
            return set()
        return reviewers(db, row)
    if event.kind == "generation_finished":
        from apps.api.services.workspace_access import access_record
        job = db.get(m.Job, event.payload['job_id'], options=[defer(m.Job.input_snapshot), defer(m.Job.result)])
        if (job is None or job.kind != 'generation' or job.department_id != event.department_id
                or job.workspace_id != event.payload['workspace_id'] or 'coordinator' not in roles.get(job.requested_by, ())):
            return set()
        identity = db.get(m.Identity, job.requested_by)
        workspace = db.get(m.Workspace, job.workspace_id)
        if identity is None or identity.disabled or workspace is None:
            return set()
        access = access_record(db, workspace)
        if (access['visibility'] == 'private' or access['purpose'] == 'sandbox') and workspace.created_by != identity.id:
            return set()
        return {identity.id}
    if event.kind == "feedback":
        identity_id = event.payload.get("reporter_identity_id")
        identity = db.get(m.Identity, identity_id) if identity_id in roles else None
        return {identity_id} if identity is not None and not identity.disabled else set()
    if event.kind == "announcement":
        from apps.api.services import audiences
        from apps.api.services.independence import today
        content, day = db.get(m.DepartmentContent, event.payload.get("content_id")), today(db, event.department_id)
        # Only the version that asked to be sent, and only while it is switched on and current.
        if (content is None or not content.active or content.revision != event.payload.get("revision")
                or content.starts_on > day or (content.ends_on is not None and day >= content.ends_on)):
            return set()
        return audiences.recipients(db, event.department_id, content.audience, day)
    if publication:
        from apps.api.services.notification_audience import publication_audience
        person_changes = (set(event.payload['person_changes'])
                          if event.payload.get('supersedes_id') and 'person_changes' in event.payload else None)
        return publication_audience(db, publication, roles, identities, exclude_identity_id=publication.published_by,
                                    person_changes=person_changes, event=event).identities
    elif event.kind == "swap":
        people = set(event.payload.get("person_ids", []))
    else:
        people = {event.payload.get("person_id")}
        if event.kind == "request" and event.payload.get("request_id"):
            from apps.api.services.training import change_people
            request = db.get(m.RequestRecord, event.payload["request_id"])
            if request is not None and request.department_id == event.department_id:
                people |= change_people(db, request)
    people.discard(None)  # sign-ins without a staff profile are not "the person" of an event
    if identities is None:
        identities = {i.id: i for i in db.scalars(select(m.Identity).where(m.Identity.id.in_(list(roles) or [""]), m.Identity.disabled.is_(False)))}
    staff_cache = None
    if event.kind == "swap" and event.payload.get("open_offer_step"):
        from apps.api.services.swaps import staff_identity
        department = db.get(m.Department, event.department_id)
        staff_cache = {m.Identity: identities, m.Department: {department.id: department}, "roles": roles,
                       m.Person: {p.id: p for p in db.scalars(select(m.Person).where(m.Person.id.in_(people)))}}
    return {iid for iid, identity in identities.items() if roles[iid] & {"coordinator", "approver"}
            or (event.kind == "review" and "programme_lead" in roles[iid])
            or (event.kind == "self_approval" and "auditor" in roles[iid])
            or ("staff" in roles[iid] and identity.person_id in people and (staff_cache is None or
                staff_identity(db, iid, identity.person_id, event.department_id, staff_cache)))}


def publication_labels(db, publication):
    """(swappers, label naming them, neutral label) for a swap amendment's publication; None for any other rota."""
    from apps.api.services.common import SWAP_NAME_READERS, neutral_rota_name, rota_names
    swap = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == publication.workspace_id))
    workspace = db.get(m.Workspace, publication.workspace_id) if swap else None
    if workspace is None:
        return None
    named = rota_names(db, SWAP_NAME_READERS, None, {workspace.id: workspace.name})[workspace.id]
    return {swap.proposer_person_id, swap.recipient_person_id}, named, neutral_rota_name(workspace.name)


def deliver_notifications(db, event, *, roles=None, identities=None):
    from apps.api.services import notification_audience
    if event.kind == "event":
        from apps.api.services import event_delivery
        event_delivery.deliver(db, event)
        return
    content = db.get(m.DepartmentContent, event.payload.get("content_id")) if event.kind == "announcement" else None
    if event.kind == "announcement" and (content is None or content.kind != "link"):
        from apps.api.services import announcements, audiences
        from apps.api.services.independence import today
        # retry_write opens a fresh Session for each batch; keep this recipient cache batch-scoped.
        contexts = db.info.setdefault("announcement_recipient_contexts", {})
        key = (event.department_id, today(db, event.department_id))
        if key not in contexts:
            contexts[key] = audiences.recipient_context(db, *key)
        announcements.deliver(db, event, recipient_context=contexts[key])
        return
    if roles is None:
        roles = department_roles(db, event.department_id)
    publication = db.get(m.Publication, event.payload.get("publication_id")) if event.kind == "publication" else None
    targets = recipients_of(db, event, publication, roles, identities)
    if publication is None:
        targets = notification_audience.filter_recipients(db, event, targets, identities)
    if event.kind != "feedback":
        targets.discard(event.payload.get("actor_id"))
    done = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == event.id)))
    remaining = sorted(targets - done)
    wishes = publication_wishes(db, publication) if publication and publication.current and remaining else {}
    labels = publication_labels(db, publication) if publication and remaining else None
    for identity_id in remaining:
        identity = db.get(m.Identity, identity_id)
        label = None
        if labels:
            swap_people, named, neutral = labels
            label = named if may_name_swap(roles.get(identity_id, ()), identity.person_id if identity else None, swap_people) else neutral
        title, message = describe(db, event, publication, identity, wishes=wishes, rota_label=label)
        db.add(m.Notification(outbox_id=event.id, identity_id=identity_id, title=title, message=message,
            **({"source_kind": "department_content", "source_id": content.id} if content is not None else {})))
    db.flush()


def dispatch_pending(engine):
    from apps.api.services import announcements, events
    with transaction(engine) as db:
        due = db.scalar(select(m.Outbox.id).where(
            m.Outbox.state.in_(['queued', 'retry']), m.Outbox.kind.not_in(('email', 'push', 'push_test')),
            m.Outbox.payload['audience_pending'].as_boolean().is_not(True),
            m.Outbox.next_attempt_at <= m.now()).limit(1))
        if not due and not announcements.reconcile(db, pending_only=True) and not events.reconcile(db, pending_only=True):
            return set()
    return retry_write(engine, dispatch_batch)


def dispatch_batch(db, limit=None):
    """One bounded transaction, retaining per-event savepoints and retry evidence."""
    if limit is None:
        limit = (db.get_bind().get_execution_options().get("rota_write_settings") or Settings()).worker_batch_size
    require_current_schema(db)
    from apps.api.services import announcements, events as event_service
    reconciled = announcements.reconcile(db)
    event_departments = event_service.reconcile(db)
    reconciled |= event_departments
    events = list(db.scalars(select(m.Outbox).where(m.Outbox.state.in_(["queued", "retry"]), m.Outbox.kind.not_in(('email', 'push', 'push_test')),
        m.Outbox.payload["audience_pending"].as_boolean().is_not(True),
        m.Outbox.next_attempt_at <= m.now()).order_by(m.Outbox.created_at).limit(limit).with_for_update(skip_locked=True)))
    recipients, departments = {}, set(reconciled)
    for event in events:
        try:
            with db.begin_nested():
                if event.kind in {"announcement", "event"}:
                    deliver_notifications(db, event)
                else:
                    if event.department_id not in recipients:
                        roles = department_roles(db, event.department_id)
                        identities = {i.id: i for i in db.scalars(select(m.Identity).where(
                            m.Identity.id.in_(list(roles) or [""]), m.Identity.disabled.is_(False)))}
                        recipients[event.department_id] = (roles, identities)
                    roles, identities = recipients[event.department_id]
                    deliver_notifications(db, event, roles=roles, identities=identities)
                from apps.api.services import pwa
                with db.begin_nested():
                    pwa.queue_for_event(db, event)
                from .email_delivery import queue
                queue(db, event)
            event.state, event.last_error = "available_in_app", None
            event.attempts += 1
        except Exception as exc:
            # No private payloads or raw exception messages enter operator-visible logs.
            event.attempts += 1
            event.state = "failed" if event.attempts >= 5 else "retry"
            event.last_error = type(exc).__name__
            event.next_attempt_at = m.now() + timedelta(seconds=min(3600, 2 ** event.attempts))
            log.warning("outbox_dispatch_failed", extra={"outbox_id": event.id, "kind": event.kind, "attempts": event.attempts,
                                                         "state": event.state, "error_code": event.last_error})
        event.revision += 1
        departments.add(event.department_id)
        live.signal(db, event.department_id, scope="inbox", id=event.id)
    for department in departments:
        if department in reconciled:
            live.signal(db, department, scope="inbox", id=department)
        if department in event_departments:
            live.signal(db, department, scope="catalogue", id="events")
            live.signal(db, department, scope="availability")
    return departments


def planner_actor_for(db, claim):
    department = db.get(m.Department, claim['department_id'])
    identity = db.get(m.Identity, claim['requested_by'])
    return Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                 current_roles(db, identity.id, department.id), identity.person_id, 'worker')


def queue_generation_outcome(db, job):
    if job.kind != 'generation' or job.status not in {'succeeded', 'failed'} or job.error_code == 'ACCESS_REVOKED':
        return
    from apps.api.serialization import job_outcome
    outcome = job_outcome(job.status, job.result)
    if outcome != 'cancelled':
        db.add(m.Outbox(department_id=job.department_id, kind='generation_finished', event_key=f'generation-finished:{job.id}',
                        payload={'job_id': job.id, 'workspace_id': job.workspace_id, 'outcome': outcome}))


def resume_days(db, claim=None):
    if claim and 'resume_days' in claim:
        return claim['resume_days']
    settings = db.get_bind().get_execution_options().get('rota_write_settings') or Settings.from_env()
    return settings.generation_resume_days


def search_spent(job, values):
    value = (values or {}).get('search_elapsed_seconds')
    if job.kind == 'generation' and isinstance(value, (int, float)) and value >= 0:
        control = dict(job.search_control or {})
        control['spent_seconds'] = max(control.get('spent_seconds', 0), value)
        job.search_control = control


def needs_more_time(job):
    return {"status": "needs_more_time", "input_fingerprint": job.input_snapshot['fingerprint'],
            "algorithm_version": job.input_snapshot.get('compiler_version'),
            "additions": [], "removals": [], "optimality_proven": False,
            "solver_status": "UNKNOWN", "weighted_status": "UNKNOWN",
            "phase_timings": (job.progress or {}).get('phase_timings', {}),
            "search_elapsed_seconds": (job.search_control or {}).get('spent_seconds', 0)}


def validated_proposal(result, fingerprint):
    return bool(result and result.get('status') in {'candidate_feasible', 'candidate_with_gaps'}
                and result.get('input_fingerprint') == fingerprint
                and not (result.get('diagnostic_proposal') and result.get('status') == 'candidate_feasible'))


def coerce_diagnostic_candidate(snapshot, result):
    diagnostic = bool((snapshot or {}).get('diagnostic_proposal') or (result or {}).get('diagnostic_proposal'))
    if diagnostic and result and result.get('status') == 'candidate_feasible':
        return {**result, 'status': 'candidate_with_gaps', 'optimality_proven': False,
                'message': DIAGNOSTIC_PROPOSAL_MESSAGE}
    return result


def recover_checkpoint_candidate(snapshot, result):
    """Rebuild visible validation for a checkpoint before exposing it for review."""
    diagnostic = bool((snapshot or {}).get('diagnostic_proposal') or (result or {}).get('diagnostic_proposal'))
    if not result:
        return result
    if not result.get('checkpointed') or isinstance(result.get('validation'), dict):
        return coerce_diagnostic_candidate(snapshot, result)
    from packages.domain.proposal_reconciliation import overlay

    additions = list(range(len(result.get('additions') or [])))
    removals = list(range(len(result.get('removals') or [])))
    candidate = overlay(snapshot, result, additions, removals)
    validation = evaluate(candidate)
    status = 'candidate_with_gaps'
    if not diagnostic and validation.get('valid') and not validation.get('error_count'):
        status = 'candidate_feasible'
    return {**result, 'status': status, 'validation': validation,
            'optimality_proven': False if diagnostic else result.get('optimality_proven', False),
            'message': (DIAGNOSTIC_PROPOSAL_MESSAGE
                        if diagnostic else 'Best schedule found before the run stopped. Review it before applying.')}


def persist_generation_candidate(engine, claim, result, *, deadline=None):
    """Durably retain a valid solver candidate before display-only summarisation begins."""
    if not validated_proposal(result, claim.get('input_fingerprint')):
        return False

    def persist(db):
        job = db.get(m.Job, claim['id'], with_for_update=True,
                     options=[load_only(m.Job.status, m.Job.attempts, m.Job.input_fingerprint, m.Job.result,
                                        m.Job.revision, m.Job.department_id)])
        if (not job or job.status != 'running' or job.attempts != claim['attempt']
                or job.input_fingerprint != claim.get('input_fingerprint')):
            return False
        job.result = result
        job.revision += 1
        live.signal(db, live.job_topic(job.department_id, job.id), scope='jobs', id=job.id)
        return True

    return retry_write(engine, persist, deadline=deadline)


def enabled_jobs():
    disabled = select(m.Workspace.id).join(m.Department).where(
        m.Workspace.origin == 'auto', m.Department.score_check_enabled.is_(False))
    return or_(m.Job.workspace_id.is_(None), m.Job.workspace_id.not_in(disabled))


def oldest_queued_job(engine):
    with transaction(engine) as db:
        job = db.execute(select(m.Job.id, m.Job.created_at).where(enabled_jobs(), m.Job.status == 'queued')
                         .order_by(m.Job.created_at, m.Job.id).limit(1)).first()
        return ({'job_id': job.id, 'queued_seconds': max(0, (m.now() - aware(job.created_at)).total_seconds())}
                if job else {'job_id': None, 'queued_seconds': 0})


def job_available(db, *, recovering_worker=None):
    enabled = enabled_jobs()
    lost = func.coalesce(m.Job.heartbeat_at, m.Job.started_at) < m.now() - timedelta(seconds=JOB_LEASE_SECONDS)
    if recovering_worker is not None:
        lost = or_(lost, m.Job.worker_id == recovering_worker)
    return db.scalar(select(m.Job.id).where(enabled, or_(m.Job.status == 'queued',
        (m.Job.status == 'running') & lost)).limit(1)) is not None


def claim_pending(engine, *, recovering_worker=None):
    with transaction(engine) as db:
        if not job_available(db, recovering_worker=recovering_worker):
            return None
    return retry_write(engine, lambda db: claim_job(db, recovering_worker=recovering_worker))


def claim_job(db, *, recovering_worker=None):
    require_current_schema(db)
    enabled = enabled_jobs()
    # Bounded lease recovery after worker death; no lost running job remains forever.
    lease_expired = func.coalesce(m.Job.heartbeat_at, m.Job.started_at) < m.now() - timedelta(seconds=JOB_LEASE_SECONDS)
    lost = lease_expired if recovering_worker is None else or_(lease_expired, m.Job.worker_id == recovering_worker)
    expired = list(db.scalars(select(m.Job).where(enabled, m.Job.status == "running", lost).order_by(m.Job.created_at, m.Job.id)
        .limit((db.get_bind().get_execution_options().get("rota_write_settings") or Settings()).worker_batch_size)))
    for old in expired:
        if old.kind == 'planner_preview':
            from apps.api.services import planner_tools
            identity = db.get(m.Identity, old.requested_by)
            workspace = db.get(m.Workspace, old.workspace_id)
            actor = planner_actor_for(db, {'department_id': old.department_id, 'requested_by': old.requested_by}) \
                if identity and workspace else None
            current = (planner_tools.source_fingerprint(
                db, actor, workspace, s.Command.model_validate(old.input_snapshot.get('planner_command') or {}))
                       if actor and workspace and not workspace.archived_at else None)
            if current != old.input_snapshot.get('planner_source_fingerprint'):
                old.status, old.error_code, old.completed_at = 'failed', 'STALE_INPUTS', m.now()
                old.progress = job_progress_record(old.progress)
                old.revision += 1
                continue
        old.status = "queued" if old.attempts < 3 else "failed"
        old.error_code = "WORKER_LEASE_EXPIRED"
        native_cp_sat = old.kind == "generation" and old.input_snapshot.get('generation_engine') == 'cp_sat'
        validated = (old.result or {}) if native_cp_sat else {}
        checkpoint = {}
        if native_cp_sat and not validated_proposal(validated, old.input_fingerprint):
            checkpoint = old.resume_checkpoint or {}
            validated = checkpoint.get('result') or {}
        if native_cp_sat:
            validated = recover_checkpoint_candidate(old.input_snapshot, validated)
        has_proposal = validated_proposal(validated, old.input_fingerprint)
        if native_cp_sat and not has_proposal and not resume_current(db, old):
            old.status, old.error_code, old.completed_at = 'failed', 'STALE_INPUTS', m.now()
            old.progress = job_progress_record(old.progress)
            if not generation_resume_available(db, old, days=resume_days(db), error=old.error_code):
                clear_generation_resume(db, job_id=old.id)
            old.revision += 1
            queue_generation_outcome(db, old)
            continue
        controls = old.search_control or {}
        has_hint = checkpoint.get('has_solution', bool(checkpoint.get('hint')))
        budget_exhausted = (controls.get('minutes') is not None and controls.get('spent_seconds', 0) >= controls['minutes'] * 60
                            and not has_hint)
        expired_cache = native_cp_sat and resume_expired(old, resume_days(db))
        if native_cp_sat and (has_proposal or old.status == "failed" or budget_exhausted or expired_cache):
            identity = db.get(m.Identity, old.requested_by)
            if identity and not identity.disabled and may_run(db, identity, old):
                old.status, old.error_code = "succeeded", None
                old.result = validated if has_proposal else needs_more_time(old)
                old.completed_at = m.now()
                live.signal(db, live.job_topic(old.department_id, old.id), scope="jobs", id=old.id)
            else:
                old.status, old.error_code = "failed", "ACCESS_REVOKED"
        if old.status == "failed":
            old.completed_at = m.now()
            if old.kind == "swap_audience":
                from apps.api.services.swaps import store_audience
                store_audience(db, old.input_snapshot, None, old.error_code)
                live.signal(db, old.department_id, scope="swaps")
                live.signal(db, old.department_id, scope="requests")
                live.signal(db, live.job_topic(old.department_id, old.id), scope="jobs", id=old.id)
        if old.status in {'failed', 'succeeded'}:
            old.progress = job_progress_record(old.progress)
        if native_cp_sat:
            resume_available = generation_resume_available(db, old, days=resume_days(db), error=old.error_code,
                                                           expired=expired_cache)
            if not resume_available:
                clear_generation_resume(db, job_id=old.id)
                if isinstance(old.result, dict):
                    old.result = {key: value for key, value in old.result.items() if key != 'resume_available'}
            elif old.result and has_generation_checkpoint(old):
                old.result = {**(old.result or {}), 'resume_available': True}
        old.revision += 1
        queue_generation_outcome(db, old)
    queued = select(m.Job).where(enabled, m.Job.status == "queued")
    # The serialized claim reserves one solve slot. A paused expired job keeps its stored status.
    heavy = m.Job.kind.in_(_HEAVY_JOB_KINDS)
    if db.scalar(select(m.Job.id).where(m.Job.status == "running", heavy, lease_expired.is_not(True)).limit(1)):
        queued = queued.where(~heavy)
    if recovering_worker is not None and expired:
        queued = queued.order_by(m.Job.id.in_([old.id for old in expired]).desc())
    job = db.scalar(queued.order_by(m.Job.created_at).limit(1))
    if job is None:
        return None
    if job.kind == 'generation' and not resume_current(db, job):
        job.status, job.error_code, job.completed_at = 'failed', 'STALE_INPUTS', m.now()
        job.progress = job_progress_record(job.progress)
        if not generation_resume_available(db, job, days=resume_days(db), error=job.error_code):
            clear_generation_resume(db, job_id=job.id)
        job.revision += 1
        queue_generation_outcome(db, job)
        return None
    identity = db.get(m.Identity, job.requested_by)
    if identity.disabled or not may_run(db, identity, job):
        job.status, job.error_code = "failed", "ACCESS_REVOKED"
        job.progress = job_progress_record(job.progress)
        job.completed_at = m.now()
        job.revision += 1
        if not generation_resume_available(db, job, days=resume_days(db), error=job.error_code):
            clear_generation_resume(db, job_id=job.id)
        if job.kind == "swap_audience":
            from apps.api.services.swaps import store_audience
            store_audience(db, job.input_snapshot, None, job.error_code)
            live.signal(db, job.department_id, scope="swaps")
            live.signal(db, job.department_id, scope="requests")
            live.signal(db, live.job_topic(job.department_id, job.id), scope="jobs", id=job.id)
        return None
    lease_started = time.monotonic()
    job.status, job.started_at = "running", m.now()
    job.heartbeat_at = job.started_at
    job.worker_id = WORKER_ID
    # Deferred generation first freezes its rota on the worker.  Expose that
    # phase immediately so the client does not label a long snapshot as a
    # stalled solver.
    initial_stage = ('preparing' if job.kind == 'generation'
                     and job.input_snapshot.get('_deferred_generation') else 'checking')
    job.progress = {"stage": initial_stage, "elapsed_seconds": 0,
                    **({key: (job.progress or {}).get(key, default) for key, default in
                        [('events', []), ('solution', None), ('improvements_found', 0), ('precheck_issues', []),
                         ('best_score', None)]}
                       if job.kind == 'generation' else {})}
    job.attempts += 1
    job.revision += 1
    if job.kind == 'generation' and not generation_resume_available(db, job, days=resume_days(db), error=None):
        clear_generation_resume(db, job_id=job.id)
    if job.kind == 'generation':
        # Let connected planners see the leased stage before snapshot
        # preparation begins; the persisted row remains the source of truth.
        live.signal(db, live.job_topic(job.department_id, job.id), scope="jobs", id=job.id)
    return {"id": job.id, "kind": job.kind, "input": job.input_snapshot, "input_fingerprint": job.input_fingerprint,
            "attempt": job.attempts,
            "department_id": job.department_id, "requested_by": job.requested_by, "lease_started": lease_started,
            "resume_state": job.resume_state, 'resume_checkpoint': job.resume_checkpoint,
            "search_control": job.search_control, "progress": job.progress}


def check_inputs_unchanged(db, workspace, stamp):
    """True when the inputs a check read are provably the writer's, so its manifest needs no rebuild.

    Mirrors the API writer (commands.py): the preparation stamp (input revisions, every department's
    counters in the organisation, compact currency) is equal under the writer, and the recurring
    projections over the window the check's token covers are the same at the check's start and now.
    Anything else takes the full rebuild.
    """
    if stamp is None or stamp[2] is None:
        return False
    from apps.api.snapshots import currency_match, occurrences_unchanged_since, preparation_stamp
    since, recorded, window = stamp
    if not currency_match(preparation_stamp(db, workspace), recorded):
        return False
    # The snapshot widens its window to block-quota periods; its manifest recorded the window it read.
    # Always compare: the hint drops the local date after the window ends, yet an overnight occurrence still counts.
    return occurrences_unchanged_since(db, workspace, since, window)


def stage_feasibility_result(db, claim, result):
    """Write the proof on its own row so cancellation can still change the job."""
    require_current_schema(db)
    table = m.JobFeasibilityResult.__table__
    insert = postgres_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert
    lease = select(m.Job.id, m.Job.attempts, literal(result, table.c.payload.type)).where(
        m.Job.id == claim['id'], m.Job.kind == 'feasibility', m.Job.status == 'running',
        m.Job.attempts == claim['attempt'], m.Job.worker_id == WORKER_ID,
        m.Job.input_fingerprint == claim['input_fingerprint'],
    )
    db.execute(insert(table).from_select(['job_id', 'attempt', 'payload'], lease).on_conflict_do_nothing())


def finish_job(db, claim, result, error, *, progress=None, error_detail=None, feasibility_projection=None):
    require_current_schema(db)
    # Feasibility completion needs current authority and lease state, not the frozen department graph.
    # Other kinds load their retained evidence lazily when their stale/proposal guards require it.
    job = db.get(m.Job, claim["id"], options=[defer(m.Job.input_snapshot), defer(m.Job.result)])
    if not job or job.status != "running" or job.attempts != claim["attempt"]:
        return False
    if job.kind == 'feasibility' and result is not None and not error:
        raise ValueError('Feasibility proof must be staged before completion')
    if feasibility_projection is not None:
        payload = m.JobFeasibilityResult
        if job.input_fingerprint != claim['input_fingerprint'] or not db.scalar(select(
                select(payload.job_id).where(payload.job_id == job.id, payload.attempt == job.attempts).exists())):
            return False
    if job.kind == 'generation' and validated_proposal(result, job.input_fingerprint):
        job.result = result
    identity = db.get(m.Identity, job.requested_by)
    cache_expired = job.kind == 'generation' and resume_expired(job, resume_days(db, claim))
    if identity.disabled or not may_run(db, identity, job):
        result, error = None, "ACCESS_REVOKED"
    if job.kind == 'generation' and not error:
        if not resume_current(db, job):
            result, error = (job.result, None) if validated_proposal(job.result, job.input_fingerprint) else (None, 'STALE_INPUTS')
        elif cache_expired and (result or {}).get('status') not in {'candidate_feasible', 'candidate_with_gaps'}:
            result = needs_more_time(job)
    if job.kind == 'catalogue_proof' and not error:
        from apps.api.services.jobs import catalogue_proof_stale
        from .catalogue_proof import actor_for
        if catalogue_proof_stale(db, actor_for(db, claim), job.input_snapshot):
            result, error = None, 'STALE_INPUTS'
    if job.kind == 'planner_preview' and not error:
        from apps.api.services import planner_tools
        workspace = db.get(m.Workspace, job.workspace_id)
        actor = planner_actor_for(db, claim)
        command = s.Command.model_validate(job.input_snapshot.get('planner_command') or {})
        if (workspace is None or workspace.status != 'draft' or workspace.archived_at is not None
                or planner_tools.source_fingerprint(db, actor, workspace, command)
                != job.input_snapshot.get('planner_source_fingerprint')):
            result, error = None, 'STALE_PREVIEW'
    if job.kind == 'fill_options' and not error:
        from apps.api.services import staffing
        from apps.api.errors import Problem
        try:
            actor = planner_actor_for(db, claim)
            value = s.FillOptionsInput.model_validate(job.input_snapshot.get('fill_options') or {})
            current = staffing.fill_options_job_scope(db, actor, value)[1]
            if current != job.input_fingerprint or (result or {}).get('source_fingerprint') != job.input_fingerprint:
                result, error = None, 'STALE_INPUTS'
        except Problem as rejected:
            result, error = None, rejected.code
    if job.kind == 'validation' and not error:
        from apps.api.snapshots import domain_snapshot
        workspace = db.get(m.Workspace, job.workspace_id)
        if (workspace.status != 'draft' or workspace.archived_at is not None
                or ('previous_check' in claim
                    and (workspace.validation, workspace.accepted_issues) != claim['previous_check'])
                or (current_manifest := result['input_manifest']
                    if 'input_manifest' in result and check_inputs_unchanged(db, workspace, claim.get('check_stamp'))
                    else domain_snapshot(db, workspace)['input_manifest'])['token'] != result['fingerprint']):
            error = 'STALE_INPUTS'
        else:
            # The same inputs can have newer counters after an access change.
            if manifest := result.get('input_manifest'):
                result = {**result, 'input_manifest': {**manifest, 'dirty_hint': current_manifest['dirty_hint']}}
            workspace.validation = result
            result = {**result, 'checked_revision': workspace.revision}
            # A current-rota check queued only an intent token; record the inputs it checked, so readers comparing a
            # job with the rota's fingerprint (panel, history, Home) see a fresh check as current.
            if job.input_snapshot.get('validation_basis') == 'current':
                job.input_fingerprint = result['fingerprint']
    if job.kind in {'merge_preview', 'merge_apply'} and not error:
        from .workspace_merge import execute
        from apps.api.services.workspace_access import audit_envelope
        from apps.api.services.audit_log import event_action, subject_detail
        from packages.contracts.schemas import Command
        command = Command.model_validate(claim['input']['command'])
        result = execute(db, claim, result, replay=True)
        db.flush()
        db.add(audit_envelope(db, command, result, m.AuditEvent(department_id=job.department_id, actor_id=job.requested_by,
            action=event_action(command.kind, result), target_id=job.workspace_id,
            reason=command.payload['reason'], detail={**subject_detail(db, command.kind, result), 'job_id': job.id,
                'input_changes': list(db.info.get('_input_changes', {}).values())}, correlation_id=job.id)))
    if job.kind == "swap_audience":
        from apps.api.services.swaps import store_audience
        result = store_audience(db, job.input_snapshot, result, error)
    if job.kind == 'generation':
        result = coerce_diagnostic_candidate(job.input_snapshot, result)
    if job.kind == 'generation' and not validated_proposal(result, job.input_fingerprint):
        # A worker exception after an incumbent checkpoint must still leave a
        # freshly validated review candidate; wording remains optional.
        checkpoint_result = job.result or {}
        if checkpoint_result.get('checkpointed'):
            result = recover_checkpoint_candidate(job.input_snapshot, checkpoint_result)
    if result is None and job.kind == 'generation' and validated_proposal(job.result, job.input_fingerprint):
        result = job.result
    job.status = "failed" if error else "succeeded"
    job.completed_at = m.now()
    if job.kind == 'generation' and not error:
        from apps.api.errors import Problem
        from apps.api.workspace_baselines import verify_generation_snapshot
        from .generation_evidence import retain_completed_candidate_evidence
        try:
            workspace = db.get(m.Workspace, job.workspace_id)
            baseline = verify_generation_snapshot(db, workspace, job.input_snapshot)
            result, manifest = retain_completed_candidate_evidence(db, job, baseline, result)
            job.input_snapshot = {**job.input_snapshot, 'generation_manifest': manifest}
        except Problem as rejected:
            error, result, job.status = rejected.code, None, 'failed'
    if error_detail is not None and error:
        result = {'status': 'refused', 'error': error_detail}
    if feasibility_projection is not None and not error:
        metadata, job.result_summary = feasibility_projection
        job.read_metadata = {**(job.read_metadata or metadata), 'result': metadata['result']}
    else:
        job.result = result
    job.error_code = error
    search_spent(job, result)
    search_spent(job, progress)
    if job.kind == 'generation':
        retain_resume = generation_resume_available(db, job, days=resume_days(db, claim), error=error,
                                                    expired=cache_expired)
        if not retain_resume:
            clear_generation_resume(db, job_id=job.id)
            if isinstance(job.result, dict):
                job.result = {key: value for key, value in job.result.items() if key != 'resume_available'}
        elif job.result and has_generation_checkpoint(job):
            job.result = {**(job.result or {}), 'resume_available': True}
    if job.kind == 'generation' and progress is not None:
        progress = _preserve_generation_stage(job.progress, progress)
    job.progress = job_progress_record(progress if progress is not None else job.progress)
    job.revision += 1
    queue_generation_outcome(db, job)
    live.signal(db, live.job_topic(job.department_id, job.id), scope="jobs", id=job.id)
    if job.kind == 'swap_audience':
        live.signal(db, job.department_id, scope="swaps")
        live.signal(db, job.department_id, scope="requests")
    if job.kind in {'validation', 'merge_apply'} and not error:
        live.signal(db, job.department_id, scope="workspace", id=job.workspace_id,
                    job_id=job.id if job.kind == 'validation' else None)
    return True


def lease_job(db, job_id):
    # Include metadata for before_update; only legacy rows without it need a one-time raw backfill.
    return db.get(m.Job, job_id, options=[load_only(
        m.Job.kind, m.Job.status, m.Job.attempts, m.Job.requested_by, m.Job.department_id,
        m.Job.workspace_id, m.Job.input_fingerprint, m.Job.progress, m.Job.heartbeat_at,
        m.Job.revision, m.Job.search_control, m.Job.read_metadata)])


def elapsed_progress(progress, since_report):
    """Advance the clock between solver callbacks without claiming unobserved progress."""
    return {**progress, 'elapsed_seconds': progress.get('elapsed_seconds', 0) + max(0, since_report)}


def heartbeat_job(db, claim, progress):
    job = lease_job(db, claim["id"])
    if not job or job.status != "running" or job.attempts != claim["attempt"]:
        return False
    identity = db.get(m.Identity, job.requested_by)
    if identity.disabled or not may_run(db, identity, job):
        clear_generation_resume(db, job_id=job.id)
        return False
    if job.kind == 'generation' and not resume_current(db, job):
        clear_generation_resume(db, job_id=job.id)
        return False
    job.heartbeat_at = m.now()
    previous = job.progress or {}
    if job.kind == 'generation' and previous.get('best_score') is not None and 'best_score' not in progress:
        progress = {**progress, 'best_score': previous['best_score']}
    if job.kind == 'generation':
        progress = _preserve_generation_stage(previous, progress)
    elapsed_tick = (job.kind == 'generation'
                    and int(max(0, progress.get('elapsed_seconds', 0)))
                    > int(max(0, previous.get('elapsed_seconds', 0))))
    updated = (job.kind == 'fill_options' and previous != progress) or (job.kind == 'generation' and (
        previous.get('stage') != progress.get('stage') or
        previous.get('precheck_issues') != progress.get('precheck_issues') or
        previous.get('best_score') != progress.get('best_score') or
        (progress.get('events') or [{}])[-1].get('seq', 0) > (previous.get('events') or [{}])[-1].get('seq', 0)
        or elapsed_tick))
    job.progress = progress
    search_spent(job, progress)
    if updated:
        job.revision += 1
        live.signal(db, live.job_topic(job.department_id, job.id), scope="jobs", id=job.id)
    return True


RESUME_CHECKPOINT_FIELDS = frozenset({'hint', 'has_solution', 'result', 'checkpoint_assignments',
                                      'search_elapsed_seconds', 'diagnostic_proposal', 'strict_conflict'})


def checkpoint_job(db, claim, state, *, prepared=None):
    """Commit only the current authorised attempt's private prepared model and best hint."""
    job = db.get(m.Job, claim['id'], with_for_update=True, options=[load_only(
        m.Job.status, m.Job.attempts, m.Job.requested_by, m.Job.department_id, m.Job.kind, m.Job.resume_checkpoint,
        m.Job.workspace_id, m.Job.input_fingerprint, m.Job.created_at, m.Job.completed_at, m.Job.search_control)])
    if not job or job.attempts != claim['attempt']:
        return False
    if job.status != 'running':
        if job.status in {'cancelled', 'failed'}:
            clear_generation_resume(db, job_id=job.id)
        return False
    if validated_proposal(state.get('result'), job.input_fingerprint):
        # Canonical proposals survive cache expiry/input invalidation before the terminal write.
        job.result = state['result']
    identity = db.get(m.Identity, job.requested_by)
    if not identity or identity.disabled or not may_run(db, identity, job):
        clear_generation_resume(db, job_id=job.id)
        return False
    if not resume_current(db, job, days=resume_days(db, claim)):
        clear_generation_resume(db, job_id=job.id)
        return False
    previous = job.resume_checkpoint or {}
    if not previous or previous.get('key') != state.get('key'):
        clear_generation_resume(db, workspace_id=job.workspace_id, keep_job_id=job.id)
        from packages.domain.solver_resume import compress_state
        job.resume_state = prepared if prepared is not None else compress_state(
            {key: value for key, value in state.items() if key not in RESUME_CHECKPOINT_FIELDS})
    job.resume_checkpoint = {key: value for key, value in state.items()
                             if key != 'result' and (key in RESUME_CHECKPOINT_FIELDS or key == 'key')}
    search_spent(job, state)
    return True


WORKER_ID = os.getenv('ROTA_WORKER_ID') or f"{socket.gethostname()}:{os.getpid()}"
JOB_HEARTBEAT_SECONDS = 3
GENERATION_HEARTBEAT_SECONDS = 1
ALIVE_FILE = os.getenv("ROTA_WORKER_ALIVE_FILE", "")
_beat: dict[int, float] = {}  # per database engine
_app_beat: dict[int, float] = {}


def _touch_alive_file():
    if ALIVE_FILE:
        with open(ALIVE_FILE, 'w') as handle:
            handle.write(m.now().isoformat())


def ping_app_heartbeat(engine):
    """Report a healthy worker pass to the configured monitor, at most once a minute."""
    url = os.getenv("FMUC_HEARTBEAT_APP_URL", "")
    if not url or time.monotonic() - _app_beat.get(id(engine), -60.0) < 60:
        return
    _app_beat[id(engine)] = time.monotonic()
    try:
        with httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        log.warning("app_heartbeat_failed", extra={"error_code": type(exc).__name__})


def heartbeat(engine, *, deadline=None, record_worker=True):
    """Refresh the container keep-alive, and optionally record a job-worker heartbeat."""
    if not record_worker:
        _touch_alive_file()
        return
    if time.monotonic() - _beat.get(id(engine), -60.0) < 10:
        return
    def write(db):
        H = m.WorkerHeartbeat
        insert = postgres_insert if engine.dialect.name == 'postgresql' else sqlite_insert
        now = m.now()
        stmt = insert(H).values(worker_id=WORKER_ID, started_at=now, last_seen_at=now, passes=1,
                                release=build()["release"])
        db.execute(stmt.on_conflict_do_update(index_elements=[H.worker_id], set_={
            "last_seen_at": stmt.excluded.last_seen_at, "passes": func.coalesce(H.passes, 0) + 1,
            "release": stmt.excluded.release,
        }))
    try:
        retry_write(engine, write, deadline=deadline, serialize=False)
        _beat[id(engine)] = time.monotonic()
        _touch_alive_file()
        ping_app_heartbeat(engine)
    except Exception as exc:  # a missing heartbeat must never stop the work itself
        log.warning("worker_heartbeat_failed", extra={"error_code": type(exc).__name__})


EXPIRED_SESSION_DAYS = 7  # kept past expiry only as long as someone might ask why a sign-in ended
# Weak keys prevent reclaimed engine IDs from throttling another database.
_pruned = WeakKeyDictionary()
FEEDBACK_SCREENSHOT_DAYS = FEEDBACK_LIMITS["screenshot_days"]
_feedback_pruned = WeakKeyDictionary()
_bulk_staged_pruned = WeakKeyDictionary()


def prune_security_state(engine, batch=500, *, settings=None):
    """Hard-delete one bounded batch of expired sessions and of login attempts older than the sign-in rate-limit
    window (plus a day), at most every 10 minutes. Both are expired security state that authorises nothing, not
    operational records or evidence. Authentication events have separate reviewed retention; other audit
    evidence is never pruned. Expired mailbox consent flows are pruned before their sessions."""
    settings = settings or engine.get_execution_options().get("rota_write_settings") or Settings.from_env()
    retention_days = settings.password_login_audit_retention_days
    if not 30 <= retention_days <= 3650 or not 1 <= batch <= 500:
        raise ValueError("Invalid authentication-event retention bounds")
    if time.monotonic() - _pruned.get(engine, -3600.0) < 600:
        return 0
    moment = m.now()
    with transaction(engine, write=True) as conn:
        from apps.api.services import mailboxes
        mailboxes.prune(conn)
        sessions = select(m.Session.id).where(or_(m.Session.expiry_unlimited.is_(False), m.Session.revoked.is_(True)),
                                               m.Session.expires_at < moment - timedelta(days=EXPIRED_SESSION_DAYS)).limit(batch)
        attempts = select(m.LoginAttempt.id).where(m.LoginAttempt.created_at < moment - timedelta(
            seconds=settings.password_login_window_seconds) - timedelta(days=1)).limit(batch)
        expired_limits = select(m.RateLimit.key_hash).where(m.RateLimit.expires_at < moment).limit(batch)
        removed = (conn.execute(delete(m.Session).where(m.Session.id.in_(sessions.scalar_subquery()))).rowcount
                   + conn.execute(delete(m.LoginAttempt).where(m.LoginAttempt.id.in_(attempts.scalar_subquery()))).rowcount
                   + conn.execute(delete(m.RateLimit).where(m.RateLimit.key_hash.in_(expired_limits.scalar_subquery()))).rowcount)
    # Commit disposable state even if separately privileged audit retention is unavailable.
    with transaction(engine, write=True) as conn:
        if engine.dialect.name == "postgresql":
            removed += conn.scalar(text("SELECT prune_auth_login_events(:days, :batch)"),
                                   {"days": retention_days, "batch": batch})
        else:
            expired_events = select(m.AuthLoginEvent.id).where(
                m.AuthLoginEvent.created_at < moment - timedelta(days=retention_days)).order_by(
                m.AuthLoginEvent.created_at, m.AuthLoginEvent.id).limit(batch)
            removed += conn.execute(delete(m.AuthLoginEvent).where(
                m.AuthLoginEvent.id.in_(expired_events.scalar_subquery()))).rowcount
    _pruned[engine] = time.monotonic()
    return removed


def prune_feedback_screenshots(engine, batch=500):
    """Clear a bounded batch of expired private image bytes every 10 minutes; reports and audit evidence remain."""
    if time.monotonic() - _feedback_pruned.get(engine, -3600.0) < 600:
        return 0
    _feedback_pruned[engine] = time.monotonic()
    moment = m.now()
    expired = select(m.FeedbackReport.id).where(
        m.FeedbackReport.screenshot.is_not(None),
        m.FeedbackReport.created_at <= moment - timedelta(days=FEEDBACK_SCREENSHOT_DAYS),
    ).order_by(m.FeedbackReport.created_at).limit(batch)
    with engine.begin() as conn:
        return conn.execute(update(m.FeedbackReport).where(m.FeedbackReport.id.in_(expired.scalar_subquery())).values(
            screenshot=None, screenshot_media_type=None)).rowcount


def prune_bulk_staged(engine):
    """Purge expired personal change payloads globally; retain their lifecycle history."""
    if time.monotonic() - _bulk_staged_pruned.get(engine, -3600.0) < 600:
        return 0
    from apps.api.services.bulk_staged import EXPIRE_BATCH_SIZE, expire
    count = retry_write(engine, expire)
    # Drain a full batch on the next pass instead of leaving a retention backlog for ten minutes.
    if count < EXPIRE_BATCH_SIZE:
        _bulk_staged_pruned[engine] = time.monotonic()
    return count


_generation_pruned = WeakKeyDictionary()


_supersessions_filled = WeakKeyDictionary()
_supersessions_retry_after = WeakKeyDictionary()
_supersessions_failures = WeakKeyDictionary()
# Reuses read_projection_warm_cursors as a done-marker; it is derived worker progress in an existing table, so no migration is needed.
_SUPERSESSION_CURSOR_KEY = m.SUPERSESSION_CURSOR_KEY
_SUPERSESSION_DONE = m.SUPERSESSION_DONE


def _supersessions_done(engine):
    with transaction(engine) as db:
        cursor = db.get(m.ReadProjectionWarmCursor, _SUPERSESSION_CURSOR_KEY)
        return cursor is not None and cursor.after_id == _SUPERSESSION_DONE


def _mark_supersessions_done(engine):
    def mark(db):
        cursor = db.get(m.ReadProjectionWarmCursor, _SUPERSESSION_CURSOR_KEY, with_for_update=True)
        pending = db.scalar(select(m.Job.id).where(m.Job.kind == 'generation', m.Job.supersedes.is_(None)).limit(1))
        if pending is not None:
            return False
        if cursor is None:
            db.add(m.ReadProjectionWarmCursor(key=_SUPERSESSION_CURSOR_KEY, after_id=_SUPERSESSION_DONE))
        else:
            cursor.after_id = _SUPERSESSION_DONE
        return True
    return retry_write(engine, mark, serialize=False)


def fill_supersessions(engine, *, settings=None):
    """Fill older generation-run supersession records in bounded housekeeping passes."""
    marker = _supersessions_filled.get(engine, -3600.0)
    now = time.monotonic()
    settings = settings or engine.get_execution_options().get('rota_write_settings') or Settings.from_env()
    if now - marker < settings.worker_supersession_interval_seconds:
        return 0
    if now < _supersessions_retry_after.get(engine, -3600.0):
        return 0
    if _supersessions_done(engine):
        return 0
    _supersessions_filled[engine] = now
    try:
        from apps.api.services.jobs import (apply_supersession_backfill, prepare_supersession_backfill)
        # The potentially expensive JSON scan and supersession computation are read-only.
        with transaction(engine) as db:
            if engine.dialect.name == 'postgresql':
                timeout_ms = min(settings.command_statement_timeout_ms,
                                 max(1, settings.command_write_budget_ms - 1000))
                db.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}ms'"))
            records, has_pending = prepare_supersession_backfill(
                db, workspaces=settings.worker_supersession_workspaces,
                rows=settings.worker_supersession_batch_size)
        if not has_pending:
            _mark_supersessions_done(engine)
            _supersessions_failures.pop(engine, None)
            _supersessions_retry_after.pop(engine, None)
            return 0
        if not records:
            raise RuntimeError('supersession backfill found pending rows without readable facts')
        count = retry_write(engine, lambda db: apply_supersession_backfill(db, records), serialize=False)
    except Exception:
        failures = _supersessions_failures.get(engine, 0) + 1
        _supersessions_failures[engine] = failures
        _supersessions_retry_after[engine] = now + min(
            settings.worker_supersession_interval_seconds * (2 ** failures),
            settings.worker_supersession_retry_max_seconds)
        raise
    _supersessions_failures.pop(engine, None)
    _supersessions_retry_after.pop(engine, None)
    return count


def prune_abandoned_feasibility_results(db, limit):
    # An expired attempt or a terminal refusal can never publish its staged payload.
    payload = m.JobFeasibilityResult.__table__
    key = (payload.c.job_id, payload.c.attempt)
    jobs = m.Job.__table__
    abandoned = select(*key).join(jobs, jobs.c.id == payload.c.job_id).where(
        or_(payload.c.attempt < jobs.c.attempts, jobs.c.status.in_(('cancelled', 'failed')))
    ).order_by(*key).limit(limit)
    db.execute(delete(payload).where(tuple_(*key).in_(abandoned)))


def prune_generation_state(engine, *, settings=None):
    """Release only disposable preparation/hints; job inputs, proposals and audit remain."""
    if time.monotonic() - _generation_pruned.get(engine, -3600.0) < 600:
        return 0
    settings = settings or engine.get_execution_options().get('rota_write_settings') or Settings.from_env()
    def prune(db):
        prune_abandoned_feasibility_results(db, settings.worker_batch_size)
        return prune_generation_resume(db, days=settings.generation_resume_days, limit=settings.worker_batch_size)

    count = retry_write(engine, prune, serialize=False)
    _generation_pruned[engine] = time.monotonic()
    return count


def prepare_deferred_generation(engine, claim, settings):
    """Recheck generation authority on lease, then freeze or refresh the solver inputs."""
    if claim['kind'] != 'generation':
        return
    prior = claim['input']
    deferred = prior.get('_deferred_generation')
    from apps.api.errors import require
    from apps.api.services.jobs import (catalogue_proof_identity, generation_request_from_snapshot,
                                        record_supersessions, require_solver_compatible)
    from apps.api.workspace_baselines import (baseline_reference, frozen_department_settings, generation_manifest,
                                             verify, verify_generation_snapshot)
    from apps.api.services.planning_groups import guard_generation
    from apps.api.snapshots import domain_snapshot, draft_policy_hint
    from packages.contracts import schemas as s
    from packages.domain.generation_scope import has_population
    from packages.domain.solver_rules import COMPILER_VERSION
    with transaction(engine) as db:
        job = db.get(m.Job, claim['id'], with_for_update=True)
        require(job is not None and job.status == 'running' and job.attempts == claim['attempt'],
                'STALE_INPUTS', 'The auto-schedule request is no longer current', 409)
        workspace = db.get(m.Workspace, job.workspace_id)
        expected_revision = prior.get('workspace', {}).get('revision')
        require(workspace is not None and (not deferred or workspace.revision == expected_revision) and workspace.status == 'draft'
                and workspace.archived_at is None, 'STALE_INPUTS',
                'The rota changed before auto-scheduling started', 409)
        department = db.get(m.Department, workspace.department_id)
        identity = db.get(m.Identity, job.requested_by)
        require(identity is not None and not identity.disabled, 'ACCESS_REVOKED', 'The requester no longer has access', 403)
        actor = Actor(identity.id, workspace.department_id, department.organisation_id,
                      identity.display_name, current_roles(db, identity.id, workspace.department_id),
                      identity.person_id, 'worker')
        require(actor.permits('coordinator'), 'FORBIDDEN', 'The requester no longer has coordinator access', 403)
        requested = generation_request_from_snapshot(workspace, prior)
        value = s.JobInput.model_validate({**requested, 'diagnostic_proposal': prior.get('diagnostic_proposal', False)})
        retained_population = prior.get('planning_population')

        def accept_frozen_scope(scope):
            require(scope.get('generation_role_bindings') == prior.get('generation_role_bindings'),
                    'STALE_INPUTS', 'Scheduling population roles changed; start a fresh run', 409)
            if prior.get('_generation_authority_pending'):
                job.input_snapshot = claim['input'] = {k: v for k, v in prior.items()
                                                       if k != '_generation_authority_pending'}
                clear_generation_resume(db, workspace_id=workspace.id, keep_job_id=job.id)
                record_supersessions(db, job)

        if not deferred:
            organisation = db.get(m.Organisation, department.organisation_id)
            hint = (prior.get('input_manifest') or {}).get('dirty_hint') or {}
            if not (prior.get('baseline') and 'policies' in prior) or (
                    hint.get('input_revision') == organisation.input_revision and all(
                        hint.get(key) == value for key, value in draft_policy_hint(workspace).items())):
                accept_frozen_scope(guard_generation(db, actor, workspace, None, value,
                                                     retained_population=retained_population))
                return
        baseline = verify_generation_snapshot(db, workspace, claim['input'])
        defaults = s.GenerationOptions.model_validate(claim['input'].get('generation_defaults') or {})
        options = s.GenerationOptions.model_validate(claim['input'].get('generation_options') or {})
        require(claim['input'].get('generation_engine', 'legacy') == settings.generation_engine,
                'STALE_SCHEDULER', 'The scheduler changed; start a fresh auto-schedule run', 409)
        if (claim['input'].get('generation_engine') == 'cp_sat'
                and claim['input'].get('compiler_version') not in (None, COMPILER_VERSION)):
            raise ValueError('Scheduler version changed; request a fresh job')
        snapshot = domain_snapshot(db, workspace,
                                   wish_debt_cap=options.scoring.wish_debt_cap,
                                   lead_balance=options.scoring.lead_balance)
        if claim['input'].get('generation_scope_source'):
            snapshot['generation_scope_source'] = claim['input']['generation_scope_source']
        scope = guard_generation(db, actor, workspace, snapshot, value, require_population=not deferred,
                                 retained_population=retained_population)
        if not deferred:
            if snapshot['policies'] == prior['policies']:
                accept_frozen_scope(scope)
                return
            require(snapshot['baseline']['id'] == baseline.id, 'STALE_INPUTS',
                    'The draft baseline changed; start a fresh run', 409)
        focus = set(snapshot.get('focus_ids', []))
        require_generation_sessions_in_scope(value.session_ids, sessions=(
            (row['id'], row['cancelled']) for row in snapshot.get('shifts', []) if row['id'] in focus))
        if value.session_ids:
            snapshot['generation_session_ids'] = value.session_ids
        department_settings = frozen_department_settings(verify(baseline), workspace.department_id, baseline.included_sources)
        snapshot['autoschedule_animation'] = s.AutoscheduleAnimationSettings.model_validate(
            (department_settings.get('display_settings') or {}).get('autoschedule_animation') or {}).model_dump(mode='json')
        snapshot['generation_options'] = options.model_dump(mode='json')
        snapshot['generation_defaults'] = defaults.model_dump(mode='json')
        snapshot['baseline'] = baseline_reference(baseline)
        snapshot['generation_options_overridden'] = prior.get('generation_options_overridden', False)
        snapshot['generation_engine'] = settings.generation_engine
        require(not value.diagnostic_proposal or snapshot['generation_engine'] == 'cp_sat',
                'DIAGNOSTIC_ENGINE_REQUIRED', 'Diagnostic proposals require the CP-SAT scheduler')
        snapshot['diagnostic_proposal'] = value.diagnostic_proposal
        if snapshot['generation_engine'] == 'cp_sat':
            require_solver_compatible(snapshot)
            snapshot['compiler_version'] = COMPILER_VERSION
        if value.generation_scope is not None:
            proof = catalogue_proof_identity()
            require(proof is not None, 'BUILD_IDENTITY_REQUIRED',
                    'Restart on a stable build before scheduling a selected population', 409)
            requested_source = claim['input'].get('generation_scope_source')
            require(requested_source is None or requested_source == proof['source_digest'],
                    'STALE_INPUTS', 'The selected population was prepared by another build; request a fresh run', 409)
            snapshot['generation_scope_source'] = proof['source_digest']
            snapshot['generation_scope'] = value.generation_scope.model_dump(mode='json')
            require(has_population(snapshot), 'INVALID_SCOPE',
                    'No people match this group during the selected duties; change the population or duty selection', 422)
        if snapshot.get('generation_scope') is None and 'planning_population' not in snapshot:
            snapshot.pop('applied_planning_population', None)
        from apps.api.input_changes import watermark
        from apps.api.services.rota_summary import reusable_check
        from packages.domain.rota_summary import rota_summary
        snapshot['audit_watermark'] = watermark(db, workspace.department_id)
        snapshot['currency_hint'] = currency_hint(db, workspace)
        snapshot['repair_unavailable'] = True
        snapshot['repair_conflicts'] = True
        if reusable_check(workspace.validation, snapshot['fingerprint'], defaults.model_dump(mode='json')):
            snapshot['fix_list_before'] = rota_summary(workspace.validation,
                accepted_issues=workspace.accepted_issues or [])['fix_list']
        snapshot['_generation_request'] = requested
        if 'continuation' in prior:
            snapshot['continuation'] = prior['continuation']
        if not deferred:
            job.resume_state = job.resume_checkpoint = job.result = None
            claim['resume_state'] = claim['resume_checkpoint'] = None
            job.progress = claim['progress'] = {'stage': 'checking', 'elapsed_seconds': 0}
        snapshot['generation_manifest'] = generation_manifest(
            baseline, snapshot, job=job, requester_id=job.requested_by)
        job.input_snapshot = snapshot
        job.input_fingerprint = snapshot['fingerprint']
        if prior.get('_generation_authority_pending'):
            clear_generation_resume(db, workspace_id=workspace.id, keep_job_id=job.id)
        record_supersessions(db, job)
        claim['input'] = snapshot
        claim['input_fingerprint'] = snapshot['fingerprint']
        job.revision += 1
        return True


def housekeeping(engine, settings):
    try:
        prune_security_state(engine, settings=settings)
    except DatabaseWriteDeferred:
        raise
    except Exception as exc:  # housekeeping must never stop the work itself
        log.warning("security_state_prune_failed", extra={"error_code": type(exc).__name__})
    try:
        prune_feedback_screenshots(engine)
    except DatabaseWriteDeferred:
        raise
    except Exception as exc:  # housekeeping must never stop the work itself
        log.warning("feedback_screenshot_prune_failed", extra={"error_code": type(exc).__name__})
    try:
        prune_bulk_staged(engine)
    except DatabaseWriteDeferred:
        raise
    except Exception as exc:  # housekeeping must never stop the work itself
        log.warning("bulk_staged_prune_failed", extra={"error_code": type(exc).__name__})
    try:
        fill_supersessions(engine, settings=settings)
    except Exception as exc:  # housekeeping must never stop the work itself
        log.warning("supersession_fill_failed", extra={"error_code": type(exc).__name__})
    # Clear the notification backlog in one pass (bounded), rather than one event every two seconds.
    departments = dispatch_pending(engine)
    try:
        from . import push_delivery
        departments |= push_delivery.dispatch_pending(engine, settings)
    except Exception as exc:
        log.warning("push_dispatch_failed", extra={"error_code": type(exc).__name__})
    return departments


def process_one(engine, *, scheduler=None, solver_workers=1, settings=None, recover_owned=False):
    settings = settings or engine.get_execution_options().get("rota_write_settings") or Settings.from_env()
    pass_engine = engine
    queued = oldest_queued_job(engine)
    claim_first = queued['job_id'] is not None and queued['queued_seconds'] >= settings.worker_claim_max_defer_seconds
    departments = set() if claim_first else housekeeping(engine, settings)
    claim_engine = engine.execution_options(rota_defer_busy_writer=False) if claim_first else engine
    claim = claim_pending(claim_engine, recovering_worker=WORKER_ID if recover_owned else None)
    # Recover a crash after final validation before TTL pruning can release its private checkpoint.
    try:
        prune_generation_state(engine, settings=settings)
    except DatabaseWriteDeferred:
        if not claim:
            raise
    except Exception as exc:
        log.warning('generation_resume_prune_failed', extra={'error_code': type(exc).__name__})
    if claim:
        # Once claimed, checkpoints and results retain their lease-bound retry budget.
        engine = engine.execution_options(rota_defer_busy_writer=False)
        claim['resume_days'] = settings.generation_resume_days
        result, error, error_detail = None, None, None
        started = time.monotonic()
        _memory_probe('before_job', claim)
        log.info("job_started", extra={"job_id": claim["id"], "kind": claim["kind"], "attempt": claim["attempt"]})
        stopped, finished = threading.Event(), threading.Event()
        progress = dict(claim.get('progress') or {"stage": "checking", "elapsed_seconds": 0})
        deferred_generation = claim['kind'] == 'generation' and claim['input'].get('_deferred_generation')
        try:
            if prepare_deferred_generation(engine, claim, settings) and not deferred_generation:
                deferred_generation = True
                progress = dict(claim['progress'])
        except Exception as exc:
            error = getattr(exc, 'code', type(exc).__name__)
            if deferred_generation:
                from apps.api.errors import PROBLEM_DETAIL_LIMIT, Problem, problem_payload
                if isinstance(exc, Problem) and exc.details is not None:
                    error_detail = problem_payload(exc, cap=PROBLEM_DETAIL_LIMIT)
            log.warning("generation_preparation_failed", extra={"job_id": claim["id"],
                "error_code": error, "exception_type": type(exc).__name__})
        preparation_elapsed = max(0, time.monotonic() - started)
        if deferred_generation and error:
            progress = {**progress, 'elapsed_seconds': preparation_elapsed,
                        'phase_timings': {**(progress.get('phase_timings') or {}),
                                          'request_preparation_seconds': preparation_elapsed}}
        elif deferred_generation:
            # SolverControl starts its own clock after this worker-side
            # preparation.  Retain the request time in the public progress
            # clock and phase evidence before the first solver callback.
            progress = _preserve_generation_stage(progress, {**progress, 'stage': 'checking',
                        'elapsed_seconds': max(progress.get('elapsed_seconds', 0), preparation_elapsed),
                        'phase_timings': {**(progress.get('phase_timings') or {}),
                                          'request_preparation_seconds': preparation_elapsed}})
        progress_reported_at = time.monotonic()
        progress_lock = threading.Lock()
        sequence_offset = max((event['seq'] for event in progress.get('events', [])), default=0)
        improvement_offset = progress.get('improvements_found', 0)
        needs_resync = bool(sequence_offset)
        control_lock = threading.Lock()
        search_controls = dict(claim.get('search_control') or {})
        checkpoint_lock = threading.Lock()
        checkpoint_write_lock = threading.Lock()
        pending_checkpoint = None
        encoded_preparation = None
        prepared_key = (claim['resume_state'].get('key') if claim.get('resume_state') is not None
                        and claim.get('resume_checkpoint') else object())
        # Start before the successful write: its database timestamp can precede commit.
        lease_settings = engine.get_execution_options().get("rota_write_settings") or settings
        heartbeat_seconds = GENERATION_HEARTBEAT_SECONDS if claim['kind'] == 'generation' else JOB_HEARTBEAT_SECONDS
        lease_margin = heartbeat_seconds + lease_settings.db_pool_timeout_seconds
        lease_deadline = claim["lease_started"] + JOB_LEASE_SECONDS - lease_margin
        def report(update):
            nonlocal progress, needs_resync, progress_reported_at
            with progress_lock:
                progress_reported_at = time.monotonic()
                if claim['kind'] == 'generation':
                    update = _preserve_generation_stage(progress, update)
                    if deferred_generation:
                        update = {**update, 'phase_timings': {**(update.get('phase_timings') or {}),
                                  'request_preparation_seconds': preparation_elapsed}}
                    events = [{**event, 'seq': sequence_offset + event['seq']} for event in update.get('events', [])]
                    if events and needs_resync:
                        events[0] = {**events[0], 'adds': [], 'removes': [], 'truncated': True}
                        needs_resync = False
                    solution = update.get('solution')
                    solution = ({**solution, 'seq': sequence_offset + solution['seq']} if solution is not None
                                else progress.get('solution'))
                    best_score = update.get('best_score', progress.get('best_score'))
                    if best_score is not None:
                        best_score = AutoscheduleBestScore.model_validate(best_score).model_dump(mode='json')
                    elapsed = max(time.monotonic() - started,
                                  preparation_elapsed + update.get('elapsed_seconds', 0))
                    progress = {**update, 'elapsed_seconds': elapsed,
                                'events': (progress.get('events', []) + events)[-50:],
                                'solution': solution,
                                'best_score': best_score,
                                'precheck_issues': update.get('precheck_issues', progress.get('precheck_issues', [])),
                                'improvements_found': max(progress.get('improvements_found', 0),
                                    improvement_offset + update.get('improvements_found', 0))}
                else:
                    progress = dict(update)
        def persist_checkpoint():
            nonlocal pending_checkpoint
            with checkpoint_write_lock:
                with checkpoint_lock:
                    snapshot = (pending_checkpoint, encoded_preparation) if pending_checkpoint is not None else None
                if snapshot is None:
                    return
                state, prepared = snapshot
                try:
                    if retry_write(engine, lambda db: checkpoint_job(db, claim, state, prepared=prepared), deadline=lease_deadline):
                        with checkpoint_lock:
                            if pending_checkpoint is state:
                                pending_checkpoint = None
                    else:
                        stopped.set()
                except DatabaseWriteTimeout:
                    log.warning('job_checkpoint_write_timeout', extra={'job_id': claim['id'], 'kind': claim['kind']})
        def checkpoint(state):
            nonlocal pending_checkpoint, encoded_preparation, prepared_key
            # Serialise callbacks and heartbeat retries so an older snapshot cannot overwrite the newest hint.
            with checkpoint_lock:
                if state.get('key') != prepared_key:
                    from packages.domain.solver_resume import compress_state
                    encoded_preparation = compress_state({key: value for key, value in state.items()
                                                          if key not in RESUME_CHECKPOINT_FIELDS})
                    prepared_key = state.get('key')
                # A blocked write retains only small mutable state, never the uncompressed preparation.
                pending_checkpoint = {key: copy.deepcopy(value) for key, value in state.items()
                                      if key in RESUME_CHECKPOINT_FIELDS or key == 'key'}
                # Incumbents are coalesced into the existing lease heartbeat; preparation and diagnostic resets
                # still persist immediately so a worker can resume before its first solution.
            if not state.get('result'):
                persist_checkpoint()
        def checkpoint_snapshot():
            with checkpoint_lock:
                return ((pending_checkpoint, encoded_preparation) if pending_checkpoint is not None else None)
        def clear_checkpoint(state):
            nonlocal pending_checkpoint
            with checkpoint_lock:
                if pending_checkpoint is state:
                    pending_checkpoint = None
        def search_control():
            with control_lock:
                return dict(search_controls)
        def renew(db, snapshot):
            with progress_lock:
                current_progress = elapsed_progress(progress, time.monotonic() - progress_reported_at)
            job = lease_job(db, claim['id'])
            alive = heartbeat_job(db, claim, current_progress)
            if alive and snapshot is not None:
                state, prepared = snapshot
                alive = checkpoint_job(db, claim, state, prepared=prepared)
            return time.monotonic(), alive, dict(job.search_control or {}) if alive else {}
        def maintain_lease():
            nonlocal lease_deadline
            while not finished.wait(min(heartbeat_seconds, max(0, lease_deadline - time.monotonic()))):
                if time.monotonic() >= lease_deadline:
                    stopped.set()
                    return
                try:
                    heartbeat(engine, deadline=lease_deadline)
                    with checkpoint_write_lock:
                        snapshot = checkpoint_snapshot()
                        renewed_at, alive, controls = retry_write(
                            engine.execution_options(rota_defer_busy_writer=True),
                            lambda db: renew(db, snapshot), deadline=lease_deadline)
                        if alive and snapshot is not None:
                            clear_checkpoint(snapshot[0])
                    if alive:
                        lease_deadline = renewed_at + JOB_LEASE_SECONDS - lease_margin
                        with control_lock:
                            search_controls.clear()
                            search_controls.update(controls)
                    else:
                        stopped.set()
                        return
                except DatabaseWriteDeferred:
                    continue  # Keep liveness independent of the writer without extending an unrenewed lease.
                except DatabaseWriteTimeout:
                    log.warning("job_heartbeat_write_timeout", extra={"job_id": claim["id"], "kind": claim["kind"]})
                except Exception as exc:
                    log.warning("job_heartbeat_failed", extra={"job_id": claim["id"], "kind": claim["kind"],
                                                               "error_code": type(exc).__name__})
        monitor = threading.Thread(target=maintain_lease, daemon=True)
        monitor.start()
        solver_started = solver_finished = None
        try:
            if error is not None:
                pass  # A preparation refusal must reach the job receipt without running the solver.
            elif claim["kind"] == "generation":
                if claim['input'].get('generation_scope') is not None:
                    identity = proof_identity.identity()
                    if identity is None or identity['source_digest'] != claim['input'].get('generation_scope_source'):
                        raise ValueError('Scheduler source changed; restart matching API and worker and request a fresh job')
                if claim['input'].get('generation_engine', 'legacy') not in {'legacy', 'cp_sat'}:
                    raise ValueError('Unsupported scheduling engine')
                if claim['input'].get('generation_engine') == 'cp_sat':
                    from packages.domain.solver_rules import COMPILER_VERSION
                    if claim['input'].get('compiler_version') != COMPILER_VERSION:
                        raise ValueError('Scheduler version changed; request a fresh job')
                native_cp_sat = scheduler is None and claim['input'].get('generation_engine') == 'cp_sat'
                candidate_evidence = None
                if native_cp_sat:
                    from packages.domain.cp_sat import generate as scheduler
                    from .generation_preparation import candidate_evidence_for_followup
                    candidate_evidence = candidate_evidence_for_followup(engine, claim)
                    if claim.get('resume_state') is not None:
                        claim['resume_state'] = {**claim['resume_state'], 'checkpoint': claim.get('resume_checkpoint') or {}}
                run_options = claim["input"].get('generation_options') or {}
                diagnostic = {"diagnostic_proposal": True} if claim["input"].get("diagnostic_proposal") else {}
                diagnostic_continuation = (run_options.get('on_conflict', 'report') == 'continue_flagged'
                                           or bool(claim["input"].get("diagnostic_proposal")))
                solver_started = time.monotonic()
                result = (scheduler(claim["input"], progress=report, cancelled=stopped.is_set,
                                    **({'workers': solver_workers, 'resume_state': claim.get('resume_state'),
                                        'checkpoint': checkpoint, 'search_control': search_control,
                                        '_diagnostic_continuation': diagnostic_continuation,
                                        '_candidate_evidence': candidate_evidence} if native_cp_sat else {}), **diagnostic)
                          if scheduler else generate(claim["input"], cancelled=stopped.is_set))
                solver_finished = time.monotonic()
                if deferred_generation:
                    result['phase_timings'] = {**(result.get('phase_timings') or {}),
                                               'request_preparation_seconds': preparation_elapsed}
                continuation = claim['input'].get('continuation')
                if isinstance(result, dict) and isinstance(continuation, dict):
                    previous_seconds = continuation.get('previous_search_seconds', 0)
                    current_seconds = result.get('search_elapsed_seconds', 0)
                    if not isinstance(previous_seconds, (int, float)) or previous_seconds < 0:
                        previous_seconds = 0
                    if not isinstance(current_seconds, (int, float)) or current_seconds < 0:
                        current_seconds = 0
                    result['continuation'] = {
                        'from_job_id': continuation.get('from_job_id'),
                        'previous_search_seconds': previous_seconds,
                        'search_seconds': current_seconds,
                        'total_search_seconds': previous_seconds + current_seconds,
                    }
                if isinstance(result.get('validation'), dict) and {'findings', 'coverage'} <= result['validation'].keys():
                    presentation_error = result.pop('_fix_list_presentation_error', None)
                    if validated_proposal(result, claim.get('input_fingerprint')):
                        persist_generation_candidate(engine, claim, result, deadline=lease_deadline)
                    from packages.domain.rota_summary import generation_fix_lists, generation_fix_lists_fallback
                    try:
                        projection = generation_fix_lists(claim['input'], result,
                            stopped=lambda: bool((search_control() or {}).get('stop')))
                        presentation_error = (projection.pop('_fix_list_presentation_error', None)
                                              or presentation_error)
                        if presentation_error:
                            log.warning('generation_fix_list_presentation_failed', extra={
                                'job_id': claim['id'], **presentation_error})
                        result.update(projection)
                    except Exception as exc:
                        origin = exc.__traceback__
                        while origin is not None and origin.tb_next is not None:
                            origin = origin.tb_next
                        log.warning('generation_fix_list_presentation_failed', extra={
                            'job_id': claim['id'], 'error_code': type(exc).__name__,
                            'error_location': {'module': origin.tb_frame.f_globals.get('__name__'),
                                               'function': origin.tb_frame.f_code.co_name,
                                               'line': origin.tb_lineno} if origin is not None else None})
                        result.update(generation_fix_lists_fallback(claim['input'], result))
            elif claim['kind'] == 'feasibility':
                from packages.domain.feasibility import check
                result = check(claim['input'], progress=report, cancelled=stopped.is_set)
            elif claim['kind'] == 'planner_preview':
                from apps.api.services import planner_tools
                from apps.api.errors import require
                from packages.domain.solver_control import Cancelled
                def check_cancelled():
                    if stopped.is_set():
                        raise Cancelled()
                check_cancelled()
                with transaction(engine) as db:
                    actor = planner_actor_for(db, claim)
                    workspace = db.get(m.Workspace, claim['input']['workspace']['id'])
                    command = s.Command.model_validate(claim['input']['planner_command'])
                    require(workspace is not None and workspace.status == 'draft' and workspace.archived_at is None,
                            'STALE_INPUTS', 'The rota changed before its impact preview started', 409)
                    require(planner_tools.source_fingerprint(db, actor, workspace, command)
                            == claim['input']['planner_source_fingerprint'],
                            'STALE_PREVIEW', 'The rota or scheduling inputs changed; preview again', 409)
                    command = command.model_copy(update={'expected_fingerprint': None, 'preview_job_id': None})
                    result = planner_tools.preview(db, actor, command)
                    result = {**result, 'preview_job_id': claim['id'],
                              'source_fingerprint': claim['input']['planner_source_fingerprint'],
                              'source_revision': claim['input']['planner_expected_revision']}
                    check_cancelled()
            elif claim['kind'] == 'fill_options':
                from apps.api.services import staffing
                from apps.api.errors import require
                from packages.domain.solver_control import Cancelled
                value = s.FillOptionsInput.model_validate(claim['input']['fill_options'])

                def check_cancelled():
                    if stopped.is_set():
                        raise Cancelled()

                reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                          if engine.dialect.name == 'postgresql' else engine)
                with transaction(reader) as db:
                    allow_long_idle(db, 30)
                    actor = planner_actor_for(db, claim)
                    workspace = db.get(m.Workspace, value.workspace_id)
                    require(workspace is not None and workspace.status == 'draft' and workspace.archived_at is None,
                            'STALE_INPUTS', 'The rota changed before staffing options were checked', 409)
                    require(workspace.revision == value.expected_revision,
                            'STALE_INPUTS', 'The rota changed before staffing options were checked', 409)
                    current = staffing.fill_options_job_scope(db, actor, value)[1]
                    require(current == claim['input']['fingerprint'],
                            'STALE_INPUTS', 'The rota or scheduling inputs changed before staffing options were checked', 409)
                    from apps.api.services.leave_disclosure import disclose_leave
                    def report_page(page):
                        checked, total = page.pop('evaluated_count'), page.pop('total')
                        report({'stage': 'checking_candidates', 'checked': checked,
                                'total': total, 'result': disclose_leave(db, actor, page)})
                    result = disclose_leave(db, actor, staffing.fill_options(
                        db, actor, value, progress=report_page, cancelled=stopped.is_set))
                    require(result['source_fingerprint'] == claim['input']['fingerprint'],
                            'STALE_INPUTS', 'The rota or scheduling inputs changed before staffing options were checked', 409)
                check_cancelled()
                result = {**result, 'provisional_page': False}
            elif claim['kind'] == 'catalogue_proof':
                from .catalogue_proof import run
                with transaction(engine) as db:
                    allow_long_idle(db, 30)
                    result = run(db, claim, progress=report, cancelled=stopped.is_set)
            elif claim["kind"] == "swap_audience":
                from apps.api.services.swaps import compute_audience
                with transaction(engine) as db:
                    result = compute_audience(db, claim["input"])
            elif claim['kind'] in {'merge_preview', 'merge_apply'}:
                from .workspace_merge import run
                result = run(engine, claim, progress=report, cancelled=stopped.is_set)
            else:
                from apps.api.services.planning import revalidate
                from packages.domain.solver_control import Cancelled
                from apps.api.errors import require
                def check_cancelled():
                    if stopped.is_set():
                        raise Cancelled()
                reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
                          if engine.dialect.name == 'postgresql' else engine)
                with transaction(reader) as db:
                    allow_long_idle(db, 30)
                    if engine.dialect.name == 'sqlite':
                        db.execute(text('BEGIN'))
                    workspace = db.get(m.Workspace, claim['input']['workspace']['id'])
                    if claim['input'].get('validation_basis') == 'current':
                        require(workspace.content_revision == claim['input']['workspace']['content_revision']
                                and workspace.status == 'draft' and workspace.archived_at is None,
                                'STALE_INPUTS', 'The rota changed before its check', 409)
                    else:
                        require(fingerprint(db, workspace) == claim['input']['fingerprint'],
                                'STALE_INPUTS', 'The check inputs changed', 409)
                    db.info['_check_budget'] = check_cancelled
                    # Fix-list receipts depend on the evidence and acknowledgements seen by this check.
                    claim['previous_check'] = (workspace.validation, workspace.accepted_issues)
                    since = m.now()
                    read = []
                    result = revalidate(db, workspace, store=False, include_internal=True, data_out=read)
                    check_cancelled()
                    # Read in this same snapshot, so the finish writer can skip rebuilding unchanged inputs.
                    from apps.api.snapshots import preparation_stamp
                    selection = (read[0].get('input_manifest') or {}).get('selection') if read else None
                    read.clear()  # Keep only the window, not the whole snapshot, through the finish.
                    claim['check_stamp'] = (since, preparation_stamp(db, workspace),
                                            selection and (selection['start'], selection['end']))
        except Exception as exc:
            error = error or (getattr(exc, 'code', type(exc).__name__)
                              if claim['kind'] in {'catalogue_proof', 'validation', 'planner_preview', 'fill_options', 'merge_preview', 'merge_apply'}
                              else type(exc).__name__)
            # Code metadata only: messages, locals and formatted tracebacks can carry rota data.
            origin = exc.__traceback__
            while origin.tb_next is not None:
                origin = origin.tb_next
            log.warning("job_failed", extra={"job_id": claim["id"], "kind": claim["kind"], "error_code": error,
                "exception_type": type(exc).__name__, "error_location": {
                    "module": origin.tb_frame.f_globals.get('__name__'),
                    "function": origin.tb_frame.f_code.co_name, "line": origin.tb_lineno}})
        finally:
            finished.set()
            monitor.join()
        computed = time.monotonic()
        generation_timings = {}
        if claim['kind'] == 'generation':
            solve_start = solver_started if solver_started is not None else computed
            solve_end = solver_finished if solver_finished is not None else computed
            generation_timings = {
                'request_preparation_seconds': preparation_elapsed,
                'worker_setup_seconds': max(0, solve_start - started - preparation_elapsed),
                'solver_wall_seconds': solve_end - solve_start,
                'result_preparation_seconds': computed - solve_end,
            }
        log.info("job_computation_finished", extra={"job_id": claim["id"], "kind": claim["kind"],
            "attempt": claim["attempt"], "duration_ms": round((computed - started) * 1000, 2),
            **generation_timings})
        feasibility_projection = None
        staged_result = False

        def finish(db):
            if feasibility_projection is not None:
                return finish_job(db, claim, None, error, progress=progress,
                                  feasibility_projection=feasibility_projection)
            if pending_checkpoint is not None:
                checkpoint_job(db, claim, pending_checkpoint, prepared=encoded_preparation)
            if claim['kind'] in {'merge_preview', 'merge_apply'} and not error:
                from apps.api.errors import Problem
                try:
                    with db.begin_nested():
                        return finish_job(db, claim, result, None, progress=progress)
                except Problem as rejected:
                    # Roll back the entire merge before committing its failed-job receipt.
                    return finish_job(db, claim, None, rejected.code, progress=progress)
            return finish_job(db, claim, result, error, progress=progress, error_detail=error_detail)
        try:
            if claim['kind'] == 'feasibility' and result is not None and not error:
                from apps.api.serialization import job_read_metadata, job_result_projection
                feasibility_projection = (job_read_metadata(claim['input'], result),
                                          job_result_projection('feasibility', result))
            delay = .1
            while time.monotonic() < lease_deadline:
                try:
                    if feasibility_projection is not None and not staged_result:
                        retry_write(engine, lambda db: stage_feasibility_result(db, claim, result),
                                    deadline=lease_deadline, serialize=False,
                                    context={"job_id": claim["id"], "kind": claim["kind"], "job_phase": "stage_result"})
                        staged_result = True
                    if not retry_write(engine, finish, deadline=lease_deadline,
                                       context={"job_id": claim["id"], "kind": claim["kind"], "job_phase": "finish"}):
                        return True  # Cancellation or a newer attempt owns the job now.
                    break
                except DatabaseWriteTimeout:
                    remaining = lease_deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(delay, remaining))
                        delay = min(delay * 2, JOB_HEARTBEAT_SECONDS)
            else:
                log.warning("job_result_write_timeout", extra={"job_id": claim["id"], "kind": claim["kind"]})
                return True  # Leave lease recovery to claim_job; do not overwrite another attempt.
            completed = time.monotonic()
            log.log(logging.WARNING if error else logging.INFO, "job_finished",
                    extra={"job_id": claim["id"], "kind": claim["kind"], "status": "failed" if error else "succeeded",
                           "error_code": error, "duration_ms": round((completed - started) * 1000, 2),
                           **({'result_persistence_seconds': completed - computed} if generation_timings else {})})
        finally:
            # Release both Python graphs and native CP-SAT arenas on success, failure, cancellation, or write timeout.
            probe_claim = {'id': claim.get('id')}
            result = None
            progress = {}
            pending_checkpoint = None
            encoded_preparation = None
            candidate_evidence = None
            feasibility_projection = None
            # Resume preparation can retain the same solver graph as the input.
            # All closures now see only the probe identity after durable finish.
            claim = probe_claim
            _release_job_memory(probe_claim)
    if claim_first:
        # Maintenance cannot consume the newly claimed job's lease before its heartbeat starts.
        departments |= housekeeping(pass_engine, settings)
    if not claim:
        from apps.api.services.read_projection_warming import warm_one
        warm_one(engine, settings=settings)
    heartbeat(engine)
    from . import email_delivery
    from packages.contracts.schemas import EmailSettings
    started, dispatched, email_departments = time.monotonic(), 0, set()
    with transaction(engine) as db:
        pending = list(db.execute(select(m.Department.id, m.Department.email_settings).where(
            select(m.Outbox.id).where(m.Outbox.department_id == m.Department.id, m.Outbox.kind == 'email',
                m.Outbox.state.in_(['queued', 'retry']), m.Outbox.next_attempt_at <= m.now()).exists())))
    for department_id, values in pending:
        if dispatched >= settings.worker_email_max_per_pass or time.monotonic() - started >= settings.worker_email_budget_seconds:
            break
        for _ in range(min(EmailSettings.model_validate(values).batch_size, settings.worker_email_max_per_pass - dispatched)):
            if time.monotonic() - started >= settings.worker_email_budget_seconds:
                break
            # External I/O locks only this recipient/sender; another job is considered before the next bounded pass.
            with transaction(engine) as db:
                changed = email_delivery.dispatch(db, settings, limit=1, department_id=department_id)
            dispatched += 1
            email_departments |= changed
            heartbeat(engine)
            if not changed:
                break
    departments |= email_departments
    # Projection warming is maintenance, not a claimed job. Callers use this
    # result to assert that cancellation left no runnable work.
    return bool(departments or claim)


def process_push_one(engine, settings, *, record_worker_heartbeat=True):
    from .push_delivery import dispatch_pending as push_pending
    departments = dispatch_pending(engine)
    departments |= push_pending(engine, settings)
    heartbeat(engine, record_worker=record_worker_heartbeat)
    return bool(departments)


def _wake_job_waiter(notification, wake, *, wake_scope=live.WORKER_JOB_WAKE_SCOPE):
    """Wake the owning loop for one committed scope; polling remains the fallback."""
    if wake is None:
        return
    if wake_scope is None:
        wake.set()
        return
    try:
        payload = json.loads(notification.payload)
    except (AttributeError, TypeError, ValueError):
        return
    if payload.get('scope') == wake_scope:
        wake.set()


def notification_loop(engine, settings, stopped, wake=None, *, dispatch=True):
    """Dispatch inbox and push outboxes independently of the long-job loop.

    Commands already publish the transaction-bound ``rota_changes`` notification.  Listening to that channel
    removes idle polling latency; the bounded timeout still recovers direct/out-of-band inserts and a missed wake-up.
    Claims remain in the existing SKIP LOCKED/lease paths, so multiple workers are safe.
    ``dispatch=False`` makes this a listener-only wake bridge for push-only workers; every committed notification
    wakes their main loop, which owns dispatch, keep-alive refreshes and the bounded polling fallback.
    """
    listener = None
    if getattr(getattr(engine, 'dialect', None), 'name', None) == 'postgresql':
        try:
            import psycopg
            from sqlalchemy.engine import make_url
            conninfo = make_url(settings.database_url).set(drivername='postgresql').render_as_string(hide_password=False)
            listener = psycopg.connect(conninfo, autocommit=True)
            listener.execute(f"LISTEN {live.CHANNEL}")
        except Exception as exc:
            log.warning('notification_listener_unavailable', extra={'error_code': type(exc).__name__})
            if listener is not None:
                listener.close()
                listener = None
    try:
        while not stopped.is_set():
            if dispatch:
                try:
                    worked = process_push_one(engine, settings, record_worker_heartbeat=True)
                except (DatabaseWriteDeferred, DatabaseWriteTimeout, SQLAlchemyError) as exc:
                    log.warning('notification_dispatch_deferred', extra={'error_code': type(exc).__name__})
                    worked = False
                except Exception as exc:
                    log.warning('notification_dispatch_failed', extra={'error_code': type(exc).__name__})
                    worked = False
                if worked:
                    continue
            timeout = settings.worker_notification_poll_seconds
            if listener is None:
                stopped.wait(timeout)
                continue
            try:
                notification = next(listener.notifies(timeout=timeout), None)
                if notification is not None:
                    _wake_job_waiter(notification, wake,
                                     wake_scope=live.WORKER_JOB_WAKE_SCOPE if dispatch else None)
            except Exception as exc:
                log.warning('notification_listener_reconnect', extra={'error_code': type(exc).__name__})
                listener.close()
                listener = None
    finally:
        if listener is not None:
            listener.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--push-only", action="store_true", help="Run inbox fan-out and leased push delivery without solver work")
    args = parser.parse_args()
    configure_logging()
    from apps.api.services.score_check_isolation import protect_parent
    protect_parent()
    settings = Settings.from_env()
    settings.check(require_authentication=False)
    if JOB_LEASE_SECONDS - JOB_HEARTBEAT_SECONDS - settings.db_pool_timeout_seconds <= 0:
        raise RuntimeError(
            "ROTA_DB_POOL_TIMEOUT_SECONDS must be less than JOB_LEASE_SECONDS - JOB_HEARTBEAT_SECONDS "
            f"({JOB_LEASE_SECONDS - JOB_HEARTBEAT_SECONDS} seconds) to leave positive worker lease time")
    settings.warn_default_generation_engine()
    solver_workers = int(os.getenv('ROTA_SOLVER_WORKERS', '1'))
    if not 1 <= solver_workers <= 8:
        raise RuntimeError('ROTA_SOLVER_WORKERS must be between 1 and 8')
    if not WORKER_ID.strip() or len(WORKER_ID) > 120:
        raise RuntimeError('ROTA_WORKER_ID must be a nonempty instance identifier of at most 120 characters')
    engine = make_engine(settings.database_url, settings=settings)
    if engine is not None and hasattr(engine, 'execution_options'):
        engine = engine.execution_options(rota_defer_busy_writer=True, rota_write_wait_callback=_touch_alive_file)
    from .score_check import run_loop
    stopped = threading.Event()
    extraction = threading.Thread(target=run_loop, args=(engine, settings, stopped),
                                  name='score-check-worker', daemon=True)
    notification_stopped = threading.Event()
    job_wake = threading.Event()
    notification_thread = None
    if (not args.once and getattr(getattr(engine, 'dialect', None), 'name', None) == 'postgresql'):
        notification_thread = threading.Thread(target=notification_loop,
                                                args=(engine, settings, notification_stopped, job_wake),
                                                kwargs={'dispatch': not args.push_only},
                                                name='notification-dispatcher', daemon=True)
        notification_thread.start()
    if not args.once and not args.push_only:
        extraction.start()
    try:
        recover_owned = True
        delay = settings.worker_poll_seconds
        while True:
            _touch_alive_file()
            worked = False
            try:
                if args.push_only:
                    worked = process_push_one(engine, settings, record_worker_heartbeat=False)
                else:
                    worked = process_one(engine, solver_workers=solver_workers, settings=settings, recover_owned=recover_owned)
                recover_owned = False
            except DatabaseWriteDeferred:
                queued = {'job_id': None, 'queued_seconds': 0}
                if not args.push_only:
                    try:
                        queued = oldest_queued_job(engine)
                    except SQLAlchemyError as exc:
                        if not is_transient_database_error(exc):
                            raise
                        queued['queued_seconds'] = None
                log.info('worker_pass_deferred', extra=queued)
            except DatabaseWriteTimeout:
                log.warning('worker_pass_write_timeout', extra={'error_code': 'DatabaseWriteTimeout'})
                if args.once:
                    raise
            except SQLAlchemyError as exc:
                # PostgreSQL can briefly reject every connection while it recovers. Keep the worker alive so
                # the lease/recovery path can resume jobs instead of creating a restart storm.
                if not is_transient_database_error(exc):
                    raise
                log.warning('worker_pass_database_error', extra={
                    'error_code': getattr(getattr(exc, 'orig', None), 'sqlstate', None) or type(exc).__name__})
                if args.once:
                    raise
            if args.once:
                break
            # PostgreSQL NOTIFY wakes this event in the notification thread;
            # the fixed poll is only the missed-notification fallback.
            if notification_thread is not None and not args.push_only:
                if not worked:
                    job_wake.wait(settings.worker_poll_seconds)
                    job_wake.clear()
            else:
                if worked:
                    delay = settings.worker_poll_seconds
                if notification_thread is not None:
                    job_wake.wait(delay)
                    job_wake.clear()
                else:
                    time.sleep(delay)
                delay = settings.worker_poll_seconds if worked else min(delay * 2, settings.worker_idle_max_seconds)
    finally:
        notification_stopped.set()
        if notification_thread is not None:
            notification_thread.join(timeout=2)
        stopped.set()
        if extraction.ident is not None:
            extraction.join(timeout=1)


if __name__ == "__main__":
    main()
