from collections import defaultdict
from copy import deepcopy

import pytest
from pydantic import ValidationError
from packages.contracts.schemas import GenerationOptions, ScoreWeights
from packages.domain.cp_sat import generate
from packages.domain.score import flags, schedule_score
from packages.domain.time import aware
from packages.domain.validation import evaluate


def test_objective_order_requires_all_four_priorities_once():
    with pytest.raises(ValidationError):
        GenerationOptions(objective_order=['targets', 'targets', 'changes', 'balance'])


def test_reserved_hours_balance_and_configurable_stability_weight(h):
    first, second = h.seed['people'][:2]
    w = h.workspace()
    for day, end in ((4, 16), (5, 12), (6, 12)):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T{end:02}:00:00+03:00', people=[first])['workspace']
    for person in (first, second):
        w = h.shift(w, start='2026-10-07T08:00:00+03:00', end='2026-10-07T12:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['requirements'][0]['maximum'] = 1
        shift['locked'] = aware(shift['start']).day == 7
    data['generation_options'] = {'mode': 'rebalance', 'scoring': {'weights': {'fairness': 0, 'stability': 100}}}
    stable = generate(data, 5)
    assert stable['status'] == 'candidate_feasible', stable
    assert stable['removals'] == stable['additions'] == []
    data['generation_options'] = {'mode': 'rebalance'}
    balanced = generate(data, 5)
    assert balanced['status'] == 'candidate_feasible', balanced
    assert balanced['removals']
    assert [step['name'] for step in balanced['objectives']] == ['coverage', 'weighted']
    removed = {r['assignment_id'] for r in balanced['removals']}
    participants = [p for shift in data['shifts'] for p in shift['participants'] if p['id'] not in removed]
    participants += [row['participant'] for row in balanced['additions']]
    hours = defaultdict(float)
    for p in participants:
        hours[p['person_id']] += (aware(p['end']) - aware(p['start'])).total_seconds() / 3600
    assert sorted(hours.values()) == [12, 12]
    assert balanced['optimality_proven']
    assert balanced['objective_components']['fairness']['value'] == 1


def test_fairness_weight_only_balances_duties_with_a_goal(h):
    first, second = h.seed['people'][:2]
    # The default one-duty tolerance would accept the initial two-to-zero split.
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'rota',
                     'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    duty_type = h.configure('assignment_types', {'name': 'Single place duty', 'starts_on': '2020-01-01',
                            'definition': {'requirements': [{'minimum': 1, 'target': 1, 'maximum': 1}]}})['record']
    w = h.workspace()
    for day in (4, 5):
        w = h.shift(w, typ=duty_type['id'], start=f'2026-10-0{day}T08:00:00+03:00',
                    end=f'2026-10-0{day}T12:00:00+03:00', people=[first])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['generation_options'] = {'mode': 'rebalance', 'scoring': {'weights': {'fairness': 20}}}

    unbalanced = generate({**data, 'policies': []}, 5, deterministic=True)
    assert unbalanced['weighted_status'] == 'OPTIMAL'
    assert unbalanced['additions'] == unbalanced['removals'] == []
    assert unbalanced['compiled_component_values']['fairness'] is None
    assert unbalanced['summary']['score']['score'] == 100

    balanced = generate({**data, 'policies': [goal]}, 5, deterministic=True)
    assert balanced['weighted_status'] == 'OPTIMAL'
    assert len(balanced['removals']) == 1
    assert [row['participant']['person_id'] for row in balanced['additions']] == [second]
    assert balanced['compiled_component_values']['fairness'] == 1


def test_a_small_quota_miss_no_longer_outweighs_an_unfilled_seat(h):
    # A desired seat on a 30-minute session against an advisory 6-minute hours cap. The quota counted in tenths
    # of an hour cost 4 units against the seat's 1, so the seat stayed empty; desired cover now precedes weighted quality.
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'metric': 'reserved_hours',
              'comparison': 'maximum', 'amount': 0.1}, {'person_id': person}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Desired cover only', 'starts_on': '2020-01-01',
                      'definition': {'requirements': [{'minimum': 0, 'target': 1}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], end='2026-10-04T08:30:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert [a['participant']['person_id'] for a in result['additions']] == [person]
    assert [t['name'] for t in result['objectives']] == ['coverage', 'desired_coverage', 'weighted']
    assert [row['value'] for row in result['objectives'][:2]] == [0, 0]
    assert result['objectives'][-1]['value'] > 0
    score = result['summary']['score']
    assert score['components']['coverage']['lost'] == 0 and score['components']['rules']['value'] < 1
    assert round(sum(w['points'] for w in score['why']), 1) == round(100 - score['score'], 1)


def test_legacy_tolerance_does_not_change_the_weighted_tradeoff(h):
    # A works six sessions; four new sessions fall on A's leave, so B must take them: 4 changes at best, 6 : 4 hours.
    # Fairness outweighs one move; the retired tolerance cannot change that tradeoff.
    first, second = h.seed['people'][:2]
    w = h.workspace()
    for day in (4, 5, 6):
        for start, end in (('08', '12'), ('13', '17')):
            w = h.shift(w, start=f'2026-10-{day:02}T{start}:00:00+03:00', end=f'2026-10-{day:02}T{end}:00:00+03:00', people=[first])['workspace']
    for day in (7, 8):
        for start, end in (('08', '12'), ('13', '17')):
            w = h.shift(w, start=f'2026-10-{day:02}T{start}:00:00+03:00', end=f'2026-10-{day:02}T{end}:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['availability'].append({'id': 'leave', 'person_id': first, 'kind': 'recorded_leave',
                                 'start': '2026-10-07T00:00:00+03:00', 'end': '2026-10-09T00:00:00+03:00'})
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['requirements'][0]['maximum'] = 1
    data['generation_options'] = {'mode': 'rebalance'}
    strict = generate(data, 5, deterministic=True)
    assert strict['status'] == 'candidate_feasible', strict
    assert len(strict['additions']) == 5 and len(strict['removals']) == 1
    data['generation_options'] = {'mode': 'rebalance', 'scoring': {'tolerance': {'changes': 50}}}
    relaxed = generate(data, 5, deterministic=True)
    assert relaxed['status'] == 'candidate_feasible', relaxed
    assert len(relaxed['additions']) == 5 and len(relaxed['removals']) == 1
    assert relaxed['objective_components'] == strict['objective_components']


def test_filling_an_empty_rota_prefers_no_changes_without_changing_the_public_score_basis(h):
    """The solver keeps existing people; public scores omit a run baseline, just as the saved rota does."""
    first, second = h.seed['people'][:2]
    w = h.workspace()
    for day in (4, 5):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    filled = generate(data, 5)
    assert filled['status'] == 'candidate_feasible', filled
    assert filled['additions'] and not filled['removals']
    assert filled['summary']['score']['components']['stability']['value'] is None
    # The second person already on the first day stays (removing them is a change; filling the second day is not),
    # although workload fairness alone would not mind who works which day.
    w = h.shift(h.workspace('Kept'), start='2026-10-04T08:00:00+03:00', end='2026-10-04T12:00:00+03:00', people=[second])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    kept = generate(data, 5)
    assert kept['status'] == 'candidate_feasible' and not kept['removals'], kept
    assert kept['summary']['score']['components']['stability']['value'] is None


@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'])
@pytest.mark.parametrize('demand', ['minimum', 'type_target', 'scoped_cover', 'pooled_cover', 'quota'])
def test_fair_share_does_not_add_work_beyond_requested_staffing(h, mode, demand):
    from .scheduler_departments import apply

    person, other, _ = h.seed['people']
    requirement = {'minimum': 2}
    if demand == 'type_target':
        requirement = {'minimum': 1, 'target': 2}
    elif demand == 'scoped_cover':
        h.cover_target(2, {'person_ids': [person, other]})
        requirement = {'minimum': 1}
    elif demand == 'pooled_cover':
        h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
                 {'service_ids': h.seed['services'], 'person_ids': [person, other]})
        requirement = {'minimum': 1}
    elif demand == 'quota':
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                  'metric': 'duties', 'comparison': 'target', 'amount': 1},
                 {'person_id': other}, severity='advisory')
        requirement = {'minimum': 1}
    typ = h.configure('assignment_types', {'name': 'Requested staffing',
        'starts_on': '2020-01-01', 'definition': {'requirements': [requirement]}})['record']['id']
    goal = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'rota', 'share_by': 'equal',
                     'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    initial = h.shift(h.workspace(), typ=typ, people=[person])['workspace']
    data = h.snapshot(initial)
    data['policies'] = [p for p in data['policies']
                        if p['id'] == goal['id'] or (p['definition']['rule']['kind'] != 'balance_goal'
                                                       and p['definition']['rule'].get('id') != 'fair_share')]
    data['generation_options'] = {'mode': mode, 'shortlist_width': 0,
        'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 100, 'preferences': 0, 'stability': 0}}}

    result = generate(data, 5, workers=1, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    if demand in {'minimum', 'scoped_cover'}:
        assert ('staffing' in [row['name'] for row in result['objectives']]) == (demand == 'scoped_cover')
        assert 'Requested work and minimum staffing precede weighted quality.' in result['optimality_boundary']
    assigned = {row['person_id'] for row in apply(data, result)['shifts'][0]['participants']}
    assert len(assigned) == (1 if demand == 'quota' and mode == 'rebalance' else 2), result
    if demand in {'scoped_cover', 'pooled_cover'}:
        assert assigned == {person, other}
    if demand == 'quota':
        assert other in assigned


def test_workload_spread_alone_does_not_overfill_needed_sessions(h):
    from .scheduler_departments import apply

    first, second, third = h.seed['people']
    goal = h.policy({'kind': 'balance_goal', 'metric': 'workload', 'aim': 'workload_spread', 'period': 'rota',
                     'share_by': 'equal', 'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    # The existing overfill gives everyone one duty, so it ties the best workload score.
    # Rebalance can remove the extra assignment while keeping that score.
    w = h.shift(h.workspace(), people=[first])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[second, third])['workspace']
    data = h.snapshot(w)
    data['policies'] = [goal]
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0, 'balance_measure': 'duties',
        'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 100, 'preferences': 0, 'stability': 0}}}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    assert result['compiled_component_values']['fairness'] == 1, result
    assert [len(shift['participants']) for shift in apply(data, result)['shifts']] == [1, 1], result


def test_fair_share_keeps_the_required_team_lead_within_staffing_need(h):
    from .scheduler_departments import apply
    from .test_governed_leadership import activate, line, proposal

    lead = h.seed['people'][1]
    typ = h.seed['type']
    requirement = activate(h, proposal(h, [line(typ, [lead])]))
    goal = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'rota', 'share_by': 'equal',
                     'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=[])['workspace'])
    data['policies'] = [requirement, goal]
    data['generation_options'] = {'shortlist_width': 0,
        'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 100, 'preferences': 0, 'stability': 0}}}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    participants = apply(data, result)['shifts'][0]['participants']
    assert [(p['person_id'], p['team_lead']) for p in participants] == [(lead, True)], result


def test_fair_share_pairs_the_trainee_within_staffing_need(h):
    from .scheduler_departments import apply

    trainee, supervisor, spare = h.seed['people']
    rank, capability = h.seed['trainee_category'], h.seed['supervisor_capability']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank,
        'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Supervised duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}], 'supervision': {'trainee_category_ids': [rank],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']['id']
    goal = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'rota', 'share_by': 'equal',
                     'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace(), typ=typ, people=[
        {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'},
        {'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee',
         'supervisor_person_id': supervisor}])['workspace']
    data = h.snapshot(w)
    # Leave the trainee on the duty. The spare can work there but cannot supervise.
    placed = next(p for p in data['shifts'][0]['participants'] if p['person_id'] == trainee)
    data['shifts'][0]['participants'] = [{**placed, 'supervisor_person_id': None}]
    data['grants'] = [g for g in data['grants'] if (g['person_id'], g['capability_id']) != (spare, capability)]
    data['policies'] = [goal]
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0,
        'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 100, 'preferences': 0, 'stability': 0}}}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    participants = apply(data, result)['shifts'][0]['participants']
    assert len(participants) == 2, result
    assigned = {p['person_id']: p for p in participants}
    assert set(assigned) == {trainee, supervisor}, result
    assert assigned[trainee]['supervisor_person_id'] == supervisor, result


def test_fair_share_covers_a_required_capability_within_staffing_need(h):
    from .scheduler_departments import apply

    qualified = h.seed['people'][0]
    capability = h.seed['capability']
    typ = h.configure('assignment_types', {'name': 'Qualified duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1,
            'capabilities': [{'capability_id': capability, 'minimum': 1}]}]}})['record']['id']
    goal = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'rota', 'share_by': 'equal',
                     'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=[])['workspace'])
    data['grants'] = [g for g in data['grants'] if g['capability_id'] != capability or g['person_id'] == qualified]
    data['policies'] = [goal]
    data['generation_options'] = {'shortlist_width': 0,
        'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 100, 'preferences': 0, 'stability': 0}}}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    assert [p['person_id'] for p in apply(data, result)['shifts'][0]['participants']] == [qualified], result


def test_fair_share_allows_extra_staffing_only_for_the_mandatory_quota(h):
    from .scheduler_departments import apply

    first, quota_person, _ = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Quota duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                      'metric': 'duties', 'comparison': 'minimum', 'amount': 1, 'assignment_type_ids': [typ]},
                     {'person_id': quota_person})
    goal = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'rota', 'share_by': 'equal',
                     'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory')
    # Fill gaps keeps both existing assignments. Only the quota duty needs one more person.
    w = h.shift(h.workspace(), typ=typ, people=[first])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[first])['workspace']
    data = h.snapshot(w)
    data['policies'] = [quota, goal]
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0,
        'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 100, 'preferences': 0, 'stability': 0}}}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    [row] = result['validation']['quota_report']['rows']
    assert row['actual'] == row['amount'] == 1, result
    assert 'Requested work and minimum staffing precede weighted quality.' in result['optimality_boundary']
    assigned = {shift['assignment_type_id']: sorted(p['person_id'] for p in shift['participants'])
                for shift in apply(data, result)['shifts']}
    assert assigned == {typ: sorted([first, quota_person]), h.seed['type']: [first]}, result


def test_result_omits_the_staffing_sentence_without_fair_share(h):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    data['policies'] = []
    data['generation_options'] = {'shortlist_width': 0}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    assert 'Requested work and minimum staffing precede weighted quality.' not in result['optimality_boundary']


@pytest.mark.parametrize('seconds, expected', [(7, 7), (0, None)])
@pytest.mark.parametrize('fair_share', [False, True])
def test_stop_without_improvement_option_is_honoured(h, monkeypatch, seconds, expected, fair_share):
    from packages.domain.solver_control import SolverControl
    solve, seen = SolverControl.solve, []
    def watched(self, solver, model, tier):
        if tier is not None:
            name = self.context['objective_names'][tier]
            seen.append((name, self.stall_seconds, self.fairness_plateau, self.fairness_stall_seconds, self.context.get('candidates')))
        return solve(self, solver, model, tier)
    monkeypatch.setattr(SolverControl, 'solve', watched)
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['policies'] = [p for p in data['policies'] if p['definition']['rule'].get('id') != 'fair_share'
                        and p['definition']['rule']['kind'] != 'balance_goal']
    if fair_share:
        data['policies'].append(h.policy({'kind': 'balance_goal', 'metric': 'hours', 'period': 'rota',
            'tolerance_units': 0, 'tolerance_percent': 0}, severity='advisory'))
    data['generation_options'] = {'stop_without_improvement_seconds': seconds,
                                  'fairness_plateau_seconds': 23}
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert result['effective_options']['stop_without_improvement_seconds'] == seconds
    assert seen and all(value == (None if name == 'coverage' else expected)
                        and fairness == (fair_share and name == 'weighted' and candidates == 'all') and window == 23
                        for name, value, fairness, window, candidates in seen)
    assert ('fair_share_search' in result) is fair_share
    assert GenerationOptions().stop_without_improvement_seconds == 10


@pytest.mark.parametrize('order', [
    ['balance', 'targets', 'preferences', 'changes'],
    ['balance', 'preferences', 'targets', 'changes'],
    ['preferences', 'targets', 'changes', 'balance'],
    ['preferences', 'balance', 'targets', 'changes'],
    ['changes', 'targets', 'preferences', 'balance'],
])
def test_legacy_priority_order_cannot_change_weights_or_move_coverage(h, order):
    from .test_staff_wishes import wish
    first, second = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'One required place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.workspace()
    for day in (4, 5, 6):
        w = h.shift(w, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[first])['workspace']
    wish(h, '2026-10-04', last='2026-10-06', person=first, points=3, role='coordinator')
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0}
    reference = generate(data, 10, deterministic=True)
    data['generation_options'] = {'mode': 'rebalance', 'objective_order': order, 'shortlist_width': 0}
    result = generate(data, 10, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert result['optimality_proven'] and result['weighted_status'] == 'OPTIMAL'
    assert [t['name'] for t in result['objectives']] == ['coverage', 'weighted']
    removed = {r['assignment_id'] for r in result['removals']}
    kept = [p for s in data['shifts'] for p in s['participants'] if p['id'] not in removed]
    final = kept + [r['participant'] for r in result['additions']]
    assert len(final) == 3
    assert result['objective_components'] == reference['objective_components']
    assert [step['value'] for step in result['objectives']] == [step['value'] for step in reference['objectives']]
    # Priority presets cannot overrule Keep my manual edits or the required-place hard constraint.
    data['generation_options']['mode'] = 'fill_gaps'
    kept = generate(data, 10, deterministic=True)
    assert kept['status'] == 'candidate_feasible' and kept['validation']['valid']
    assert kept['additions'] == kept['removals'] == []


@pytest.mark.parametrize('tolerance', [0, 50])
def test_retired_preference_tolerance_keeps_the_same_weighted_result(h, tolerance):
    # A must work twice despite an off wish. Default fairness makes a third duty worthwhile in either stored format.
    from .test_staff_wishes import wish
    first, second = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'One required place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.workspace()
    for day in (4, 5, 6, 7, 8, 9):
        w = h.shift(w, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    wish(h, '2026-10-04', last='2026-10-09', person=first, points=6, role='coordinator')
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['availability'].append({'id': 'leave', 'person_id': second, 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00'})
    data['generation_options'] = {'objective_order': ['preferences', 'balance', 'targets', 'changes'],
        'shortlist_width': 0, 'scoring': {'tolerance': {'preferences': tolerance, 'balance': 0}}}
    result = generate(data, 10, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert [t['name'] for t in result['objectives']] == ['coverage', 'weighted']
    assert len(result['additions']) == 6 and result['removals'] == []
    assert sum(r['participant']['person_id'] == first for r in result['additions']) == 3
    assert result['weighted_status'] == 'OPTIMAL'


@pytest.mark.parametrize('preferences,first_nights', [(15, 3), (24, 3), (25, 0), (100, 0)],
                         ids=['default-fairness', 'weight-24', 'weight-25', 'raised-wish-weight'])
def test_wish_vs_three_extra_nights_uses_score_weights_and_reports_score_components(h, preferences, first_nights):
    from .test_staff_wishes import wish
    first, second = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'One night place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.workspace()
    # One equal kept duty each makes both people subjects of every candidate's score, including the all-wishes rota.
    for person in (first, second):
        w = h.shift(w, typ=typ, start='2026-10-10T08:00:00+03:00', end='2026-10-10T12:00:00+03:00', people=[person])['workspace']
    for day in range(4, 10):
        w = h.shift(w, typ=typ, start=f'2026-10-{day:02}T20:00:00+03:00',
                    end=f'2026-10-{day + 1:02}T00:00:00+03:00', people=[])['workspace']
    wish(h, '2026-10-04', last='2026-10-09', person=first, points=3, role='coordinator')
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    for shift in data['shifts']:
        shift['locked'] = bool(shift['participants'])
    options = {'mode': 'rebalance', 'shortlist_width': 0,
               'scoring': {'weights': ScoreWeights(preferences=preferences).model_dump()}}
    data['generation_options'] = options
    baseline = {(s['id'], p['person_id']): flags(p) for s in data['shifts'] for p in s['participants']}
    result = generate(data, 10, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert result['optimality_proven'] and result['weighted_status'] == 'OPTIMAL'
    assert len(result['additions']) == 6 and not result['removals']
    assert sum(a['participant']['person_id'] == first for a in result['additions']) == first_nights
    # Raising the wish weight assigns B six nights instead of three, without weakening required cover or kept work.
    assert sum(a['participant']['person_id'] == second for a in result['additions']) == 6 - first_nights
    after = deepcopy(data)
    for addition in result['additions']:
        next(s for s in after['shifts'] if s['id'] == addition['shift_id'])['participants'].append(addition['participant'])
    score = schedule_score(after, evaluate(after), options, baseline=baseline)
    assert result['objective_components'] == score['components']
    for component, value in result['compiled_component_values'].items():
        expected = score['components'][component]['value']
        assert value == pytest.approx(expected, abs=0.000051) if expected is not None else value is None
    assert score['components']['fairness']['value'] == (1 if first_nights == 3 else 0.3906)
    assert score['components']['preferences']['value'] == (0.5 if first_nights == 3 else 1)


@pytest.mark.parametrize('coverage_weight, changed, expected', [(0, True, 62.5), (3, False, 80)])
@pytest.mark.parametrize('factor, offset', [(1, 0), (2_000, 0), (200_000, 0), (200_000, 1)])
def test_active_preference_weight_normalizes_the_objective_before_stability(h, coverage_weight, changed, expected, factor, offset):
    first, second = h.seed['people'][:2]
    # Safety kinds are always mandatory. The required single seat misses this advisory target.
    h.cover_target(2)
    h.policy({'kind': 'run_length', 'maximum': 2}, {'person_id': second}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Exactly one place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.workspace()
    for day in (4, 5):
        workspace = h.shift(workspace, typ=typ, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[first])['workspace']
    snapshot = h.snapshot(workspace)
    snapshot['people'] = {pid: snapshot['people'][pid] for pid in (first, second)}
    options = {'mode': 'rebalance', 'shortlist_width': 0, 'scoring': {'weights': {
        'coverage': coverage_weight * factor, 'rules': factor, 'fairness': 0,
        'preferences': 2 * factor + offset, 'stability': factor}}}
    snapshot['generation_options'] = options
    baseline = {(s['id'], p['person_id']): flags(p) for s in snapshot['shifts'] for p in s['participants']}
    before = schedule_score(snapshot, evaluate(snapshot), options, baseline=baseline)
    alternative = deepcopy(snapshot)
    alternative['shifts'][0]['participants'][0]['person_id'] = second
    alternative_score = schedule_score(alternative, evaluate(alternative), options, baseline=baseline)
    assert before['score'] == (50 if not coverage_weight else 80)
    assert alternative_score['score'] == (62.5 if not coverage_weight else 78.6)
    assert before['components']['preferences']['value'] is None
    assert alternative_score['components']['preferences']['value'] == 1
    result = generate(snapshot, 10, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['optimality_proven'] and result['weighted_status'] == 'OPTIMAL'
    assert ('weighted_prefix' in [row['name'] for row in result['objectives']]) == bool(offset)
    assert len(result['removals']) == len(result['additions']) == int(changed)
    if changed:
        assert result['additions'][0]['participant']['person_id'] == second
    assert result['objective_components'] == (alternative_score if changed else before)['components']
    assert max(before['score'], alternative_score['score']) == expected


def test_search_without_an_incumbent_reports_unknown(h, monkeypatch):
    from ortools.sat.python import cp_model
    from packages.domain.solver_control import SolverControl
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    w = h.shift(h.workspace(), people=[])['workspace']
    result = generate(h.snapshot(w), 5)
    assert result['status'] == 'needs_more_time'
    assert result['weighted_status'] == 'UNKNOWN'
    assert not result['additions'] and not result['removals']


def test_corrupt_goal_weights_fail_validation_and_defensive_arithmetic(h, monkeypatch):
    from packages.domain import cp_sat, cp_sat_goals
    from packages.domain.cp_sat_score import SCORE_SCALE
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'weight': 0.0001},
             severity='advisory')
    workspace = h.workspace()
    for day in (4, 5, 6):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    next(policy for policy in data['policies'] if policy['id'] == goal['id'])['definition']['rule']['weight'] = 1e-15
    assert generate(data, 5)['status'] == 'unsupported_rules'
    # Inject after canonical reads, which independently discard malformed goal settings.
    monkeypatch.setattr(cp_sat, 'compatibility_issues', lambda snapshot: [])
    def corrupt_goals(model, *args, presence, **kwargs):
        loss = model.new_int_var(0, SCORE_SCALE, 'corrupt_goal_loss')
        presence.extend([1, 1])
        return [(1e-15, loss), (1, loss)]
    monkeypatch.setattr(cp_sat_goals, 'add_score_goals', corrupt_goals)
    result = generate(data, 5)
    assert result['status'] == 'invalid_inputs', result
    assert result['issues'][0]['code'] == 'SCORE_PRECISION_UNSUPPORTED'
    assert result['additions'] == result['removals'] == []


@pytest.mark.parametrize('case', ['balancing_weights', 'default_weights', 'maximum_people_rule'])
def test_allocation_preference_balancing_alone_does_not_overfill_needed_sessions(h, case):
    from .scheduler_departments import apply

    first, second = h.seed['people'][:2]
    preference = h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'service_mode',
                           'balance_window': 'rota', 'balance_target_share': 0.5}, severity='advisory')
    policies = [preference]
    if case == 'maximum_people_rule':
        # A mandatory ceiling of one person per duty: a second person breaks a safety rule.
        policies.append(h.policy({'kind': 'maximum_participants', 'maximum': 1}))
    # Each existing assignment is in a different service mode. Adding both people to both
    # duties would give each a perfect split while exceeding both one-person staffing needs.
    w = h.shift(h.workspace(), people=[first])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                service=1, people=[second])['workspace']
    data = h.snapshot(w)
    data['policies'] = policies
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    for service, mode in zip(h.seed['services'], ('physical', 'virtual')):
        data['services'][service]['mode'] = mode
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0}
    if case != 'default_weights':
        data['generation_options']['scoring'] = {
            'weights': {'coverage': 0, 'rules': 0, 'fairness': 0, 'preferences': 100, 'stability': 0}}

    result = generate(data, 5, deterministic=True)
    assert result['solver_status'] == 'OPTIMAL', result
    assert result['status'] == 'candidate_feasible', result
    assert result['compiled_component_values']['preferences'] is not None, result
    assert [len(shift['participants']) for shift in apply(data, result)['shifts']] == [1, 1], result
