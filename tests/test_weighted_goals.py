"""The solver uses the fairness report's bands, history, line weights and share score."""
from fractions import Fraction

import pytest
from ortools.sat.python import cp_model

from packages.domain import fairness
from packages.domain.cp_sat_goals import add_balance_goals, add_score_goals
from packages.domain.cp_sat_score import SCORE_SCALE, ScorePrecisionError
from packages.domain.score import schedule_score
from tests.test_balance_between_people import catalogue
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world

STRENGTHS = {'low': 1, 'medium': 3, 'high': 10, 'critical': 30}


def solver_losses(data, *, overtime=False, lead=False):
    model, choices, participants, overtime_choices, leads = cp_model.CpModel(), {}, {}, {}, {}
    presence = []
    for shift in data['shifts']:
        for pid in (A, B):
            participant = next((p for p in shift['participants'] if p['person_id'] == pid),
                               {'person_id': pid, 'start': shift['start'], 'end': shift['end']})
            key = shift['id'], pid
            choices[key] = model.new_constant(int(participant in shift['participants']))
            participants[key] = participant
            if overtime:
                overtime_choices[key] = model.new_constant(int(participant.get('overtime', False)))
            if lead:
                leads[key] = model.new_constant(int(participant.get('team_lead', False)))
    terms = add_score_goals(model, data, choices, participants, overtime_choices, {}, set(data['focus_ids']),
                            STRENGTHS, leads if lead else None, presence=presence)
    model.minimize(sum(loss for _, loss in terms))
    assert model.validate() == ''
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    assert len(terms) == len(presence)
    return [(weight, solver.value(loss) / SCORE_SCALE) for (weight, loss), present in zip(terms, presence)
            if solver.value(present)]


def report_losses(data):
    result = []
    for report in fairness.goal_reports(data):
        if not report.get('score_present', True):
            continue
        if report['pairs']:
            result.append((Fraction(STRENGTHS[report['strength']], STRENGTHS['medium']) * Fraction(str(report.get('weight', 1))),
                           sum(row['cost'] for row in report['pairs']) / len(report['pairs'])))
        elif report['people']:
            weights = fairness.report_weights(report, STRENGTHS)
            result.append((Fraction(str(sum(weights.values()))) / (len(weights) * STRENGTHS['medium']),
                           1 - (report['value'] if report.get('workload_spread') else fairness.share_value(
                               report['people'], weights,
                               (data.get('generation_options') or {}).get('scoring', {}).get('fairness_worst_off_emphasis', 0.5)))))
    return result


@pytest.mark.parametrize('metric', ['duties', 'hours', 'session_units', 'weekends', 'overtime_duties', 'overtime_hours'])
@pytest.mark.parametrize('aim', ['equal_share', 'target', 'max_min'])
def test_weighted_goal_matches_report_cost_and_bands(metric, aim):
    shifts = [duty(i, 2 + i * 7, [A] if i < 3 else [B]) for i in range(4)]
    for shift in shifts:
        shift['end'] = shift['start'].replace('08:00', '12:07')
        shift['type_snapshot']['definition']['session_units'] = 1.337
        for participant in shift['participants']:
            participant.update(end=shift['end'], overtime=True)
    data = world(shifts)
    goal = policy({'metric': metric, 'aim': aim, **({'amount': 5.3} if aim == 'target' else {})})
    data['policies'] = [goal]
    data['fairness_history'] = {fairness.goal_identity(goal): {
        'people': {A: {'value': 7.125, 'available': 12}, B: {'value': 1.5, 'available': 24}}}}
    assert_losses(solver_losses(data, overtime=True), report_losses(data))


def assert_losses(actual, expected):
    assert [weight for weight, _ in actual] == [weight for weight, _ in expected]
    assert [loss for _, loss in actual] == pytest.approx([loss for _, loss in expected], abs=3e-6)


@pytest.mark.parametrize('aim', ['equal_share', 'target', 'max_min'])
def test_named_line_strengths_and_history_match_report(aim):
    data = world([duty(i, i + 11, [A]) for i in range(1, 6)], first='2026-10-11', last='2026-10-18')
    goal = catalogue(amounts=(2, 5) if aim == 'target' else None, period='month', levels=('critical', 'low'))
    goal['definition']['rule']['settings']['balance_goal']['aim'] = aim
    data['policies'] = [goal]
    groups = fairness.goal_groups(data)
    data['fairness_history'] = {fairness.goal_identity(fairness.comparison_goal(groups[0])): {
        'people': {A: {'value': 3, 'available': 10}, B: {'value': 0, 'available': 10}}}}
    assert_losses(solver_losses(data), report_losses(data))


def test_grouped_source_weights_reach_report_and_score_goal():
    data = world([duty(i, i, [A]) for i in range(1, 6)])
    goal = catalogue(levels=('critical', 'low'))
    for line, weight in zip(goal['definition']['rule']['lines'], (0.125, 2.75)):
        line['settings'] = {'balance_goal': {'weight': weight}}
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    assert fairness.report_weights(report, STRENGTHS) == {A: 3.75, B: 2.75}
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('field', ['tags', 'category_ids'])
@pytest.mark.parametrize('actual', [(2, 5), (5, 2)])
def test_disjoint_scoped_targets_match_report(field, actual):
    data = world([duty(i, i, [pid]) for i, pid in enumerate([A] * actual[0] + [B] * actual[1], 1)])
    goal = catalogue(amounts=(2, 5))
    for pid, group, line in zip((A, B), (uid(70), uid(71)), goal['definition']['rule']['lines']):
        line['who'] = {'conditions': [{'field': field, 'values': [group]}]}
        if field == 'tags':
            data['people'][pid]['tags'] = [group]
        else:
            data['categories'][group] = {'active': True}
            next(row for row in data['memberships'] if row['person_id'] == pid)['category_id'] = group
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    assert {row['person_id']: row['expected'] for row in report['people']} == {A: 2, B: 5}
    if actual == (2, 5):
        assert report['score'] == 100
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('pairing', ['present', 'supervised'])
@pytest.mark.parametrize('attendees', [[], [A], [B], [A, B]])
@pytest.mark.parametrize('amount,prior', [(4.3, 0.5), (4.3, 0), (9.3, 0), (4.3, 2)])
def test_prorated_fractional_pair_target_matches_report(pairing, attendees, amount, prior):
    shifts = [duty(1, 15, attendees)]
    for participant in shifts[0]['participants']:
        if participant['person_id'] == A:
            participant['supervisor_person_id'] = B
    data = world(shifts, first='2026-10-11', last='2026-10-18')
    goal = policy({'metric': 'pair_exposure', 'aim': 'min_per_pair', 'pairing': pairing,
                   'period': 'month', 'amount': amount,
                   'partners': {'conditions': [{'field': 'person_ids', 'values': [B]}]}}, {'person_ids': [A]})
    data['policies'] = [goal]
    data['fairness_history'] = {fairness.goal_identity(goal): {'pairs': {A: {B: prior}}}}
    expected = report_losses(data)
    assert len(expected) == 1
    assert_losses(solver_losses(data), expected)


def test_lead_turns_use_lead_decisions_and_the_same_report():
    shifts = [duty(i, i, [A, B]) for i in range(1, 7)]
    for shift in shifts:
        shift['type_snapshot']['definition']['lead'] = {'minimum': 1, 'maximum': 1,
                                                        'capability_ids': [], 'category_ids': []}
        shift['participants'][0]['team_lead'] = True
    data = world(shifts)
    data['policies'] = [policy({'metric': 'lead_turns'})]
    assert_losses(solver_losses(data, lead=True), report_losses(data))


def test_coprime_goal_quantities_fail_with_structured_precision_error():
    shifts = [duty(i, i, [A]) for i in (1, 2, 3)]
    for shift, denominator in zip(shifts, (999983, 999979, 999961)):
        shift['type_snapshot']['definition']['session_units'] = 1 / denominator
    data = world(shifts)
    data['policies'] = [policy({'metric': 'session_units'})]
    with pytest.raises(ScorePrecisionError, match='Fair-share quantities'):
        solver_losses(data)


@pytest.mark.parametrize('case', ['left', 'right', 'negative', 'offset', 'sum', 'nonlinear'])
def test_fair_share_product_rejects_unsafe_scalar_and_affine_spans_before_native_construction(case):
    from packages.domain.cp_sat_goals import _multiply
    from packages.domain.cp_sat_score import ScoreModel

    model = cp_model.CpModel()
    score = ScoreModel(model)
    x = model.new_bool_var('quantity')
    with pytest.raises(ScorePrecisionError, match='Fair-share quantities'):
        if case == 'left':
            _multiply(score, 2**62, x)
        elif case == 'right':
            _multiply(score, x, 2**62)
        elif case == 'negative':
            _multiply(score, x, -2**62)
        elif case == 'offset':
            _multiply(score, x, 1, offset=-2**62)
        elif case == 'sum':
            _multiply(score, x, 2**61, offset=2**61)
        else:
            _multiply(score, x, model.new_int_var(0, 2**62, 'wide'))


@pytest.mark.parametrize('coefficient', [-(2**62 - 1), -3, 3, 2**62 - 1])
def test_fair_share_product_keeps_supported_signed_bounds_and_exact_fixed_zero(coefficient):
    from packages.domain.cp_sat_goals import _multiply
    from packages.domain.cp_sat_score import ScoreModel

    model = cp_model.CpModel()
    score = ScoreModel(model)
    x = model.new_int_var(-1, 1, 'quantity')
    result = _multiply(score, coefficient, x)
    assert score.bounds(result) == (-abs(coefficient), abs(coefficient))
    assert _multiply(score, model.new_int_var(0, 0, 'fixed_zero'), 10**22) == 0
    assert model.validate() == ''


@pytest.mark.parametrize('setting', [
    {'share_adjustment': {'mode': 'percent', 'value': 1e-20}},
    {'share_adjustment': {'mode': 'amount', 'value': 1e-20}},
    {'share_adjustment': {'mode': 'amount', 'value': -1e-20}},
    {'tolerance_percent': 1e-20},
    {'tolerance_units': 1e-20},
], ids=['percent', 'amount', 'negative-amount', 'tolerance-percent', 'tolerance-units'])
def test_contract_valid_small_dials_fail_with_structured_compiler_precision_error(setting):
    from packages.contracts.schemas import PolicyDefinition

    data = world([duty(i, i, [A] if i < 3 else [B]) for i in range(1, 5)])
    goal = catalogue()
    goal['definition']['rule']['lines'][0]['settings'] = {'balance_goal': setting}
    goal['definition'] = PolicyDefinition.model_validate(goal['definition']).model_dump(mode='json')
    data['policies'] = [goal]
    with pytest.raises(ScorePrecisionError, match='Fair-share quantities'):
        solver_losses(data)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('field', ['tolerance_percent', 'tolerance_units'])
def test_workload_reduces_then_rejects_unsupported_dial_spans_before_native_construction(measure, field):
    from tests.test_fairness_context_cache import modern

    data = modern(world([duty(i, i, [A]) for i in range(1, 5)]))
    data.update(policies=[policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                                 'tolerance_percent': 0, 'tolerance_units': 0, field: 1e-20})],
                generation_options={'balance_measure': measure})
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        solver_losses(data)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('zero_demand', [False, True])
def test_unused_workload_precision_keeps_neutral_loss_and_presence(measure, zero_demand):
    from packages.domain.cp_sat_score import workload_component
    from tests.test_fairness_context_cache import modern

    data = modern(world([duty(i, 1 + i % 30) for i in range(500)]))
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                   'tolerance_percent': 0, 'tolerance_units': 1.0000000000000002 if measure == 'duties'
                   else 4.000000000000001})
    if zero_demand:
        for shift in data['shifts']:
            shift['type_snapshot']['definition']['requirements'] = [{'minimum': 0}]
    data.update(policies=[goal], generation_options={'balance_measure': measure})
    model = cp_model.CpModel()
    participants = {(shift['id'], pid): {'person_id': pid, 'start': shift['start'], 'end': shift['end']}
                    for shift in data['shifts'] for pid in (A, B)}
    choices = {key: model.new_bool_var('assigned') if zero_demand else model.new_int_var(0, 0, 'fixed_zero')
               for key in participants}
    parameters = fairness.workload_parameters(data, goal)
    assert parameters['present'] and parameters['people'] == [A, B]
    assert (parameters['demand'] == 0) is zero_demand
    assert workload_component(model, data, choices, participants, measure, policy=goal) == (0, 1)
    report = fairness.goal_report(data, goal)
    assert report['value'] == 1 and report['score_present']
    assert model.validate() == ''


def test_workload_variation_denominator_is_guarded_even_when_each_load_span_fits():
    from packages.domain.cp_sat_score import workload_component
    from tests.test_fairness_context_cache import modern

    data = modern(world([duty(1, 5), duty(2, 6)]))
    people = [uid(1000 + index) for index in range(32)]
    data['people'] = {pid: {**data['people'][A], 'display_name': pid} for pid in people}
    data['memberships'] = [{**data['memberships'][0], 'person_id': pid} for pid in people]
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['requirements'] = [{'minimum': 16, 'maximum': 16}]
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                   'tolerance_percent': 0, 'tolerance_units': .9999999999999999})
    data.update(policies=[goal], generation_options={'balance_measure': 'duties'})
    parameters = fairness.workload_parameters(data, goal)
    assert len(parameters['people']) == 32 and parameters['demand'] == 32
    assert set(parameters['limits'].values()) == {Fraction(19_999_999_999_999_999, 10**16)}
    assert 32 * 32 * 10**16 > 2**63 > 2**62 > 32 * 2 * 10**16
    model = cp_model.CpModel()
    participants = {(shift['id'], pid): {'person_id': pid, 'start': shift['start'], 'end': shift['end']}
                    for shift in data['shifts'] for pid in people}
    choices = {key: model.new_bool_var('assigned') for key in participants}
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        workload_component(model, data, choices, participants, 'duties', policy=goal)


@pytest.mark.parametrize('operation', ['divide', 'ratio'])
def test_score_division_rejects_unsafe_native_constant_denominators(operation):
    from packages.domain.cp_sat_score import ScoreModel

    model = cp_model.CpModel()
    score = ScoreModel(model)
    value = model.new_bool_var('variable_numerator')
    with pytest.raises(ScorePrecisionError, match='integer precision'):
        getattr(score, operation)(value, 10**20)


@pytest.mark.parametrize('operation', ['divide', 'ratio'])
def test_score_division_keeps_exact_constants_and_fixed_zero_without_native_admission(operation):
    from packages.domain.cp_sat_score import ScoreModel

    model = cp_model.CpModel()
    score = ScoreModel(model)
    assert getattr(score, operation)(3, 10**20) == 0
    assert getattr(score, operation)(model.new_int_var(0, 0, 'fixed_zero'), 10**20) == 0
    assert model.validate() == ''


def test_score_ratio_reduces_exact_coefficients_before_native_denominator_admission():
    from packages.domain.cp_sat_score import ScoreModel

    model = cp_model.CpModel()
    score = ScoreModel(model)
    value = model.new_bool_var('variable_numerator')
    ratio = score.ratio(10**13 * value, 10**19)
    assert score.bounds(ratio) == (0, 1)
    model.add(value == 1)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(ratio) == 1 and model.validate() == ''


def test_goal_cost_midpoint_has_the_same_quantization_in_report_and_model():
    shift = duty(1, 1, [A])
    shift['type_snapshot']['definition']['session_units'] = 3.0021
    data = world([shift])
    data['policies'] = [policy({'metric': 'session_units', 'aim': 'target', 'amount': 2}, {'person_ids': [A]})]
    [report] = fairness.goal_reports(data)
    assert report['people'][0]['cost'] == 0.0011
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('adjustments', [
    ({'mode': 'percent', 'value': 50}, {'mode': 'percent', 'value': 150}),
    ({'mode': 'percent', 'value': 0}, {'mode': 'percent', 'value': 0}),
    ({'mode': 'amount', 'value': 2}, {'mode': 'amount', 'value': -1}),
    ({'mode': 'amount', 'value': -100}, {'mode': 'amount', 'value': -100}),
    ({'mode': 'amount', 'value': -100}, {'mode': 'percent', 'value': 100}),
])
@pytest.mark.parametrize('tolerance', [(0, 0), (25, 0.5)])
def test_line_share_dials_and_tolerance_match_report(adjustments, tolerance):
    data = world([duty(i, i, [A] if i < 8 else [B]) for i in range(1, 9)])
    goal = catalogue()
    for line, adjustment in zip(goal['definition']['rule']['lines'], adjustments):
        line['settings'] = {'balance_goal': {'share_adjustment': adjustment,
                                            'tolerance_percent': tolerance[0], 'tolerance_units': tolerance[1]}}
    data['policies'] = [goal]
    assert_losses(solver_losses(data), report_losses(data))
    [report] = fairness.goal_reports(data)
    assert sum(row['expected'] for row in report['people']) == pytest.approx(8, abs=0.01)


def test_off_line_is_excluded_from_solver_goal_audience():
    data = world([duty(i, i, [A] if i < 4 else [B]) for i in range(1, 5)])
    goal = catalogue()
    goal['definition']['rule']['lines'][0]['numbers']['apply']['level'] = 'off'
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    assert [row['person_id'] for row in report['people']] == [B]
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('emphasis', [0, 0.8])
def test_configured_worst_off_emphasis_matches_report(emphasis):
    data = world([duty(i, i, [A]) for i in range(1, 9)])
    data['policies'] = [policy({'metric': 'duties'})]
    data['generation_options'] = {'scoring': {'fairness_worst_off_emphasis': emphasis}}
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('emphasis,expected_value,integer_weight', [(0, 0.5, 0), (0.5, 0.375, 5), (0.8, 0.3, 8)])
def test_one_worst_off_setting_controls_score_and_solver(emphasis, expected_value, integer_weight):
    data = world([duty(i, i, [A] if i < 4 else [B]) for i in range(1, 5)])
    data['policies'] = [policy({'metric': 'duties', 'tolerance_percent': 0, 'tolerance_units': 0})]
    data['generation_options'] = {'scoring': {'fairness_worst_off_emphasis': emphasis}}
    value = schedule_score(data, {'coverage': [], 'findings': []})['components']['fairness']['value']
    assert value == pytest.approx(expected_value)
    assert_losses(solver_losses(data), [(Fraction(1), 1 - expected_value)])

    model = cp_model.CpModel()
    choices = {(shift['id'], row['person_id']): model.new_constant(1)
               for shift in data['shifts'] for row in shift['participants']}
    participants = {(shift['id'], row['person_id']): row for shift in data['shifts'] for row in shift['participants']}
    terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS)
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.objective_value == 1500 * (2 * integer_weight + 1)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('adjustment', [{'mode': 'percent', 'value': 50}, {'mode': 'amount', 'value': -1}])
def test_explicit_workload_goal_dials_match_fixed_budget_report(measure, adjustment):
    data = world([duty(i, i + 4, [A]) for i in range(6)])
    for person in data['people'].values():
        person['active'] = True
    data['categories'] = {'rank': {}}
    for row in data['memberships']:
        row['category_id'] = 'rank'
    data['generation_options'] = {'balance_measure': measure}
    goal = catalogue()
    goal['definition']['rule']['settings']['balance_goal'].update(metric='workload', aim='workload_spread',
                                                               tolerance_percent=0, tolerance_units=0)
    goal['definition']['rule']['lines'][0]['settings'] = {'balance_goal': {'share_adjustment': adjustment}}
    data['policies'] = [goal]
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('inactive_flag', ['closed', 'cancelled'])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_explicit_workload_goal_excludes_inactive_duties_from_compiled_and_reported_load(inactive_flag, measure):
    data = world([duty(i, i, [A]) for i in range(1, 7)])
    data['shifts'][0][inactive_flag] = True
    for person in data['people'].values():
        person['active'] = True
    data['categories'] = {'rank': {}}
    for row in data['memberships']:
        row['category_id'] = 'rank'
    data['generation_options'] = {'balance_measure': measure}
    goal = catalogue()
    goal['definition']['rule']['settings']['balance_goal'].update(
        metric='workload', aim='workload_spread', tolerance_percent=0, tolerance_units=0)
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    assert sum(row['planned'] for row in report['people']) == (5 if measure == 'duties' else 20)
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('personal', [{'mode': 'percent', 'value': 50}, {'mode': 'amount', 'value': 2}])
@pytest.mark.parametrize('legacy', [False, True])
def test_dated_line_dial_uses_each_winning_available_day(personal, legacy):
    data = world([duty(i, i, [A]) for i in range(1, 9)])
    goal = catalogue()
    broad, dated = goal['definition']['rule']['lines']
    broad.pop('who')
    dated['who'] = {'conditions': [{'field': 'person_ids', 'values': [A]}]}
    dated.update(starts_on='2026-10-01', ends_on='2026-10-16',
                 settings={'balance_goal': {'share_adjustment': personal}})
    data['policies'] = [goal]
    if legacy:
        data['fairness_semantics'] = 1
    [report] = fairness.goal_reports(data)
    if not legacy:
        expected = fairness.expected_shares(8, {A: 31, B: 31}, {A: personal})[A]
    elif personal['mode'] == 'percent':
        expected = 8 * Fraction(16 + 15 * 0.5) / Fraction(31 + 16 + 15 * 0.5)
    else:
        offset = Fraction(2 * 15, 31)
        expected = (4 + offset) * 8 / (8 + offset)
    assert next(row['expected'] for row in report['people'] if row['person_id'] == A) == round(float(expected), 2)
    assert_losses(solver_losses(data), report_losses(data))


def test_workload_available_days_share_uses_matching_membership_days():
    data = world([duty(i, i + 16, [A]) for i in range(6)])
    for person in data['people'].values():
        person['active'] = True
    data['categories'] = {'rank': {}}
    for row in data['memberships']:
        row['category_id'] = 'rank'
        if row['person_id'] == B:
            row['starts_on'] = '2026-10-16'
    data['generation_options'] = {'balance_measure': 'duties'}
    goal = catalogue()
    goal['definition']['rule']['settings']['balance_goal'].update(metric='workload', aim='workload_spread',
                                                               share_by='available_days', tolerance_percent=0,
                                                               tolerance_units=0)
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    assert next(row['expected'] for row in report['people'] if row['person_id'] == A) == pytest.approx(6 * 31 / 47)
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('distribution', ['share', 'period_spread', 'pair_limit'])
@pytest.mark.parametrize('populated', [False, True])
def test_distribution_goals_match_report_normalization_and_active_audience(distribution, populated):
    people = [A, B] if distribution == 'pair_limit' else [A]
    shifts = [duty(i, i + 1, people if populated else []) for i in range(1, 5)]
    data = world(shifts, last='2026-10-15')
    settings = ({'within': {'conditions': [{'field': 'service_ids', 'values': [uid(70)]}]},
                 'target_share': 0.3} if distribution == 'share' else
                {'metric': 'pair_exposure', 'aim': 'target', 'amount': 1.3,
                 'partners': {'conditions': [{'field': 'person_ids', 'values': [B]}]}}
                if distribution == 'pair_limit' else {})
    data['policies'] = [policy({'metric': 'duties', 'distribution': distribution, 'weight': 0.125, **settings})]
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('distribution', ['people', 'share', 'period_spread', 'pair_limit'])
def test_score_goals_ignore_closed_recurring_sessions(distribution):
    shifts = [duty(i, i, [A, B]) for i in range(1, 5)]
    shifts[0]['closed'] = True
    data = world(shifts)
    settings = ({'within': {'conditions': [{'field': 'service_ids', 'values': [uid(70)]}]}}
                if distribution == 'share' else
                {'metric': 'pair_exposure', 'aim': 'target', 'amount': 2.5,
                 'partners': {'conditions': [{'field': 'person_ids', 'values': [B]}]}}
                if distribution == 'pair_limit' else {})
    data['policies'] = [policy({'metric': 'duties', 'distribution': distribution, **settings})]
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize('aim', ['equal_share', 'max_min'])
@pytest.mark.parametrize('metric', ['overtime_duties', 'overtime_hours'])
@pytest.mark.parametrize('offered', [(4, 8), (0, 8), (0, 0)])
def test_offered_share_score_matches_report_and_no_offer_neutrality(aim, metric, offered):
    from tests.test_overtime_offered import offer
    shifts = [duty(i, i, [A] if i < 7 else [B]) for i in range(1, 8)]
    for shift in shifts:
        shift['participants'][0]['overtime'] = True
    data = world(shifts)
    data['scheduling_facts'] = []
    for pid, amount in zip((A, B), offered):
        if amount:
            offer(data, pid, pid, start='08:00', end=f'{8 + amount:02}:00')
    goal = policy({'metric': metric, 'aim': aim, 'share_by': 'offered', 'weight': 0.125})
    data['policies'] = [goal]
    data['fairness_history'] = {fairness.goal_identity(goal): {'people': {A: {'value': 2.5}, B: {'value': 0.75}}}}
    assert_losses(solver_losses(data, overtime=True), report_losses(data))


@pytest.mark.parametrize('offered', [(0, 8), (4, 8)])
def test_offered_shares_apply_line_dials_without_giving_nonvolunteers_a_share(offered):
    from tests.test_overtime_offered import offer
    data = world([duty(i, i, [A] if i < 7 else [B]) for i in range(1, 8)])
    data['scheduling_facts'] = []
    for shift in data['shifts']:
        shift['participants'][0]['overtime'] = True
    for pid, amount in zip((A, B), offered):
        if amount:
            offer(data, pid, pid, start='08:00', end=f'{8 + amount:02}:00')
    goal = catalogue(metric='overtime_duties')
    goal['definition']['rule']['settings']['balance_goal'].update(share_by='offered', tolerance_percent=0, tolerance_units=0)
    for line, value in zip(goal['definition']['rule']['lines'], (2, -1)):
        line['settings'] = {'balance_goal': {'share_adjustment': {'mode': 'amount', 'value': value}}}
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    if not offered[0]:
        row = next(row for row in report['people'] if row['person_id'] == A)
        assert row['expected'] == row['cost'] == 0
    assert sum(row['expected'] for row in report['people']) == pytest.approx(7, abs=0.01)
    assert_losses(solver_losses(data, overtime=True), report_losses(data))


def test_grouped_custom_target_reuses_canonical_source_rounding():
    data = world([duty(i, i, [A]) for i in (1, 2)], last='2026-10-10')
    goal = catalogue(amounts=(0.1, 0.1))
    goal['definition']['rule']['settings']['balance_goal'].update(
        period='custom', starts_on='2026-10-01', ends_on='2026-10-21')
    data['policies'] = [goal]
    [report] = fairness.goal_reports(data)
    assert {row['expected'] for row in report['people']} == {0.05}
    assert next(row['cost'] for row in report['people'] if row['person_id'] == A) == 0.95
    assert_losses(solver_losses(data), report_losses(data))


def test_yearly_hours_compile_at_department_scale_with_fractional_history():
    data = world([duty(i, 1 + i % 30) for i in range(500)])
    people = [uid(1000 + i) for i in range(150)]
    data['people'] = {pid: {**data['people'][A], 'display_name': pid} for pid in people}
    data['memberships'] = [{**data['memberships'][0], 'person_id': pid} for pid in people]
    goal = policy({'metric': 'hours', 'period': 'year'})
    data['policies'] = [goal]
    data['fairness_history'] = {fairness.goal_identity(goal): {'people': {
        pid: {'value': 5.1167, 'available': 333 if i == 0 else 334} for i, pid in enumerate(people)}}}
    model, choices, participants = cp_model.CpModel(), {}, {}
    for shift in data['shifts']:
        shift['end'] = shift['start'].replace('08:00', '12:07')
        for pid in people:
            key = shift['id'], pid
            choices[key] = model.new_bool_var('assigned')
            participants[key] = {'person_id': pid, 'start': shift['start'], 'end': shift['end']}
    terms = add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS)
    assert len(choices) == 75_000
    assert len(terms) == 1
    assert model.validate() == ''
