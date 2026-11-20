"""A late positive duty cap must survive merging a catalogue with a safety line."""
import json
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.workspace_baselines import _rule_shape
from packages.contracts.schemas import safety_rule
from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.solver_provenance import policy_id
from packages.domain.validation import evaluate
from .test_assignment_override_reason import payload
from .test_generation_superseded_apply import apply, group, run
from .test_rule_catalogue_api import line, person as person_scope
from .test_rule_catalogue_v2_api import proposed


BACKENDS = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def mixed_rota(h):
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    person, restricted = h.seed['people'][:2]
    cap = line(numbers={'max': {'value': 5, 'level': 'mandatory'}},
               settings={'max_duties': {'window': 'week'}})
    restriction = line(who=person_scope(restricted), numbers={'apply': {'value': None}},
                       settings={'restriction': {}})
    waiting = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'lines': [cap, restriction],
        'reason': 'Synthetic mixed workload rule'})['record']
    policy = h.command('approver', 'policy.activate', {'reason': 'Independent workload review'},
                       waiting['id'], waiting['revision'])['record']
    assert safety_rule(policy['definition'])
    draft, target = draft_with_duties(h, person)
    return draft, target, person, policy, cap['id']


def draft_with_duties(h, person, *, occupied=False):
    draft = h.workspace('Synthetic mixed duty limits')
    for day in ('04', '05', '06'):
        session = h.shift(draft, f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00',
                          people=[] if day == '06' and not occupied else [person])
        draft = session['workspace']
    target = next(shift for shift in h.detail(draft)['shifts'] if shift['id'] == session['shift_id'])
    return draft, target


def revise_cap(h, policy, line_id):
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, policy['id'])
        revised = deepcopy(row.definition)
        maximum = next(line['numbers']['max'] for line in revised['rule']['lines'] if line['id'] == line_id)
        maximum['value'] = 2
        assert _rule_shape({'definition': row.definition}, 'catalogue') == _rule_shape(
            {'definition': revised}, 'catalogue')
        row.definition = revised
        row.revision += 1
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1


def update_placement(h, draft, target, people):
    value = payload(h, draft, target, start=target['start'], end=target['end'], people=people)
    return h.command('coordinator', 'shift.update', value, revision=draft['revision'])['workspace']


def cap_findings(findings, person):
    return [row for row in findings if row['code'] == 'MAX_DUTIES' and row['person_id'] == person]


def assert_cap_finding(findings, person):
    finding, = cap_findings(findings, person)
    assert finding['severity'] == 'error' and finding['detail']['maximum'] == 2


def solver_inputs(h, draft, person):
    data = h.snapshot(draft)
    data['people'] = {person: data['people'][person]}
    data['planning_population'] = {shift['id']: [person] for shift in data['shifts']}
    return data


def assert_apply_refusal(receipt, person):
    assert receipt['status'] == 'unresolved' and receipt['addition_indexes'] == []
    assert_cap_finding([row for placement in receipt['conflicting_placements'] for row in placement['findings']], person)


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('path', ['check', 'greedy', 'cp_sat', 'apply'])
def test_late_positive_cap_in_mixed_safety_rule_binds_each_path(request, backend, path):
    h = request.getfixturevalue(backend)
    draft, target, person, policy, line_id = mixed_rota(h)
    job = run(h, draft, sessions=[target['id']], population=group(person)) if path == 'apply' else None
    if path == 'check':
        draft = update_placement(h, draft, target, [person])
    revise_cap(h, policy, line_id)

    if path == 'check':
        assert_cap_finding(h.lifecycle(draft, 'validate')['validation']['findings'], person)
        data = h.snapshot(draft)
        session = next(shift for shift in data['shifts'] if shift['id'] == target['id'])
        restricted = h.seed['people'][1]
        session['participants'].append({**session['participants'][0], 'person_id': restricted})
        findings = evaluate(data)['findings']
        assert_cap_finding(findings, person)
        restriction, = [row for row in findings if row['code'] == 'RESTRICTION' and row['person_id'] == restricted]
        assert restriction['rule_id'] == policy['id'] and restriction['severity'] == 'error'
    elif path == 'apply':
        before = h.snapshot(draft)
        assert_apply_refusal(apply(h, draft, job)['reconciliation'], person)
        assert h.snapshot(draft) == before
    else:
        data = solver_inputs(h, draft, person)
        generate = greedy_generate if path == 'greedy' else cp_sat_generate
        result = generate(deepcopy(data), 2)
        assert result['additions'] == result['removals'] == []
        if path == 'cp_sat':
            models = []
            prepared = generate(data, 2, feasibility_only=True, _on_model=models.append)
            assert prepared['status'] == 'model_ready'
            model, = models
            revised_ids = {row['id'] for row in data['policies']
                           if row.get('_source_policy_id', row['id']) == policy['id']
                           and row['revision'] == policy['revision'] + 1}
            constraints = [constraint for constraint in model.proto.constraints
                           if policy_id(constraint.name) in revised_ids
                           and json.loads(constraint.name.removeprefix('policy:'))['line_id'] == line_id]
            assert len(constraints) == 1


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('late', [False, True], ids=['unchanged', 'tightened'])
def test_mixed_safety_cap_stays_consistent_across_check_generation_and_apply(request, backend, late):
    h = request.getfixturevalue(backend)
    draft, target, person, policy, line_id = mixed_rota(h)
    occupied, _ = draft_with_duties(h, person, occupied=True)
    job = run(h, draft, sessions=[target['id']], population=group(person))
    if late:
        revise_cap(h, policy, line_id)

    findings = h.lifecycle(occupied, 'validate')['validation']['findings']
    if late:
        assert_cap_finding(findings, person)
    else:
        assert cap_findings(findings, person) == []

    data = solver_inputs(h, draft, person)
    result = cp_sat_generate(data, 2)
    assert result['removals'] == []
    expected = [] if late else [(target['id'], person)]
    assert [(row['shift_id'], row['participant']['person_id']) for row in result['additions']] == expected

    receipt = apply(h, draft, job)['reconciliation']
    if late:
        assert_apply_refusal(receipt, person)
    else:
        assert receipt['status'] == 'applied' and receipt['addition_indexes'] == [0]
