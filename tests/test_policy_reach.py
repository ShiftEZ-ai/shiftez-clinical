"""How many people each rule reaches on a day (GET /api/policies/reach): the Rules list's "who · N people"."""
import gc
import json
import time
from datetime import date
from types import SimpleNamespace

import pytest

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.services import policies as service
from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES

from packages.domain.rule_map import reach
from packages.domain.time import local_midnight
from packages.domain.validation import index_person_rows, person_in_scope
from .test_rule_conditions import line, tree
from .test_rule_map import U, big_department
from .test_rule_templates import activate, propose
from .governance_support import identity
from .test_performance_budget import python_budget

DAY = '2026-10-04'
HOURS = {'kind': 'max_hours', 'hours': 48}


def counts(h, role='coordinator'):
    result = h.get(role, f'/api/policies/reach?day={DAY}')
    assert result['day'] == DAY
    assert set(result) == {'day', 'reach'}
    return result['reach']


def test_reach_counts_people_per_rule_with_lines_overrides_and_proposals(h):
    a, b, _ = h.seed['people']
    everyone = activate(h, propose(h, HOURS, name='Everyone'))
    pair = activate(h, propose(h, HOURS, {'person_ids': [a, b]}, name='Pair'))
    narrow = activate(h, propose(h, {'kind': 'max_hours', 'hours': 40}, {'person_id': a}, name='Narrow', overrides=[everyone['id']]))
    lines = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 6}, {'conditions': tree(line('person_ids', a, b))}, name='Lines',
                                per_person=[{'person_id': b, 'amount': 4}]))
    waiting = propose(h, HOURS, {'person_id': b}, name='Waiting')
    later = activate(h, propose(h, HOURS, name='Later', starts_on='2027-01-01'))
    total = h.client('coordinator').post('/api/policies/check', json={'definition': {'rule': HOURS, 'scope': {}, 'severity': 'mandatory'}}).json()
    reached = counts(h)
    assert reached[later['id']] == 0   # not in force on the day
    people = total['people_now']
    assert reached[everyone['id']] == people - 1   # the narrower rule replaces it for a
    assert (reached[pair['id']], reached[narrow['id']], reached[lines['id']], reached[waiting['id']]) == (2, 1, 2, 1)
    for role in ('admin', 'approver', 'auditor', 'credentialer'):
        if role in PERMISSIONS['rules.read']:
            assert counts(h, role)[pair['id']] == 2
        else:
            h.get(role, f'/api/policies/reach?day={DAY}', status=403)
    for role in ('staff', 'operator'):
        h.get(role, f'/api/policies/reach?day={DAY}', status=403)


@pytest.mark.parametrize('role', ROLE_NAMES)
@pytest.mark.parametrize('query', ['details=true', 'starts_on=2026-10-04&ends_on=2026-10-11'])
def test_reach_details_use_rule_map_roles(h, role, query):
    if role == 'programme_lead':
        identity(h, role, [role])
    allowed = role in PERMISSIONS['rules.read']
    result = h.get(role, f'/api/policies/reach?day={DAY}&{query}', status=200 if allowed else 403)
    if allowed:
        assert 'details' in result


@pytest.fixture(scope='module')
def default_reach_measurement():
    from packages.domain import feasibility, rule_map
    evaluations = 0
    original, original_witness = rule_map.scope_applies, rule_map.scope_has_duty_witness
    def counted(*args):
        nonlocal evaluations
        evaluations += 1
        return original(*args)
    def counted_witness(*args):
        nonlocal evaluations
        evaluations += 1
        return original_witness(*args)
    def no_rule_comparison(*args, **kwargs):
        pytest.fail('Default reach must not run activation checks or comparisons')
    data = big_department()
    a, b = (local_midnight(date(2026, 10, day), data['organisation']['timezone']) for day in (16, 17))
    rows = {m.Policy: data['policies'], m.PolicySeedLine: [], m.Service: list(data['services'].values()),
            m.Location: list(data['locations'].values())}
    rows = {model: [SimpleNamespace(**row) for row in records] for model, records in rows.items()}
    db = SimpleNamespace(scalars=lambda query: rows[query.column_descriptions[0]['entity']])
    actor = Actor('test', 'dept', 'org', 'Test coordinator', frozenset({'coordinator'}), None, 'session')
    samples = []
    # Exclude worker coordination CPU; every stateless sample retains the result and scope-work guards.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(rule_map, 'scope_applies', counted)
        monkeypatch.setattr(rule_map, 'scope_has_duty_witness', counted_witness)
        for name in ('check', 'compare_findings', 'evaluate'):
            monkeypatch.setattr(feasibility, name, no_rule_comparison)
        monkeypatch.setattr(service, 'people_data', lambda *_: (data, a, b))
        monkeypatch.setattr(service, 'record', vars)
        for _ in range(3):
            evaluations = 0
            gc.collect()  # Keep fixture/earlier-test collection outside the service computation budget.
            started, wall = time.thread_time(), time.perf_counter()
            result = service.policy_reach(db, actor, date(2026, 10, 16))
            elapsed = time.thread_time() - started
            payload_bytes = len(json.dumps({'day': '2026-10-16', **result}, separators=(',', ':')).encode())
            print(f'default reach service: {time.perf_counter() - wall:.3f} wall s / {elapsed:.3f} CPU s / {payload_bytes} bytes '
                  f'for 974 rules / 142 people / {evaluations} scope evaluations')
            samples.append((result, elapsed, evaluations))
    return samples


def test_default_reach_service_stays_within_department_budget(default_reach_measurement):
    for result, _, evaluations in default_reach_measurement:
        assert set(result) == {'reach'}
        assert len(result['reach']) == 974 and result['reach'][U('own3')] == 1
        # Baseline 9,199: doubling scope work fails independently of shared-runner timing noise.
        assert 0 < evaluations < 12_000, evaluations
    assert all(result == default_reach_measurement[0][0] for result, _, _ in default_reach_measurement), 'Repeated reads preserve every reach count'


@pytest.mark.cpu_budget
def test_default_reach_service_stays_within_department_budget_cpu_budget(default_reach_measurement):
    # Native 0.312 s; CI's 3.339 s under tracing needs the shared allowance, keeping the 0.5 s native ceiling.
    budget = python_budget(0.5)
    elapsed = min(cpu for _, cpu, _ in default_reach_measurement)
    assert elapsed < budget, (elapsed, budget)


@pytest.fixture(scope='module')
def department_reach_measurement():
    data = big_department()
    zone = data['organisation']['timezone']
    a, b = local_midnight(date(2026, 10, 16), zone), local_midnight(date(2026, 10, 17), zone)
    began = time.process_time()
    result = reach(data, data['policies'], a, b)
    elapsed = time.process_time() - began
    print(f'reach: {elapsed:.2f} CPU s for {len(result)} rules, {len(data["people"])} people')
    return data, a, b, result, elapsed


@pytest.mark.cpu_budget
def test_reach_at_department_scale_is_fast_and_matches_the_evaluator_cpu_budget(department_reach_measurement):
    *_, elapsed = department_reach_measurement
    import coverage
    budget = 30 if coverage.Coverage.current() else 6
    assert elapsed < budget, (elapsed, budget)


def test_reach_at_department_scale_is_fast_and_matches_the_evaluator(department_reach_measurement):
    data, a, b, result, _ = department_reach_measurement
    assert result[U('own3')] == 1
    # Rules without lines or overrides: the count is exactly who the evaluator says yes to.
    view = {**data, '_person_rows': index_person_rows(data)}
    plain = [p for p in data['policies'] if not p['definition'].get('per_person') and not p['definition'].get('overrides')
             and not any(p['id'] in (o['definition'].get('overrides') or []) for o in data['policies'])][::40]
    assert len(plain) > 10
    for p in plain:
        expected = sum(person_in_scope(view, p['definition']['scope'], pid, a, b) for pid in data['people'])
        assert result[p['id']] == expected, p['name']
