"""Finding causes share session and eligible-person work at department scale."""
from collections import Counter
import copy
import gc
import time
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition, TypeDefinition
from packages.domain import quotas, validation
from packages.domain.validation import evaluate
from .test_performance_budget import python_budget
from .test_quota_subject_performance import department_quota_preparation
from .test_rule_map import rule


@pytest.fixture(scope='module')
def department_finding_causes(department_quota_preparation):
    _, _, source, *_ = department_quota_preparation
    data = copy.deepcopy(source)
    data['rotations'] = []  # This case keeps every person's full-window minimum in scope.
    missing_type = str(uuid.uuid5(uuid.NAMESPACE_URL, 'finding-causes-missing-duty-type'))
    exemplar = next(iter(data['types'].values()))
    data['types'][missing_type] = {**copy.deepcopy(exemplar), 'id': missing_type, 'name': 'Required session kind'}
    for policy in data['policies']:
        for line in policy['definition']['rule']['lines']:
            line['numbers'] = {'min': {'value': 1, 'level': 'mandatory'}}
            line['where'] = {'conditions': [{'field': 'assignment_type_ids', 'values': [missing_type]}]}
        policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    before = copy.deepcopy(data)
    contribution_calls = 0
    original = quotas.quota_contribution

    def counted(*args, **kwargs):
        nonlocal contribution_calls
        contribution_calls += 1
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(quotas, 'quota_contribution', counted)
        gc.collect()
        began = time.process_time()
        checked = evaluate(data, quota_only=True)
        elapsed = time.process_time() - began
    return data, before, checked, contribution_calls, elapsed


def test_missing_session_causes_at_department_scale_share_scope_scans(department_finding_causes):
    data, before, checked, calls, elapsed = department_finding_causes
    assert len(data['people']) == 150 and len(data['shifts']) >= 500
    assert sum(len(p['definition']['rule']['lines']) for p in data['policies']) >= 900
    assert data == before
    assert checked['quota_report']['complete'] and len(checked['quota_report']['rows']) == 900
    findings = checked['findings']
    assert len(findings) == 900
    assert all(f['cause']['code'] == 'no_candidate_sessions' and f['cause']['sessions_found'] == 0 for f in findings)
    assert all(f['fix_hints'][0]['action'] == 'add_sessions' for f in findings)
    # Six source rules share their session scope across 150 people; never scan all sessions for every person.
    assert calls <= len(data['shifts']) * len(data['policies']) * 2, (calls, elapsed)


@pytest.mark.cpu_budget
def test_department_missing_session_cause_cpu_budget(department_finding_causes):
    *_, elapsed = department_finding_causes
    assert elapsed < python_budget(8), elapsed


@pytest.fixture(scope='module')
def department_coverage_causes(department_quota_preparation):
    _, _, source, *_ = department_quota_preparation
    data = copy.deepcopy(source)
    definition = TypeDefinition.model_validate({'requirements': [{'minimum': 3, 'maximum': 5}],
        'allow_shared_services': True, 'shared_duty_reason': 'Synthetic common coverage workload'}).model_dump(mode='json')
    for typ in data['types'].values():
        typ['definition'] = copy.deepcopy(definition)
    people = list(data['people'])
    for n, shift in enumerate(data['shifts']):
        shift['type_snapshot'] = copy.deepcopy(data['types'][shift['assignment_type_id']])
        shift['participants'] = [{'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'coverage-assignment-{n}')),
            'person_id': people[n % len(people)], 'start': shift['start'], 'end': shift['end'],
            'participation': 'staff', 'overtime': False}]
    policies = [rule(data, f'Synthetic repeated coverage {n}', {'kind': 'minimum_cover', 'minimum': 3 + n})
                for n in range(8)]
    data['policies'].extend(policies)
    before = copy.deepcopy(data)
    pools, participant_work = Counter(), 0
    original = validation.rule_coverage_people

    def counted(data, shift, a, b, **kwargs):
        nonlocal participant_work
        if len(shift['participants']) == len(people):
            assert kwargs.get('presence') == {(shift['id'], pid): [(a, b)] for pid in people}, \
                'Synthetic candidates already have known full-segment presence'
            pools[(shift['id'], a, b)] += 1
            participant_work += len(shift['participants'])
        return original(data, shift, a, b, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(validation, 'rule_coverage_people', counted)
        gc.collect()
        began = time.process_time()
        checked = evaluate(data)
        elapsed = time.process_time() - began
    return data, before, checked, {p['id'] for p in policies}, pools, participant_work, elapsed


def test_populated_session_causes_share_eligible_person_pools_at_department_scale(department_coverage_causes):
    data, before, checked, policy_ids, pools, work, elapsed = department_coverage_causes
    from packages.domain.finding_causes import COVERAGE_CODES
    from packages.domain.validation import finding_observations

    assert len(data['people']) == 150 and len(data['shifts']) >= 500
    assert all(len(shift['participants']) == 1 for shift in data['shifts'])
    assert sum(len(p['definition']['rule'].get('lines', [])) for p in data['policies']) >= 900
    assert data == before
    findings = [f for f in finding_observations(checked) if f['code'] in COVERAGE_CODES]
    assert findings and {f['rule_id'] for f in findings if f.get('rule_id') in policy_ids} == policy_ids
    assert all(f.get('cause') and f.get('fix_hints') for f in findings)
    assert {f['shift_id'] for f in findings} == set(data['focus_ids'])
    # Eight matching rules and the duty floor reuse one synthetic person pool per authority segment.
    assert set(pools.values()) == {1}, (pools, elapsed)
    assert len(pools) <= len(data['shifts']) * 2, (len(pools), elapsed)
    assert work == len(data['people']) * len(pools), (work, elapsed)


@pytest.mark.cpu_budget
def test_department_populated_session_cause_cpu_budget(department_coverage_causes):
    *_, elapsed = department_coverage_causes
    assert elapsed < python_budget(15), elapsed
