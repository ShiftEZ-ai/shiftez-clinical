"""Build retained draft staffing for tests that diagnose a gap already on disk."""
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import revalidate
from packages.contracts.schemas import ParticipantInput
from packages.domain.time import aware


def retained_staffing(h, workspace, *times, people, service_ids=(), **shift):
    created = h.shift(workspace, *times, people=[], **shift)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Shift, created['shift_id'])
        draft = db.get(m.Workspace, workspace['id'])
        for service_id in service_ids:
            if service_id != h.seed['services'][shift.get('service', 0)]:
                db.add(m.ShiftService(shift_id=row.id, service_id=service_id))
        for participant in people:
            value = participant if isinstance(participant, dict) else {
                'person_id': participant, 'start': aware(row.start), 'end': aware(row.end)}
            value = ParticipantInput.model_validate(value)
            db.add(m.Assignment(shift_id=row.id, **value.model_dump(exclude={'segments'}),
                segments=value.model_dump(mode='json')['segments']))
        revalidate(db, draft)
    detail = h.detail(workspace)
    return {**created, 'workspace': detail['workspace'], 'validation': detail['workspace']['validation'],
            'shifts': detail['shifts']}
