"""Access changes invalidate proposals that froze different event attendance."""
import pytest

from tests.test_accounts import with_users_file
from tests.test_events import event, event_type, input_revisions
from tests.test_generation_apply_scale import completed_proposal


@pytest.mark.parametrize('change', ['role.grant', 'role.revoke', 'identity.disable', 'identity.enable', 'account.invite'])
@pytest.mark.parametrize('blocks,ended', [(True, False), (True, True), (False, False), (None, False)],
    ids=['blocking', 'ended-blocking', 'nonblocking', 'no-events'])
def test_event_attendance_access_change_stales_only_blocking_generation(h, tmp_path, change, blocks, ended):
    if change == 'account.invite':
        with_users_file(h, tmp_path)
    staff, person = h.seed['identities']['staff'], h.seed['people'][0]
    day = '2026-10-01' if ended else '2026-10-04'
    if blocks is not None:
        event(h, event_type(h, blocks_duties=blocks, default_audience={'roles': ['approver']}),
            start=f'{day}T09:00:00+03:00', end=f'{day}T10:00:00+03:00')
    grant = {'identity_id': staff, 'role': 'approver', 'starts_on': '2020-01-01',
             'reason': 'Synthetic event audience access'}
    binding = None
    if change in {'role.revoke', 'identity.disable', 'identity.enable'}:
        binding = h.command('admin', 'role.grant', grant)['record']
    identity_revision = 1
    if change == 'identity.enable':
        disabled = h.command('admin', 'identity.disable', {'reason': 'Temporarily disable event attendance'},
            staff, identity_revision)['record']
        identity_revision = disabled['revision']
    workspace = h.shift(h.workspace(start=day), start=f'{day}T08:00:00+03:00',
        end=f'{day}T12:00:00+03:00', people=[])['workspace']
    before = h.snapshot(workspace)
    shift = before['shifts'][0]
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': person, 'start': shift['start'], 'end': shift['end']}}])
    revisions = input_revisions(h)

    if change == 'role.grant':
        h.command('admin', change, grant)
    elif change == 'role.revoke':
        h.command('admin', change, {'reason': 'End synthetic event access'}, binding['id'], binding['revision'])
    elif change == 'account.invite':
        h.command('admin', change, {'display_name': 'Event attendee (test)', 'username': 'event.attendee',
            'email': 'event.attendee@example.test', 'person_id': person, 'roles': ['approver'],
            'reason': 'Synthetic event attendee invitation'})
    else:
        h.command('admin', change, {'reason': 'Change synthetic event access'}, staff, identity_revision)

    assert input_revisions(h) == tuple(value + bool(blocks) for value in revisions)
    current = h.snapshot(workspace)
    # Event attendance enters the snapshot as protected-time scheduling facts naming the attendees.
    protected = lambda snapshot: any(row.get('event_occurrence_id') and person in row['data']['line']['who']['conditions'][0]['values']
                                    for row in snapshot['scheduling_facts'])
    assert protected(before) == (bool(blocks) and change in {'role.revoke', 'identity.disable'})
    # Invitations grant access today; they cannot change attendance at an earlier meeting.
    joins = change in {'role.grant', 'identity.enable'} or change == 'account.invite' and not ended
    assert protected(current) == (bool(blocks) and joins)
    changed = protected(current) != protected(before)
    assert (current['fingerprint'] != before['fingerprint']) == changed
    stale = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale']
    assert stale is True if changed else stale in (False, None)
    if not changed:
        return
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Reconcile changed event attendance'}, revision=workspace['revision'])
    if joins:
        assert applied['proposal_retained'] and applied['reconciliation']['status'] == 'unresolved'
        assert h.snapshot(workspace)['shifts'] == before['shifts']
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    else:
        assert applied['reconciliation']['status'] == 'applied'
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'applied'
