"""Email also reaches active department people who have never had an Rota identity."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.snapshots import tag_data
from packages.domain.conditions import absolute_values
from . import audiences


def _lines(tree):
    if "field" in tree:
        yield tree
    for child in tree.get("conditions", []):
        yield from _lines(child)


def extra_context(db, department_id):
    """Load unlinked people and their dated facts once for a request's audience window."""
    department = db.get(m.Department, department_id)
    people = list(db.scalars(select(m.Person).where(m.Person.organisation_id == department.organisation_id,
        m.Person.active.is_(True), ~select(m.Identity.id).where(m.Identity.person_id == m.Person.id).exists())
        .order_by(m.Person.id)))
    facts = audiences.profile_context(db, department_id, department.organisation_id, {person.id for person in people})
    return people, facts, tag_data(db, [department_id])


def extra_people(db, department_id, audience, day, *, context=None):
    """Keep department scope, account revocation and the shared audience evaluator intact."""
    audience = audience or {"everyone": True}
    lines = list(_lines(audience.get("where") or {}))
    if not audience.get("everyone") and lines and {line["field"] for line in lines} == {"roles"}:
        return []
    people, facts, tags = context if context is not None else extra_context(db, department_id)
    holds = audiences.profiles(db, department_id, None, {person.id for person in people}, day, context=facts)
    named = set(audience.get("person_ids", [])) | {person_id for line in lines
        if line["field"] == "person_ids" and line.get("op", "any") not in {"none", "is_not"}
        for person_id in absolute_values(line) or []}
    return [person for person in people if (person.id in named or any(holds[person.id].get(key) for key, _, _ in audiences.GROUPS))
            and audiences.matches(audience, set(), person.id, holds[person.id], tags)]
