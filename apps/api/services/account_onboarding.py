"""Preview and reconcile a department's selected staff through the account invitation command."""
from collections import Counter, defaultdict

from sqlalchemy import and_, or_, select

from apps.api import models as m
from apps.api.auth import authenticate, require_role
from apps.api.commands import execute
from apps.api.db import transaction
from apps.api.errors import Problem, require
from apps.api.identity_store import pending_owner
from apps.api.snapshots import snapshot_hash
from packages.contracts import schemas as s
from . import accounts, auth_configuration
from .independence import today
from .people import active_people_on


def pending_for(department_id, row):
    return snapshot_hash({"department_id": department_id, "row": row.model_dump(mode="json")})


def plan(db, actor, rows, settings):
    require_role(actor, "admin")
    require(auth_configuration.current(db, settings).mode != "institutional_only", "PASSWORD_SIGN_IN_DISABLED",
            "Password sign-in is unavailable to staff", 409)
    users = accounts.written(accounts.store(settings).onboarding_users)
    persons = Counter(row.person_id for row in rows)
    usernames = Counter(row.username for row in rows)
    emails = Counter(row.email.casefold() for row in rows)
    day = today(db, actor.department_id)
    membership = select(m.Membership.id).where(
        m.Membership.department_id == actor.department_id, m.Membership.person_id == m.Person.id,
        m.Membership.withdrawn.is_(False), m.Membership.starts_on <= day,
        or_(m.Membership.ends_on.is_(None), m.Membership.ends_on > day)).exists()
    labels = dict(db.execute(select(m.Person.id, m.Person.display_name).where(
        m.Person.id.in_(persons), m.Person.organisation_id == actor.organisation_id,
        active_people_on(day), membership)).all())

    by_person, by_username, by_email = defaultdict(list), defaultdict(set), defaultdict(set)
    identities = db.execute(select(m.Identity, m.Account).outerjoin(m.Account, m.Account.identity_id == m.Identity.id)
        .where(or_(m.Identity.person_id.in_(persons),
                   and_(m.Identity.issuer == accounts.LOCAL_ISSUER, m.Identity.subject_normalized.in_(usernames)),
                   m.Account.username_normalized.in_(usernames), m.Account.email_normalized.in_(emails)))).all()
    for identity, account in identities:
        by_person[identity.person_id].append((identity, account))
        if identity.issuer == accounts.LOCAL_ISSUER:
            by_username[identity.subject.casefold()].add(identity.id)
        if account:
            by_username[account.username.casefold()].add(identity.id)
            by_email[account.email.casefold()].add(identity.id)

    bindings = defaultdict(list)
    for grant in db.scalars(select(m.RoleBinding).where(
            m.RoleBinding.identity_id.in_([identity.id for identity, _ in identities]))):
        bindings[grant.identity_id].append(grant)

    file_names, file_emails = defaultdict(set), defaultdict(set)
    for username, user in users.items():
        file_names[username.casefold()].add(username)
        file_emails[str(user.get("email") or "").casefold()].add(username)

    ledger = {}
    for row in rows:
        person_id, username, email = row.person_id, row.username, row.email.casefold()
        if person_id not in labels:
            ledger[person_id] = {"status": "conflict", "code": "PERSON_NOT_IN_DEPARTMENT"}
            continue
        if persons[person_id] > 1 or usernames[username] > 1 or emails[email] > 1:
            ledger[person_id] = {"status": "conflict", "code": "DUPLICATE_SELECTION"}
            continue
        linked = by_person[person_id]
        owners = by_username[username] | by_email[email]
        file_owners = file_names[username] | file_emails[email]
        user = users.get(username)
        if linked:
            identity, account = linked[0]
            grants = bindings[identity.id]
            if (len(linked) == 1 and not identity.disabled and account and account.username == username
                and account.email.casefold() == email and owners == {identity.id} and file_owners == {username}
                and user and str(user.get("email") or "").casefold() == email
                and (user.get("disabled") is True) == (account.accepted_at is None)
                and all(g.department_id == actor.department_id and g.role == "staff" for g in grants)
                and any(not g.revoked and g.starts_on <= day and (g.ends_on is None or g.ends_on > day) for g in grants)):
                ledger[person_id] = {"status": "already_linked", "identity_id": identity.id, "account_id": account.id}
            else:
                ledger[person_id] = {"status": "conflict", "code": "PERSON_IDENTITY_CONFLICT"}
        elif owners or (file_owners and not (
            file_owners == {username} and user and user.get("email") == row.email
            and user.get("displayname") == labels[person_id] and pending_owner(user, pending_for(actor.department_id, row))
        )):
            ledger[person_id] = {"status": "conflict", "code": "SIGN_IN_TAKEN"}
        else:
            ledger[person_id] = {"status": "ready"}
    return labels, ledger


def counts(ledger):
    totals = Counter(row["status"] for row in ledger.values())
    return {key: totals[key] for key in ("ready", "already_linked", "created", "conflict", "stopped")}


def fingerprint(actor, value, labels, ledger):
    # The same selection can move from ready to linked after a partial apply. A
    # change in which people have conflicts requires the admin to review it again.
    conflicts = {person_id: row["code"] for person_id, row in ledger.items() if row["status"] == "conflict"}
    return snapshot_hash({"actor_id": actor.identity_id, "department_id": actor.department_id,
                          "selection": value.model_dump(mode="json", exclude={"expected_fingerprint"}),
                          "labels": labels, "conflicts": conflicts})


def preview(db, actor, value, settings):
    labels, ledger = plan(db, actor, value.rows, settings)
    return {"expected_fingerprint": fingerprint(actor, value, labels, ledger), "people": labels,
            "ledger": ledger, "counts": counts(ledger)}


def apply(engine, settings, token, csrf, value, correlation_id):
    with transaction(engine) as db:
        actor = authenticate(db, token, csrf, write=True, settings=settings)
        labels, ledger = plan(db, actor, value.rows, settings)
    require(value.expected_fingerprint == fingerprint(actor, value, labels, ledger), "STALE_PREVIEW",
            "The selection or its account checks changed. Preview staff onboarding again.", 409,
            {"ledger": ledger, "counts": counts(ledger)})
    invitations = []
    if counts(ledger)["conflict"]:
        return {"ledger": ledger, "counts": counts(ledger), "invitations": invitations}

    for row in value.rows:
        person_id = row.person_id
        if ledger[person_id]["status"] != "ready":
            continue
        owner = pending_for(actor.department_id, row)

        def guard(db, current):
            require((current.identity_id, current.department_id) == (actor.identity_id, actor.department_id),
                    "ONBOARDING_SCOPE_CHANGED", "The signed-in department changed. Preview again.", 403)
            live_labels, live_ledger = plan(db, current, [row], settings)
            state = live_ledger[person_id]
            require(live_labels.get(person_id) == labels[person_id] and state["status"] != "conflict",
                    state.get("code", "STALE_PREVIEW"), "This person's onboarding eligibility changed. Preview again.", 409)
            db.info["staff_onboarding"] = owner
            return state if state["status"] == "already_linked" else None

        payload = s.AccountInvite(person_id=person_id, username=row.username, email=row.email,
                                  display_name=labels[person_id], roles=["staff"], reason=value.reason)
        command = s.Command(kind="account.invite", payload=payload.model_dump(mode="json"))
        key = "onboarding-" + snapshot_hash({"owner": owner, "selection": value.expected_fingerprint})
        try:
            result = execute(engine, settings, token, csrf, key, command, correlation_id, invite_guard=guard)
        except Problem as error:
            if error.status not in {409, 503}:
                raise
            ledger[person_id] = {"status": "conflict" if error.status == 409 else "stopped", "code": error.code}
            break
        if result.get("status") == "already_linked":
            ledger[person_id] = result
        elif link := result.get("one_time_link"):
            ledger[person_id] = {"status": "created", "identity_id": result["record"]["id"],
                                 "account_id": result["account"]["id"]}
            invitations.append({"person_id": person_id, "person_label": labels[person_id], "one_time_link": link})
        else:
            ledger[person_id] = {"status": "conflict", "code": "INVITATION_RECEIPT_STALE"}
            break
    return {"ledger": ledger, "counts": counts(ledger), "invitations": invitations}
