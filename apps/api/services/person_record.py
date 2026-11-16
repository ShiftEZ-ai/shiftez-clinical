"""Department affiliation and self/peer access for person-record reads."""
from sqlalchemy import or_, select

from apps.api import models as m
from apps.api.auth import person_access
from apps.api.errors import require


def require_person(db, actor, person_id, readers):
    require(actor.permits(*readers) or actor.person_id == person_id,
            'FORBIDDEN', 'Your current role does not permit reading this person record', 403)
    person = person_access(db, actor, person_id)
    identities = select(m.Identity.id).where(m.Identity.person_id == person_id)
    affiliated = db.scalar(select(m.Person.id).where(m.Person.id == person_id, or_(
        select(m.DefinitionHistory.id).where(m.DefinitionHistory.department_id == actor.department_id,
                                            m.DefinitionHistory.kind == 'people',
                                            m.DefinitionHistory.object_id == person_id).exists(),
        select(m.Membership.id).where(m.Membership.department_id == actor.department_id,
                                     m.Membership.person_id == person_id).exists(),
        select(m.RoleBinding.id).where(m.RoleBinding.department_id == actor.department_id,
                                      m.RoleBinding.identity_id.in_(identities)).exists(),
        select(m.Session.id).where(m.Session.department_id == actor.department_id,
                                  m.Session.identity_id.in_(identities)).exists())))
    require(bool(affiliated), 'NOT_FOUND', 'Person not found in this department', 404)
    return person
