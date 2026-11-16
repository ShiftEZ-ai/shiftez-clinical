"""SW3-D-06: a rank or eligibility that has not started can be withdrawn, keeping its history and Undo."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s

REASON = 'Added for the wrong person by mistake'


@pytest.mark.parametrize('kind', ['programme_membership.withdraw', 'programme_membership.restore'])
def test_programme_withdrawal_emits_live_scopes_for_single_and_bulk_commands(kind):
    from apps.api.live import command_changes
    expected = [{'scope': 'people', 'id': None}, {'scope': 'catalogue', 'id': None}]
    assert command_changes(kind) == expected
    assert command_changes('bulk.apply', result={'item_kinds': [kind]}) == expected


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_programme_withdrawal_permission_matches_create_and_update_for_every_role(role, monkeypatch):
    from apps.api.auth import Actor
    from apps.api.commands import command_roles
    actor = Actor('identity', 'department', 'organisation', 'Synthetic', frozenset({role}), None, 'session')
    commands = [s.Command(kind=kind, payload={'catalogue': 'programme_memberships'})
                for kind in ('catalogue.create', 'catalogue.update')]
    commands += [s.Command(kind=kind) for kind in ('programme_membership.withdraw', 'programme_membership.restore')]
    for allowed in (s.PERMISSIONS['catalogue.programmes'], ('auditor',)):
        monkeypatch.setitem(s.PERMISSIONS, 'catalogue.programmes', allowed)
        assert all(command_roles(command) == list(allowed) for command in commands)
        assert all(actor.permits(*command_roles(command)) == actor.permits(*allowed) for command in commands)


@pytest.mark.parametrize('kind,make,role', [
    ('membership.restore', lambda h, day: _rank(h, day), 'coordinator'),
    ('eligibility.restore', lambda h, day: _eligibility(h, day), 'credentialer'),
    ('programme_membership.restore', lambda h, day: _programme(h, day), 'coordinator')])
def test_restore_present_relationship_has_specific_conflict(h, kind, make, role):
    row = make(h, '2028-02-01')
    error = h.command(role, kind, {'reason': REASON}, row['id'], row['revision'], status=409)['error']
    assert error['code'] == 'RELATIONSHIP_NOT_WITHDRAWN'
    assert 'already present' in error['message']


def _rank(h, starts_on):
    return h.command('coordinator', 'membership.create', {'person_id': h.seed['people'][0],
                     'category_id': h.seed['trainee_category'], 'starts_on': starts_on})['record']


def _eligibility(h, starts_on):
    service = h.configure('services', {'name': 'Withdrawal clinic', 'starts_on': '2020-01-01'})['record']
    return h.command('credentialer', 'eligibility.create', {'person_id': h.seed['people'][0],
                     'service_id': service['id'], 'starts_on': starts_on, 'ends_on': None})['record']


def _programme(h, starts_on):
    programme = h.configure('programmes', {'name': 'Withdrawal programme', 'starts_on': '2020-01-01'})['record']
    return h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': programme['id'],
                        'starts_on': starts_on, 'ends_on': '2029-01-01'}, role='coordinator')['record']


def _undo(h, role, result, status=200):
    undo = result['undo']
    return h.command(role, undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'], status=status)


@pytest.mark.parametrize('kind, make, role, cls', [
    ('membership.withdraw', _rank, 'coordinator', m.Membership),
    ('eligibility.withdraw', _eligibility, 'credentialer', m.Eligibility),
    ('programme_membership.withdraw', _programme, 'coordinator', m.ProgrammeMembership)])
def test_upcoming_record_is_withdrawn_with_history_and_undo(h, kind, make, role, cls):
    row = make(h, '2028-07-01')
    with transaction(h.app.state.engine) as db:
        before = db.get(m.Organisation, h.seed['organisation_id']).input_revision
    result = h.command(role, kind, {'reason': REASON}, row['id'], row['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.get(cls, row['id']) is None
        retained = db.get(cls, row['id'], execution_options={'include_withdrawn': True})
        assert retained.withdrawn and retained.revision == 2
        after = db.get(m.Organisation, h.seed['organisation_id']).input_revision
        assert after == before + 1
        reasons = list(db.scalars(select(m.DefinitionHistory.reason).where(m.DefinitionHistory.object_id == row['id'])
                                  .order_by(m.DefinitionHistory.object_revision)))
    assert len(reasons) == 2 and reasons[-1] == REASON
    assert result['record']['withdrawn'] is True
    restored = _undo(h, role, result)['record']
    assert restored['id'] == row['id'] and not restored['withdrawn'] and restored['revision'] == 3
    assert (restored['person_id'], restored['starts_on']) == (row['person_id'], '2028-07-01')
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed['organisation_id']).input_revision == after + 1
    if cls is m.ProgrammeMembership:
        versions = h.get(role, f"/api/catalogues/programme_memberships/{row['id']}/history")['versions']
        assert [entry['snapshot']['withdrawn'] for entry in versions] == [False, True, False]


@pytest.mark.parametrize('kind, make, role, cls', [
    ('membership.withdraw', _rank, 'coordinator', m.Membership),
    ('eligibility.withdraw', _eligibility, 'credentialer', m.Eligibility)])
def test_upcoming_one_day_withdrawal_preserves_frozen_review(h, kind, make, role, cls):
    row = make(h, '2028-02-01')
    row = h.command(role, kind.replace('.withdraw', '.update_dates'),
                    {'ends_on': '2028-02-02', 'reason': REASON}, row['id'], row['revision'])['record']
    assert row['revision'] == 2
    workspace = h.workspace(start='2028-02-01', end='2028-02-08')
    frozen = h.snapshot(workspace)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace['id']).review_snapshot = frozen
    result = h.command(role, kind, {'reason': REASON}, row['id'], row['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.get(cls, row['id']) is None
        retained = db.get(cls, row['id'], execution_options={'include_withdrawn': True})
        assert retained.withdrawn and retained.revision == 3
        assert db.get(m.Workspace, workspace['id']).review_snapshot == frozen
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id'])
                                 .order_by(m.DefinitionHistory.object_revision)))
        assert [entry.object_revision for entry in history] == [1, 2, 3]
        assert history[-1].kind == kind and history[-1].reason == REASON
        assert history[-1].snapshot['starts_on'] == '2028-02-01'
        assert history[-1].snapshot['ends_on'] == '2028-02-02'
    restored = _undo(h, role, result)['record']
    assert restored['id'] == row['id'] and not restored['withdrawn']
    assert {key: restored[key] for key in result['previous_value']} == result['previous_value']


@pytest.mark.parametrize('kind, make, role', [
    ('membership.withdraw', _rank, 'coordinator'), ('eligibility.withdraw', _eligibility, 'credentialer'),
    ('programme_membership.withdraw', _programme, 'coordinator')])
def test_started_record_must_be_ended_not_withdrawn(h, kind, make, role):
    row = make(h, '2026-01-01')
    error = h.command(role, kind, {'reason': REASON}, row['id'], row['revision'], status=409)['error']
    assert error['code'] == 'WITHDRAWAL_ALREADY_STARTED'
    assert error['message'] == 'This record has started; end it instead'


def test_withdrawal_keeps_each_record_family_to_its_own_role(h):
    row = _eligibility(h, '2028-07-01')
    h.command('coordinator', 'eligibility.withdraw', {'reason': REASON}, row['id'], row['revision'], status=403)


def test_admin_can_withdraw_a_rank_and_staff_cannot(h):
    """The gate follows the declared permission ('admin', 'coordinator'); staff are refused."""
    row = _rank(h, '2028-07-01')
    h.command('staff', 'membership.withdraw', {'reason': REASON}, row['id'], row['revision'], status=403)
    h.command('admin', 'membership.withdraw', {'reason': REASON}, row['id'], row['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Membership, row['id']) is None


@pytest.mark.parametrize('role', ['admin', 'coordinator', 'programme_lead'])
def test_programme_withdrawal_uses_existing_edit_scope_and_audits_subject(h, role):
    from .test_residency import lead_of
    row = _programme(h, '2028-02-01')
    lead_of(h, {'id': row['programme_id']})
    result = h.command(role, 'programme_membership.withdraw', {'reason': REASON}, row['id'], row['revision'])
    versions = h.get(role, f"/api/catalogues/programme_memberships/{row['id']}/history")['versions']
    assert len(versions) == 2 and versions[-1]['snapshot']['withdrawn']
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'programme_membership.withdraw'))
        assert event.reason == REASON and event.target_id == row['id']
        assert event.detail['subject']['programme_id'] == row['programme_id']
    assert _undo(h, role, result)['record']['id'] == row['id']


@pytest.mark.parametrize('role', ['staff', 'credentialer', 'approver', 'auditor', 'operator', 'programme_lead'])
def test_programme_withdrawal_refuses_unpermitted_or_unrelated_actor(h, role):
    row = _programme(h, '2028-02-01')
    if role == 'programme_lead':
        from .governance_support import identity
        identity(h, role, [role])
    assert h.command(role, 'programme_membership.withdraw', {'reason': REASON}, row['id'], row['revision'],
                     status=403)['error']['code'] == 'FORBIDDEN'


def test_programme_lead_cannot_withdraw_own_future_membership(h):
    from .test_residency import lead_of
    row = _programme(h, '2028-02-01')
    person_id = lead_of(h, {'id': row['programme_id']})
    own = h.configure('programme_memberships', {'person_id': person_id, 'programme_id': row['programme_id'],
                      'starts_on': '2028-02-01', 'ends_on': '2028-02-02'}, role='coordinator')['record']
    assert h.command('programme_lead', 'programme_membership.withdraw', {'reason': REASON}, own['id'],
                     own['revision'], status=403)['error']['code'] == 'FORBIDDEN'


def test_programme_withdrawal_receipt_and_undo_recheck_current_programme_authority(h):
    from uuid import uuid4
    from .test_residency import lead_of
    row = _programme(h, '2028-02-01')
    person_id = lead_of(h, {'id': row['programme_id']})
    key = str(uuid4())
    result = h.command('programme_lead', 'programme_membership.withdraw', {'reason': REASON},
                       row['id'], row['revision'], key=key)
    assert h.committed('programme_lead', key)['record']['withdrawn']
    membership = next(r for r in h.get('coordinator', '/api/catalogues')['programme_memberships']
                      if r['person_id'] == person_id and r['programme_id'] == row['programme_id'])
    value = {name: membership[name] for name in s.ProgrammeMembershipInput.model_fields}
    h.configure('programme_memberships', {**value, 'ends_on': '2026-10-02'}, membership['id'],
                membership['revision'], role='coordinator')
    h.get('programme_lead', '/api/commands/' + key, status=403)
    h.command('programme_lead', 'programme_membership.withdraw', {'reason': REASON},
              row['id'], row['revision'], key=key, status=403)
    _undo(h, 'programme_lead', result, status=403)


def test_withdrawn_future_programme_membership_never_grants_history_access(h, monkeypatch):
    from datetime import datetime
    from .clock_support import freeze_clock
    from .test_residency import lead_of
    row = _programme(h, '2028-02-01')
    person_id = lead_of(h, {'id': row['programme_id']})
    own = h.configure('programme_memberships', {'person_id': person_id, 'programme_id': row['programme_id'],
                      'starts_on': '2028-02-01', 'ends_on': '2029-02-01'}, role='coordinator')['record']
    h.command('coordinator', 'programme_membership.withdraw', {'reason': REASON}, own['id'], own['revision'])
    h.client('programme_lead')
    freeze_clock(monkeypatch, datetime.fromisoformat('2028-02-02T06:00:00+00:00'))
    # Reauthenticate after moving past both the old membership and the session's expiry.
    h.clients.pop('programme_lead').close()
    h.get('programme_lead', f"/api/catalogues/programme_memberships/{own['id']}/history", status=404)
    h.get('programme_lead', f"/api/catalogues/programme_memberships/{row['id']}/history", status=404)


@pytest.mark.parametrize('family,make,role,cls', [
    ('membership', _rank, 'coordinator', m.Membership),
    ('eligibility', _eligibility, 'credentialer', m.Eligibility),
    ('programme_membership', _programme, 'coordinator', m.ProgrammeMembership)])
def test_withdrawal_and_restore_validate_reason_revision_and_overlaps(h, family, make, role, cls):
    row = make(h, '2028-02-01')
    h.command(role, family + '.withdraw', {}, row['id'], row['revision'], status=422)
    h.command(role, family + '.withdraw', {'reason': REASON}, row['id'], row['revision'] + 1, status=409)
    result = h.command(role, family + '.withdraw', {'reason': REASON}, row['id'], row['revision'])
    assert row['id'] not in {r['id'] for r in h.get(role, '/api/catalogues')[cls.__tablename__]}
    from sqlalchemy import func
    from sqlalchemy.orm import aliased
    with transaction(h.app.state.engine) as db:
        alias = aliased(cls)
        assert db.scalar(select(func.count()).select_from(alias).where(alias.id == row['id'])) == 0
        assert db.scalar(select(cls.__table__.c.withdrawn).where(cls.__table__.c.id == row['id'])) is True
    workspace = h.workspace(start='2028-02-01', end='2028-02-08')
    assert row['id'] not in {r['id'] for r in h.snapshot(workspace)[cls.__tablename__]}
    values = result['previous_value']
    if family == 'programme_membership':
        replacement = h.configure('programme_memberships', values, role=role)['record']
    else:
        replacement = h.command(role, family + '.create', values)['record']
    assert _undo(h, role, result, status=409)['error']['code'] == 'DUPLICATE_RELATIONSHIP'
    with transaction(h.app.state.engine) as db:
        retained = db.get(cls, row['id'], execution_options={'include_withdrawn': True})
        assert retained.withdrawn and retained.revision == result['record']['revision']
    h.command(role, family + '.withdraw', {'reason': REASON}, replacement['id'], replacement['revision'])
    restored = _undo(h, role, result)['record']
    assert restored['id'] == row['id'] and not restored['withdrawn']
    assert _undo(h, role, result, status=409)['error']['code'] == 'STALE_REVISION'


def test_programme_withdrawal_permission_is_the_existing_edit_capability():
    from apps.api.commands import COMMAND_PERMISSIONS, command_roles
    for kind in ('programme_membership.withdraw', 'programme_membership.restore'):
        assert COMMAND_PERMISSIONS[kind] == 'catalogue.programmes'
        assert command_roles(s.Command(kind=kind)) == list(s.PERMISSIONS['catalogue.programmes'])
        assert kind in s.AUDIT_ACTION_CATALOGUE
