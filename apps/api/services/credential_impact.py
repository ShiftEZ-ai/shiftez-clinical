"""Duties whose clinic eligibility was removed by a dated credential change."""
from collections import defaultdict
from zoneinfo import ZoneInfo
from sqlalchemy import select
from apps.api import models as m
from packages.domain.time import aware, local_midnight
from .common import outbox
from . import schedule


def affected(db, actor, credential, *, notify=False, proposed_dates=None, drafts=False):
    bulk = db.info.get('bulk') if not drafts else None
    context = bulk.get('credential_impact') if bulk is not None else None
    if context is None:
        zone = schedule.zone_of(db, actor.department_id)
        if drafts:
            from .workspace_access import shared_clause
            W = m.Workspace
            workspaces = db.execute(select(W.id, W.name, W.status).where(W.department_id == actor.department_id,
                W.archived_at.is_(None), W.status.in_(('draft', 'submitted', 'approved')), shared_clause(),
                W.id.not_in(select(m.PublicationChangeList.workspace_id).where(
                    m.PublicationChangeList.department_id == actor.department_id))))
            by_workspace = {w.id: w for w in workspaces}
        else:
            pubs = schedule.current_publications(db, [actor.department_id], m.now().astimezone(ZoneInfo(zone)).date(), light=True)
            by_workspace = {p.workspace_id: p for p in pubs}
        context = {'zone': zone, 'by_workspace': by_workspace}
        if bulk is not None:
            bulk['credential_impact'] = context
    zone, by_workspace = context['zone'], context['by_workspace']
    if not by_workspace:
        return {'count': 0, 'sessions': []}
    S, A = m.Shift, m.Assignment
    if bulk is not None:
        if 'assignments' not in context:
            context['assignments'], context['eligibilities'] = defaultdict(list), defaultdict(list)
            context['people'] = {}
            for shift, assignment, service, person in db.execute(select(S, A, m.ShiftService.service_id, m.Person)
                    .join(A, A.shift_id == S.id).join(m.ShiftService, m.ShiftService.shift_id == S.id)
                    .join(m.Person, m.Person.id == A.person_id)
                    .where(S.workspace_id.in_(by_workspace), S.cancelled.is_(False), A.end > m.now())):
                context['assignments'][assignment.person_id, service].append((shift, assignment))
                context['people'][assignment.person_id] = person
            for eligibility in db.scalars(select(m.Eligibility).where(m.Eligibility.department_id == actor.department_id)):
                context['eligibilities'][eligibility.person_id, eligibility.service_id].append(eligibility)
        key = credential.person_id, credential.service_id
        rows = context['assignments'][key]
        records = [*context['eligibilities'][key], *(row for row in db.new if isinstance(row, m.Eligibility)
            and row.department_id == actor.department_id and (row.person_id, row.service_id) == key)]
    else:
        rows = db.execute(select(S, A).join(A, A.shift_id == S.id).where(S.workspace_id.in_(by_workspace),
            S.cancelled.is_(False), A.person_id == credential.person_id, A.end > m.now(),
            S.id.in_(select(m.ShiftService.shift_id).where(m.ShiftService.service_id == credential.service_id))))
        records = list(db.scalars(select(m.Eligibility).where(m.Eligibility.department_id == actor.department_id,
            m.Eligibility.person_id == credential.person_id, m.Eligibility.service_id == credential.service_id)))
    if proposed_dates is not None:
        from types import SimpleNamespace
        records = [SimpleNamespace(starts_on=proposed_dates.get('starts_on', credential.starts_on),
                                   ends_on=proposed_dates.get('ends_on', credential.ends_on)) if r.id == credential.id else r
                   for r in records]
    spans = sorted(((local_midnight(r.starts_on, zone), local_midnight(r.ends_on, zone) if r.ends_on else None)
                    for r in records if r not in db.deleted and not getattr(r, 'withdrawn', False)), key=lambda pair: pair[0])
    if bulk is not None and not rows:
        return {'count': 0, 'sessions': []}
    people = context.setdefault('people', {}) if bulk is not None else {}
    person = people.get(credential.person_id) or db.info.get('bulk_relationship_references', {}).get((m.Person, credential.person_id))
    if person is None:
        person = next((row for row in db.info.get('bulk_preloaded_rows', [])
                       if isinstance(row, m.Person) and row.id == credential.person_id), None)
    if person is None:
        person = db.get(m.Person, credential.person_id)
    people[credential.person_id] = person
    person_name = person.display_name
    hits = []
    for shift, assignment in rows:
        owner = by_workspace[shift.workspace_id]
        if not drafts and owner.effective_until and aware(shift.start) >= local_midnight(owner.effective_until, zone):
            continue
        covered = aware(assignment.start)
        for start, end in spans:
            if start > covered:
                break
            if end is None:
                covered = aware(assignment.end)
                break
            covered = max(covered, end)
        if covered >= aware(assignment.end):
            continue
        reference = ({'workspace_id': owner.id, 'rota_name': owner.name, 'status': owner.status} if drafts else
                     {'publication_id': owner.id})
        hits.append({**reference, 'shift_id': shift.id, 'assignment_id': assignment.id,
                     'person_id': assignment.person_id, 'person': person_name,
                     'type': shift.type_snapshot['name'], 'service_id': credential.service_id,
                     'start': aware(assignment.start).isoformat(), 'end': aware(assignment.end).isoformat()})
    hits.sort(key=lambda r: (r['start'], r['assignment_id']))
    if hits and notify and not drafts:
        outbox(db, actor, f'credential-impact:{credential.id}:{credential.revision}', 'credential_impact',
               {'person_id': credential.person_id, 'credential_id': credential.id, 'count': len(hits), 'sessions': hits})
    return {'count': len(hits), 'sessions': hits}


def preview(db, actor, credential_id, value):
    from apps.api.auth import require_role
    from apps.api.errors import require
    from .common import scoped
    require_role(actor, 'credentialer', 'coordinator', 'approver', 'auditor')
    credential = scoped(db, m.Eligibility, credential_id, actor)
    dates = value.model_dump(include={'starts_on', 'ends_on'}, exclude_unset=True)
    start, end = dates.get('starts_on', credential.starts_on), dates.get('ends_on', credential.ends_on)
    require(start is not None and (end is None or start < end), 'INVALID_INTERVAL', 'End must follow start')
    return {'credential_id': credential.id, 'revision': credential.revision,
            'published_impact': affected(db, actor, credential, proposed_dates=dates),
            'draft_impact': affected(db, actor, credential, proposed_dates=dates, drafts=True)}
