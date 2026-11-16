"""SW3-C-07: who swapped with whom is read only by the two swappers and the swap reviewers, never by colleagues."""
from sqlalchemy import select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from .governance_support import transition
from .test_staff_swaps import fixed_clock, offered, source_pair  # noqa: F401 (autouse clock)

SUFFIX = ' · swap '


def published_swap(h):
    pid, first, second, _ = source_pair(h, third=True)
    row = offered(h, pid, first)
    row = transition(h, row, 'counteroffer', 'staff_two', {'assignment_id': second, 'reason': 'Offer this exact duty'})['swap']
    row = transition(h, row, 'confirm')['swap']
    workspace = h.detail(row['workspace_id'])['workspace']
    published = h.lifecycle(workspace, 'approve', 'approver')
    assert published['workspace']['status'] == 'published'
    for _ in range(50):
        if not process_one(h.app.state.engine):
            break
    return row, published['publication_id']


def names(h):
    with transaction(h.app.state.engine) as db:
        return [db.get(m.Person, pid).display_name for pid in h.seed['people'][:2]]


def labels(h, role, publication_id):
    """Every rota label a reader is shown for the amendment, across the reads that carry one."""
    found = [n['title'] for n in h.get(role, '/api/notifications')['notifications'] if n['kind'] == 'publication']
    found += [p['name'] for p in h.get(role, '/api/publications')['publications'] if p['id'] == publication_id]
    found.append(h.get(role, '/api/publications/' + publication_id)['snapshot']['workspace']['name'])
    days = h.get(role, '/api/schedule/days?start=2026-10-04&end=2026-10-07')
    found += [s['name'] for s in days.get('sources', []) if s.get('id') == publication_id]
    home = h.get(role, '/api/home')
    found += [i.get('rota_name') for i in home.get('attention', []) if i.get('rota_name')]
    return found


def test_colleagues_never_read_the_swappers_names(h):
    _, publication_id = published_swap(h)
    first, second = names(h)
    colleague = labels(h, 'staff_three', publication_id)
    assert colleague, 'the third doctor is on the amendment and reads its label'
    assert not [label for label in colleague if first in label or second in label or SUFFIX in label]
    for role in ('staff', 'staff_two', 'coordinator'):
        assert any(first in label and second in label for label in labels(h, role, publication_id)), role


def test_names_stored_before_the_fix_are_redacted_per_reader(h):
    swap, publication_id = published_swap(h)
    first, second = names(h)
    legacy = f'Rota{SUFFIX}{first} ↔ {second}'
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Workspace).where(m.Workspace.id == swap['workspace_id']).values(name=legacy))
        event = db.scalar(select(m.Outbox.id).where(m.Outbox.event_key == 'publication:' + publication_id))
        db.execute(update(m.Notification).where(m.Notification.outbox_id == event).values(title=legacy + ' published'))
        if not db.scalar(select(m.Notification.id).where(m.Notification.outbox_id == event,
                                                         m.Notification.identity_id == h.seed['identities']['staff_three'])):
            db.add(m.Notification(outbox_id=event, identity_id=h.seed['identities']['staff_three'],
                                  title=legacy + ' published', message='The rota is final.'))
    colleague = [n['title'] for n in h.get('staff_three', '/api/notifications')['notifications'] if n['kind'] == 'publication']
    assert 'Rota published' in colleague and not [t for t in colleague if first in t or second in t]
    assert any(t == legacy + ' published' for t in [n['title'] for n in h.get('staff', '/api/notifications')['notifications']])
    rows = {p['id']: p['name'] for p in h.get('staff_three', '/api/publications')['publications']}
    assert first not in rows.get(publication_id, '') and second not in rows.get(publication_id, '')
