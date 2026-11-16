"""A linked staff member's dated facts, without granting access to the department catalogues."""
from sqlalchemy import or_, select

from apps.api import models as m
from apps.api.auth import person_access, require_role
from apps.api.errors import require
from apps.api.serialization import json_value

LIMIT = 2000  # Same bounded catalogue ceiling; never return a silently incomplete history.


def profile(db, actor):
    require_role(actor, 'staff')
    require(bool(actor.person_id), 'PERSON_REQUIRED', 'Your account is not linked to a person', 403)
    person = person_access(db, actor, actor.person_id)
    data = {}

    def read(cls, fields, *conditions):
        rows = db.execute(select(*(getattr(cls, key) for key in sorted(fields)))
            .where(cls.department_id == actor.department_id, *conditions)
            .order_by(cls.created_at, cls.seq).limit(LIMIT + 1)).mappings().all()
        require(len(rows) <= LIMIT, 'PROFILE_LIMIT', 'This profile exceeds the current history limit; contact your administrator', 409)
        return [{key: json_value(value) for key, value in row.items()} for row in rows]

    dated = {'id', 'person_id', 'starts_on', 'ends_on'}
    for name, cls, fields in (
        ('memberships', m.Membership, {'category_id'}),
        ('eligibilities', m.Eligibility, {'service_id'}),
        ('grants', m.CapabilityGrant, {'capability_id', 'service_id', 'revoked'}),
        ('programme_memberships', m.ProgrammeMembership, {'programme_id'}),
        ('rotations', m.Rotation, {'cohort_id', 'service_id', 'rotation_type_id', 'department_available'}),
    ):
        data[name] = read(cls, dated | fields, cls.person_id == person.id)
    data['mentorships'] = read(m.Mentorship, dated | {'mentor_id', 'programme_id', 'assignment_type_ids', 'service_ids', 'strength'},
                               or_(m.Mentorship.person_id == person.id, m.Mentorship.mentor_id == person.id))
    memberships = select(m.ProgrammeMembership.id).where(
        m.ProgrammeMembership.department_id == actor.department_id, m.ProgrammeMembership.person_id == person.id,
        m.ProgrammeMembership.programme_id == m.ProgrammeCalendarEntry.programme_id,
        m.ProgrammeMembership.starts_on < m.ProgrammeCalendarEntry.ends_on,
        m.ProgrammeMembership.ends_on > m.ProgrammeCalendarEntry.starts_on).exists()
    data['programme_calendar_entries'] = read(m.ProgrammeCalendarEntry, {'id', 'programme_id', 'name', 'kind', 'starts_on', 'ends_on'}, memberships)

    references = {
        'categories': (m.Category, {r['category_id'] for r in data['memberships']}),
        'services': (m.Service, {r.get('service_id') for name in ('eligibilities', 'grants', 'rotations') for r in data[name]}
                     | {i for r in data['mentorships'] for i in r['service_ids']}),
        'capabilities': (m.Capability, {r['capability_id'] for r in data['grants']}),
        'rotation_types': (m.RotationType, {r.get('rotation_type_id') for r in data['rotations']}),
        'cohorts': (m.Cohort, {r['cohort_id'] for r in data['rotations']}),
        'programmes': (m.Programme, {r.get('programme_id') for name in ('programme_memberships', 'mentorships') for r in data[name]}),
        'assignment_types': (m.AssignmentType, {i for r in data['mentorships'] for i in r['assignment_type_ids']}),
    }
    for name, (cls, ids) in references.items():
        if name == 'programmes':
            ids |= {r['programme_id'] for r in data['cohorts']}
        data[name] = read(cls, {'id', 'name', *({'programme_id'} if name == 'cohorts' else set())}, cls.id.in_(ids - {None}))
    paired = {person.id} | {
        r[key] for r in data['mentorships'] for key in ('person_id', 'mentor_id')}
    people = list(db.execute(select(m.Person.id, m.Person.display_name)
        .where(m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(paired - {None}))
        .order_by(m.Person.display_name, m.Person.id).limit(LIMIT + 1)))
    require(len(people) <= LIMIT, 'PROFILE_LIMIT', 'This profile exceeds the current history limit; contact your administrator', 409)
    data['people'] = [{'id': pid, 'display_name': name} for pid, name in people]
    return {'person_id': person.id, 'catalogues': data}
