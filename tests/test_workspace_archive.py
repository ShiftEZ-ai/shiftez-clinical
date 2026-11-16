"""Rota archive lifecycle: every status is reversible and drafts remain deletable."""

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction


def test_discard_and_restore_a_draft(h):
    w = h.shift(h.workspace('Old draft'))['workspace']
    ids = lambda q='': [x['id'] for x in h.get('coordinator', '/api/workspaces?limit=50&offset=0' + q)['workspaces']]
    w = h.command('coordinator', 'workspace.archive', {'reason': 'Not needed'}, w['id'], w['revision'])['workspace']
    assert w['id'] not in ids() and w['id'] in ids('&archived=true')
    h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', status=409)  # no edits while discarded
    w = h.command('coordinator', 'workspace.restore', {'reason': 'Needed after all'}, w['id'], w['revision'])['workspace']
    assert w['id'] in ids()


def test_submitted_archive_withdraws_review_and_restores_it(h):
    w = h.lifecycle(h.shift(h.workspace('Submitted'))['workspace'], 'submit')['workspace']
    archived = h.command('coordinator', 'workspace.archive', {'reason': 'Withdraw before archive'}, w['id'], w['revision'])['workspace']
    assert archived['archived_at'] and archived['archive_state']['status'] == 'submitted'
    restored = h.command('coordinator', 'workspace.restore', {'reason': 'Restore for review'}, archived['id'], archived['revision'])['workspace']
    assert restored['status'] == 'submitted' and restored['archived_at'] is None


def test_delete_draft_alias_archives_each_december_draft_and_keeps_history(h):
    drafts = [h.workspace(name, '2026-12-01', '2027-01-01')
              for name in ('December proposal A', 'December proposal B')]
    denied = h.command('staff', 'workspace.delete_draft', {'reason': 'Delete December proposal'},
                       drafts[0]['id'], drafts[0]['revision'], status=403)
    assert denied['error']['code'] == 'FORBIDDEN'
    for draft in drafts:
        result = h.command('coordinator', 'workspace.delete_draft',
                           {'reason': 'Delete December proposal'}, draft['id'], draft['revision'])
        assert result['workspace']['archived_at'] is not None
        assert h.get('coordinator', '/api/workspaces/' + draft['id'])['workspace']['archived_at'] is not None

    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.target_id.in_([draft['id'] for draft in drafts]),
            m.AuditEvent.action == 'workspace.delete_draft')))
    assert {event.target_id for event in events} == {draft['id'] for draft in drafts}

    submitted = h.lifecycle(h.shift(h.workspace('December submitted', '2026-12-01', '2027-01-01'),
                                    start='2026-12-01T08:00:00+03:00', end='2026-12-01T12:00:00+03:00')['workspace'], 'submit')['workspace']
    rejected = h.command('coordinator', 'workspace.delete_draft', {'reason': 'Delete submitted December proposal'},
                         submitted['id'], submitted['revision'], status=409)
    assert rejected['error']['code'] == 'WORKFLOW_STATE'
