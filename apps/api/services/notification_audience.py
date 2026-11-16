"""Set-based notification audience computation shared by API previews and the worker."""
from dataclasses import dataclass
import logging

from sqlalchemy import func, null, or_, select

from apps.api import models as m
from apps.api.auth import department_roles
from apps.api.settings import Settings
from packages.contracts.notification_audiences import audience_definition
from packages.contracts.schemas import PUBLICATION_AUDIENCE_REASON_LABELS, PublicationAudienceReason


# Payload source -> model, creator column, retained creation-history kind when needed.
OUTBOX_SOURCES = {
    "content_id": (m.DepartmentContent, None, "department_content"),
    "event_id": (m.Event, None, "events"),
    "event_occurrence_id": (m.EventOccurrence, None, None),
    "occurrence_id": (m.EventOccurrence, None, None),
    "request_id": (m.RequestRecord, "created_by", None),
    "workspace_id": (m.Workspace, "created_by", None),
    "publication_id": (m.Publication, "published_by", None),
    "supersedes_id": (m.Publication, "published_by", None),
    "policy_id": (m.Policy, "created_by", None),
    "pattern_id": (m.AvailabilityPattern, "created_by", None),
    "availability_id": (m.Availability, "recorded_by", None),
    "swap_id": (m.SwapRequest, "proposer_id", None),
    "feedback_id": (m.FeedbackReport, "reporter_identity_id", None),
    "feedback_report_id": (m.FeedbackReport, "reporter_identity_id", None),
    "staged_id": (m.BulkStaged, "owner_id", None),
    "credential_id": (m.Eligibility, None, "eligibility.create"),
    "fact_id": (m.SchedulingFact, "proposed_by", None),
    "conversion_id": (m.CatalogueConversion, "proposed_by", None),
}

NEVER_TEST_AUTHORED = {
    "actor_id": "The event actor is checked directly, rather than treated as a source record.",
    "person_id": "Audience person, not the author of the source.",
    "person_ids": "Audience people, not authors of the source.",
    "reporter_identity_id": "Feedback recipient; authorship comes from the feedback source record.",
    "target_id": "Polymorphic self-approval audit target; the explicit event actor authors this notice.",
    "source_outbox_id": "Transport child; parent audience and pre-send authorization enforce its source privacy.",
    "notification_id": "Transport child; the existing notification parent is reauthorized before sending.",
    "occurrence_ids": "Delivery subset; the owning event_id supplies the source author.",
}


@dataclass(frozen=True)
class PublicationAudience:
    reasons: dict[str, frozenset[PublicationAudienceReason]]

    @property
    def identities(self) -> set[str]:
        return set(self.reasons)


def is_test_identity(value):
    # shortcut: "(test)" name suffix; upgrade to an is_test flag (POST-668 item 24) when accounts carry it.
    suffix = "(test)"
    name = getattr(value, "display_name", None)
    if name is None or isinstance(name, str):
        return bool(name and name.endswith(suffix))
    return func.substr(name, func.length(name) - len(suffix) + 1) == suffix


def source_creator(cls, department_id, object_id=None):
    """Use the creator column or retained creation revision; occurrences inherit their event's author."""
    if cls is m.EventOccurrence:
        object_id = (cls.event_id if object_id is None else select(cls.event_id).where(
            cls.department_id == department_id, cls.id == object_id).scalar_subquery())
        cls = m.Event
    _, column, kind = next((spec for spec in OUTBOX_SOURCES.values() if spec[0] is cls), (cls, None, None))
    column = column or ("created_by" if hasattr(cls, "created_by") else None)
    creator = null() if column is None else getattr(cls, column)
    if object_id is not None and column is not None:
        creator = select(creator).where(cls.department_id == department_id, cls.id == object_id).scalar_subquery()
    object_id = cls.id if object_id is None else object_id
    from .common import CATALOGUES
    kind = kind or next((kind for kind, model in CATALOGUES.items() if model is cls), None)
    if kind is None:
        return creator
    H = m.DefinitionHistory
    history = select(H.actor_id).where(H.department_id == department_id, H.kind == kind,
        H.object_id == object_id, H.object_revision == 1).correlate_except(H).scalar_subquery()
    return func.coalesce(creator, history) if column is not None else history


def _test_source(creator):
    """Unknown provenance is test-only, just like a known test author."""
    return ~select(m.Identity.id).where(m.Identity.id == creator, ~is_test_identity(m.Identity)).exists()


def source_visibility(db, viewer, cls, object_id=None):
    """One SQL author predicate, shared by paged lists, calendar feeds and stored inbox items."""
    if not _settings(db).publish_test_people_only or is_test_identity(db.get(m.Identity, viewer.identity_id)):
        return True
    creator = source_creator(cls, viewer.department_id, object_id)
    visible = or_(creator == viewer.identity_id, ~_test_source(creator))
    return visible


def _publication_people(db, publication, *, person_changes=None):
    """Return rota and wish people for a stored or pre-publication snapshot."""
    if person_changes is not None:
        rota = set(person_changes)
    elif publication.id:
        rota = set(db.scalars(select(m.PublicationPerson.person_id).where(
            m.PublicationPerson.publication_id == publication.id)))
    else:
        rota = {participant['person_id'] for shift in publication.snapshot.get('shifts', [])
                for participant in shift.get('participants', []) if participant.get('person_id')}
    # The StaffWish query serves pre-P6 snapshots without embedded wishes.
    wishes = (set(db.scalars(select(m.StaffWish.person_id).where(
        m.StaffWish.publication_id == publication.id))) if publication.id else set())
    wishes |= {wish['person_id'] for wish in publication.snapshot.get('wishes', []) if wish.get('person_id')}
    return rota, wishes


def _settings(db, settings=None):
    return settings or db.get_bind().get_execution_options().get("rota_write_settings") or Settings()


def _test_only_targets(db, targets, *, cause_identity_id=None, identities=None, settings=None, test_source=False):
    targets = set(targets)
    settings = _settings(db, settings)
    if not settings.publish_test_people_only or not targets:
        return targets
    if not test_source:
        cause = ((identities or {}).get(cause_identity_id) or db.get(m.Identity, cause_identity_id)) if cause_identity_id else None
        if not is_test_identity(cause):
            return targets
    if identities is None:
        identities = {row.id: row for row in db.scalars(select(m.Identity).where(m.Identity.id.in_(targets)))}
    return {identity_id for identity_id in targets if is_test_identity(identities.get(identity_id))}


def _publication_ids(db, event):
    payload = event.payload or {}
    ids = set()

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "publication_id" and isinstance(child, str):
                    ids.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    workspace_id = payload.get("workspace_id")
    if workspace_id:
        workspace = db.scalar(select(m.Workspace).where(m.Workspace.id == workspace_id,
            m.Workspace.department_id == event.department_id))
        if workspace is not None:
            if workspace.base_publication_id:
                ids.add(workspace.base_publication_id)
            publication_id = db.scalar(select(m.Publication.id).where(
                m.Publication.workspace_id == workspace_id, m.Publication.department_id == event.department_id))
            if publication_id:
                ids.add(publication_id)
    return ids


def _test_cause(db, event):
    payload = event.payload or {}
    actor_id = payload.get("actor_id")
    cause = actor_id if actor_id and is_test_identity(db.get(m.Identity, actor_id)) else None
    sources = {(spec[0], payload[key]) for key, spec in OUTBOX_SOURCES.items() if payload.get(key)}
    sources.update((OUTBOX_SOURCES["publication_id"][0], object_id) for object_id in _publication_ids(db, event))
    unknown = 0
    for cls, object_id in sources:
        creator = source_creator(cls, event.department_id, object_id)
        creator_id, test_source = db.execute(select(creator, _test_source(creator))).one()
        unknown += creator_id is None
        if test_source and creator_id is not None:
            cause = cause or creator_id
    return cause, unknown


def _audit_suppression(db, event, actor_id, count, unknown_source_count=0):
    # Actorless legacy sources cannot invent the identity required by the audit table.
    if unknown_source_count:
        logged = db.info.setdefault("test_people_unknown_sources_logged", set())
        if event.id not in logged:
            logging.getLogger(__name__).info("Test-only notification recipients suppressed: count=%s unknown_source_count=%s",
                count, unknown_source_count)
            logged.add(event.id)
    if actor_id is None:
        return
    action = "notification.test_people_only_suppressed"
    exists = db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.department_id == event.department_id,
        m.AuditEvent.action == action, m.AuditEvent.target_id == event.id).limit(1))
    if exists:
        return
    db.add(m.AuditEvent(department_id=event.department_id, actor_id=actor_id, action=action,
        target_id=event.id, reason="Test-only notification recipients suppressed",
        detail={"kind": event.kind, "count": count}, correlation_id=event.id))


def filter_recipients(db, event, targets, identities=None, *, settings=None, audit=True):
    """Apply the deployment privacy gate to one outbox audience."""
    targets = set(targets)
    settings = _settings(db, settings)
    if not settings.publish_test_people_only or not targets:
        return targets
    cause, unknown = _test_cause(db, event)
    if cause is None and not unknown:
        return targets
    filtered = _test_only_targets(db, targets, identities=identities, settings=settings, test_source=True)
    suppressed = targets - filtered
    if audit and (suppressed or unknown):
        _audit_suppression(db, event, cause or (event.payload or {}).get("actor_id"), len(suppressed), unknown)
    return filtered


def filter_people(db, event, people, *, settings=None, audit=True):
    """Apply the same privacy gate to email-only recipients without identities."""
    people = {person.id: person for person in people}
    allowed = filter_recipients(db, event, people, people, settings=settings, audit=audit)
    return [person for person_id, person in people.items() if person_id in allowed]


def publication_audience(db, publication, roles, identities=None, *, exclude_identity_id=None, person_changes=None,
                         publisher_identity_id=None, event=None):
    """Compute the publication audience and each identity's matching reasons.

    Stored publications follow the worker's existing PublicationPerson and wish
    queries.  A pre-publication context uses the immutable candidate snapshot.
    """
    if identities is None:
        identities = {identity.id: identity for identity in db.scalars(select(m.Identity).where(
            m.Identity.id.in_(list(roles) or ['']), m.Identity.disabled.is_(False)))}
    rota_people, wish_people = _publication_people(db, publication, person_changes=person_changes)
    reasons = {}
    for identity_id, identity in identities.items():
        if identity_id == exclude_identity_id:
            continue
        held = roles.get(identity_id, ())
        current = set()
        if 'coordinator' in held:
            current.add('coordinator')
        if 'approver' in held:
            current.add('approver')
        if 'staff' in held and identity.person_id in rota_people:
            current.add('on_rota')
        if 'staff' in held and identity.person_id in wish_people:
            current.add('has_wish')
        if current:
            reasons[identity_id] = frozenset(current)
    cause = getattr(publication, "published_by", None) or publisher_identity_id
    filtered = (filter_recipients(db, event, reasons, identities) if event is not None else
                _test_only_targets(db, reasons, cause_identity_id=cause, identities=identities))
    return PublicationAudience({identity_id: reasons[identity_id] for identity_id in filtered})


def preview(db, actor, publication, *, person_changes=None):
    """Project a bounded, privacy-filtered in-app publication audience."""
    roles = department_roles(db, publication.department_id)
    audience = publication_audience(db, publication, roles, exclude_identity_id=actor.identity_id,
                                   person_changes=person_changes, publisher_identity_id=actor.identity_id)
    from .people import visible_identity_names
    names = visible_identity_names(db, actor, audience.reasons)
    reason_order = tuple(PUBLICATION_AUDIENCE_REASON_LABELS)

    rows = []
    for identity_id, reasons in audience.reasons.items():
        visible = identity_id in names
        rows.append({
            'identity_id': identity_id,
            'display_name': names.get(identity_id),
            'reasons': sorted(reasons, key=reason_order.index),
            'channels': ['in_app'],
            '_visible': visible,
        })
    rows.sort(key=lambda row: (not row['_visible'], row['display_name'] or '', row['identity_id']))
    visible_rows = [{key: value for key, value in row.items() if key != '_visible'} for row in rows if row['_visible']]
    groups = []
    for reason in reason_order:
        group_rows = [row for row in rows if reason in row['reasons']]
        visible_count = sum(row['_visible'] for row in group_rows)
        groups.append({'reason': reason, 'count': len(group_rows), 'visible_count': visible_count,
                       'hidden_count': len(group_rows) - visible_count})
    definition = audience_definition('publication')
    return {'workspace_id': publication.workspace_id, 'description': definition.description,
            'delivery': 'in_app', 'count': len(rows),
            'visible_count': len(visible_rows), 'hidden_count': len(rows) - len(visible_rows),
            'recipients': visible_rows, 'groups': groups,
           }


def non_test_assignee_count(db, workspace_id):
    people = set(db.scalars(select(m.Assignment.person_id).join(m.Shift, m.Shift.id == m.Assignment.shift_id)
        .where(m.Shift.workspace_id == workspace_id)))
    if not people:
        return 0
    rows = db.scalars(select(m.Person).where(m.Person.id.in_(people)))
    return sum(not is_test_identity(person) for person in rows)
