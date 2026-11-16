"""A catalogue's single number may change for one rota; safety and department rules stay intact."""
import copy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import adjusted_rule
from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.rule_map import adjustable, rule_map, rule_number
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize('catalogue_id,version,settings,unit', [
    ('duties_per_week', 1, {}, 'duties'),
    ('workload_limit', 2, {'max_hours': {'window': 'week'}}, 'hours'),
    ('workload_limit', 2, {'block_quota': {'metric': 'session_units', 'window_alignment': 'calendar_week',
                                        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}}, 'session_units'),
])
def test_catalogue_number_uses_its_contract_slot(catalogue_id, version, settings, unit):
    rule = PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': catalogue_id,
        'catalogue_version': version, 'settings': settings, 'lines': [line(numbers={'max': {'value': 6}})]}}).model_dump(mode='json')['rule']
    before = copy.deepcopy(rule)
    assert rule_number(rule) == {'field': 'max', 'value': 6, 'unit': unit, 'stricter': 'lower'}
    assert adjustable(rule)
    assert rule == before
    policy = {'id': str(uuid.uuid4()), 'name': 'Synthetic limit', 'status': 'active', 'active': True,
        'starts_on': '2020-01-01', 'definition': PolicyDefinition.model_validate({'rule': rule}).model_dump(mode='json')}
    adjusted = adjusted_rule(policy, {'mode': 'set', 'amount': 4})
    assert rule_number(adjusted['definition']['rule'])['value'] == 4
    assert rule_number(policy['definition']['rule'])['value'] == 6
    assert active_rules([adjusted])


@pytest.mark.parametrize('lines', [[], [line(numbers={'max': {'value': None}})],
    [line(numbers={'min': {'value': 2}, 'max': {'value': 6}})],
    [line(numbers={'max': {'value': 6}}), line(numbers={'max': {'value': 4}})]])
def test_catalogue_without_one_unambiguous_number_is_not_adjustable(lines):
    rule = {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': lines}
    assert rule_number(rule) is None
    assert not adjustable(rule)


def mapped(h, rota, policy):
    return next(r for r in h.get('coordinator', f"/api/workspaces/{rota['id']}/rule-map")['rules'] if r['id'] == policy['id'])


def test_catalogue_adjustment_is_rota_local_audited_and_undoable(h):
    policy = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 6}})])['record'])
    rota = h.shift(h.workspace())['workspace']
    rota = h.shift(rota, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    other = h.workspace('Department default unchanged')
    row = mapped(h, rota, policy)
    assert row['adjustable'] and row['number']['value'] == row['department_value'] == 6
    reason = 'Agreed workload for this rota only'
    rota = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'set',
        'amount': 1, 'reason': reason}, rota['id'], rota['revision'])['workspace']
    row = mapped(h, rota, policy)
    assert (row['number']['value'], row['department_value'], row['adjusted']['amount']) == (1, 6, 1)
    assert any(f.get('rule_id') == policy['id'] for f in rota['validation']['findings'])
    resolved = [p for p in active_rules(h.snapshot(rota)['policies']) if p['id'] == policy['id']]
    assert resolved and all(p['definition']['rule']['maximum'] == 1 for p in resolved)
    assert mapped(h, other, policy)['number']['value'] == 6
    assert h.detail(other)['workspace']['rule_adjustments'] == []
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy['id']).definition == policy['definition']
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'workspace.rule_adjust', m.AuditEvent.target_id == rota['id']))
        assert audit.reason == reason and audit.actor_id == h.seed['identities']['coordinator']
    rota = h.command('coordinator', 'workspace.undo', {'reason': 'Undo rota number'}, rota['id'], rota['revision'])['workspace']
    assert h.detail(rota)['workspace']['rule_adjustments'] == []
    assert mapped(h, rota, policy)['number']['value'] == 6
    assert not any(f.get('rule_id') == policy['id'] for f in rota['validation']['findings'])


@pytest.mark.parametrize('amount', [-1, 1.5, 501])
def test_catalogue_adjustment_validates_the_resolved_primitive(h, amount):
    policy = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 6}})])['record'])
    rota = h.workspace()
    refused = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'set',
        'amount': amount, 'reason': 'Invalid duty count'}, rota['id'], rota['revision'], status=422)
    assert refused['error']['code'] == ('SCHEMA_INVALID' if amount < 0 else 'INVALID_ADJUSTMENT')
    assert h.detail(rota)['workspace']['rule_adjustments'] == []


@pytest.mark.parametrize('catalogue_id,numbers,settings', [
    ('keep_off', {'apply': {}}, {}),
    ('rest_between_duties', {'min': {'value': 12}}, {}),
    ('not_after', {'apply': {}}, {}),
    ('supervision', {'max': {'value': 2}}, {'supervision_limit': {'mode': 'capacity'}}),
    ('people_per_session', {'min': {'value': 2}}, {}),
    ('people_per_session', {'max': {'value': 2}}, {}),
])
def test_catalogue_safety_rules_refuse_rota_adjustments(h, catalogue_id, numbers, settings):
    if catalogue_id == 'not_after':
        settings = {'forbidden_transition': {'from_type_ids': [h.seed['type']], 'to_type_ids': [h.seed['type']]}}
    policy = approve(h, propose(h, catalogue_id, [line(numbers=numbers)], settings=settings)['record'])
    rota = h.shift(h.workspace(), seed_restricted=True)['workspace']
    row = mapped(h, rota, policy)
    assert row['safety'] and not row['adjustable']
    before = h.snapshot(rota)
    for mode in ('off', 'set'):
        refused = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': mode,
            **({'amount': 0} if mode == 'set' else {}), 'reason': 'No safety waiver'}, rota['id'], rota['revision'], status=409)
        assert refused['error']['code'] == 'SAFETY_RULE'
    assert h.detail(rota)['workspace']['rule_adjustments'] == []
    assert h.snapshot(rota)['policies'] == before['policies']


def test_catalogue_adjustment_can_be_paused_and_cleared_but_cannot_choose_between_numbers(h):
    policy = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 6}})])['record'])
    rota = h.shift(h.workspace())['workspace']
    rota = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'off',
        'reason': 'Pause this rota limit'}, rota['id'], rota['revision'])['workspace']
    assert mapped(h, rota, policy)['off']
    assert all(p['id'] != policy['id'] for p in active_rules(h.snapshot(rota)['policies']))
    rota = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'clear',
        'reason': 'Restore department default'}, rota['id'], rota['revision'])['workspace']
    assert h.detail(rota)['workspace']['rule_adjustments'] == []
    assert mapped(h, rota, policy)['number']['value'] == 6
    multi = approve(h, propose(h, 'hours_per_week', [line(numbers={'min': {'value': 2}, 'max': {'value': 6}})])['record'])
    refused = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': multi['id'], 'mode': 'set',
        'amount': 4, 'reason': 'No ambiguous adjustment'}, rota['id'], rota['revision'], status=422)
    assert refused['error']['code'] == 'NOT_ADJUSTABLE'


def test_domain_rule_map_shows_the_catalogue_number(h):
    rota = h.shift(h.workspace())['workspace']
    data = h.snapshot(rota)
    policy = {'id': str(uuid.uuid4()), 'name': 'Weekly duties', 'kind': 'catalogue', 'status': 'active', 'active': True,
        'revision': 1, 'department_id': h.seed['department_id'], 'starts_on': '2020-01-01',
        'definition': PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'duties_per_week',
            'lines': [line(numbers={'max': {'value': 6}})]}}).model_dump(mode='json')}
    data['policies'] = [policy]
    [row] = rule_map(data)['rules']
    assert row['adjustable'] and not row['safety']
    assert row['number'] == {'field': 'max', 'value': 6, 'unit': 'duties', 'stricter': 'lower'}
    assert row['department_value'] == 6
