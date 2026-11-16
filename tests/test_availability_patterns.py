"""Weekly availability: staff propose, a coordinator approves, and only then does it count in rota checks."""
from datetime import date, timedelta

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import transition
from .test_staff_swaps import source_pair


def pattern(person, **extra):
    return {'person_id': person, 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00', 'starts_on': '2026-09-01', **extra}


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('decision', ['approve', 'decline'])
@pytest.mark.parametrize('batch', [False, True])
def test_pattern_decision_and_undo_publish_captured_revision_status_once(request, database, decision, batch):
    from tests.outbox_support import process_one_after_undo
    h = request.getfixturevalue(database)
    row = h.command('staff', 'availability_pattern.propose', pattern(h.seed['people'][0], note='Private synthetic explanation'))['pattern']
    if batch:
        result = h.command('coordinator', 'request.decide_many', {'items': [{'source': 'pattern', 'id': row['id'],
            'expected_revision': row['revision']}], 'decision': 'approve' if decision == 'approve' else 'reject',
            'reason': 'Synthetic independent decision'})
        decided = result['records'][0]
    else:
        decided = h.command('coordinator', 'availability_pattern.decide', {'decision': decision,
            'reason': 'Synthetic independent decision'}, row['id'], row['revision'])['pattern']
    # Deliver the captured proposal/decision before Undo; undispatched inverses have a separate grace-window control.
    for _ in range(8):
        if not process_one_after_undo(h.app.state.engine):
            break
    if batch:
        undone = h.command('coordinator', 'request.undo', {'batch_id': result['batch_id'], 'reason': 'Undo the reviewed batch'})['records'][0]
    else:
        undone = h.command('coordinator', 'availability_pattern.undo', {'reason': 'Undo the decision'}, decided['id'], decided['revision'])['pattern']
    assert undone['status'] == 'proposed' and undone['revision'] == 3
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.Outbox).where(m.Outbox.payload['pattern_id'].as_string() == row['id'])
            .order_by(m.Outbox.created_at, m.Outbox.id)))
        assert {e.event_key for e in events} == {f'availability-pattern:{row["id"]}',
            f'availability-pattern:{row["id"]}:2:decided', f'availability-pattern:{row["id"]}:3:undone'}
        assert {e.payload['pattern_revision']: e.payload['pattern_status'] for e in events} == {
            1: 'proposed', 2: 'active' if decision == 'approve' else 'declined', 3: 'proposed'}
        assert all('Private synthetic explanation' not in str(e.payload) for e in events)
    for _ in range(8):
        if not process_one_after_undo(h.app.state.engine):
            break
    notes = h.get('staff', '/api/notifications')['notifications']
    titles = [n['title'] for n in notes]
    assert sum(t.endswith('weekly unavailability was ' + ('approved' if decision == 'approve' else 'declined')) for t in titles) == 1
    assert titles.count('Weekly unavailability decision undone') == 1
    # A proposal goes to its decider, not the staff member who sent it (the worker excludes the actor).
    decider_notes = h.get('coordinator', '/api/notifications')['notifications']
    assert sum(n['title'].startswith('Weekly unavailability to approve') for n in decider_notes) == 1
    assert not any(t.startswith('Weekly unavailability to approve') for t in titles)
    assert 'Private synthetic explanation' not in str(decider_notes)
    assert 'Private synthetic explanation' not in str(notes)
    assert not process_one_after_undo(h.app.state.engine)
    assert h.get('staff', '/api/notifications')['notifications'] == notes
    assert not any('weekly unavailability' in n['title'].lower() for n in h.get('auditor', '/api/notifications')['notifications'])


def test_pattern_quick_undo_preserves_the_existing_outbox_inverse_grace(h):
    from tests.outbox_support import process_one_after_undo
    row = h.command('staff', 'availability_pattern.propose', pattern(h.seed['people'][0]))['pattern']
    decided = h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Synthetic decision'},
        row['id'], row['revision'])['pattern']
    h.command('coordinator', 'availability_pattern.undo', {'reason': 'Immediate inverse'}, row['id'], decided['revision'])
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f'availability-pattern:{row["id"]}:2:decided'))
        assert event.state == 'superseded'
        assert db.scalar(select(m.Outbox.id).where(m.Outbox.event_key == f'availability-pattern:{row["id"]}:3:undone')) is None
    for _ in range(8):
        if not process_one_after_undo(h.app.state.engine):
            break
    titles = [n['title'] for n in h.get('staff', '/api/notifications')['notifications']]
    assert not any('was approved' in t or 'decision undone' in t for t in titles)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('decision', ['approve', 'decline'])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('creator', ['pattern_owner', 'staff'])
def test_permitted_self_pattern_decision_has_undo_but_direct_active_record_does_not(request, database, decision, legacy, creator, monkeypatch):
    from apps.api.services import availability_patterns
    from .governance_support import identity
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    owner = identity(h, 'pattern_owner', ['staff', 'coordinator'], person=person)
    row = h.command(creator, 'availability_pattern.propose', pattern(person))['pattern']
    assert h.command('pattern_owner', 'availability_pattern.decide', {'decision': decision, 'reason': 'Not independently allowed'},
        row['id'], row['revision'], status=403)['error']['code'] == 'SELF_APPROVAL'
    permission = h.command('admin', 'self_approval.grant', {'identity_id': owner, 'actions': ['request.decide'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Explicit synthetic single-coordinator permission'})['permission']
    with monkeypatch.context() as legacy_setup:
        if legacy:
            # Older decisions had a receipt/audit event but no lifecycle snapshot; never delete immutable evidence.
            legacy_setup.setattr(availability_patterns, 'history', lambda *args, **kwargs: None)
        decided = h.command('pattern_owner', 'availability_pattern.decide', {'decision': decision,
            'reason': 'Reviewed with the independent department lead by phone'}, row['id'], row['revision'])['pattern']
    if legacy:
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(m.DefinitionHistory.id).where(m.DefinitionHistory.kind == 'availability_pattern.decide',
                m.DefinitionHistory.object_id == row['id'])) is None
    # Undo restores the original proposal; it does not require a fresh self-decision grant.
    h.command('admin', 'self_approval.revoke', {'reason': 'Independent reviewer returned'}, permission['id'], permission['revision'])
    h.command('coordinator', 'availability_pattern.undo', {'reason': 'Not my decision'}, decided['id'], decided['revision'], status=403)
    h.command('pattern_owner', 'availability_pattern.undo', {'reason': 'Stale decision version'}, decided['id'], 1, status=409)
    undone = h.command('pattern_owner', 'availability_pattern.undo', {'reason': 'Restore my original proposal'}, decided['id'], decided['revision'])['pattern']
    assert undone['status'] == 'proposed' and undone['decided_by'] is None and undone['decided_at'] is None
    direct = h.command('coordinator', 'availability_pattern.propose', pattern(h.seed['people'][1]))['pattern']
    assert direct['status'] == 'active'
    h.command('coordinator', 'availability_pattern.undo', {'reason': 'Direct record was never a proposal decision'}, direct['id'], direct['revision'], status=403)
    ended = h.command('coordinator', 'availability_pattern.end', {'reason': 'Temporarily end direct record'}, direct['id'], direct['revision'])['pattern']
    restored = h.command('coordinator', 'availability_pattern.restore', {'reason': 'Restore direct record'}, direct['id'], ended['revision'])['pattern']
    h.command('coordinator', 'availability_pattern.undo', {'reason': 'Higher revision is not decision provenance'}, direct['id'], restored['revision'], status=403)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_mixed_self_pattern_batch_undo_rolls_back_events_history_and_inputs_on_stale_second_row(request, database):
    from .governance_support import identity
    from .test_request_decide_many import state
    h = request.getfixturevalue(database)
    actor = identity(h, 'pattern_batch_owner', ['coordinator', 'staff'], person=h.seed['people'][0])
    h.command('admin', 'self_approval.grant', {'identity_id': actor, 'actions': ['request.decide'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Synthetic bounded self-decision permission'})
    rows = [h.command('pattern_batch_owner', 'availability_pattern.propose', pattern(h.seed['people'][0], weekdays=[day]))['pattern']
        for day in [1, 2]]
    result = h.command('pattern_batch_owner', 'request.decide_many', {'items': [{'source': 'pattern', 'id': r['id'],
        'expected_revision': r['revision']} for r in rows], 'decision': 'approve',
        'reason': 'Independent colleague reviewed these synthetic patterns'})
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.AvailabilityPattern, rows[1]['id']).revision += 1
    before = state(h)
    with transaction(h.app.state.engine) as db:
        history_before = [(r.id, r.snapshot) for r in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id.in_([row['id'] for row in rows])).order_by(m.DefinitionHistory.id))]
    failed = h.command('pattern_batch_owner', 'request.undo', {'batch_id': result['batch_id'], 'reason': 'Undo both decisions'}, status=409)
    assert failed['error']['code'] == 'STALE_REVISION' and state(h) == before
    with transaction(h.app.state.engine) as db:
        assert [(r.id, r.snapshot) for r in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id.in_([row['id'] for row in rows])).order_by(m.DefinitionHistory.id))] == history_before


def codes(h, w):
    return {(f['code'], f.get('person_id')) for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings']}


def test_staff_propose_coordinator_approves_and_checks_respect_it(h):
    me = h.get('staff', '/api/me')['person_id']
    # 2026-10-04 is a Sunday (weekday 6).
    w = h.workspace('Pattern rota', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-04T09:00:00+03:00', '2026-10-04T11:00:00+03:00', people=[me])['workspace']
    proposed = h.command('staff', 'availability_pattern.propose', pattern(me, note='School run'))['pattern']
    assert proposed['status'] == 'proposed'
    assert ('UNAVAILABLE', me) not in codes(h, w), 'a proposal does not count yet'
    w = h.detail(w)['workspace']
    h.command('staff', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Own approval'}, proposed['id'], 1, status=403)
    h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Agreed pattern'}, proposed['id'], 1)
    assert ('UNAVAILABLE', me) in codes(h, h.detail(w)['workspace'])
    listed = h.get('staff', '/api/availability-patterns')['patterns']
    assert [p['status'] for p in listed] == ['active']
    h.command('staff', 'availability_pattern.end', {'reason': 'No longer needed'}, proposed['id'], 2)
    assert ('UNAVAILABLE', me) not in codes(h, h.detail(w)['workspace'])


def test_staff_cannot_set_someone_elses_pattern_and_input_is_checked(h):
    other = h.seed['people'][1]
    me = h.get('staff', '/api/me')['person_id']
    if other == me:
        other = h.seed['people'][2]
    h.command('staff', 'availability_pattern.propose', pattern(other), status=403)
    h.command('coordinator', 'availability_pattern.propose', pattern(me, start_time='12:00', end_time='08:00'), status=422)
    saved = h.command('coordinator', 'availability_pattern.propose', pattern(other))['pattern']
    assert saved['status'] == 'active', 'a coordinator records a pattern directly'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgres'])
def test_requests_swap_history_bounds_offer_and_wish_budget_snapshot_reads(request, database):
    h = request.getfixturevalue(database)
    publication, first, second, _ = source_pair(h)
    history = {}
    for number in range(3):
        row = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
            'assignment_id': first, 'recipient_person_id': h.seed['people'][1],
            'expires_at': (m.now() + timedelta(hours=2)).isoformat(), 'reason': 'Historical named offer'})['swap']
        transition(h, row, 'withdraw')
        history[row['id']] = publication
        publication = h.publish(h.workspace(f'History amendment {number}', base=publication))['publication_id']
        snapshot = h.get('coordinator', '/api/publications/' + publication)['snapshot']
        first, second = [shift['participants'][0]['id'] for shift in snapshot['shifts']]
    offers = []
    for role, assignment in [('staff', first), ('staff_two', second)]:
        offers.append(h.command(role, 'swap.offer', {'kind': 'give', 'publication_id': publication,
            'assignment_id': assignment, 'expires_at': (m.now() + timedelta(hours=2)).isoformat(),
            'reason': 'Current open offer'})['swap'])
    public, own = offers
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SwapOfferAudience(swap_id=public['id'], person_id=h.seed['people'][1]))
    snapshots = []

    def query(_connection, _cursor, statement, parameters, _context, _many):
        if 'FROM publications' in statement and 'publications.snapshot' in statement:
            values = parameters.values() if isinstance(parameters, dict) else parameters
            snapshots.append({value.isoformat() if isinstance(value, date) else value for value in values})

    for role in ['staff_two', 'coordinator']:
        h.client(role)
        snapshots.clear()
        event.listen(h.app.state.engine, 'before_cursor_execute', query)
        try:
            page = h.get(role, '/api/requests/all?type=swap')
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', query)
        indexed = {item['id']: item for item in page['items']}
        assert set(indexed) == {*history, public['id'], own['id']}
        if role == 'staff_two':
            budget = page['wishes']
            # The header reserves frozen wish charges in today's period, even while filtering to swaps.
            assert snapshots == [{publication, 'shared'}, {h.seed['department_id'], budget['start'], budget['end'], 'shared'}]
            assert budget == {'budget_points': 10, 'period': 'month', 'start': '2026-10-01',
                              'end': '2026-11-01', 'used': 0, 'multiplier': 1}
            assert 'reason' not in indexed[public['id']] and 'workspace_id' not in indexed[public['id']]
        else:
            assert snapshots == []
        assert all(indexed[key]['stage'] == 'withdrawn' and indexed[key]['offered_assignment']['start'] for key in history)
        assert indexed[public['id']]['offered_assignment']['assignment_id'] == first
        assert indexed[own['id']]['audience_pending'] is True
        assert indexed[own['id']]['audience_failed'] is False
    with transaction(h.app.state.engine, write=True) as db:
        notice = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{own['id']}:1:offered"))
        notice.payload = {**notice.payload, 'audience_pending': False, 'audience_failed': True}
    failed = next(item for item in h.get('staff_two', '/api/requests/all?type=swap')['items'] if item['id'] == own['id'])
    assert failed['audience_pending'] is False and failed['audience_failed'] is True
