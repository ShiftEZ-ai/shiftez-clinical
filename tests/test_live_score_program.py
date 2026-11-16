"""Recorded arithmetic reuses the model definitions without trusting callback score variables."""
from fractions import Fraction
import json
from math import floor

from ortools.sat.python import cp_model
import pytest

from packages.domain.cp_sat_goals import _pair_deficit, add_score_goals
from packages.domain.cp_sat_patterns import add_patterns
from packages.domain.cp_sat_quotas import add_quotas
from packages.domain.cp_sat_score import SCORE_SCALE, ScoreModel, begin_score_program, finish_score_program
from packages.domain.score import COMPONENTS, compiled_progress_score, settings_of
from packages.domain.time import aware
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world
from tests.test_pattern_gap_construction import gap_input
from tests.test_weighted_goals import STRENGTHS


def arithmetic_model(record):
    model = cp_model.CpModel()
    x, y, present = model.new_int_var(0, 8, 'x'), model.new_int_var(0, 4, 'y'), model.new_bool_var('present')
    big = model.new_int_var(0, 10**13, 'big')
    if record:
        begin_score_program(model)
    q = ScoreModel(model)
    left, right = q.ratio(x, 8), q.ratio(y, 4)
    roots = {'coverage': (q.ratio(big, 10_000_000 + y), 1),
             'rules': (q.ratio(q.var(x + y), 20), 1),
             'fairness': (q.share_loss(q.mean([(Fraction(1, 3), left), (2, right)]), q.maximum([left, right])), 1),
             'preferences': (q.product(left, right), q.any([present, q.positive(x - y)])), 'stability': None}
    return model, [x, y, present, big], roots


def assert_program_matches(model, roots, assigned=1, *, allow_rounding_boundary=False):
    program = finish_score_program(model, roots, assigned)
    assert program is not None
    program = json.loads(json.dumps(program))  # Prepared state persists plain data, not model attributes.
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 2
    assert solver.solve(model) == cp_model.OPTIMAL
    prefix = list(solver.response_proto.solution)[:program['prefix_size']]
    poisoned = prefix + [123456789] * len(model.proto.variables)
    actual = compiled_progress_score(program, poisoned)
    compared = actual
    if actual is None:
        assert allow_rounding_boundary
        weights = settings_of(None)['weights']
        native = {name: Fraction(SCORE_SCALE - solver.value(pair[0]), SCORE_SCALE)
                  for name, pair in roots.items() if pair is not None and solver.value(pair[1]) and weights[name]}
        total = sum(weights[name] for name in native)
        raw = float(100 * sum(weights[name] * value for name, value in native.items()) / total) if total else 100
        raw = raw if solver.value(assigned) else 0
        boundary = (floor(raw * 10) + .5) / 10
        assert abs(raw - boundary) <= .01, raw
        # Change aggregation only: the same graph must still compute every component correctly.
        options = {'scoring': {'weights': {'coverage': 1, 'rules': 0, 'fairness': 0,
                                           'preferences': 0, 'stability': 0}}}
        compared = compiled_progress_score(program, poisoned, options)
        assert compared is not None and compared['total'] == 100
    for name in COMPONENTS:
        pair = roots.get(name)
        expected = (round(1 - solver.value(pair[0]) / SCORE_SCALE, 4)
                    if pair is not None and solver.value(pair[1]) else None)
        assert compared['components'][name] == expected
    return program, actual


@pytest.mark.parametrize('values', [(0, 0, 0, 0), (4, 2, 0, 4_000_000), (8, 4, 1, 10_000_000)])
def test_recorded_arithmetic_matches_native_values_and_preserves_native_model(values):
    model, inputs, roots = arithmetic_model(True)
    plain, _, _ = arithmetic_model(False)
    assert str(model.proto) == str(plain.proto)
    for variable, value in zip(inputs, values):
        model.add(variable == value)
    program, _ = assert_program_matches(model, roots)
    assert {'linear', 'max', 'min', 'positive', 'div', 'mul', 'mod'} <= {row[1] for row in program['ops']}


@pytest.mark.parametrize('need', [1, 2])
@pytest.mark.parametrize('values', [(0, 0, 0, 0), (1, 1, 1, 0), (1, 1, 1, 1)])
def test_recorded_pair_automaton_and_conjunctions_are_exact(need, values):
    model = cp_model.CpModel()
    flags = [model.new_bool_var(str(index)) for index in range(4)]
    begin_score_program(model)
    deficit = _pair_deficit(model, list(zip(flags[::2], flags[1::2])), need)
    roots = {'fairness': (ScoreModel(model).ratio(deficit, need), 1), 'stability': None}
    for flag, value in zip(flags, values):
        model.add(flag == value)
    program, _ = assert_program_matches(model, roots)
    assert any(row[0] < 0 for row in program['ops']) is (need == 1)


@pytest.mark.parametrize('metric', ['duties', 'weekends', 'share', 'period_spread', 'pair_limit'])
def test_direct_fairness_and_distribution_definitions_are_recorded(metric):
    data = world([duty(day, day, [A]) for day in (2, 3, 9, 10)])
    settings = {'metric': metric, 'aim': 'equal_share'}
    if metric not in {'duties', 'weekends'}:
        settings = {'metric': 'duties', 'distribution': metric}
        if metric == 'share':
            settings.update(within={'conditions': [{'field': 'service_ids', 'values': [uid(70)]}]}, target_share=0.3)
        elif metric == 'pair_limit':
            settings.update(metric='pair_exposure', aim='target', amount=1.3,
                            partners={'conditions': [{'field': 'person_ids', 'values': [B]}]})
    data['policies'] = [policy(settings)]
    model, choices, participants = cp_model.CpModel(), {}, {}
    for session in data['shifts']:
        for person in (A, B):
            key = session['id'], person
            choices[key] = model.new_bool_var('choice')
            participants[key] = {'person_id': person, 'start': session['start'], 'end': session['end']}
    begin_score_program(model)
    presence = []
    terms = add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS,
                            presence=presence)
    q = ScoreModel(model)
    roots = {'fairness': (q.mean(terms), q.any(presence)), 'stability': None}
    for (sid, person), choice in choices.items():
        model.add(choice == int(person == A))
    assert_program_matches(model, roots, allow_rounding_boundary=metric == 'period_spread')


def test_direct_pattern_worked_flags_and_prefixes_are_recorded():
    data = gap_input(8, limit=4, worked=(0, 2, 7))
    model = cp_model.CpModel()
    # Two choices on a day exercise the shared worked flag; long gaps also emit cumulative prefixes.
    flags = [[model.new_bool_var('first'), model.new_bool_var('second')] for _ in data['shifts']]
    rows = [(session, None, aware(session['start']), aware(session['end']), flag)
            for session, day_flags in zip(data['shifts'], flags) for flag in day_flags]
    begin_score_program(model)
    breaches = {}
    add_patterns(model, data, {'person': rows}, {'medium': 3}, wish_breaches=breaches)
    roots = {'preferences': (SCORE_SCALE * breaches['gap-wish'], 1), 'stability': None}
    for offset, day_flags in enumerate(flags):
        for index, flag in enumerate(day_flags):
            model.add(flag == int(offset in {0, 2, 7} and index == 0))
    assert_program_matches(model, roots)


def test_program_export_prunes_unneeded_work_and_fails_closed_for_missing_definitions():
    model = cp_model.CpModel()
    source = model.new_bool_var('source')
    begin_score_program(model)
    q = ScoreModel(model)
    required = q.ratio(source, 2)
    q.product(q.ratio(source, 3), q.ratio(source, 4))
    program = finish_score_program(model, {'coverage': (required, 1), 'stability': None}, source)
    assert program is not None and len(program['ops']) < len(model._score_program['ops'])
    unknown = model.new_int_var(0, SCORE_SCALE, 'unrecorded')
    assert finish_score_program(model, {'coverage': (unknown, 1)}, source) is None


def test_quota_strength_provenance_aligns_excepted_and_normal_score_items(monkeypatch):
    from packages.domain import cp_sat_quotas

    data = world([duty(1, 4, [A]), duty(2, 5, [B])])
    data['workspace'].update(id=uid(19), status='draft')
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+00:00',
                             'end': '2026-11-01T00:00:00+00:00', 'publications': []}
    for index, session in enumerate(data['shifts'], 30):
        for person in session['participants']:
            person.update(id=uid(index), start=session['start'], end=session['end'],
                          participation='staff', overtime=False, segments=[])
    for index, (person, level) in enumerate(((A, 'low'), (B, 'high')), 20):
        quota = policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 0,
                        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, {'person_id': person})
        quota.update(id=uid(index), revision=1)
        quota['definition']['strength'] = level
        data['policies'].append(quota)
    data['_allocation_strengths'] = {'off': 0, 'low': 3, 'medium': 3, 'high': 3, 'critical': 30}
    monkeypatch.setattr(cp_sat_quotas, 'exception_for', lambda view, quota, *args:
                        {'id': 'synthetic-exception'} if quota['id'] == uid(20) else None)
    model = cp_model.CpModel()
    selected = model.new_bool_var('selected')
    session = data['shifts'][1]
    by_person = {B: [(session, session['participants'][0], None, None, selected)]}
    items, levels = [], []
    assert add_quotas(model, data, by_person, None, score_items=items, score_strengths=levels) == []
    assert levels == ['low', 'high'] and len(items) == len(levels)
    assert items[0] == (3, 0, 1) and items[1][0] == 3
    # Equal run multipliers cannot identify the source level; the sidecar preserves independent footer settings.
    footer = {'low': 1, 'high': 10}
    assert [footer[level] for level in levels] == [1, 10]
