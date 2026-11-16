"""The person hub overview, composed from existing person and schedule readers."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from apps.api import models as m
from apps.api.auth import CATALOGUE_READ_ROLES
from apps.api.serialization import record
from apps.api.snapshots import shift_records
from packages.contracts import schemas as s
from packages.domain.time import aware, local_midnight
from . import schedule, schedule_view
from .people import current_people, visible_people
from .person_record import require_person
from .conditional_reads import revision_etag
from apps.api import live


def card_etag(db, actor, person, upcoming=3):
    """Build a bounded validator before workload, schedule and fairness projections."""
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    contact_stamp = db.execute(select(
        func.count(m.PersonContact.id), func.coalesce(func.sum(m.PersonContact.revision), 0),
        func.max(m.PersonContact.revision)
    ).where(m.PersonContact.department_id == actor.department_id,
            m.PersonContact.person_id == person.id)).one()
    publication_stamp = db.execute(select(
        func.count(m.Publication.id), func.coalesce(func.sum(m.Publication.revision), 0),
        func.max(m.Publication.revision), func.max(m.Publication.created_at)
    ).where(m.Publication.department_id == actor.department_id, m.Publication.current.is_(True))).one()
    return revision_etag(
        "person-card", person.id, actor.identity_id, actor.person_id, sorted(actor.roles), upcoming,
        person.revision, tuple(contact_stamp), tuple(publication_stamp),
        department.revision, department.catalogue_revision, organisation.revision,
        organisation.input_revision, organisation.availability_revision,
        live.activity(db, actor.department_id))


def read(db, actor, person_id, upcoming=3, *, person=None):
    person = person if person is not None else require_person(db, actor, person_id, s.ROLE_NAMES)
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    day = m.now().astimezone(ZoneInfo(zone)).date()
    first, tomorrow, end = day, day + timedelta(days=1), day + timedelta(days=366)
    lo, hi = local_midnight(first, zone), local_midnight(tomorrow, zone)
    facts = current_people(db, actor, person_ids={person_id})['people'].get(person_id, {})
    entry = visible_people(db, actor, [{**record(person), **facts}])[0]
    # Current reference labels are small, scoped acquisitions; no programme or rank lists are copied into the client.
    for field, cls in (('category', m.Category), ('programme', m.Programme),
                       ('rotation_type', m.RotationType), ('cohort', m.Cohort)):
        ids = facts.get('current_' + field + '_ids', [])
        entry[field + 's'] = [{'id': r.id, 'name': r.name} for r in db.scalars(select(cls).where(
            cls.department_id == actor.department_id, cls.id.in_(ids)))] if ids else []
    pubs = schedule.current_publications(db, [actor.department_id], day - timedelta(days=1), end, light=True)
    duties = schedule_view.published_shifts(db, pubs, zone, lo, local_midnight(end, zone), person_id=person_id)
    states = {p.workspace_id: 'published' for p in pubs}
    planner = actor.permits(*s.PERMISSIONS['schedule.read_team'])
    if planner:
        W, S, A = m.Workspace, m.Shift, m.Assignment
        drafts = list(db.execute(select(W.id, W.status).where(W.department_id == actor.department_id,
            W.status.in_(schedule.LIVE), W.archived_at.is_(None), W.starts_on < end, W.ends_on > day)))
        states.update(dict(drafts))
        duties += shift_records(db, db.scalars(select(S).where(S.workspace_id.in_(dict(drafts)),
            S.cancelled.is_(False), S.start < local_midnight(end, zone), S.end > lo,
            S.id.in_(select(A.shift_id).where(A.person_id == person_id))).order_by(S.start, S.id)))
    duties.sort(key=lambda r: (r['start'], r['id']))
    today = [d for d in duties if aware(d['start']) < hi and aware(d['end']) > lo]
    coming = [d for d in duties if aware(d['start']) >= hi][:upcoming]
    chosen = today + coming
    ids = {p['person_id'] for duty in chosen for p in duty['participants']}
    names = {r.id: {'person_id': r.id, 'display_name': r.display_name, 'short_code': r.short_code}
             for r in db.scalars(select(m.Person).where(m.Person.id.in_(ids)))} if ids else {}
    team_facts = current_people(db, actor, person_ids=ids)['people'] if ids else {}
    for pid, name in names.items():
        name['rank'] = team_facts.get(pid, {}).get('display_title', '')
    places = dict(db.execute(select(m.Location.id, m.Location.name).where(
        m.Location.id.in_({d['location_id'] for d in chosen}))).all()) if chosen else {}
    clinics = dict(db.execute(select(m.Service.id, m.Service.name).where(
        m.Service.id.in_({sid for d in chosen for sid in d['service_ids']}))).all()) if chosen else {}

    def duty(row):
        participants = row['participants'] if planner else schedule_view.own_and_paired(row['participants'], person_id)
        return {'id': row['id'], 'start': row['start'], 'end': row['end'], 'workspace_id': row['workspace_id'],
                'publication_id': row.get('publication_id'), 'status': states[row['workspace_id']],
                'duty_type': row['type_snapshot']['name'], 'location': places.get(row['location_id'], ''),
                'services': [clinics[i] for i in row['service_ids'] if i in clinics],
                'team': [{**names.get(p['person_id'], {'person_id': p['person_id']}),
                          'supervisor_person_id': p.get('supervisor_person_id'), 'start': p['start'], 'end': p['end']}
                         for p in participants]}

    result = {'person': entry, 'contacts': entry.pop('contacts', []), 'today': [duty(d) for d in today],
              'coming_up': [duty(d) for d in coming], 'permitted_sections': ['overview', 'schedule', 'contacts']}
    own = actor.person_id == person_id
    if actor.permits(*s.PERMISSIONS['fairness.read_team']) or own and actor.permits('staff'):
        from . import person_workload
        result['workload'] = person_workload.read(db, actor, person_id, on=day)
        # Fairness is department-scale. Keep the response marker for existing
        # clients, while its canonical detail remains /api/people/{id}/fairness.
        result['fairness'] = {'person_id': person_id,
                              'peers_visible': actor.permits(*s.PERMISSIONS['fairness.read_team']),
                              'workspace': None, 'goals': []}
        result['permitted_sections'].append('workload')
    if actor.permits('coordinator', 'approver') or own and actor.permits('staff'):
        from .person_availability import list_availability
        result['availability'] = list_availability(db, actor, person_id, day, end)
        result['permitted_sections'].append('availability')
    if actor.permits(*CATALOGUE_READ_ROLES):
        result['can_work'] = {key: [record(row) for row in db.scalars(select(cls).where(
            cls.department_id == actor.department_id, cls.person_id == person_id).limit(2000))]
            for key, cls in (('skills', m.CapabilityGrant), ('eligibilities', m.Eligibility))}
        result['permitted_sections'].append('can_work')
    if actor.permits(*s.TEAM_READER_ROLES):
        from .policies import rules_for_person
        result['rules'] = rules_for_person(db, actor, person_id, day)['rules']
        result['permitted_sections'].append('rules')
    if actor.permits(*s.PERMISSIONS['audit.read']):
        result['permitted_sections'].append('history')
    if actor.permits('admin') or own:
        result['permitted_sections'].append('access')
    if actor.permits(*s.TEAM_READER_ROLES) or own and actor.permits('staff'):
        result['permitted_sections'] += ['requests', 'quotas']
    result['actions'] = ['schedule.open'] + [kind for kind in ('availability.record', 'shift.update')
        if actor.permits(*s.PERMISSIONS[kind])]
    return result
