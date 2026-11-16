"""Live scores reuse the canonical footer score and discard obsolete work."""

import pytest

from packages.domain import score


def arithmetic_program():
    def ref(index):
        return {'offset': 0, 'terms': [[index, 1]]}

    return {'prefix_size': 2, 'scale': 1_000_000, 'has_sessions': True, 'goal_cost_checks': [],
        'ops': [[2, 'linear', [{'offset': 0, 'terms': [[0, 2]]}]],
                [-1, 'div', [-7, ref(1)]], [3, 'mod', [-7, ref(1)]],
                [4, 'mul', [ref(-1), -50_000]], [5, 'positive', [ref(3)]],
                [6, 'max', [ref(2), ref(4)]], [7, 'min', [ref(6), 250_000]]],
        'components': {'coverage': [ref(7), 1], 'rules': [0, ref(5)], 'stability': [0, 1]},
        'assigned': 1}


def test_recorded_arithmetic_ignores_unchecked_tail_and_keeps_signed_division():
    result = score.compiled_progress_score(arithmetic_program(), [200_000, 3, *([999_999] * 6)])
    assert result == {'total': 75, 'components': {'coverage': .75, 'rules': None, 'fairness': None,
                                                'preferences': None, 'stability': None}}
    division = arithmetic_program()
    division['components']['coverage'] = [{'offset': 0, 'terms': [[4, 1]]}, 1]
    assert score.compiled_progress_score(division, [200_000, 3])['total'] == 90
    empty = arithmetic_program()
    empty['assigned'] = 0
    assert score.compiled_progress_score(empty, [200_000, 3])['total'] == 0
    empty['has_sessions'] = False
    assert score.compiled_progress_score(empty, [200_000, 3]) is None


def test_progress_projection_keeps_footer_uncapped_total_and_null_components():
    components = {name: {'value': None if name == 'preferences' else .75} for name in score.COMPONENTS}
    value = score.progress_score({'score': 59, 'uncapped_score': 87.5, 'components': components})
    assert value['total'] == 87.5 and value['components']['preferences'] is None
    assert score.progress_score({'uncapped_score': None}) is None


def test_fixed_point_rounding_ambiguity_preserves_the_canonical_footer():
    from tests.test_schedule_score import data, shift, coverage, policy, warning

    rules = [policy(f'r{index}') for index in range(3)]
    for rule in rules:
        rule['definition']['scope'] = {'person_id': 'a'}
    source = data([shift('s1', 1, ['a'])], rules)
    source['shifts'][0]['type_snapshot']['definition']['requirements'][0]['minimum'] = 6
    source['wishes'] = [{'id': 'honoured', 'person_id': 'a', 'kind': 'prefer_on',
                         'first_day': '2026-10-01', 'last_day': '2026-10-01', 'weight': 1}]
    options = {'scoring': {'weights': {'coverage': 1, 'rules': 1, 'preferences': 998,
                                       'fairness': 0, 'stability': 0}}}
    checked = {'coverage': [coverage('s1', 1, 6, 1)],
               'findings': [warning('r0', 'a'), warning('r1', 'a')]}
    canonical = score.schedule_score(source, checked, options)
    assert canonical['uncapped_score'] == 99.8
    assert score.progress_score(canonical)['total'] == 99.8
    program = {'prefix_size': 0, 'scale': 1_000_000, 'has_sessions': True, 'ops': [], 'assigned': 1,
               'goal_cost_checks': [],
               'components': {'coverage': [833_333, 1], 'rules': [666_666, 1], 'preferences': [0, 1]}}
    assert score.compiled_progress_score(program, [], options) is None
    program['components']['coverage'][0] = 500_000
    assert score.compiled_progress_score(program, [], options)['total'] == 99.9


@pytest.mark.parametrize('factor, expected', [(.749925, None), (.74991, .5001), (.74994, .5)])
def test_component_rounding_ambiguity_is_not_hidden_by_an_unambiguous_total(factor, expected):
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_score import begin_score_program, finish_score_program, coverage_component
    from tests.test_schedule_score import data, shift, coverage

    model = cp_model.CpModel()
    covered = model.new_int_var(0, 5, 'cover')
    begin_score_program(model)
    component = coverage_component(model, [(covered, 3, [5], 60)], factor)
    program = finish_score_program(model, {'coverage': component, 'fairness': (0, 1)}, 1)
    source = data([shift('s1', 1, list('abc'))])
    source['shifts'][0]['type_snapshot']['definition']['requirements'][0]['minimum'] = 3
    options = {'scoring': {'desired_cover_factor': factor,
        'weights': {name: int(name == 'coverage') for name in score.COMPONENTS}}}
    checked = {'findings': [{'code': 'COVERAGE_TARGET', 'severity': 'warning', 'rule_id': None,
        'person_id': None, 'shift_id': 's1', 'start': source['shifts'][0]['start'],
        'end': source['shifts'][0]['end'], 'detail': {'target': 5, 'available': 3}}],
        'coverage': [coverage('s1', 1, 3, 3)]}
    canonical = score.progress_score(score.schedule_score(source, checked, options))
    assert canonical['total'] == 50
    decoded = score.compiled_progress_score(program, [3], options)
    if expected is None:
        assert canonical['components']['coverage'] == .5 and decoded is None
    else:
        assert decoded == canonical and decoded['components']['coverage'] == expected


@pytest.mark.parametrize('invalid', ['native_overwrite', 'future', 'negative_input', 'unknown_op',
    'zero_divisor', 'overflow', 'scale', 'presence', 'loss'])
def test_recorded_arithmetic_rejects_missing_or_invalid_definitions(invalid):
    program = arithmetic_program()
    if invalid == 'native_overwrite':
        program['ops'][0][0] = 0
    elif invalid in {'future', 'negative_input'}:
        program['ops'][0][2] = [{'offset': 0, 'terms': [[99 if invalid == 'future' else -99, 1]]}]
    elif invalid == 'unknown_op':
        program['ops'][0][1] = 'solve'
    elif invalid == 'zero_divisor':
        program['ops'][1][2][1] = 0
    elif invalid == 'overflow':
        program['ops'][0] = [2, 'mul', [1 << 62, 4]]
    elif invalid == 'scale':
        program['scale'] = 1000
    else:
        program['components']['coverage'] = [0, 2] if invalid == 'presence' else [1_000_001, 1]
    assert score.compiled_progress_score(program, [200_000, 3, *([0] * 100)]) is None


def test_recorded_arithmetic_stops_at_cooperative_cpu_allowance(monkeypatch):
    readings = iter([0, .2])
    monkeypatch.setattr(score.time, 'thread_time', lambda: next(readings))
    assert score.compiled_progress_score(arithmetic_program(), [200_000, 3]) is None


@pytest.mark.parametrize('assigned', [0, 1, 2])
def test_recorded_coverage_uses_same_canonical_footer_total(assigned):
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_score import begin_score_program, finish_score_program, coverage_component, ScoreModel
    from tests.test_schedule_score import data, shift, coverage

    model = cp_model.CpModel()
    covered = model.new_int_var(0, 2, 'checked_cover')
    begin_score_program(model)
    component = coverage_component(model, [(covered, 2, [3], 60)])
    program = finish_score_program(model, {'coverage': component}, ScoreModel(model).positive(covered))
    assert program is not None
    options = {'scoring': {'weights': {name: int(name == 'coverage') for name in score.COMPONENTS}}}
    source = data([shift('s1', 1, list('ab')[:assigned])])
    findings = []
    if assigned == 2:
        findings.append({'code': 'COVERAGE_TARGET', 'severity': 'warning', 'rule_id': None, 'person_id': None,
            'shift_id': 's1', 'start': source['shifts'][0]['start'], 'end': source['shifts'][0]['end'],
            'detail': {'target': 3, 'available': 2}, 'message': 'Desired cover'})
    checked = {'findings': findings, 'coverage': [coverage('s1', 1, 2, assigned)]}
    expected = score.schedule_score(source, checked, options)
    actual = score.compiled_progress_score(program, [assigned, *([999_999] * (len(model.proto.variables) - 1))], options)
    assert actual['total'] == expected['uncapped_score']
    assert actual['components']['coverage'] == expected['components']['coverage']['value']
