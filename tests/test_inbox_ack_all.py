"""Mark all as read is one step, however many notifications are waiting."""
from datetime import timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one


def test_mark_all_as_read_in_one_step(h):
    me = h.get('staff', '/api/me')['person_id']
    for day in ('2026-10-04', '2026-10-11'):
        end = '2026-10-11' if day == '2026-10-04' else '2026-10-18'
        w = h.workspace('Inbox rota ' + day, day, end)
        h.publish(h.shift(w, start=day + 'T08:00:00+03:00', end=day + 'T12:00:00+03:00', people=[me])['workspace'])
    for _ in range(10):
        process_one(h.app.state.engine)
    unread = [n for n in h.get('staff', '/api/notifications')['notifications'] if not n['read_at']]
    assert len(unread) >= 2
    assert h.command('staff', 'notification.ack_all')['acknowledged'] == len(unread)
    notes = h.get('staff', '/api/notifications')['notifications']
    assert all(n['read_at'] for n in notes)
    assert all(n['acknowledged_at'] for n in notes)
    assert all(n['kind'] == 'publication' for n in notes)


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind', ['notification.ack', 'notification.ack_all'])
@pytest.mark.parametrize('source_kind', [None, 'publication'])
@pytest.mark.parametrize('already_read', [False, True])
def test_routine_reads_need_no_reason_preserve_evidence_and_stay_scoped(request, backend, kind, source_kind, already_read):
    h = request.getfixturevalue(backend)
    earlier = m.now() - timedelta(days=1)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(other)
        db.flush()
        ids = []
        for department, recipient in [(h.seed['department_id'], 'staff'),
                                      (h.seed['department_id'], 'coordinator'), (other.id, 'staff')]:
            event = m.Outbox(department_id=department, event_key=m.uid(), kind='publication', payload={})
            db.add(event)
            db.flush()
            note = m.Notification(outbox_id=event.id, identity_id=h.seed['identities'][recipient],
                title='Synthetic routine item', message='Read compatibility', source_kind=source_kind,
                read_at=earlier if already_read and not ids else None)
            db.add(note)
            db.flush()
            ids.append(note.id)
    target = ids[0] if kind == 'notification.ack' else None
    before = next(n for n in h.get('staff', '/api/notifications')['notifications'] if n['id'] == ids[0])
    result = h.command('staff', kind, target=target, revision=1 if target else None)
    if not target:
        assert result['read'] == result['acknowledged'] == 1
    saved = next(n for n in h.get('staff', '/api/notifications')['notifications'] if n['id'] == ids[0])
    assert saved['read_at'] and saved['acknowledged_at'] and saved['revision'] == 2
    if already_read:
        assert saved['read_at'] == before['read_at']
    repeated = h.command('staff', kind, target=target, revision=2 if target else None)
    if not target:
        assert repeated['read'] == repeated['acknowledged'] == 0
    assert next(n for n in h.get('staff', '/api/notifications')['notifications'] if n['id'] == ids[0]) == saved
    with transaction(h.app.state.engine) as db:
        for note_id in ids[1:]:
            note = db.get(m.Notification, note_id)
            assert note.read_at is None and note.acknowledged_at is None and note.revision == 1


@pytest.mark.parametrize('kind', ['notification.ack', 'notification.ack_all'])
@pytest.mark.parametrize('payload', [{'reason': ''}, {'reason': 42}, {'unexpected': True}])
def test_routine_read_payloads_still_validate_supplied_fields(h, kind, payload):
    reply = h.command('staff', kind, payload, status=422)
    assert reply['error']['code'] == 'SCHEMA_INVALID'
