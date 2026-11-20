"""Build retained draft staffing for tests that diagnose a gap already on disk."""
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import name_attached_supervisors, revalidate
from packages.contracts.schemas import ParticipantInput
from packages.domain.finding_causes import DRAFT_STAFFING_GAP_CODES
from packages.domain.time import aware
from packages.domain.validation import finding_observations


def retained_staffing(h, workspace, *times, people, pair_attachments=False, **shift):
    """Seed a gap already in the draft after the normal create path would refuse it."""
    created = h.shift(workspace, *times, people=[], **shift)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Shift, created['shift_id'])
        draft = db.get(m.Workspace, workspace['id'])
        for participant in people:
            value = participant if isinstance(participant, dict) else {
                'person_id': participant, 'start': aware(row.start), 'end': aware(row.end)}
            value = ParticipantInput.model_validate(value)
            db.add(m.Assignment(shift_id=row.id, **value.model_dump(exclude={'segments'}),
                segments=value.model_dump(mode='json')['segments']))
        if pair_attachments:
            name_attached_supervisors(db, draft, row)
        revalidate(db, draft)
    detail = h.detail(workspace)
    validation = detail['workspace']['validation']
    assert any(f['code'] in DRAFT_STAFFING_GAP_CODES and f['severity'] == 'error'
               and f.get('shift_id') == created['shift_id'] for f in finding_observations(validation))
    return {**created, 'workspace': detail['workspace'], 'validation': validation, 'shifts': detail['shifts']}
