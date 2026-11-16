"""Assignment trials reuse fixed Fair share inputs, while counts and narrowed views stay current."""
from copy import deepcopy
from datetime import date

import pytest

from packages.domain import fairness as f, score_normalization
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world


def uncached(data):
    return {key: value for key, value in data.items()
            if key not in {'_distribution_cache', '_workload_normalization_cache'}}


def modern(data):
    data['categories'] = {'rank': {}}
    for person in data['people'].values():
        person['active'] = True
    for row in data['memberships']:
        row['category_id'] = 'rank'
    data['eligibilities'] = []  # These synthetic duties name no clinics.
    return data


def test_assignment_trials_reuse_exact_share_terms_and_keep_live_counts(monkeypatch):
    data = world([duty(i, i + 4, [A] if i < 5 else []) for i in range(1, 6)])
    source = deepcopy(data)
    goal = policy({'metric': 'duties', 'tolerance_percent': 0, 'tolerance_units': 0})
    data['_distribution_cache'] = {}
    calls, original = [], f.expected_share_terms

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(f, 'expected_share_terms', counted)
    before = f.goal_report(data, goal)
    trial = {**data, 'shifts': [*data['shifts'][:4], duty(5, 9, [B])]}
    after = f.goal_report(trial, goal)
    assert len(calls) == 1
    assert before['people'] != after['people']
    assert after == f.goal_report(uncached(trial), goal)
    edited = deepcopy(goal)
    edited['definition']['rule']['tolerance_units'] = 2
    assert f.goal_report(trial, edited) == f.goal_report(uncached(trial), edited)
    assert f.goal_report(trial, edited)['score'] != after['score']
    assert uncached(data) == source


def test_goal_audience_cache_distinguishes_filtered_and_fixed_usual_views():
    data = modern(world([duty(1, 5, [B]), duty(2, 6)]))
    data['shifts'][0]['series_snapshot'] = {'definition': {
        'usual_assignees_fixed': True, 'default_person_ids': [A]}}
    data['_distribution_cache'] = {}
    goal = policy({'metric': 'duties'})
    assert f.goal_people(data, goal)['who'] == [A, B]
    fixed_only = {**data, 'shifts': data['shifts'][:1]}
    assert f.goal_people(fixed_only, goal)['who'] == [A, B]
    participants = data['shifts'][0]['participants']
    data['shifts'][0]['participants'] = []
    assert f.goal_people(fixed_only, goal)['who'] == [A]
    assert f.goal_people(fixed_only, goal) == f.goal_people(uncached(fixed_only), goal)
    data['shifts'][0]['participants'] = participants
    assert f.goal_people(data, goal)['who'] == [A, B]
    filtered = {**data, 'shifts': data['shifts'][:1]}
    assert filtered['focus_ids'] == data['focus_ids']
    assert f.goal_people(filtered, goal)['who'] == [A, B]
    trial = {**filtered, 'shifts': [{**filtered['shifts'][0], 'participants': []}]}
    assert f.goal_people(trial, goal)['who'] == [A]
    assert f.goal_people(trial, goal) == f.goal_people(uncached(trial), goal)
    closed = {**data, 'shifts': [{**s, 'closed': True} for s in data['shifts']]}
    assert f.goal_people(closed, goal)['who'] == []
    assert f.goal_people(data, goal)['who'] == [A, B]
    focus = list(data['focus_ids'])
    refocused = {**data, 'focus_ids': focus}
    assert f.goal_people(refocused, goal)['who'] == [A, B]
    focus.clear()
    assert f.goal_people(refocused, goal)['who'] == []
    assert f.goal_people(refocused, goal) == f.goal_people(uncached(refocused), goal)
    fresh = {**data, 'people': {pid: {**person, 'active': pid != B} for pid, person in data['people'].items()},
             '_distribution_cache': {}}
    assert f.goal_people(fresh, goal)['who'] == [A]
    assert f.goal_people(fresh, goal) == f.goal_people(uncached(fresh), goal)


def test_goal_applicability_reuses_only_person_independent_scopes_and_periods(monkeypatch):
    from packages.domain.time import aware

    shift = duty(1, 5)
    data = world([shift])
    data['_policy_applies_cache'] = {}
    start, end = aware(shift['start']), aware(shift['end'])
    calls, original = [], f._goal_applies

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(f, '_goal_applies', counted)
    goal = policy({'metric': 'duties'})
    assert all(f.goal_applies(data, goal, pid, shift, start, end) for pid in (A, B))
    assert len(calls) == 1
    scoped = policy({'metric': 'duties'}, {'person_ids': [A]})
    assert f.goal_applies(data, scoped, A, shift, start, end)
    assert not f.goal_applies(data, scoped, B, shift, start, end)
    assert len(calls) == 3
    block = policy({'metric': 'duties', 'period': 'block'})
    data['rotations'] = [{'person_id': A, 'starts_on': '2026-10-01', 'ends_on': '2026-10-03'},
                         {'person_id': B, 'starts_on': '2026-10-01', 'ends_on': '2026-10-10'}]
    assert not f.goal_applies(data, block, A, shift, start, end)
    assert f.goal_applies(data, block, B, shift, start, end)
    assert len(calls) == 5
    # A fresh read with edited facts has no opt-in cache and evaluates the canonical predicate again.
    fresh = {key: value for key, value in data.items() if key != '_policy_applies_cache'}
    assert f.goal_applies(fresh, scoped, A, shift, start, end)
    assert len(calls) == 6


@pytest.mark.parametrize('dimension', ['rank', 'skill', 'where', 'sources'])
def test_cached_goal_applicability_matches_dated_source_and_condition_scopes(dimension):
    from packages.domain.time import aware

    rank, other_rank, skill, clinic, other_clinic = [uid(n) for n in range(30, 35)]
    data = world([duty(1, 5, services=[clinic]), duty(2, 15, services=[other_clinic])])
    data['categories'] = {rank: {}, other_rank: {}}
    for member in data['memberships']:
        member['category_id'] = rank if member['person_id'] == A else other_rank
        if member['person_id'] == A:
            member['ends_on'] = '2026-10-10'
    data['capabilities'] = {skill: {}}
    data['grants'] = [{'person_id': A, 'department_id': data['workspace']['department_id'],
                       'capability_id': skill, 'service_id': None,
                       'starts_on': '2026-10-01', 'ends_on': '2026-10-10'}]
    scopes = {'rank': {'category_ids': [rank]}, 'skill': {'capability_ids': [skill]}}
    if dimension == 'sources':
        first = policy({'metric': 'duties'}, {'person_ids': [A]})
        second = {**policy({'metric': 'duties', 'service_ids': [clinic]}), 'id': uid(35)}
        goal = f.comparison_goal([first, second])
    elif dimension == 'where':
        goal = policy({'metric': 'duties', 'service_ids': [clinic]})
    else:
        goal = policy({'metric': 'duties'}, scopes[dimension])
    cached = {**data, '_policy_applies_cache': {}, '_policy_applies_people': True, '_line_index_cache': {}}
    for _ in range(2):
        for shift in data['shifts']:
            for pid in (A, B):
                args = goal, pid, shift, aware(shift['start']), aware(shift['end'])
                assert f.goal_applies(cached, *args) == f.goal_applies(data, *args)
    sources = [entry for key, entry in f.measurement_context(cached)['_policy_applies_cache'].items()
               if isinstance(key, tuple) and key[0] == 'measurement_source']
    assert 1 <= len(sources) <= (2 if dimension == 'sources' else 1)
    assert all(original is not measured and measured['starts_on'] is None
               and original['starts_on'] == '2020-01-01' for original, measured in sources)


def test_cached_report_preserves_dated_membership_audiences_and_available_days():
    data = world([duty(1, 5, [A]), duty(2, 15, [B])])
    data['memberships'][0]['ends_on'] = '2026-10-10'
    data['memberships'][1]['starts_on'] = '2026-10-10'
    goal = policy({'metric': 'duties'})
    cached = {**data, '_distribution_cache': {}, '_policy_applies_cache': {}, '_person_dates_cache': {}}
    before = f.goal_report(cached, goal)
    assert before == f.goal_report(data, goal)
    assert {r['person_id']: r['available_days'] for r in before['people']} == {A: 9, B: 22}


def test_equal_policy_scopes_share_only_the_complete_immutable_predicate(monkeypatch):
    from packages.domain import validation
    from packages.domain.time import aware

    shift = duty(1, 5)
    data = world([shift])
    cached = {**data, '_policy_applies_cache': {}, '_policy_applies_people': True}
    start, end = aware(shift['start']), aware(shift['end'])
    first = policy({'metric': 'duties'})
    second = {**policy({'metric': 'hours'}), 'id': uid(40), 'name': 'Another goal, same matching inputs'}
    second['definition']['scope'] = dict(reversed(list(first['definition']['scope'].items())))
    calls, original = [], validation._policy_applies

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(validation, '_policy_applies', counted)
    for source in (first, second):
        for pid in (None, A, B):
            assert validation.policy_applies(cached, source, pid, shift, start, end) == original(
                data, source, pid, shift, start, end)
    assert len(calls) == 2, 'One answer for real people and a separate answer for person-less reach'
    variants = [{'status': 'retired'}, {'active': False}, {'department_id': uid(41)},
                {'starts_on': '2026-10-06'}, {'ends_on': '2026-10-05'},
                {'definition': {**first['definition'], 'scope': {'person_ids': [B]}}}]
    for change in variants:
        source = {**deepcopy(first), **change}
        for pid in (None, A, B):
            assert validation.policy_applies(cached, source, pid, shift, start, end) == original(
                data, source, pid, shift, start, end)


def test_workload_parameters_reuse_only_the_same_measure_and_session_view(monkeypatch):
    data = modern(world([duty(1, 5, [A]), duty(2, 6)]))
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                   'tolerance_percent': 0, 'tolerance_units': 0})
    data.update(_distribution_cache={}, _workload_normalization_cache={},
                generation_options={'balance_measure': 'duties'})
    calls, original = [], score_normalization.workload_normalization

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(score_normalization, 'workload_normalization', counted)
    before = f.workload_parameters(data, goal)
    prepared = len(calls)
    trial = {**data, 'shifts': [{**s, 'participants': []} for s in data['shifts']]}
    assert f.workload_parameters(trial, goal) == before
    assert len(calls) == prepared
    filtered = {**trial, 'shifts': trial['shifts'][:1]}
    assert f.workload_parameters(filtered, goal)['demand'] == 1
    hours = {**filtered, 'generation_options': {'balance_measure': 'reserved_hours'}}
    assert f.workload_parameters(hours, goal)['demand'] == 4 * 3_600_000_000
    for view in (trial, filtered, hours):
        assert f.workload_parameters(view, goal) == f.workload_parameters(uncached(view), goal)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('cached', [False, True])
def test_fixed_usual_workload_keeps_its_audience_without_adding_a_singleton_score(measure, cached):
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_goals import add_score_goals
    from packages.domain.cp_sat_score import workload_component

    data = modern(world([duty(n, 5 + n, [A]) for n in range(4)]))
    for session in data['shifts']:
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': [A]}}
    first = data['shifts'][0]
    first['participants'].append({**first['participants'][0], 'person_id': B})
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                   'tolerance_percent': 0, 'tolerance_units': 0})
    data.update(policies=[goal], generation_options={'balance_measure': measure})
    if cached:
        data.update(_distribution_cache={}, _workload_normalization_cache={})
    before = f.workload_parameters(data, goal)
    assert before['people'] == [A] and not before['legacy']
    assert not before['present'], 'One unchanged share must retain the former built-in component absence'
    assert before['demand'] == (4 if measure == 'duties' else 16 * 3_600_000_000)
    report = f.goal_report(data, goal)
    assert report['value'] == 1 and report['score'] == 100 and not report['score_present']
    removed = {**data, 'shifts': [{**session, 'participants': [p for p in session['participants']
                                  if p['person_id'] != B]} for session in data['shifts']]}
    for view in (data, removed):
        assert f.workload_parameters(view, goal) == f.workload_parameters(uncached(view), goal) == before
        assert f.goal_report(view, goal) == f.goal_report(uncached(view), goal) == report
        participants = {(session['id'], p['person_id']): p for session in view['shifts'] for p in session['participants']}
        choices = {key: int(key[1] == A) for key in participants}
        model, presence = cp_model.CpModel(), []
        terms = add_score_goals(model, view, choices, participants, {}, {}, set(view['focus_ids']),
                                f.strengths_of(view), presence=presence)
        assert terms == presence == []
        assert workload_component(model, view, choices, participants, measure, policy=goal) == (0, 0)
        assert f.report_value(report) == 1
        assert model.validate() == ''


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('tolerance', [0, 50])
def test_singleton_workload_presence_keeps_meaningful_custom_bounds_on_the_matching_grid(measure, cached, tolerance):
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_score import SCORE_SCALE, workload_component
    from packages.domain.fairness_distribution import people_marginal_cost

    data = modern(world([duty(n, 5 + n, [A]) for n in range(4)]))
    for index, session in enumerate(data['shifts']):
        session['type_snapshot']['definition']['requirements'] = [{'minimum': int(index < 2)}]
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': [A]}}
    unrelated = duty(10, 20)
    unrelated.update(assignment_type_id=uid(90), end=unrelated['end'].replace('12:00', '20:00'))
    data['types'][uid(90)] = deepcopy(next(iter(data['types'].values())))
    data['shifts'].append(unrelated)
    data['focus_ids'].append(unrelated['id'])
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                   'assignment_type_ids': [data['shifts'][0]['assignment_type_id']],
                   'tolerance_percent': tolerance, 'tolerance_units': 0})
    data.update(policies=[goal], generation_options={'balance_measure': measure})
    if cached:
        data.update(_distribution_cache={}, _workload_normalization_cache={})
    parameters = f.workload_parameters(data, goal)
    unit = 1 if measure == 'duties' else 4 * 3_600_000_000
    assert parameters['people'] == [A] and parameters['demand'] == 2 * unit
    assert parameters['limits'] == {A: (3 if tolerance else 2) * unit}
    assert parameters['present'] is bool(tolerance)
    report = f.goal_report(data, goal)
    assert report['score_present'] is bool(tolerance)
    assert report['value'] == (0.375 if tolerance else 0)
    assert f.report_value(report) == (report['value'] if tolerance else 1)
    assert parameters == f.workload_parameters(uncached(data), goal)
    assert report == f.goal_report(uncached(data), goal)
    baseline = {**data, 'shifts': [*data['shifts'][:3], {**data['shifts'][3], 'participants': []}, unrelated]}
    added = data['shifts'][3]['participants'][0]
    assert people_marginal_cost(baseline, goal, baseline['shifts'][3], added) == (0.625 if tolerance else 0)
    participants = {(s['id'], p['person_id']): p for s in data['shifts'] for p in s['participants']}
    choices = dict.fromkeys(participants, 1)
    model = cp_model.CpModel()
    loss, present = workload_component(model, data, choices, participants, measure, policy=goal)
    assert present == int(bool(tolerance))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert 1 - solver.value(loss) / SCORE_SCALE == f.report_value(report)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_singleton_zero_demand_or_zero_share_does_not_create_a_comparison(measure):
    data = modern(world([duty(1, 5, [A])]))
    data['shifts'][0]['series_snapshot'] = {'definition': {
        'usual_assignees_fixed': True, 'default_person_ids': [A]}}
    data.update(generation_options={'balance_measure': measure},
                _distribution_cache={}, _workload_normalization_cache={})
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'share_by': 'equal',
                   'share_adjustment': {'mode': 'percent', 'value': 0},
                   'tolerance_percent': 0, 'tolerance_units': 0})
    parameters = f.workload_parameters(data, goal)
    assert parameters['expected'][A] * parameters['unit'] == parameters['demand'] > 0
    assert not parameters['present']
    optional = {**data, 'shifts': [deepcopy(data['shifts'][0])], '_distribution_cache': {},
                '_workload_normalization_cache': {}}
    optional['shifts'][0]['type_snapshot']['definition']['requirements'] = [{'minimum': 0}]
    custom = deepcopy(goal)
    custom['definition']['rule']['tolerance_percent'] = 50
    parameters = f.workload_parameters(optional, custom)
    assert parameters['demand'] == 0 and not parameters['present']
    report = f.goal_report(optional, custom)
    assert report['value'] == f.report_value(report) == 1 and not report['score_present']


@pytest.mark.parametrize('usual_excluded', [None, 'inactive', 'membership'])
def test_cached_fixed_usual_workload_preserves_unassigned_qualified_trainees(usual_excluded):
    from tests.test_schedule_score import data, shift

    snapshot = data([shift('first', 4, ['a', 'c']), shift('second', 5, ['a'])])
    snapshot['categories']['trainee'] = {}
    for session in snapshot['shifts']:
        session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['a']}}
        session['type_snapshot']['definition']['supervision'] = {
            'trainee_category_ids': ['trainee'], 'capability_id': 'supervisor'}
    for row in snapshot['memberships']:
        if row['person_id'] == 'b':
            row.update(category_id='trainee', starts_on='2026-10-05')
        elif row['person_id'] == 'a' and usual_excluded == 'membership':
            row['ends_on'] = '2026-10-04'
    if usual_excluded == 'inactive':
        snapshot['people']['a']['active'] = False
    goal = {**policy({'metric': 'workload', 'aim': 'workload_spread'}),
            'department_id': snapshot['workspace']['department_id']}
    snapshot.update(policies=[goal], generation_options={'balance_measure': 'duties'},
                    _distribution_cache={}, _workload_normalization_cache={})
    before = f.workload_parameters(snapshot, goal)
    assert before['people'] == (['a', 'b'] if usual_excluded is None else ['b'])
    assert before['demand'] == 2 and not before['legacy']
    empty = {**snapshot, 'shifts': [{**session, 'participants': []} for session in snapshot['shifts']]}
    for view in (snapshot, empty):
        assert f.workload_parameters(view, goal) == f.workload_parameters(uncached(view), goal) == before


def test_legacy_assignment_based_workload_parameters_are_never_cached():
    data = world([duty(1, 5, [A]), duty(2, 6)])
    goal = policy({'metric': 'workload', 'aim': 'workload_spread'})
    data['_distribution_cache'] = {}
    before = f.workload_parameters(data, goal)
    trial = {**data, 'shifts': [data['shifts'][0], duty(2, 6, [B])]}
    after = f.workload_parameters(trial, goal)
    assert before['legacy'] and before['people'] == [A] and before['demand'] == 4 * 3_600_000_000
    assert not before['present']
    assert after['people'] == [A, B] and after['demand'] == 8 * 3_600_000_000
    assert after['present']
    assert after == f.workload_parameters(uncached(trial), goal)


def test_history_preload_uses_matching_dated_members_before_duties_are_loaded():
    data = world([], '2026-10-11', '2026-10-18')
    goal = policy({'metric': 'overtime_duties', 'period': 'month'}, {'person_ids': [A]})
    for field in ('shifts', 'focus_ids', 'availability'):
        data.pop(field)
    assert f.history_windows(data, goal, preload=True) == {A: (date(2026, 10, 1), date(2026, 10, 11))}
    # The conservative preload is explicit; ordinary audience evaluation still requires duty facts.
    with pytest.raises(KeyError, match='focus_ids'):
        f.history_windows(data, goal)


@pytest.mark.parametrize('preload', [False, True])
@pytest.mark.parametrize('explicit_period', [False, True])
@pytest.mark.parametrize('metric', ['duties', 'hours'])
def test_rota_history_does_not_resolve_people_with_no_historical_window(monkeypatch, preload, explicit_period, metric):
    data = world([], '2026-10-11', '2026-10-18')
    goal = policy({'metric': metric, 'period': 'rota'})
    if not explicit_period:
        goal['definition']['rule'].pop('period')

    def unexpected(*args, **kwargs):
        pytest.fail('A rota-period goal has no history to resolve for any person')

    monkeypatch.setattr(f, 'goal_people', unexpected)
    assert f.history_windows(data, goal, preload=preload) == {}
