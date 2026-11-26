"""Database adapter for the domain's current people facts; no stored values are changed."""
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from apps.api import models as m
from apps.api.serialization import record
from packages.domain.people import current_people as project_people
from packages.domain.validation import person_active_on


def active_people_on(day):
    return func.coalesce(person_active_on(m.Person.active, m.Person.inactive_from, day), False)


def period_records(db, department_id, first, last):
    return {name: [record(r) for r in db.scalars(select(cls).where(cls.department_id == department_id,
                cls.starts_on < last, cls.ends_on.is_(None) | (cls.ends_on > first)))]
            for name, cls in {'memberships': m.Membership, 'eligibilities': m.Eligibility,
                              'grants': m.CapabilityGrant, 'rotations': m.Rotation,
                              'programme_memberships': m.ProgrammeMembership}.items()}


def imported_aliases(db, actor, person_ids=None):
    aliases = {}
    mapping_table = m.SourceMapping
    query = select(mapping_table).where(mapping_table.department_id == actor.department_id,
        mapping_table.target_kind == 'people', mapping_table.source_key.startswith('alias:'))
    if person_ids is not None:
        query = query.where(mapping_table.target_id.in_(person_ids))
    for mapping in db.scalars(query):
        aliases.setdefault(mapping.target_id, set()).add(mapping.source_key.removeprefix('alias:'))
    return aliases


def current_people(db, actor, day, *, person_ids=(), people=None):
    org = db.get(m.Organisation, actor.organisation_id)
    if people is None:
        people = {p.id: record(p) for p in db.scalars(select(m.Person).where(
            m.Person.organisation_id == actor.organisation_id,
            m.Person.id.in_(person_ids)))}
    data = {'organisation': {'timezone': org.timezone}, 'people': people}
    for name, cls in (('categories', m.Category), ('programmes', m.Programme),
                      ('rotation_types', m.RotationType), ('cohorts', m.Cohort)):
        from .training import catalogue_scope
        data[name] = {r.id: record(r) for r in db.scalars(select(cls).where(cls.department_id == actor.department_id,
            catalogue_scope(db, actor, name, cls)))}
    for name, cls in (('memberships', m.Membership), ('programme_memberships', m.ProgrammeMembership), ('rotations', m.Rotation)):
        data[name] = [record(r) for r in db.scalars(select(cls).where(cls.department_id == actor.department_id,
            cls.person_id.in_(people),
            cls.starts_on <= day, cls.ends_on.is_(None) | (cls.ends_on > day),
            catalogue_scope(db, actor, name, cls)))]
    return project_people(data, day)


def current_people_counts(db, actor, day=None):
    """Count active organisation people and those without an effective rank here."""
    day = day or m.now().astimezone(ZoneInfo(db.get(m.Organisation, actor.organisation_id).timezone)).date()
    membership, category = m.Membership, m.Category
    has_rank = select(membership.id).join(category, category.id == membership.category_id).where(
        membership.person_id == m.Person.id,
        membership.department_id == actor.department_id,
        membership.withdrawn.is_(False),
        membership.starts_on <= day,
        membership.ends_on.is_(None) | (membership.ends_on > day),
        category.department_id == actor.department_id,
        category.active.is_(True),
        category.starts_on <= day,
        category.ends_on.is_(None) | (category.ends_on > day),
    ).exists()
    active, unranked = db.execute(select(func.count(), func.count().filter(~has_rank))
        .select_from(m.Person).where(m.Person.organisation_id == actor.organisation_id, active_people_on(day))).one()
    return {"active_people": active, "people_without_current_rank": unranked}


from .contacts import LEGACY_FIELDS as CONTACT_FIELDS


def visible_person(actor, person, contacts=()):
    """Fail closed for stored/legacy contacts: only the shared contact reader may supply values."""
    person = {key: value for key, value in person.items() if key not in CONTACT_FIELDS | {'contacts'}}
    if not actor.permits('admin', 'coordinator'):
        fields = {'id', 'revision', 'display_name', 'given_name', 'family_name', 'active', 'title', 'tags', 'display_title',
                  'current_category_ids', 'current_programme_ids', 'current_rotation_type_ids', 'current_cohort_ids',
                  'colour', 'short_code', 'rota_name', 'rota_aliases', 'aliases', 'employee_number_redacted', 'directory_order'}
        if actor.permits('approver'):
            fields.add('directory_section')
        person = {key: value for key, value in person.items() if key in fields}
    if contacts:
        person['contacts'] = list(contacts)
        person.update({c['legacy_field']: c['value'] for c in contacts if c.get('legacy_field')})
    return person


def visible_people(db, actor, people, *, directory=False):
    from .contacts import visible_contacts
    contacts = visible_contacts(db, actor, people)
    out = []
    for person in people:
        entry = visible_person(actor, person, contacts.get(person['id'], ()))
        if directory and 'directory_section' in person:
            entry['directory_section'] = person['directory_section']
        out.append(entry)
    return out


def visible_identity_names(db, actor, identity_ids):
    """Resolve department-affiliated actor names once, with the same person privacy projection as people reads."""
    identity_ids = {identity_id for identity_id in identity_ids if identity_id}
    if not identity_ids:
        return {}
    I, P, B = m.Identity, m.Person, m.RoleBinding
    department_binding = select(B.id).where(B.identity_id == I.id, B.department_id == actor.department_id).exists()
    rows = db.execute(select(I.id, I.display_name, I.person_id, P.id, P.display_name, P.organisation_id)
        .outerjoin(P, P.id == I.person_id)
        .where(I.id.in_(identity_ids),
               (I.id == actor.identity_id) | department_binding)).all()
    names = {}
    for identity_id, identity_name, person_id, linked_person_id, person_name, organisation_id in rows:
        if linked_person_id and organisation_id == actor.organisation_id:
            person = visible_person(actor, {'id': linked_person_id, 'display_name': person_name})
            names[identity_id] = person.get('display_name')
        else:
            names[identity_id] = identity_name
    return names


def add_visible_identity_names(rows, names):
    return [{**row, 'by_name': names.get(row.get('by'))} for row in (rows or [])]


def with_adjustment_names(db, actor, workspace_record):
    adjustments = workspace_record.get('rule_adjustments') or []
    overrides = workspace_record.get('target_overrides') or []
    names = visible_identity_names(db, actor, [row.get('by') for row in (*adjustments, *overrides)])
    workspace_record['rule_adjustments'] = add_visible_identity_names(adjustments, names)
    workspace_record['target_overrides'] = add_visible_identity_names(overrides, names)
    return workspace_record


def duty_ranks(db, department_id, shifts, zone):
    """Expose dated rank labels on authorised duties without disclosing membership/contact rows."""
    from datetime import timedelta
    from packages.domain.time import aware
    from packages.domain.supervision_teams import with_duty_ranks

    pending = [s for s in shifts if any('rank' not in p for p in s['participants'])]
    if not pending:
        return shifts
    people = {p['person_id'] for s in pending for p in s['participants'] if 'rank' not in p}
    days = [aware(s['start']).astimezone(ZoneInfo(zone)).date() for s in pending]
    first, end = min(days), max(days) + timedelta(days=1)
    M, C = m.Membership, m.Category
    memberships = [record(r) for r in db.scalars(select(M).where(M.department_id == department_id,
        M.person_id.in_(people), M.starts_on < end, M.ends_on.is_(None) | (M.ends_on > first)))]
    categories = {r.id: record(r) for r in db.scalars(select(C).where(C.department_id == department_id,
        C.id.in_({r['category_id'] for r in memberships})))} if memberships else {}
    return with_duty_ranks({'organisation': {'timezone': zone}, 'memberships': memberships,
                            'categories': categories}, shifts)
