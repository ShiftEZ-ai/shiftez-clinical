"""Leavers: remove a former staff member's personal details once they have left (command person.anonymise, admins).

Scrubs what identifies or describes the person: the Directory fields (name, title, work email, phones, staff number, section,
short code, tags, colour), copied email delivery addresses, private reasons and notes on their leave, requests, patterns
and wishes, their calendar feeds, and their sign-in (display name, e-mail, username freed, sign-in service entry removed,
sessions ended).
Kept on purpose: the person record itself (rotas, fairness and history still point at it, now as "Former staff member
<id>"), and immutable evidence by design: publications, the audit log and definition history keep what they recorded
at the time, except accidentally retained private leave reasons are removed from availability history. Unapplied
staged payloads involving the person are cleared. The change is audited; old values are not copied anywhere.
"""
from sqlalchemy import func, inspect, select
from packages.contracts import schemas as s
from packages.domain.time import aware
from apps.api import models as m
from apps.api.auth import person_access, require_role, revoke_session, session_event
from apps.api.errors import require
from apps.api.identity_store import store_for
from apps.api.serialization import record
from .accounts import written
from .access import keep_developer
from .common import bump_inputs, expected, materialise_defaults
from . import tags

ANONYMISED_FIELDS = ("given_name", "family_name", "title", "work_email", "work_phone", "work_extension", "staff_number", "pager", "directory_section", "short_code", "rota_name", "colour")
AVAILABILITY_HISTORY_KINDS = ("availability", "availability.update", "availability.undo_update", "availability.restore")


def _current_rows(db, model, query, matches):
    """A deferred bulk flush must not leave personal data in new or reassigned records."""
    with db.no_autoflush:
        rows = list(db.scalars(query))
    rows = dict.fromkeys([*rows, *(row for row in [*db.new, *db.dirty] if isinstance(row, model))])
    return [row for row in rows if row in db and row not in db.deleted and matches(row)]


def _person_rows(db, model, person_id):
    return _current_rows(db, model, select(model).where(model.person_id == person_id),
                         lambda row: row.person_id == person_id)


def _scrub_availability_history(db, person_id, availability_ids):
    rows = _current_rows(db, m.DefinitionHistory, select(m.DefinitionHistory).where(
        m.DefinitionHistory.object_id.in_(availability_ids), m.DefinitionHistory.kind.in_(AVAILABILITY_HISTORY_KINDS)),
        lambda row: row.object_id in availability_ids and row.kind in AVAILABILITY_HISTORY_KINDS)
    postgres = db.get_bind().dialect.name == "postgresql"
    count = 0
    for row in rows:
        if isinstance(row.snapshot, dict) and "private_reason" in row.snapshot and (not postgres or inspect(row).pending):
            row.snapshot = {key: value for key, value in row.snapshot.items() if key != "private_reason"}
            count += 1
    for kind, row, reason, snapshot in db.info.get("bulk", {}).get("history", []):
        if kind in AVAILABILITY_HISTORY_KINDS and getattr(row, "person_id", None) == person_id:
            count += int("private_reason" in snapshot)
            snapshot.pop("private_reason", None)
    if postgres:
        # Sanitize pending inserts first; the definer then sees current ownership without granting history UPDATE.
        db.flush()
        count += db.scalar(select(func.rota_redact_person_history(person_id)))
        for row in rows:
            db.expire(row, ["snapshot"])
    return count


def anonymise(db, actor, command, settings):
    require_role(actor, "admin")
    s.Decision.model_validate(command.payload)  # the reason goes to the audit log
    person = person_access(db, actor, command.target_id)
    expected(person, command.expected_revision)
    require(not person.active, "PERSON_ACTIVE", "Mark the person as left (inactive) first, then remove their details", 409)
    identities = _person_rows(db, m.Identity, person.id)
    keep_developer(db, {identity.id for identity in identities})
    affected = 1
    name = f"Former staff member {person.id[:8]}"
    person.display_name, person.tags, person.rota_aliases = name, [], []
    for row in tags.members(db, kind="people", member_id=person.id):
        tags.remove_member(db, actor, row, "Former staff member: details removed")
        affected += 1
    for field in ANONYMISED_FIELDS:
        setattr(person, field, "")
    person.revision += 1
    erased = db.info.setdefault("anonymised_records", {}) if db.info.get("bulk") else {}
    erased[person.id] = record(person)
    scrubbed = {}
    availability_ids = set()
    for model, column in ((m.Availability, "private_reason"), (m.RequestRecord, "private_reason"),
                          (m.AvailabilityPattern, "note"), (m.StaffWish, "note")):
        rows = _person_rows(db, model, person.id)
        if model is m.Availability:
            availability_ids = {row.id for row in rows}
        changed = [row for row in rows if getattr(row, column)]
        for row in changed:
            setattr(row, column, "")
        erased.update((row.id, record(row, exclude=("private_reason",))) for row in rows)
        scrubbed[model.__tablename__] = len(changed)
        affected += len(changed)
    scrubbed["availability_history"] = _scrub_availability_history(db, person.id, availability_ids)
    from .bulk_staged import scrub_for_person
    scrubbed["bulk_staged"] = scrub_for_person(db, person.id)
    affected += scrubbed["bulk_staged"]
    feeds = [row for row in _person_rows(db, m.CalendarFeed, person.id) if not row.revoked]
    for row in feeds:
        row.revoked = True
    scrubbed["calendar_feeds"] = len(feeds)
    affected += len(feeds)
    identity_ids = {identity.id for identity in identities}
    deliveries = _current_rows(db, m.EmailDelivery, select(m.EmailDelivery).where(
        (m.EmailDelivery.person_id == person.id) | m.EmailDelivery.identity_id.in_(identity_ids)),
        lambda row: bool(row.address) and (row.person_id == person.id or row.identity_id in identity_ids))
    for row in deliveries:
        materialise_defaults(row)
        row.address, row.revision = "", row.revision + 1
    scrubbed["email_deliveries"] = len(deliveries)
    affected += len(deliveries)
    users = store_for(settings)
    # Stage all sign-in removals before touching the file so rollback can preserve the final recovery admin.
    for identity in identities:
        identity.disabled = True
    from .auth_configuration import protect_break_glass, remove_identity_bindings
    protect_break_glass(db, settings)
    remove_identity_bindings(db, actor, identity_ids, command.payload["reason"])
    for identity in identities:
        materialise_defaults(identity)
        identity.display_name, identity.disabled, identity.revision = name, True, identity.revision + 1
        identity.subject = f"former:{identity.id}"
        erased[identity.id] = record(identity, exclude=("password_hash",))
        moment = m.now()
        sessions = _current_rows(db, m.Session, select(m.Session).where(m.Session.identity_id == identity.id),
            lambda row: row.identity_id == identity.id and not row.revoked and
                (row.expiry_unlimited or aware(row.expires_at) > moment))
        for session in sessions:
            materialise_defaults(session)
            revoke_session(session)
            session_event(db, session, person.id, "auth.session_revoke", actor_id=actor.identity_id)
        accounts = _current_rows(db, m.Account, select(m.Account).where(m.Account.identity_id == identity.id),
                                 lambda row: row.identity_id == identity.id)
        affected += 1 + len(sessions) + len(accounts)
        for account in accounts:
            materialise_defaults(account)
            old = account.username
            account.username, account.email, account.revision = f"former-{identity.id[:8]}", "", account.revision + 1
            if users is not None:
                written(lambda: users.remove(old))
    bump_inputs(db, actor)
    return {"record": record(person), "scrubbed": scrubbed, "affected_count": affected}
