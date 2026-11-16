"""Review sends a rota back with a reason; a disabled sign-in can be enabled again. Both are audited."""
from apps.worker.main import process_one


def test_approver_returns_a_submitted_rota_to_the_coordinator(h):
    w = h.lifecycle(h.shift(h.workspace('Returned rota'))['workspace'], 'submit')['workspace']
    # A reason is required, and only a rota in review can be returned.
    h.command('approver', 'workspace.reopen', {'reason': ''}, w['id'], w['revision'], status=422)
    h.command('staff', 'workspace.reopen', {'reason': 'Not mine to return'}, w['id'], w['revision'], status=403)
    back = h.command('approver', 'workspace.reopen', {'reason': 'Clinic B is short on Tuesday'}, w['id'], w['revision'])['workspace']
    assert back['status'] == 'draft'
    assert h.lifecycle(back, 'reopen', 'approver', status=409)['error']['code'] == 'WORKFLOW_STATE'
    event = next(e for e in h.get('auditor', '/api/audit')['events'] if e['action'] == 'workspace.reopen')
    assert event['reason'] == 'Clinic B is short on Tuesday' and event['actor_id'] == h.seed['identities']['approver']
    for _ in range(5):
        process_one(h.app.state.engine)
    note = next(n for n in h.get('coordinator', '/api/notifications')['notifications'] if 'returned to the coordinator' in n['title'])
    assert 'Clinic B is short on Tuesday' in note['message']
    # The return stays on the draft (who, when, why) until it is submitted again.
    stored = h.detail(back)['workspace']['return_note']
    assert stored['reason'] == 'Clinic B is short on Tuesday' and stored['by'] == h.seed['identities']['approver']
    assert stored['by_name'] and stored['at']
    assert h.lifecycle(back, 'submit')['workspace']['return_note'] is None


def test_a_coordinator_reopening_their_own_rota_leaves_no_return_note(h):
    w = h.lifecycle(h.shift(h.workspace('Withdrawn rota'))['workspace'], 'submit')['workspace']
    assert h.lifecycle(w, 'reopen')['workspace']['return_note'] is None


def test_approver_cannot_return_an_approved_rota(h):
    w = h.lifecycle(h.shift(h.workspace('Approved rota'))['workspace'], 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    assert h.lifecycle(w, 'reopen', 'approver', status=409)['error']['code'] == 'WORKFLOW_STATE'
    assert h.lifecycle(w, 'reopen')['workspace']['status'] == 'draft'  # the coordinator still can


def test_enable_sign_in_undoes_disable(h):
    staff = h.seed['identities']['staff']
    h.get('staff', '/api/me')  # signed in before the change
    row = h.command('admin', 'identity.disable', {'reason': 'Left the department'}, staff, 1)['record']
    assert row['disabled'] is True
    h.get('staff', '/api/me', status=401)
    assert h.command('admin', 'identity.disable', {'reason': 'Again'}, staff, row['revision'], status=409)['error']['code'] == 'IDENTITY_STATE'
    h.command('coordinator', 'identity.enable', {'reason': 'Back from leave'}, staff, row['revision'], status=403)
    row = h.command('admin', 'identity.enable', {'reason': 'Back from leave'}, staff, row['revision'])['record']
    assert row['disabled'] is False
    assert h.get('staff', '/api/me')['identity_id'] == staff
    assert any(e['action'] == 'identity.enable' and e['reason'] == 'Back from leave' for e in h.get('auditor', '/api/audit')['events'])
