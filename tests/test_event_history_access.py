"""Event history uses the same access boundary as complete event definitions."""
import pytest

from packages.contracts import schemas as s
from tests.governance_support import identity
from tests.test_events import event, event_type, input_of


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_event_definition_and_history_share_editor_access(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    row = event(h, event_type(h, default_audience={'roles': ['coordinator']}),
        format='online', place='', url='https://example.test/private-meeting')
    edited = h.configure('events', input_of(row, heading='Updated private agenda'),
        row['id'], row['revision'], role='coordinator')['record']
    allowed = role in {'admin', 'coordinator'}
    status = 200 if allowed else 403
    definition = h.get(role, f"/api/events/definitions/{row['id']}", status=status)
    history = h.get(role, f"/api/catalogues/events/{row['id']}/history", status=status)
    if allowed:
        assert definition['record']['heading'] == edited['heading']
        assert [version['snapshot']['heading'] for version in history['versions']] == [row['heading'], edited['heading']]
        assert all(version['snapshot']['url'] == row['url'] for version in history['versions'])
    else:
        assert history['error']['code'] == definition['error']['code'] == 'FORBIDDEN'
    h.get(role, f"/api/catalogues/event_occurrence/{row['id']}/history", status=404 if allowed else 403)


@pytest.mark.parametrize('role', ['approver', 'credentialer', 'auditor', 'programme_lead'])
def test_event_history_restriction_preserves_other_catalogue_history(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    typ = event_type(h)
    history = h.get(role, f"/api/catalogues/event_types/{typ['id']}/history")['versions']
    assert [version['snapshot']['name'] for version in history] == [typ['name']]
