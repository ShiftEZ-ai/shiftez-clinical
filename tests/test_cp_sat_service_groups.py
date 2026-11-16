"""Run the established pooled staffing scenarios through CP-SAT as well."""
import copy
import json
from collections import Counter
import pytest
from packages.contracts.schemas import TypeDefinition
from packages.domain.cp_sat import generate
from . import test_service_group_rules as cases
from .test_schedule_score_scale import department


@pytest.fixture
def cp_cases(monkeypatch):
    monkeypatch.setattr(cases, 'generate', lambda data: generate(
        {**data, 'generation_options': {'mode': 'fill_gaps'}}, time_budget_seconds=10))
    return cases


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_pooled_clinics(cp_cases, request, database):
    cp_cases.test_pooled_clinic_cover_counts_people_once_and_splits_peer_absence(request, database)


@pytest.mark.parametrize('normal_alternative', [True, False])
def test_normal_places_before_overflow(cp_cases, h, normal_alternative):
    cp_cases.test_allocation_uses_normal_places_before_explicit_overflow(h, normal_alternative)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_conditional_cover(cp_cases, request, database):
    cp_cases.test_conditional_cover_requires_other_eligible_people_and_guides_allocation(request.getfixturevalue(database))


@pytest.mark.parametrize('intervals, expected', [
    ([(8, 12), (8, 12)], 0.5),
    ([(8, 10), (8, 10), (10, 12), (10, 12)], 0.5),
    ([(8, 12), (9, 11), (9, 11)], 0.6667),
    ([(9, 11), (8, 12), (9, 11)], 0.3333),
])
def test_advisory_pooled_cover_uses_canonical_session_score_subject(h, intervals, expected):
    from copy import deepcopy
    from packages.domain.score import schedule_score
    from packages.domain.validation import evaluate

    typ = h.configure('assignment_types', {'name': 'Optional pooled places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2]}, severity='advisory')
    workspace = h.workspace()
    for index in range(len(intervals)):
        workspace = h.shift(workspace, typ=typ, people=[], service=index % 2, location=index % 2)['workspace']
    data = h.snapshot(workspace)
    person = h.seed['people'][0]
    data['people'] = {person: data['people'][person]}
    # Order the intervals by canonical session identity, independent of generated UUIDs.
    for session, (start, end) in zip(sorted(data['shifts'], key=lambda s: s['id']), intervals):
        session['start'] = f'2026-10-04T{start:02}:00:00+03:00'
        session['end'] = f'2026-10-04T{end:02}:00:00+03:00'
    data['generation_options'] = {'mode': 'fill_gaps'}

    result = generate(data, 10, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    after = deepcopy(data)
    for addition in result['additions']:
        next(s for s in after['shifts'] if s['id'] == addition['shift_id'])['participants'].append(addition['participant'])
    canonical = schedule_score(after, evaluate(after), data['generation_options'])['components']['rules']['value']
    assert canonical == expected
    assert result['objective_components']['rules']['value'] == canonical
    assert result['compiled_component_values']['rules'] == pytest.approx(canonical, abs=0.000051)


def _pooled_receipt_fixture():
    data = department()
    people = list(data['people'])[:3]
    data['people'] = {pid: data['people'][pid] for pid in people}
    data['memberships'] = [row for row in data['memberships'] if row['person_id'] in data['people']]
    data['eligibilities'] = [row for row in data['eligibilities'] if row['person_id'] in data['people']]
    data['availability'] = []
    template = copy.deepcopy(data['shifts'][0])
    template['participants'] = []
    template['start'], template['end'] = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    template['locked'] = False
    template['type_snapshot']['definition']['requirements'] = []
    data['types'][template['assignment_type_id']]['definition']['requirements'] = []
    services = list(data['services'])[:2]
    data['shifts'] = [
        {**copy.deepcopy(template), 'id': 'pooled-a', 'service_ids': [services[0]]},
        {**copy.deepcopy(template), 'id': 'pooled-b', 'service_ids': [services[1]]},
    ]
    data['focus_ids'] = ['pooled-a', 'pooled-b']
    data['generation_session_ids'] = ['pooled-a', 'pooled-b']
    data['policies'] = [{
        'id': 'pooled-policy', 'revision': 1, 'name': 'Pooled receipt fixture', 'kind': 'minimum_cover',
        'status': 'active', 'active': True, 'department_id': data['workspace']['department_id'],
        'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'rule': {'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
                       'scope': {'service_ids': services}, 'severity': 'mandatory',
                       'exception_allowed': False},
    }]
    data['generation_options'] = {'mode': 'fill_gaps'}
    data['fingerprint'] = 'g8-pooled-fixture-v1'
    return data


def test_pooled_cover_fixed_seed_keeps_solver_receipt():
    data = _pooled_receipt_fixture()

    models = []
    compiled = generate(copy.deepcopy(data), 5, workers=1, deterministic=True, feasibility_only=True,
                        _on_model=models.append)
    assert compiled['status'] == 'model_ready'
    assert (len(models[0].proto.variables), len(models[0].proto.constraints)) == (9, 7)
    result = generate(copy.deepcopy(data), 5, workers=1, deterministic=True)
    repeated = generate(copy.deepcopy(data), 5, workers=1, deterministic=True)
    assert result['status'] == repeated['status'] == 'candidate_feasible'
    assert result['solver_status'] == repeated['solver_status'] == 'OPTIMAL'
    assert result['weighted_status'] == repeated['weighted_status'] == 'OPTIMAL'
    assert result['optimality_proven'] and repeated['optimality_proven']
    objectives = [(row['name'], row['value'], row['proven_optimal']) for row in result['objectives']]
    additions = [(row['shift_id'], row['participant']['person_id']) for row in result['additions']]
    assert objectives == [('coverage', 0, True), ('weighted', 2, True)]
    assert additions == [
        ('pooled-b', '70a7e206-3c3b-5007-bb9c-2f7f5a81f2dd'),
        ('pooled-b', 'd197e3f0-3530-5256-80eb-1e332a82c2aa'),
    ]
    assert objectives == [(row['name'], row['value'], row['proven_optimal']) for row in repeated['objectives']]
    assert additions == [(row['shift_id'], row['participant']['person_id']) for row in repeated['additions']]
    assert result['removals'] == repeated['removals']
    assert result['generation_outcome'] == {
        'status': 'OPTIMAL', 'candidate': 'valid', 'strict_status': None, 'optimality_proven': True,
    }
    assert result['generation_outcome'] == repeated['generation_outcome']
    assert {key: result.get(key) for key in ('generation_outcome', 'generation_scope', 'planning_population')} == \
        {key: repeated.get(key) for key in ('generation_outcome', 'generation_scope', 'planning_population')}


def _pooled_objective_fixture(severity):
    data = _pooled_receipt_fixture()
    services = list(data['services'])[:3]
    template = copy.deepcopy(data['shifts'][0])
    template['participants'] = []
    template['locked'] = False
    shifts = [
        {**copy.deepcopy(template), 'id': 'pooled-a', 'service_ids': [services[0]]},
        {**copy.deepcopy(template), 'id': 'pooled-b', 'service_ids': [services[1]]},
        {**copy.deepcopy(template), 'id': 'pooled-c', 'service_ids': [services[0]]},
        {**copy.deepcopy(template), 'id': 'target', 'service_ids': [services[2]]},
    ]
    shifts[-1]['type_snapshot']['definition']['requirements'] = TypeDefinition(
        requirements=[{'minimum': 0, 'target': 1, 'maximum': 1}]).model_dump(mode='json')['requirements']
    data['shifts'] = shifts
    data['focus_ids'] = [shift['id'] for shift in shifts]
    data['generation_session_ids'] = list(data['focus_ids'])
    policy = copy.deepcopy(data['policies'][0])
    policy['definition']['rule']['minimum'] = 4
    policy['definition']['severity'] = severity
    data['policies'] = [policy]
    people = list(data['people'])
    data['planning_population'] = {shift['id']: [people[index % 3]] for index, shift in enumerate(shifts)}
    data['planning_population']['target'] = [people[2]]
    if severity == 'advisory':
        preference = copy.deepcopy(policy)
        preference.update(id='fill-priority', name='Configured fill priority', kind='allocation_preference')
        preference['definition'] = {
            'rule': {'kind': 'allocation_preference', 'priority': 0, 'session_priority': 5},
            'scope': {}, 'severity': 'advisory',
        }
        data['policies'].append(preference)
        data['generation_options']['allocation_order'] = 'tiered'
    data['fingerprint'] = 'g8-pooled-objective-' + severity
    return data


# Pre-dedup 57fddb5f0 receipts: one worker, deterministic seed 1, three pooled anchors competing with a target.
@pytest.mark.parametrize('severity,diagnostic,expected', [
    ('mandatory', True, {
        'status': 'candidate_with_gaps', 'solver_status': 'OPTIMAL', 'weighted_status': 'OPTIMAL',
        'objectives': [('coverage', 3120), ('weighted', 70000003)],
        'additions': ['pooled-a', 'pooled-b', 'pooled-c'],
    }),
    ('advisory', False, {
        'status': 'candidate_feasible', 'solver_status': 'OPTIMAL', 'weighted_status': 'OPTIMAL',
        'objectives': [('coverage', 0), ('fill:5', 1500), ('weighted', 237036803)],
        'additions': ['pooled-a', 'pooled-b', 'target'],
    }),
], ids=['mandatory-diagnostic', 'advisory'])
def test_pooled_cover_replays_objective_terms_across_anchor_cells(severity, diagnostic, expected, record_property):
    data = _pooled_objective_fixture(severity)
    result = generate(data, 5, workers=1, deterministic=True,
                      diagnostic_proposal=diagnostic)
    receipt = {
        **{key: result[key] for key in ('status', 'solver_status', 'weighted_status', 'generation_outcome')},
        'objectives': [(row['name'], row['value'], row['proven_optimal']) for row in result['objectives']],
        'assignments': [(row['shift_id'], row['participant']['person_id']) for row in result['additions']],
        'removals': result['removals'],
    }
    record_property('pooled_receipt', json.dumps(receipt, sort_keys=True, separators=(',', ':')))
    assert all(row['proven_optimal'] for row in result['objectives'])
    assert not result['removals']
    assert {key: result[key] for key in ('status', 'solver_status', 'weighted_status')} == {
        key: expected[key] for key in ('status', 'solver_status', 'weighted_status')}
    assert [(row['name'], row['value']) for row in result['objectives']] == expected['objectives']
    assert [(row['shift_id'], row['participant']['person_id']) for row in result['additions']] == [
        (sid, data['planning_population'][sid][0]) for sid in expected['additions']]


@pytest.mark.parametrize('severity', ['mandatory', 'advisory'])
@pytest.mark.parametrize('triggered', [False, True], ids=['always', 'triggered'])
def test_pooled_cover_replay_reuses_deficit_and_fill_variables(severity, triggered, monkeypatch, record_property):
    from packages.domain.cp_sat_score import ScoreModel

    data = _pooled_objective_fixture('advisory')
    data.pop('planning_population')
    data['policies'][0]['definition']['severity'] = severity
    if triggered:
        data['policies'][0]['definition']['rule']['when_person_ids'] = [next(iter(data['people']))]
    positive, deficit_flags, models = ScoreModel.positive, [], []

    def count_deficit_flags(self, expression):
        if getattr(expression, 'name', None) == 'preference_deficit':
            deficit_flags.append(expression.index)
        return positive(self, expression)

    def capture(model, tiers):
        names = Counter(variable.name for variable in model.proto.variables)
        assert names['distinct_person'] == 3 - int(triggered)  # Trigger people do not supply staffing credit.
        assert names['preference_deficit'] == 1
        assert names['fill_stage_cost'] == 2
        fill = next(terms for name, terms, _ in tiers if name == 'fill:5')
        assert sorted(Counter(term.index for term in fill).values()) == [1, 3]
        models.append((len(model.proto.variables), len(model.proto.constraints)))

    monkeypatch.setattr(ScoreModel, 'positive', count_deficit_flags)
    result = generate(data, 5, workers=1, deterministic=True,
                      diagnostic_proposal=severity == 'mandatory', _on_objectives=capture)
    assert result['status'] == 'model_ready'
    assert result['solver_status'] == result['weighted_status'] == 'UNKNOWN'
    assert len(deficit_flags) == (1 if severity == 'advisory' else 0)
    assert len(models) == 1
    record_property('model_variables_constraints', json.dumps(models[0]))
