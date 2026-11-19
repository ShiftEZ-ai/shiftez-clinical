"""Deciding your own work: refused by default, allowed only under an active permission, always with a reason and a trail."""
from datetime import date, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.independence import today
from apps.worker.main import process_one
from packages.contracts.schemas import INDEPENDENT_ACTIONS, ROLE_NAMES
from .governance_support import identity


def rule(h, who):
    return h.command(who, 'policy.create', {'name': 'Own rest rule', 'owner': 'QA', 'source': 'Self-approval test', 'starts_on': '2020-01-01',
                     'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']


def grant(h, actions=('rule.activate', 'rota.approve'), **subject):
    return h.command('admin', 'self_approval.grant', {'actions': list(actions), 'starts_on': '2026-01-01', 'ends_on': '2027-01-01',
                     'reason': 'Only one approver this quarter', **subject})['permission']


def test_own_rule_needs_a_permission_and_a_real_reason_and_leaves_a_trail(h):
    identity(h, 'both', ['coordinator', 'approver'])
    proposal = rule(h, 'both')
    refused = h.command('both', 'policy.activate', {'reason': 'Looks right'}, proposal['id'], proposal['revision'], status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    permission = grant(h, identity_id=h.seed['identities']['both'])
    assert permission['ends_on'] == '2027-01-01'
    assert set(h.get('both', '/api/me')['self_approval']) == {'rule.activate', 'rota.approve'}
    # The screens' default note is not a reason.
    assert h.command('both', 'policy.activate', {'reason': 'Routine update'}, proposal['id'], proposal['revision'], status=422)['error']['code'] == 'SELF_APPROVAL_REASON'
    h.command('both', 'policy.activate', {'reason': 'Reviewed with the clinical lead by phone'}, proposal['id'], proposal['revision'])
    event = next(e for e in h.get('auditor', '/api/audit')['events'] if e['action'] == 'policy.activate')
    assert event['detail']['self_approved'][0] == {'action': 'rule.activate', 'permission_id': permission['id']}
    assert [e['id'] for e in h.get('auditor', '/api/audit?self_approved=true')['events']] == [event['id']]
    assert ',"yes",' in h.client('auditor').get('/api/audit.csv?self_approved=true').text
    # Approvers and auditors are told afterwards; the person who acted is not.
    for _ in range(10):
        process_one(h.app.state.engine)
    note = next(n for n in h.get('approver', '/api/notifications')['notifications'] if 'decided their own work' in n['title'])
    assert 'phone' not in note['message']  # the reason can be private; it stays in the decision history
    assert not any('decided their own work' in n['title'] for n in h.get('both', '/api/notifications')['notifications'])
    # Ending the permission restores independent review.
    h.command('admin', 'self_approval.revoke', {'reason': 'Second approver back'}, permission['id'], permission['revision'])
    second = rule(h, 'both')
    h.command('both', 'policy.activate', {'reason': 'Reviewed again'}, second['id'], second['revision'], status=403)


def test_role_permission_lets_a_builder_approve_and_publish_their_own_rota(h):
    identity(h, 'solo', ['coordinator', 'approver'])
    own = h.command('solo', 'workspace.create', {'name': 'Solo built', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['workspace']
    own = h.shift(own)['workspace']
    own = h.command('solo', 'workspace.submit', {'reason': 'Ready'}, own['id'], own['revision'])['workspace']
    assert h.command('solo', 'workspace.approve', {'reason': 'Checked it'}, own['id'], own['revision'], status=403)['error']['code'] == 'SELF_APPROVAL'
    permission = grant(h, role='approver')  # everyone holding the approver role
    own = h.command('solo', 'workspace.approve', {'reason': 'Checked the rest gaps myself'}, own['id'], own['revision'])['workspace']
    assert own['status'] == 'approved'
    # The approval stays valid at publication even after the permission ends.
    h.command('admin', 'self_approval.revoke', {'reason': 'Cover over'}, permission['id'], permission['revision'])
    assert h.command('solo', 'workspace.publish', {'reason': 'Publish'}, own['id'], own['revision'])['workspace']['status'] == 'published'


def rota_review_access(h, *, second_reviewer=False, permission_state=None):
    operator = h.seed['identities']['coordinator']
    enabled = {operator, h.seed['identities']['approver']} if second_reviewer else {operator}
    department = h.seed['department_id']
    with transaction(h.app.state.engine, write=True) as db:
        for ident in h.seed['identities'].values():
            if ident not in enabled:
                db.get(m.Identity, ident).disabled = True
        held = set(db.scalars(select(m.RoleBinding.role).where(
            m.RoleBinding.identity_id == operator, m.RoleBinding.department_id == department)))
        for role in set(ROLE_NAMES) - held:
            db.add(m.RoleBinding(identity_id=operator, department_id=department, role=role, starts_on=date(2020, 1, 1)))
        day = today(db, department)
    if permission_state:
        end = day if permission_state == 'expired' else day + timedelta(days=1)
        permission = h.command('coordinator', 'self_approval.grant', {
            'identity_id': operator, 'actions': ['rota.approve'], 'starts_on': (day - timedelta(days=1)).isoformat(),
            'ends_on': end.isoformat(), 'reason': 'Temporary cover for rota review',
        })['permission']
        assert permission['ends_on'] == end.isoformat()
        return permission['id']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('second_reviewer,permission_state', [
    (False, 'active'), (False, None), (False, 'expired'), (True, None), (True, 'active'),
], ids=['sole-current', 'sole-missing', 'sole-expired', 'peer-missing', 'peer-current'])
def test_rota_item_approval_uses_the_same_permission_as_the_rota(h, second_reviewer, permission_state):
    quota = {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'metric': 'duties'}
    h.policy({**quota, 'comparison': 'minimum', 'amount': 3}, {'person_id': h.seed['people'][0]})
    target = h.policy({**quota, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'comparison': 'target',
                       'amount': 4, 'prorate_department_days': True}, severity='advisory')
    permission_id = rota_review_access(h, second_reviewer=second_reviewer, permission_state=permission_state)
    w = h.shift(h.workspace())['workspace']
    w = h.command('coordinator', 'workspace.target_override', {
        'policy_id': target['id'], 'person_id': h.seed['people'][1], 'amount': 2, 'reason': 'Adjust the planned target',
    }, w['id'], w['revision'])['workspace']
    cause, = [c for c in h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes'] if c['acknowledgeable']]
    submitted = h.command('coordinator', 'workspace.submit', {
        'reason': 'Submit for review', 'acknowledgements': [{'cause_id': cause['id'], 'reason': 'Acknowledge the shortfall'}],
    }, w['id'], w['revision'])
    w = submitted['workspace']
    items = {item['kind']: item['id'] for item in submitted['review_items']}
    assert set(items) == {'target_override', 'acknowledged_gap'}
    decisions = [
        {'item_id': items['target_override'], 'kind': 'adjustment', 'accept': True, 'reason': 'Target reviewed'},
        {'item_id': items['acknowledged_gap'], 'kind': 'gap', 'accept': True, 'reason': 'Shortfall reviewed'},
    ]
    reason = 'Reviewed the adjustment and the shortfall'
    path = f"/api/workspaces/{w['id']}"
    can_self_approve = permission_state == 'active'
    for view in (path, path + '?view=compact'):
        assert h.get('coordinator', view)['review_progress']['can_decide'] is can_self_approve
    if not can_self_approve:
        refused = h.command('coordinator', 'workspace.approve', {'reason': reason, 'rejected_review_item_ids': [cause['id']]},
                            w['id'], w['revision'], status=403)
        assert refused['error']['code'] == 'SELF_APPROVAL'
        refused = h.command('coordinator', 'workspace.approve', {'reason': reason, 'decisions': decisions},
                            w['id'], w['revision'], status=403)
        assert refused['error']['code'] == 'SELF_APPROVAL'
        assert refused['error']['message'] == 'Rota review items require an independent approver'
        assert h.detail(w)['workspace']['status'] == 'submitted'
        if second_reviewer:
            assert h.get('approver', path)['review_progress']['can_decide']
            approved = h.command('approver', 'workspace.approve', {'decisions': decisions}, w['id'], w['revision'])
            assert approved['workspace']['status'] == 'approved'
            assert [(row.get('self_approval_id'), row['reason']) for row in approved['review_item_decisions']] == [
                (None, choice['reason']) for choice in decisions]
        return
    refused = h.command('coordinator', 'workspace.approve', {'decisions': decisions}, w['id'], w['revision'], status=422)
    assert refused['error']['code'] == 'SELF_APPROVAL_REASON'
    approved = h.command('coordinator', 'workspace.approve', {'reason': reason, 'decisions': decisions},
                         w['id'], w['revision'])
    assert approved['workspace']['status'] == 'approved'
    rows = approved['review_item_decisions']
    assert {row['item_id']: (row['reason'], row['self_approval_id']) for row in rows} == {
        choice['item_id']: (choice['reason'], permission_id) for choice in decisions}
    detail = h.detail(w)
    assert detail['workspace']['validation']['review_item_decisions'] == rows
    reviewer, = detail['review_progress']['steps'][0]['reviewers']
    assert (reviewer['self_approval_id'], reviewer['reason']) == (permission_id, reason)
    gap, = approved['gap_acknowledgements']
    gap_decision, = gap['decisions']
    assert (gap_decision['self_approval_id'], gap_decision['reason'], gap_decision['review_decision_id']) == (
        permission_id, decisions[1]['reason'], reviewer['id'])
    event, = (e for e in h.get('coordinator', '/api/audit?self_approved=true')['events'] if e['action'] == 'workspace.approve')
    assert event['target_id'] == w['id'] and event['reason'] == reason
    assert event['detail']['self_approved'] == [{'action': 'rota.approve', 'permission_id': permission_id}]
    assert event['detail']['review_item_decisions'] == rows
    assert event['detail']['gap_acknowledgements'] == [gap]
    with transaction(h.app.state.engine) as db:
        announcements = list(db.scalars(select(m.Outbox.payload).where(m.Outbox.kind == 'self_approval')))
    assert [notice for notice in announcements if notice['action'] == 'rota.approve'] == [
        {'action': 'rota.approve', 'target_id': w['id'], 'actor_id': reviewer['approved_by']}]
    assert h.lifecycle(approved['workspace'], 'publish')['workspace']['status'] == 'published'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_rota_item_rejection_uses_the_same_permission_as_approval(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 3}, {'person_id': person})
    permission_id = rota_review_access(h, permission_state='active')
    w = h.shift(h.workspace(), people=[person])['workspace']
    cause, = [c for c in h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes'] if c['acknowledgeable']]
    w = h.command('coordinator', 'workspace.submit', {
        'reason': 'Submit for review', 'acknowledgements': [{'cause_id': cause['id'], 'reason': 'Acknowledge the shortfall'}],
    }, w['id'], w['revision'])['workspace']
    decisions = [{'item_id': cause['id'], 'kind': 'gap', 'accept': False, 'reason': 'Fill the remaining duties'}]
    refused = h.command('coordinator', 'workspace.approve', {'decisions': decisions}, w['id'], w['revision'], status=422)
    assert refused['error']['code'] == 'SELF_APPROVAL_REASON'
    reason = 'Return this rota for revision'
    returned = h.command('coordinator', 'workspace.approve', {'reason': reason, 'decisions': decisions}, w['id'], w['revision'])
    assert returned['workspace']['status'] == 'draft' and returned['workspace']['return_note']['reason'] == reason
    assert [item['id'] for item in returned['rejected_review_items']] == [cause['id']]
    assert returned['gap_acknowledgements'] == []
    detail = h.detail(w)
    assert detail['workspace']['status'] == 'draft' and not detail['approvals']
    event, = (e for e in h.get('coordinator', '/api/audit?self_approved=true')['events'] if e['action'] == 'workspace.approve')
    assert event['reason'] == reason
    assert event['detail']['self_approved'] == [{'action': 'rota.approve', 'permission_id': permission_id}]
    assert event['detail']['review_item_decisions'][0]['reason'] == decisions[0]['reason']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_rota_self_approval_grant_cannot_pass_safety_findings(h):
    duty_type = h.configure('assignment_types', {'name': 'Two-person duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']
    assert rota_review_access(h, permission_state='active')
    w = h.shift(h.workspace(), typ=duty_type['id'])['workspace']
    fix_list = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")
    cause, = [c for c in fix_list['causes'] if c['safety']]
    assert not cause['acknowledgeable']
    refused = h.command('coordinator', 'workspace.submit', {
        'reason': 'Request safety review', 'acknowledgements': [{'cause_id': cause['id'], 'reason': 'Attempt to accept'}],
    }, w['id'], w['revision'], status=409)
    assert refused['error']['code'] == 'SAFETY_RULE'
    refused = h.lifecycle(w, 'submit', status=422)
    assert refused['error']['code'] == 'VALIDATION_FAILED'
    assert h.detail(w)['workspace']['status'] == 'draft'


def test_permission_input_is_validated_and_actions_come_from_one_list(h):
    listing = h.get('approver', '/api/self-approval')
    assert [a['key'] for a in listing['actions']] == list(INDEPENDENT_ACTIONS)
    assert set(INDEPENDENT_ACTIONS) == {'rota.approve', 'rule.activate', 'exception.approve',
                                        'request.decide', 'route.decide', 'skills.self'}
    base = {'actions': ['rule.activate'], 'starts_on': '2026-10-01', 'ends_on': '2027-01-01', 'reason': 'Validation check'}
    h.command('admin', 'self_approval.grant', {**base, 'identity_id': h.seed['identities']['staff'], 'role': 'approver'}, status=422)
    h.command('admin', 'self_approval.grant', base, status=422)
    h.command('admin', 'self_approval.grant', {**base, 'role': 'approver', 'actions': ['publish.anything']}, status=422)
    h.command('admin', 'self_approval.grant', {**base, 'role': 'admin', 'actions': ['self_approval.grant']}, status=422)
    h.command('admin', 'self_approval.grant', {**base, 'role': 'approver', 'ends_on': '2026-09-01'}, status=422)
    h.command('coordinator', 'self_approval.grant', {**base, 'role': 'approver'}, status=403)
    upcoming = h.command('admin', 'self_approval.grant', {**base, 'role': 'approver', 'actions': ['rota.approve'],
                         'starts_on': '2099-01-01', 'ends_on': '2100-01-01'})['permission']
    assert next(p for p in h.get('admin', '/api/self-approval')['permissions'] if p['id'] == upcoming['id'])['state'] == 'upcoming'


@pytest.mark.parametrize('subject', ['identity_id', 'role'])
@pytest.mark.parametrize('ends_on', [None, '2099-01-01'])
def test_self_grant_is_refused_with_another_current_admin_even_with_permission(h, subject, ends_on):
    grant(h, identity_id=h.seed['identities']['admin'])
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'admin',
        'starts_on': '2020-01-01', 'ends_on': ends_on, 'reason': 'Second administrator available'})
    before = h.get('admin', '/api/self-approval')['permissions']
    payload = {subject: h.seed['identities']['admin'] if subject == 'identity_id' else 'admin',
               'actions': ['rule.activate'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Explicit self-grant request'}
    refused = h.command('admin', 'self_approval.grant', payload, status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    assert refused['error']['message'] == 'Ask another admin to grant this'
    assert h.get('admin', '/api/self-approval')['permissions'] == before
    assert len(h.get('auditor', '/api/audit?self_approved=true')['events']) == 1
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'self_approval')))) == 1


@pytest.mark.parametrize('subject', ['identity_id', 'role'])
def test_only_admin_self_grant_is_announced_and_marked(h, subject):
    permission = grant(h, **{subject: h.seed['identities']['admin'] if subject == 'identity_id' else 'admin'},
                       starts_on='2099-01-01', ends_on='2100-01-01')
    event, = h.get('auditor', '/api/audit?self_approved=true')['events']
    assert event['action'] == 'self_approval.grant'
    assert event['detail']['self_approved'] == [{'action': 'self_approval.grant', 'permission_id': None}]
    with transaction(h.app.state.engine) as db:
        note, = db.scalars(select(m.Outbox).where(m.Outbox.kind == 'self_approval'))
        assert note.payload == {'action': 'self_approval.grant', 'target_id': permission['id'],
                                'actor_id': h.seed['identities']['admin']}
    process_one(h.app.state.engine)
    for recipient in ('approver', 'auditor'):
        note, = h.get(recipient, '/api/notifications')['notifications']
        assert 'decided their own work' in note['title']
        assert 'Grant self-approval permissions covering themselves' in note['message']
        assert 'allowed because no other admin is available' in note['message']
        assert permission['reason'] not in note['message']
    assert not h.get('admin', '/api/notifications')['notifications']


@pytest.mark.parametrize('reason', ['Routine update', 'Routine rota check'])
def test_only_admin_self_grant_requires_a_real_reason(h, reason):
    refused = h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities']['admin'],
        'actions': ['rule.activate'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': reason}, status=422)
    assert refused['error']['code'] == 'SELF_APPROVAL_REASON'
    assert not h.get('admin', '/api/self-approval')['permissions']
    assert not h.get('auditor', '/api/audit?self_approved=true')['events']
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'self_approval')))


def test_disabled_admin_does_not_prevent_self_grant_bootstrap(h):
    second = identity(h, 'disabled_admin', ['admin'])
    h.command('admin', 'identity.disable', {'reason': 'Second admin has left'}, second, 1)
    grant(h, identity_id=h.seed['identities']['admin'])
    assert len(h.get('auditor', '/api/audit?self_approved=true')['events']) == 1


def test_grants_for_other_people_are_unchanged_with_another_admin_present(h):
    identity(h, 'second_admin', ['admin'])
    permission = grant(h, ['rota.approve'], identity_id=h.seed['identities']['staff'], reason='Routine update')
    assert permission['identity_id'] == h.seed['identities']['staff']
    assert set(h.get('staff', '/api/me')['self_approval']) == {'rota.approve'}
    event, = (e for e in h.get('auditor', '/api/audit')['events'] if e['action'] == 'self_approval.grant')
    assert event['action'] == 'self_approval.grant' and 'self_approved' not in event['detail']
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'self_approval')))


def test_role_grants_and_use_match_held_roles_not_implied_admin_roles(h):
    identity(h, 'second_admin', ['admin'])
    identity(h, 'dual', ['coordinator', 'approver'])  # an approver who can propose rules makes the role grant usable
    permission = grant(h, role='approver', reason='Routine update')
    assert not h.get('auditor', '/api/audit?self_approved=true')['events']
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'self_approval')))
    proposals = {}
    for who in ('admin', 'second_admin'):
        assert not h.get(who, '/api/me')['self_approval']
        proposals[who] = proposal = rule(h, who)
        refused = h.command(who, 'policy.activate', {'reason': 'Reviewed my own rule'},
                            proposal['id'], proposal['revision'], status=403)
        assert refused['error']['code'] == 'SELF_APPROVAL'
    binding = h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['admin'], 'role': 'approver',
        'starts_on': '2020-01-01', 'reason': 'Explicit approver responsibility'})['record']
    assert set(h.get('admin', '/api/me')['self_approval']) == set(permission['actions'])
    proposal = proposals['admin']
    h.command('admin', 'policy.activate', {'reason': 'Reviewed with the clinical lead'}, proposal['id'], proposal['revision'])
    event, = h.get('auditor', '/api/audit?self_approved=true')['events']
    assert event['detail']['self_approved'] == [{'action': 'rule.activate', 'permission_id': permission['id']}]
    refused = h.command('admin', 'self_approval.grant', {'role': 'approver', 'actions': ['rule.activate'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Explicit self-grant request'}, status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    h.command('admin', 'role.revoke', {'reason': 'Approver responsibility ended'}, binding['id'], binding['revision'])
    assert not h.get('admin', '/api/me')['self_approval']
    proposal = rule(h, 'admin')
    h.command('admin', 'policy.activate', {'reason': 'Reviewed my next rule'}, proposal['id'], proposal['revision'], status=403)


@pytest.mark.parametrize('end', ['2026-10-01', '2027-10-02'])
def test_grant_refuses_invalid_explicit_end_date(h, end):
    payload = {'role': 'approver', 'actions': ['rule.activate'], 'starts_on': '2026-10-01',
               'reason': 'Explicit bounded cover', 'ends_on': end}
    h.command('admin', 'self_approval.grant', payload, status=422)
    assert not h.get('admin', '/api/self-approval')['permissions']


@pytest.mark.parametrize('end', ['2027-01-02'])
def test_grant_accepts_one_year_and_restore_refuses_a_legacy_invalid_grant(h, end):
    permission = grant(h, ['rota.approve'], role='approver')
    revoked = h.command('admin', 'self_approval.revoke', {'reason': 'End cover'},
                        permission['id'], permission['revision'])['permission']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.SelfApprovalPermission, revoked['id']).ends_on = date.fromisoformat(end) if end else None
    refused = h.command('admin', 'self_approval.restore', {'reason': 'Undo'},
                        revoked['id'], revoked['revision'], status=422)
    assert refused['error']['code'] == 'SELF_APPROVAL_PERIOD'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SelfApprovalPermission, revoked['id']).revoked


@pytest.mark.parametrize('subject', ['identity', 'role'])
@pytest.mark.parametrize('end', ['2027-01-02'])
def test_legacy_invalid_permissions_need_renewal_without_authorizing_or_rewriting(h, subject, end):
    from apps.api.services import independence
    identity(h, 'legacy_reviewer', ['coordinator', 'approver'])
    subject_value = {'identity_id': h.seed['identities']['legacy_reviewer']} if subject == 'identity' else {'role': 'approver'}
    permission = grant(h, **subject_value)
    proposal = rule(h, 'legacy_reviewer')
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.SelfApprovalPermission, permission['id'])
        row.ends_on = date.fromisoformat(end) if end else None
        original_revision = row.revision
        original_history = [(entry.id, entry.snapshot) for entry in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'self_approval', m.DefinitionHistory.object_id == row.id))]
        assert independence.permission_state(row, date(2026, 10, 3)) == 'needs_renewal'
        assert row.id not in {item.id for item in independence.active_permissions(db, row.department_id, date(2026, 10, 3))}
    assert h.get('legacy_reviewer', '/api/me')['self_approval'] == []
    listed = next(item for item in h.get('admin', '/api/self-approval')['permissions'] if item['id'] == permission['id'])
    assert listed['state'] == 'needs_renewal'
    assert listed['starts_on'] == permission['starts_on'] and listed['ends_on'] == end
    refused = h.command('legacy_reviewer', 'policy.activate', {'reason': 'Own rule must need independent review'},
                        proposal['id'], proposal['revision'], status=403)
    assert refused['error']['code'] == 'SELF_APPROVAL'
    with transaction(h.app.state.engine) as db:
        row = db.get(m.SelfApprovalPermission, permission['id'])
        assert row.ends_on == (date.fromisoformat(end) if end else None)
        assert row.revision == original_revision and not row.revoked
        assert [(entry.id, entry.snapshot) for entry in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'self_approval', m.DefinitionHistory.object_id == row.id))] == original_history


@pytest.mark.parametrize('end', ['missing', None])
def test_new_grant_without_end_defaults_to_bounded_period_and_undo(h, end):
    from packages.contracts.schemas import SelfApprovalInput
    payload = {'role': 'approver', 'actions': ['rota.approve'], 'starts_on': '2026-01-01',
               'reason': 'Current UI compatibility'}
    if end != 'missing':
        payload['ends_on'] = end
    assert 'ends_on' not in SelfApprovalInput.model_json_schema()['required']
    permission = h.command('admin', 'self_approval.grant', payload)['permission']
    assert permission['ends_on'] == '2027-01-01'
    listed = next(p for p in h.get('admin', '/api/self-approval')['permissions'] if p['id'] == permission['id'])
    assert listed['state'] == 'active'
    assert 'rota.approve' in h.get('approver', '/api/me')['self_approval']
    revoked = h.command('admin', 'self_approval.revoke', {'reason': 'End compatibility grant'},
                        permission['id'], permission['revision'])['permission']
    restored = h.command('admin', 'self_approval.restore', {'reason': 'Undo ending grant'},
                         revoked['id'], revoked['revision'])['permission']
    assert restored['ends_on'] == permission['ends_on'] and not restored['revoked']


def test_rota_detail_says_when_the_viewer_contributed_to_it(h):
    """C creates, A edits, B makes the final edit: A is still involved (the approval dialog says
    Self-approved), as the server's contributor test counts every editor; an outside approver is not."""
    identity(h, 'middle', ['coordinator', 'approver'])
    w = h.workspace('Three editors')
    w = h.command('middle', 'shift.create', {'workspace_id': w['id'], 'assignment_type_id': h.seed['type'],
                  'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]], 'start': '2026-10-05T08:00:00+03:00',
                  'end': '2026-10-05T12:00:00+03:00', 'participants': [], 'attributes': {}}, revision=w['revision'])['workspace']
    w = h.shift(w)['workspace']
    assert w['last_edited_by'] != h.seed['identities']['middle'] and w['created_by'] != h.seed['identities']['middle']
    assert h.get('middle', '/api/workspaces/' + w['id'])['reviewer_involved'] is True
    assert h.get('approver', '/api/workspaces/' + w['id'])['reviewer_involved'] is False


@pytest.mark.parametrize('viewer', ['creator', 'last_editor'])
def test_known_rota_author_needs_no_contributor_queries(viewer):
    from apps.api.services.reviews import contributed
    workspace = SimpleNamespace(created_by='creator', last_edited_by='last_editor')
    assert contributed(None, workspace, viewer)


def test_rota_detail_counts_another_login_for_a_contributor(h):
    person = h.seed['people'][2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = person
    identity(h, 'editor_alias', ['approver'], person)
    w = h.workspace()
    assert h.get('editor_alias', '/api/workspaces/' + w['id'])['reviewer_involved'] is True


def test_an_ended_permission_says_when_by_whom_and_why_it_ended(h):
    """SW3-B-20: an ended permission kept only its granted dates; the list now says when it ended, by whom and why,
    from the entry that ended it (a later restore's own entry is not taken for the ending)."""
    identity(h, 'both', ['coordinator', 'approver'])
    permission = grant(h, identity_id=h.seed['identities']['both'])
    ended = h.command('admin', 'self_approval.revoke', {'reason': 'Second approver joined'}, permission['id'], permission['revision'])['permission']
    rows = {p['id']: p for p in h.get('admin', '/api/self-approval')['permissions']}
    said = rows[permission['id']]['ended']
    admin = h.get('admin', '/api/me')['display_name']
    assert said['reason'] == 'Second approver joined' and said['by_name'] == admin and said['at']
    h.command('admin', 'self_approval.restore', {'reason': 'Second approver left again'}, ended['id'], ended['revision'])
    rows = {p['id']: p for p in h.get('admin', '/api/self-approval')['permissions']}
    assert rows[permission['id']]['ended']['reason'] == 'Second approver joined'
    assert all(p['ended'] is None for p in rows.values() if p['state'] != 'ended')
