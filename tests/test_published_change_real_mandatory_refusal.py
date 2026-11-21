"""Mandatory restrictions on changed duties bind amendment review and publication."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning

from .test_published_changes import change, issued


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('activation', ['submit', 'approve', 'publish'])
def test_published_change_refuses_a_real_mandatory_restriction(h, activation, request):
    publication, shifts = issued(h)
    result = change(h, publication, shifts[0])
    person = h.seed['people'][2]
    assert result['validation']['valid']

    error = None
    for action in ('submit', 'approve', 'publish'):
        workspace = result['workspace']
        if action == activation:
            rule = h.policy({'kind': 'restriction'}, {'person_id': person})
            assert rule['status'] == 'active'
            session, = result['change_diff']['sessions']
            changed = session['after']
            assert [row['person_id'] for row in changed['participants']] == [person]
            # Verify the actual change is unsafe without refreshing its stored review evidence.
            with transaction(h.app.state.engine) as db:
                row = db.get(m.Workspace, workspace['id'])
                check = planning.revalidate(db, row, store=False, mandatory_rules=False, current_mandatory=True)
            finding, = [f for f in check['change_findings'] if f['code'] == 'RESTRICTION'
                        and f['person_id'] == person and f['shift_id'] == changed['id']]
            assert finding['severity'] == 'error' and finding['rule_id'] == rule['id']
            request.node.user_properties.append(('mandatory_finding', finding['code']))

        status = (200, 409, 422)
        if action == 'submit':
            result = h.command('coordinator', 'publication.changes.submit', {'reason': 'Review published cover'},
                               target=publication, revision=workspace['revision'], status=status)
        else:
            result = h.lifecycle(workspace, action, 'approver' if action == 'approve' else 'coordinator', status=status)
        error = result.get('error')
        request.node.user_properties.append((action, error['code'] if error else 'accepted'))
        if error:
            break

    publications = h.get('coordinator', '/api/publications')['publications']
    request.node.user_properties.append(('publication_count', len(publications)))
    assert [row['id'] for row in publications] == [publication], 'A restricted amendment was published'
    assert error is not None, 'The restricted amendment passed every gate'
    assert h.detail(workspace)['workspace']['status'] == workspace['status']
    if error['code'] == 'STALE_INPUTS':
        assert activation == action == 'approve'
    else:
        assert error['code'] == 'VALIDATION_FAILED'
        assert any(f['code'] == 'RESTRICTION' and f['rule_id'] == rule['id'] and f['person_id'] == person
                   for f in error['details']['findings'])
