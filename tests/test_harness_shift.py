"""Session seeds retain restrictions without admitting unrelated command failures."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction


def test_plain_shift_refuses_a_restricted_person_without_writing(h):
    h.policy({'kind': 'restriction'})
    workspace = h.workspace('Strict harness assignment')
    before = h.detail(workspace)
    with pytest.raises(AssertionError, match='A protected scheduling restriction applies'):
        h.shift(workspace)
    assert h.detail(workspace) == before


def test_restricted_seed_uses_bulk_and_retains_full_validation(h, monkeypatch):
    policy = h.policy({'kind': 'restriction'})
    workspace = h.workspace('Restricted harness seed')
    people = h.seed['people'][:2]
    start, end = '2026-10-05T09:00:00+03:00', '2026-10-05T13:00:00+03:00'
    calls = []
    command = h.command

    def record(role, kind, payload, **kwargs):
        calls.append((kind, deepcopy(payload)))
        return command(role, kind, payload, **kwargs)

    monkeypatch.setattr(h, 'command', record)
    result = h.shift(workspace, start=start, end=end, people=people, service=1, location=1, seed_restricted=True)
    assert [kind for kind, _ in calls] == ['shift.create', 'bulk.apply']
    assert calls[0][1]['override_reason']
    bulk = calls[1][1]
    assert bulk['reason'] and len(bulk['items']) == 1
    assert bulk['items'][0]['item_kind'] == 'shift.create'
    assert 'override_reason' not in bulk['items'][0]['payload']
    detail = h.detail(workspace)
    assert result == {'workspace': detail['workspace']}
    assert len(detail['shifts']) == 1
    shift = detail['shifts'][0]
    assert shift['service_ids'] == [h.seed['services'][1]]
    assert shift['location_id'] == h.seed['locations'][1]
    assert {p['person_id'] for p in shift['participants']} == set(people)
    assert all(p['override_reason'] is None for p in shift['participants'])
    check = result['workspace']['validation']
    assert not check['valid'] and check['error_count'] >= len(people)
    findings = [f for f in check['findings'] if f['code'] == 'RESTRICTION' and f['rule_id'] == policy['id']]
    assert {f['person_id'] for f in findings} == set(people)
    assert {f['shift_id'] for f in findings} == {shift['id']}
    with transaction(h.app.state.engine) as db:
        change = db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id'])).one()
        audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'bulk.apply')).one()
        receipt = db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind == 'bulk.apply')).one()
        assert change.reason and audit.reason
        assert receipt.result['undo']['kind'] == 'bulk.apply'
        assert len(receipt.result['undo']['payload']['items']) == 1


def test_unrestricted_opt_in_keeps_the_normal_shift_command_and_full_findings(h):
    result = h.shift(h.workspace('Unrestricted opt-in'), seed_restricted=True)
    assert result['workspace']['validation']['findings'] == []
    with transaction(h.app.state.engine) as db:
        assert db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'shift.create')).one()
        assert db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'bulk.apply')).first() is None


def test_restricted_seed_keeps_non_admissible_unavailability(h):
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {
        'person_id': person, 'kind': 'off_duty',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Synthetic off-duty source',
    })
    result = h.shift(h.workspace('Unavailable harness seed'), people=[person], seed_restricted=True)
    findings = result['workspace']['validation']['findings']
    assert any(f['code'] == 'UNAVAILABLE' and f['person_id'] == person for f in findings)


@pytest.mark.parametrize('seed_restricted', [False, True])
def test_malformed_seed_still_fails_without_writing(h, seed_restricted):
    workspace = h.workspace('Malformed harness seed')
    before = h.detail(workspace)
    with pytest.raises(AssertionError, match='SCHEMA_INVALID'):
        h.shift(workspace, end='2026-10-04T08:00:00+03:00', seed_restricted=seed_restricted)
    assert h.detail(workspace) == before


@pytest.mark.parametrize('seed_restricted', [False, True])
def test_expected_restriction_refusal_is_returned_without_seeding(h, seed_restricted):
    h.policy({'kind': 'restriction'})
    workspace = h.workspace('Expected restriction refusal')
    before = h.detail(workspace)
    error = h.shift(workspace, status=422, seed_restricted=seed_restricted)['error']
    assert error['code'] == 'EDIT_INVALID'
    assert error['details']['findings']
    assert all(f['code'] == 'RESTRICTION' for f in error['details']['findings'])
    assert h.detail(workspace) == before


@pytest.mark.parametrize('error', [
    {'code': 'EDIT_INVALID', 'details': {'findings': [{'code': 'MIN_REST'}]}},
    {'code': 'EDIT_INVALID', 'details': {'findings': [{'code': 'RESTRICTION'}, {'code': 'MIN_REST'}]}},
    {'code': 'EDIT_INVALID', 'details': {'findings': []}},
    {'code': 'SCHEMA_INVALID', 'details': {'findings': [{'code': 'RESTRICTION'}]}},
], ids=['other-finding', 'mixed-findings', 'empty-findings', 'other-error'])
def test_unrelated_refusal_replies_fail_without_seeding(h, monkeypatch, error):
    workspace = h.workspace('Refused harness seed')
    reply = {'error': error}
    calls = []

    def command(role, kind, *args, **kwargs):
        calls.append(kind)
        assert kwargs['status'] == (200, 422)
        return reply

    monkeypatch.setattr(h, 'command', command)
    with pytest.raises(AssertionError) as raised:
        h.shift(workspace, seed_restricted=True)
    assert str(raised.value).startswith(json.dumps(reply))
    assert calls == ['shift.create']


def test_unrelated_refusals_preserve_the_original_assertion(h, monkeypatch):
    workspace = h.workspace('Refused harness seed')
    refusal = AssertionError('Unexpected response')
    calls = []

    def command(role, kind, *args, **kwargs):
        calls.append(kind)
        raise refusal

    monkeypatch.setattr(h, 'command', command)
    with pytest.raises(AssertionError) as raised:
        h.shift(workspace, seed_restricted=True)
    assert raised.value is refusal
    assert calls == ['shift.create']
