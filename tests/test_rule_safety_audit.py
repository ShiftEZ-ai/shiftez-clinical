"""AGENTS §3: exceptions and rota adjustments never relax any of the six protected safety kinds.

Historical flags and exception records are deliberately inserted without new-input validation: old data must remain
readable, but cannot authorize a safety violation. The normal Harness uses a migrated disposable SQLite database.
"""
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition
from packages.domain.validation import evaluate, exception_for


SAFETY_KINDS = ('restriction', 'min_rest', 'forbidden_transition', 'supervision_limit', 'minimum_cover', 'maximum_participants')


def safety_policy(h, kind, legacy_exception=False):
    settings = {'restriction': {}, 'min_rest': {'hours': 12},
                'forbidden_transition': {'from_type_ids': [h.seed['type']], 'to_type_ids': [h.seed['type']]},
                'supervision_limit': {'mode': 'capacity', 'maximum': 2},
                'minimum_cover': {'minimum': 2}, 'maximum_participants': {'maximum': 2}}
    policy = h.policy({'kind': kind, **settings[kind]})
    if legacy_exception:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, policy['id'])
            row.definition = {**row.definition, 'exception_allowed': True}
        policy['definition'] = {**policy['definition'], 'exception_allowed': True}
    return policy


def historical_exception(h, policy, status):
    with transaction(h.app.state.engine, write=True) as db:
        row = m.PolicyException(department_id=policy['department_id'], policy_id=policy['id'], policy_revision=policy['revision'],
                                person_id=h.seed['people'][0], starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11),
                                reason='Historical synthetic exception, no safety waiver', created_by=h.seed['identities']['coordinator'],
                                approved_by=h.seed['identities']['approver'] if status == 'approved' else None, status=status)
        db.add(row)
        db.flush()
        return row.id, row.revision


@pytest.mark.parametrize('kind', SAFETY_KINDS)
def test_direct_exception_creation_rejects_historical_safety_flags(h, kind):
    policy = safety_policy(h, kind, legacy_exception=True)
    h.command('coordinator', 'exception.create', {'policy_id': policy['id'], 'person_id': h.seed['people'][0],
              'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'reason': 'Cannot waive safety directly'}, status=409)


@pytest.mark.parametrize('kind', SAFETY_KINDS)
def test_direct_exception_approval_rejects_historical_safety_requests(h, kind):
    policy = safety_policy(h, kind, legacy_exception=True)
    exception_id, revision = historical_exception(h, policy, 'proposed')
    h.command('approver', 'exception.approve', {'reason': 'Independent approval must still preserve safety'},
              exception_id, revision, status=409)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.PolicyException, exception_id).status == 'proposed'


@pytest.mark.parametrize('kind', SAFETY_KINDS)
def test_persisted_approved_safety_exceptions_never_authorize_a_waiver(h, kind):
    policy = safety_policy(h, kind, legacy_exception=True)
    historical_exception(h, policy, 'approved')
    data = h.snapshot(h.workspace())
    start, end = (datetime.fromisoformat(f'2026-10-05T{clock}:00+03:00') for clock in ('08:00', '12:00'))
    assert exception_for(data, policy, h.seed['people'][0], start, end) is None


@pytest.mark.parametrize('kind,code', [('restriction', 'RESTRICTION'), ('min_rest', 'MIN_REST'),
                                       ('forbidden_transition', 'FORBIDDEN_TRANSITION')])
def test_validator_keeps_safety_errors_with_a_persisted_approved_exception(h, kind, code):
    policy = safety_policy(h, kind, legacy_exception=True)
    rota = h.workspace()
    rota = h.shift(rota, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00',
                   seed_restricted=kind == 'restriction')['workspace']
    rota = h.shift(rota, '2026-10-05T04:00:00+03:00', '2026-10-05T10:00:00+03:00', location=1,
                   seed_restricted=kind == 'restriction')['workspace']
    historical_exception(h, policy, 'approved')
    findings = [f for f in evaluate(h.snapshot(rota))['findings'] if f.get('rule_id') == policy['id']]
    assert any(f['code'] == code and f['severity'] == 'error' for f in findings), findings
    assert not any(f['code'] == 'RULE_EXCEPTION' for f in findings)


@pytest.mark.parametrize('kind', SAFETY_KINDS)
def test_rota_cannot_switch_off_a_safety_rule(h, kind):
    policy = safety_policy(h, kind)
    rota = h.workspace()
    h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'off',
              'reason': 'A rota adjustment cannot remove safety'}, rota['id'], rota['revision'], status=409)


@pytest.mark.parametrize('kind,amount', [('min_rest', 8), ('minimum_cover', 1), ('maximum_participants', 3)])
def test_rota_cannot_weaken_a_safety_number(h, kind, amount):
    policy = safety_policy(h, kind)
    rota = h.workspace()
    h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'set', 'amount': amount,
              'reason': 'A rota adjustment cannot weaken safety'}, rota['id'], rota['revision'], status=409)


@pytest.mark.parametrize('mode,amount', [('off', None), ('set', 8)])
def test_historical_safety_adjustments_retain_the_original_constraint_and_can_be_cleared(h, mode, amount):
    from packages.domain.rule_map import rule_map
    policy = safety_policy(h, 'min_rest')
    rota = h.workspace()
    rota = h.shift(rota, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00')['workspace']
    rota = h.shift(rota, '2026-10-05T04:00:00+03:00', '2026-10-05T10:00:00+03:00', location=1)['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, rota['id'])
        stored.rule_adjustments = [{'policy_id': policy['id'], 'policy_revision': policy['revision'], 'mode': mode,
                                    'amount': amount, 'by': h.seed['identities']['coordinator'],
                                    'at': '2026-10-03T06:00:00+00:00'}]
    data = h.snapshot(rota)
    retained = next(p for p in data['policies'] if p['id'] == policy['id'])
    assert retained['definition']['rule']['hours'] == 12, 'historical adjustments must not weaken the department minimum'
    assert any(f['code'] == 'MIN_REST' and f['severity'] == 'error' and f.get('rule_id') == policy['id']
               for f in evaluate(data)['findings'])
    reach = rule_map(data, {policy['id']: policy})
    mapped = next(r for r in reach['rules'] if r['id'] == policy['id'])
    assert mapped['adjusted']['applied'] is False and not mapped['off']
    assert not any(marker['kind'] == 'rota_adjustment' and marker.get('rule_id') == policy['id'] for marker in reach['markers'])
    cleared = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'clear',
                        'reason': 'Remove the invalid historical safety adjustment'}, rota['id'], rota['revision'])['workspace']
    assert cleared['rule_adjustments'] == []


def test_new_safety_definitions_cannot_enable_person_exceptions():
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({'rule': {'kind': 'min_rest', 'hours': 12}, 'exception_allowed': True})


def test_non_safety_exception_path_still_requires_and_accepts_independent_approval(h):
    policy = h.policy({'kind': 'max_duties', 'maximum': 1}, exception=True)
    made = h.command('coordinator', 'exception.create', {'policy_id': policy['id'], 'person_id': h.seed['people'][0],
                     'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'reason': 'Approved synthetic workload adjustment'})['record']
    approved = h.command('approver', 'exception.approve', {'reason': 'Independent synthetic workload review'}, made['id'], made['revision'])['record']
    assert approved['status'] == 'approved'
