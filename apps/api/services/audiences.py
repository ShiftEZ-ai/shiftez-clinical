"""Who a message is for: everyone, roles, or people picked by rank, clinic, programme, tag or skill, plus named people.

Groups narrow each other (a rank *and* a tag means people with both); several values in one group widen it
(two ranks means either). Named people are always included. Roles match the roles people actually hold, so an
administrator (who may do everything) is not swept into every "staff" message.

`profile` gathers what one person holds; `matches` is the single rule, used both for one viewer (Overview) and for
every recipient of a delivery.
"""
from datetime import timedelta
import json
from sqlalchemy import String, cast, select
from apps.api import models as m
from apps.api.auth import department_roles, held_roles, role_bindings
from packages.domain import conditions
from packages.domain.time import local_midnight
from packages.domain.validation import index_person_rows
from apps.api.serialization import record
from apps.api.snapshots import tag_data

GROUPS = (("category_ids", m.Membership, "category_id"), ("service_ids", m.Eligibility, "service_id"),
          ("programme_ids", m.ProgrammeMembership, "programme_id"), ("capability_ids", m.CapabilityGrant, "capability_id"))


def evaluate(group, roles, person_id, holds):
    """A condition tree, through the one evaluator rules use too (packages/domain/conditions.py)."""
    def held(field):
        if field == "roles":
            return set(roles)
        if field == "person_ids":
            return {person_id} if person_id else set()
        return holds.get(field, set())
    context = holds.get("_condition_context")
    if context:
        conditions.validate_relative_conditions(group, context)
    return conditions.evaluate(group, held, context, row=holds)


def matches(audience, roles, person_id, holds, tags=None):
    """`holds` maps each group key (and "tags") to what this person has on the audience date; `tags` (snapshots.tag_data): a tag in a
    condition line reads as its members, as in rules (conditions.tag_scope)."""
    audience = audience or {"everyone": True}
    if audience.get("where") and not audience.get("everyone"):
        where = conditions.tag_scope({"tags": tags}, {"conditions": audience["where"]})["conditions"] if tags else audience["where"]
        return evaluate(where, roles, person_id, holds)
    if audience.get("everyone") or set(audience.get("roles") or []) & set(roles):
        return True
    if not person_id:
        return False
    if person_id in (audience.get("person_ids") or []):
        return True
    asked = [(key, set(audience.get(key) or [])) for key, _, _ in GROUPS] + [("tags", {t.lower() for t in audience.get("tags") or []})]
    asked = [(key, values) for key, values in asked if values]
    return bool(asked) and all(values & holds.get(key, set()) for key, values in asked)


def _calendar(organisation, day):
    return {'date': day, '_condition_context': conditions.EvaluationContext(
        timezone=organisation['timezone'], week_start=organisation['week_start'], today=day, duty_day=day, context='audience')}


def profile_context(db, department_id, organisation_id, person_ids):
    """Load person facts once for a window of audience dates, in one query per group."""

    organisation = db.get(m.Organisation, organisation_id)
    data = {'organisation': {'timezone': organisation.timezone, 'week_start': organisation.week_start}}
    if not person_ids:
        return data
    data['people'] = {p.id: record(p) for p in db.scalars(select(m.Person).where(
        m.Person.organisation_id == organisation_id, m.Person.id.in_(person_ids)))}
    for name, cls in (('memberships', m.Membership), ('eligibilities', m.Eligibility),
                      ('programme_memberships', m.ProgrammeMembership), ('grants', m.CapabilityGrant)):
        data[name] = [record(r) for r in db.scalars(select(cls).where(
            cls.department_id == department_id, cls.person_id.in_(person_ids)))]
    for name, cls in (('categories', m.Category), ('capabilities', m.Capability), ('programmes', m.Programme)):
        data[name] = {r.id: record(r) for r in db.scalars(select(cls).where(cls.department_id == department_id))}
    data['_person_rows'] = index_person_rows(data)
    return data


def profiles(db, department_id, organisation_id, person_ids, day, *, context=None):
    """What each of these people holds on this date, through the shared condition evaluator."""
    if not person_ids:
        return {}
    data = context if context is not None else profile_context(db, department_id, organisation_id, person_ids)
    zone = data['organisation']['timezone']
    calendar = _calendar(data['organisation'], day)
    holds = {pid: dict(calendar) for pid in person_ids}
    start, end = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
    for pid in holds:
        held = conditions.rule_holds(data, pid, None, start, end)
        for key in [*(group[0] for group in GROUPS), 'tags']:
            values = held('eligible_service_ids' if key == 'service_ids' else key)
            if values or key == 'tags':
                holds[pid][key] = values
    return holds


def person_role_context(db, department_id, person_ids):
    """Load enabled person links and dated grants once for a window of audience dates."""
    bindings = role_bindings(db, department_id)
    identities = list(db.execute(select(m.Identity.id, m.Identity.person_id).where(
        m.Identity.id.in_({b.identity_id for b in bindings}), m.Identity.disabled.is_(False), m.Identity.person_id.in_(person_ids))))
    return bindings, identities


def person_roles(db, department_id, person_ids, day, *, context=None):
    """Roles held by each rota person on the audience date, across their enabled sign-ins."""
    bindings, identities = context if context is not None else person_role_context(db, department_id, person_ids)
    active = department_roles(db, department_id, effective=False, day=day, bindings=bindings)
    roles = {}
    for identity_id, person_id in identities:
        if identity_id in active:
            roles.setdefault(person_id, set()).update(active[identity_id])
    return roles


def recipient_facts(db, department_id, *, identity_ids=None):
    """Load immutable audience inputs once for a request spanning several dates."""
    department = db.get(m.Department, department_id)
    bindings = role_bindings(db, department_id) if identity_ids is None else list(db.scalars(select(m.RoleBinding).where(
        m.RoleBinding.department_id == department_id, m.RoleBinding.identity_id.in_(identity_ids))))
    identities = list(db.scalars(select(m.Identity).where(
        m.Identity.id.in_({b.identity_id for b in bindings}), m.Identity.disabled.is_(False))))
    people = {i.person_id for i in identities if i.person_id}
    context = profile_context(db, department_id, department.organisation_id, people)
    return bindings, identities, context, tag_data(db, [department_id])


def recipient_context(db, department_id, day, *, facts=None, identity_ids=None):
    """Evaluate one date from shared inputs; roles and memberships remain date-specific."""
    bindings, identities, context, tags = facts if facts is not None else recipient_facts(db, department_id, identity_ids=identity_ids)
    if identity_ids is not None:
        bindings = [binding for binding in bindings if binding.identity_id in identity_ids]
        identities = [identity for identity in identities if identity.id in identity_ids]
    active = department_roles(db, department_id, effective=False, day=day, bindings=bindings)
    identities = [identity for identity in identities if identity.id in active]
    people = {i.person_id for i in identities if i.person_id}
    holds = profiles(db, department_id, None, people, day, context=context)
    calendar = next(iter(holds.values()), None)
    holds[None] = ({key: calendar[key] for key in ('date', '_condition_context')} if calendar else
                   _calendar(context['organisation'], day))
    return active, identities, holds, tags


def recipients(db, department_id, audience, day, context=None):
    """Sign-ins in the department that this audience reaches; a batch can share its loaded facts."""
    active, identities, holds, tags = context or recipient_context(db, department_id, day)
    audience = audience or {"everyone": True}
    if audience.get("everyone"):
        return {i.id for i in identities}
    return {i.id for i in identities if matches(audience, active[i.id], i.person_id, holds.get(i.person_id, holds[None]), tags)}



def viewer(db, actor, day, *, context=None, tags=None, bindings=None):
    """Everything needed to test many audiences against one person, loaded once."""
    organisation_id = getattr(actor, 'organisation_id', None) or db.get(m.Department, actor.department_id).organisation_id
    people = {actor.person_id} if actor.person_id else set()
    context = context if context is not None else profile_context(db, actor.department_id, organisation_id, people)
    holds = profiles(db, actor.department_id, organisation_id, people, day, context=context)
    return (held_roles(db, actor.identity_id, actor.department_id, day=day, bindings=bindings), actor.person_id,
        (holds.get(actor.person_id) or _calendar(context['organisation'], day)), tag_data(db, [actor.department_id]) if tags is None else tags)



def reaches(db, actor, audience, day, seen=None):
    roles, person_id, holds, tags = seen or viewer(db, actor, day)
    return matches(audience, roles, person_id, holds, tags)


def visible_content(db, actor, day, query):
    """Filter in SQL before paging, evaluating each distinct audience through the shared evaluator."""
    from .notification_audience import source_visibility
    query = query.where(source_visibility(db, actor, m.DepartmentContent))
    seen = viewer(db, actor, day)
    audience = cast(m.DepartmentContent.audience, String)
    # shortcut: one evaluation per distinct department audience; materialize/index membership if that count grows.
    visible = [value for value in db.scalars(query.with_only_columns(audience).order_by(None).distinct())
               if reaches(db, actor, json.loads(value), day, seen)]
    return query.where(audience.in_(visible))
