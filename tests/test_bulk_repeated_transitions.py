"""A repeated lifecycle decision must never make its inverse grant new authority."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_bulk import preview
from .test_bulk_reverse_transitions import REASON, role_binding, transition
from .test_bulk_single_commands import relationship


def one_item(kind, row, payload=None):
    return {'item_kind': kind, 'items': [{'target_id': row['id'], 'expected_revision': row['revision'],
            'payload': payload or REASON}], **REASON}


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('kind', ['grant.revoke', 'role.revoke'])
def test_repeated_revocation_refuses_single_and_bulk_without_changing_history(request, database, kind):
    h = request.getfixturevalue(database)
    if kind == 'grant.revoke':
        row, _, role = relationship(h, 'grant')
        model, payload = m.CapabilityGrant, {**REASON, 'ends_on': '2026-10-15'}
    else:
        row, role, model, payload = role_binding(h), 'admin', m.RoleBinding, REASON
    revoked = transition(h, role, kind, row, payload=payload)['record']
    payload = {**payload, **({'starts_on': '2026-09-01', 'ends_on': None} if kind == 'grant.revoke' else {})}
    with transaction(h.app.state.engine) as db:
        before = record(db.get(model, row['id']))
        versions = [record(v) for v in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id == row['id']).order_by(m.DefinitionHistory.id))]
    single = transition(h, role, kind, revoked, payload=payload, status=409)
    assert single['error']['code'] == 'WORKFLOW_STATE'
    check = preview(h, role, one_item(kind, revoked, payload))
    assert check['counts']['problem'] == 1
    assert check['items'][0]['issues'][0]['message'] == single['error']['message']
    refused = h.command(role, 'bulk.apply', one_item(kind, revoked, payload), status=409)
    assert refused['error']['code'] == 'WORKFLOW_STATE' and refused['error']['details']['failed_index'] == 0
    with transaction(h.app.state.engine) as db:
        assert record(db.get(model, row['id'])) == before
        assert [record(v) for v in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id == row['id']).order_by(m.DefinitionHistory.id))] == versions


@pytest.mark.parametrize('kind', ['grant.revoke', 'role.revoke'])
def test_bulk_repeat_revocation_rolls_back_the_first_change_and_undo_only_restores_its_own_change(h, kind):
    if kind == 'grant.revoke':
        row, _, role = relationship(h, 'grant')
        model, payload = m.CapabilityGrant, {**REASON, 'ends_on': '2026-10-15'}
    else:
        row, role, model, payload = role_binding(h), 'admin', m.RoleBinding, REASON
    batch = one_item(kind, row, payload)
    refused = h.command(role, 'bulk.apply', {**batch, 'items': batch['items'] * 2}, status=409)
    assert refused['error']['code'] == 'WORKFLOW_STATE' and refused['error']['details']['failed_index'] == 1
    with transaction(h.app.state.engine) as db:
        assert not db.get(model, row['id']).revoked
        assert db.get(model, row['id']).revision == row['revision']
    changed = h.command(role, 'bulk.apply', batch)
    restored = h.command(role, **changed['undo'])
    with transaction(h.app.state.engine) as db:
        assert not db.get(model, row['id']).revoked
    h.command(role, **restored['undo'])
    with transaction(h.app.state.engine) as db:
        assert db.get(model, row['id']).revoked


DECISION_ACTIONS = {'activate', 'decline', 'retire', 'revoke', 'restore', 'restore_proposal',
                    'disable', 'enable', 'withdraw', 'end', 'retract'}
DATE_ENDINGS = {'membership.end', 'eligibility.end'}
DECISIONS = [kind for kind in s.BULK_ITEM_KINDS
             if (kind.rsplit('.', 1)[-1] in DECISION_ACTIONS and kind not in DATE_ENDINGS)
             or kind in {'request.decide', 'availability_pattern.decide', 'exception.approve'}]


@pytest.mark.parametrize('kind', DECISIONS)
def test_registry_decisions_refuse_repeated_state_transitions(h, kind):
    from apps.api.services.bulk import _model
    from .test_bulk_equivalence import case
    role, command = case(h, kind)
    h.command(role, kind, command.payload, command.target_id, command.expected_revision)
    model = _model(kind, command.payload)
    with transaction(h.app.state.engine) as db:
        before = record(db.get(model, command.target_id))
    result = h.command(role, 'bulk.apply', one_item(kind, before, command.payload), status=409)
    assert result['error']['code'] in {'WORKFLOW_STATE', 'IDENTITY_STATE'}
    with transaction(h.app.state.engine) as db:
        assert record(db.get(model, before['id'])) == before


@pytest.mark.parametrize('kind', sorted(DATE_ENDINGS))
def test_repeated_dated_end_undo_keeps_the_previously_ended_date(h, kind):
    row, _, role = relationship(h, kind.split('.')[0])
    ended = transition(h, role, kind, row, payload={**REASON, 'ends_on': '2026-10-15'})['record']
    changed = h.command(role, 'bulk.apply', one_item(kind, ended, {**REASON, 'ends_on': ended['ends_on']}))
    h.command(role, **changed['undo'])
    with transaction(h.app.state.engine) as db:
        model = m.Membership if kind == 'membership.end' else m.Eligibility
        assert db.get(model, row['id']).ends_on.isoformat() == ended['ends_on']


@pytest.mark.parametrize('action', ['cancel', 'reopen'])
def test_repeated_session_action_undo_keeps_its_previous_state_and_reason(h, action):
    created = h.shift(h.workspace())
    workspace, shift = created['workspace'], created['shifts'][0]
    payload = {'workspace_id': workspace['id'], 'shift_ids': [shift['id']], 'action': action}
    workspace = h.command('coordinator', 'shift.action', {**payload, **REASON},
                          workspace['id'], workspace['revision'])['workspace']
    with transaction(h.app.state.engine) as db:
        before = record(db.get(m.Shift, shift['id']))
    applied = h.command('coordinator', 'bulk.apply', one_item('shift.action', workspace,
        {**payload, 'reason': 'Repeated session action with another recorded reason'}))
    h.command('coordinator', **applied['undo'])
    with transaction(h.app.state.engine) as db:
        restored = record(db.get(m.Shift, shift['id']))
        assert all(restored[key] == before[key] for key in ('cancelled', 'cancellation_reason'))


def test_bulk_availability_update_then_request_undo_restores_the_same_absence(h):
    from .governance_support import identity
    identity(h, 'mixed_decider', ['coordinator', 'approver'])
    request = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave',
        'start': '2026-11-01T00:00:00+03:00', 'end': '2026-11-02T00:00:00+03:00',
        'private_reason': 'Synthetic private request'})['record']
    request = transition(h, 'mixed_decider', 'request.decide', request,
                         payload={**REASON, 'decision': 'approve'})['record']
    with transaction(h.app.state.engine) as db:
        absence = record(db.scalar(select(m.Availability).where(m.Availability.request_id == request['id'])))
    applied = h.command('mixed_decider', 'bulk.apply', {'items': [
        {'item_kind': 'availability.update', 'target_id': absence['id'], 'expected_revision': absence['revision'],
         'payload': {'start': '2026-11-01T01:00:00+03:00', **REASON}},
        {'item_kind': 'request.undo', 'target_id': request['id'], 'expected_revision': request['revision'],
         'payload': REASON}], **REASON})
    h.command('mixed_decider', **applied['undo'])
    with transaction(h.app.state.engine) as db:
        restored = db.get(m.Availability, absence['id'])
        assert not restored.retracted and record(restored)['start'] == absence['start']
        assert len(list(db.scalars(select(m.Availability).where(m.Availability.request_id == request['id'])))) == 1
        assert db.get(m.RequestRecord, request['id']).status == 'approved'
