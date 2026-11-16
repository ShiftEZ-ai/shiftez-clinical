"""Greedy fairness trials preserve full-audience costs without rescanning every duty per candidate."""
import copy
import time
from datetime import timedelta

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import ShiftInput
from packages.domain import fairness as f
from packages.domain.fairness_distribution import people_marginal_cost, people_pair_affected
from packages.domain.generation import allocation_settings, generate
from tests.scheduler_departments import START, department
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world
from tests.test_scheduler_performance import _budget


def original_trial_cost(data, goal, shift, participant):
    """Whole-rota replacement trial, independent of incremental caches."""
    original = {key: value for key, value in data.items() if not key.startswith('_distribution')}
    before = f.goal_report(original, goal)
    # adf40abbe2ee: trials replace a person's row; ShiftInput allows one participation per duty.
    trial = {**original, 'shifts': [{**s, 'participants': [p for p in s['participants']
                                   if p['person_id'] != participant['person_id']] + [participant]}
                                   if s['id'] == shift['id'] else s for s in original['shifts']]}
    after = f.goal_report(trial, goal)
    strengths = f.strengths_of(original)
    emphasis = f.scoring_settings(original)['fairness_worst_off_emphasis']
    return f.report_value(before, strengths, emphasis=emphasis) - f.report_value(after, strengths, emphasis=emphasis)


@pytest.mark.parametrize('split_presence', [False, True])
def test_pair_trial_preserves_one_participation_per_duty(split_presence):
    shift = duty(1, 5, [A, B])
    for person in shift['participants']:
        person['participation'] = 'staff'
    participant = dict(shift['participants'][0])
    if split_presence:
        participant['segments'] = [{'start': shift['start'], 'end': '2026-10-05T09:00:00+00:00'},
                                   {'start': '2026-10-05T11:00:00+00:00', 'end': shift['end']}]
    payload = {key: shift[key] for key in ('start', 'end', 'assignment_type_id', 'service_ids')}
    payload.update(location_id=uid(90), participants=[participant, shift['participants'][1]])
    ShiftInput.model_validate(payload)
    duplicate = {**participant, 'segments': []}
    if split_presence:
        payload['participants'][0] = {**duplicate, 'end': participant['segments'][0]['end']}
        duplicate = {**duplicate, 'start': participant['segments'][1]['start']}
    with pytest.raises(ValidationError, match='One person may have one participation interval per duty'):
        ShiftInput.model_validate({**payload, 'participants': [*payload['participants'], duplicate]})
    data = world([shift])
    goal = policy({'metric': 'pair_exposure', 'aim': 'min_per_pair', 'amount': 2,
                   'partners': {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': [B]}]}},
                  {'person_ids': [A]})
    assert original_trial_cost(data, goal, shift, participant) == 0
    assert people_marginal_cost(data, goal, shift, participant) == 0


@pytest.mark.parametrize('emphasis,expected_gain', [(0, 0.25), (0.8, 0.35)])
def test_opted_in_greedy_uses_configured_worst_off_emphasis_for_ordinary_goals(emphasis, expected_gain):
    data = world([duty(1, 5, [B]), duty(2, 6, [B]), duty(3, 7)])
    goal = policy({'metric': 'duties', 'aim': 'target', 'amount': 2, 'optimize_greedy': True,
                   'tolerance_percent': 0, 'tolerance_units': 0})
    data['policies'], data['_distribution_goals'] = [goal], [goal]
    data['generation_options'] = {'scoring': {'fairness_worst_off_emphasis': emphasis}}
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    shift = data['shifts'][-1]
    participant = {'person_id': A, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
    before = f.goal_report(data, goal)
    assert [row['cost'] for row in before['people']] == [1, 0]
    assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(-expected_gain)
    assert original_trial_cost(data, goal, shift, participant) == pytest.approx(-expected_gain)
    priority, _, _ = allocation_settings(data, shift, A)
    assert priority == pytest.approx(expected_gain)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('emphasis', [0, 0.8])
@pytest.mark.parametrize('extra_cover', [False, True])
def test_workload_opt_in_marginal_cost_uses_the_full_rota_and_canonical_workload_value(measure, emphasis, extra_cover):
    data = world([duty(i, i, [A] if i < 5 else []) for i in range(1, 7)])
    for person in data['people'].values():
        person['active'] = True
    data['categories'] = {'rank': {}}
    for row in data['memberships']:
        row['category_id'] = 'rank'
    goal = policy({'metric': 'workload', 'aim': 'workload_spread', 'optimize_greedy': True,
                   'share_by': 'equal', 'tolerance_percent': 0, 'tolerance_units': 0})
    data['policies'], data['_distribution_goals'] = [goal], [goal]
    if extra_cover:
        for shift in data['shifts']:
            shift['type_snapshot']['definition']['extra_cover'] = True
        permission = policy({'kind': 'allocation_overtime'})
        data['policies'].append({**permission, 'id': uid(81)})
    data['generation_options'] = {'balance_measure': measure, 'scoring': {'fairness_worst_off_emphasis': emphasis}}
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    original = copy.deepcopy(data)
    before = f.goal_report(data, goal)
    assert f.report_value(before) == before['value']
    shift = data['shifts'][-1]
    for pid in (A, B):
        participant = {'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
        expected = original_trial_cost(data, goal, shift, participant)
        if pid == A:
            assert expected > 0
        else:
            assert expected == 0
        assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected, abs=1e-12)
    assert {key: value for key, value in data.items() if not key.startswith('_distribution')} == {
        key: value for key, value in original.items() if not key.startswith('_distribution')}
    # Accepted work changes the load while the focused cover budget still includes every rota duty.
    shift['participants'].append({'person_id': A, 'start': shift['start'], 'end': shift['end'], 'overtime': False})
    data['_distribution_revision'] += 1
    shift = data['shifts'][-2]
    participant = {'person_id': A, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
    assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(
        original_trial_cost(data, goal, shift, participant), abs=1e-12)


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_greedy_workload_opt_in_fills_a_gap_without_adding_to_existing_overload(h, measure):
    first, second = h.seed['people'][:2]
    goal = h.policy({'kind': 'balance_goal', 'metric': 'workload', 'aim': 'workload_spread',
                     'optimize_greedy': True, 'share_by': 'equal', 'tolerance_percent': 0, 'tolerance_units': 0},
                    {'person_ids': [first, second]}, severity='advisory')
    workspace = h.workspace(start='2026-10-04', end='2026-10-11')
    for day in range(4, 10):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[first] if day < 8 else [])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['generation_options'] = {'mode': 'fill_gaps', 'balance_measure': measure,
                                'scoring': {'fairness_worst_off_emphasis': 0.8}}
    original = copy.deepcopy(data)
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result
    assert len(result['additions']) == 2
    assert {row['participant']['person_id'] for row in result['additions']} == {second}
    assert any(report['policy_id'] == goal['id'] for report in result['validation']['goals'])
    assert data == original


@pytest.mark.parametrize('metric', ['duties', 'weekends', 'hours'])
@pytest.mark.parametrize('aim', ['equal_share', 'max_min', 'target'])
def test_incremental_people_cost_matches_full_trial_with_weights_history_and_weekend_dedup(metric, aim):
    data = world([duty(1, 5, [A]), duty(2, 6, [A]), duty(3, 12, [B]), duty(4, 13)],
                 '2026-10-05', '2026-10-19')
    data['memberships'][0]['starts_on'] = '2026-10-06'
    goal = policy({'metric': metric, 'aim': aim, 'weight': 2.375, 'optimize_greedy': True,
                   **({'amount': 2.75, 'person_amounts': {A: 1.825}} if aim == 'target' else {}),
                   **({'weekend_start': 0, 'weekend_days': 2} if metric == 'weekends' else {})})
    data['policies'] = [goal]
    data['fairness_history'] = {f.goal_identity(goal): {'people': {
        A: {'value': .375, 'available': 2}, B: {'value': 1.125, 'available': 3}}}}
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    for shift in (data['shifts'][1], data['shifts'][-1]):
        for person in (A, B):
            participant = {'person_id': person, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
            assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(
                original_trial_cost(data, goal, shift, participant), abs=1e-12)
    # An accepted assignment changes counts, while the same immutable membership/history context remains valid.
    shift = data['shifts'][-1]
    shift['participants'].append({'person_id': A, 'start': shift['start'], 'end': shift['end'], 'overtime': False})
    data['_distribution_revision'] += 1
    participant = {'person_id': B, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
    assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(
        original_trial_cost(data, goal, shift, participant), abs=1e-12)


@pytest.mark.parametrize('emphasis', [0, 0.8])
def test_incremental_grouped_people_cost_refreshes_after_strength_changes_and_accepted_work(emphasis):
    data = world([*(duty(n, 5 + n, [A]) for n in range(6)), duty(7, 12), duty(8, 13)],
                 '2026-10-05', '2026-10-19')
    sources = [policy({'metric': 'duties', 'aim': 'target', 'amount': 2.75,
                       'weight': weight, 'optimize_greedy': True}, {'person_ids': [person]})
               for person, weight in ((A, 2.375), (B, .625))]
    for index, (source, strength) in enumerate(zip(sources, ('high', 'medium'))):
        source['definition']['strength'] = strength
        source['template_line'] = {'id': uid(40 + index), 'line': str(index + 1), 'slot': 'apply'}
    goal = f.comparison_goal(sources)
    data['policies'] = sources
    data['generation_options'] = {'scoring': {'fairness_worst_off_emphasis': emphasis}}
    data['_allocation_strengths'] = {'high': 9, 'medium': 3, 'low': 1}
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    shift = data['shifts'][-2]
    participant = {'person_id': B, 'start': shift['start'], 'end': shift['end'], 'overtime': False}

    def check():
        expected = original_trial_cost(data, goal, shift, participant)
        assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected, abs=1e-12)
        assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected, abs=1e-12)
        return expected

    first = check()
    data['generation_options']['scoring']['fairness_worst_off_emphasis'] = 0.8 if emphasis == 0 else 0
    assert check() != pytest.approx(first, abs=1e-12)
    data['generation_options']['scoring']['fairness_worst_off_emphasis'] = emphasis
    assert check() == pytest.approx(first, abs=1e-12)
    data['_allocation_strengths'].update(high=2, medium=7)
    changed_strengths = check()
    assert changed_strengths != pytest.approx(first, abs=1e-12)
    shift['participants'].append(participant)
    data['_distribution_revision'] += 1
    shift = data['shifts'][-1]
    participant = {**participant, 'start': shift['start'], 'end': shift['end']}
    accepted = check()
    assert accepted != pytest.approx(changed_strengths, abs=1e-12)
    data['_allocation_strengths'] = {'high': 5, 'medium': 1, 'low': 2}
    assert check() != pytest.approx(accepted, abs=1e-12)


def test_incremental_people_cost_skips_public_display_formatting(monkeypatch):
    data = world([*(duty(n, 5 + n, [A]) for n in range(4)), duty(5, 9)])
    goal = policy({'metric': 'duties', 'optimize_greedy': True})
    data['policies'] = [goal]
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    shift = data['shifts'][-1]
    participant = {'person_id': B, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
    expected = original_trial_cost(data, goal, shift, participant)
    assert expected < 0

    def display_used(*args, **kwargs):
        pytest.fail('Ordinary greedy trials must not format public report fields')

    monkeypatch.setattr(f, 'round', display_used, raising=False)
    monkeypatch.setattr(f, 'status', display_used)
    assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected, abs=1e-12)
    assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected, abs=1e-12)


def test_incremental_people_trial_reuses_context_and_scans_only_the_candidates_duties(monkeypatch):
    data = world([duty(n, 5 + n % 10, [A if n % 2 else B]) for n in range(100)],
                 '2026-10-05', '2026-10-19')
    shift = duty(101, 15)
    data['shifts'].append(shift)
    data['focus_ids'].append(shift['id'])
    goal = policy({'metric': 'duties', 'optimize_greedy': True})
    data['_distribution_cache'] = {}
    participant = {'person_id': A, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
    people_marginal_cost(data, goal, shift, participant)
    original = f.planned
    scanned = []

    def record(context, source, who=None, partners=None):
        scanned.append((len(context['shifts']), who))
        return original(context, source, who, partners)

    monkeypatch.setattr(f, 'planned', record)
    monkeypatch.setattr(f, 'goal_people', lambda *args: pytest.fail('Immutable audience was rebuilt for a candidate'))
    people_marginal_cost(data, goal, shift, participant)
    assert scanned == [(51, [A])]


@pytest.mark.parametrize('candidate_role', ['who', 'partner', 'both'])
@pytest.mark.parametrize('emphasis', [0, 0.8])
def test_ordinary_pair_goal_matches_full_trial_for_candidate_and_affected_partners(candidate_role, emphasis):
    third = uid(30)
    candidate = A if candidate_role == 'who' else B
    present = [B, third] if candidate_role == 'who' else [A, third]
    data = world([duty(1, 5, [A, third]), duty(2, 6, present)], '2026-10-05', '2026-10-19')
    data['people'][third] = {'display_name': 'Third', 'tags': []}
    data['memberships'].append({'person_id': third, 'department_id': data['workspace']['department_id'],
                                'starts_on': '2020-01-01'})
    who = [A, B] if candidate_role == 'both' else [A]
    goal = policy({'metric': 'pair_exposure', 'aim': 'min_per_pair', 'amount': 2.75,
                   'person_amounts': {B: 1.825}, 'weight': 3.125, 'optimize_greedy': True,
                   'partners': {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': [B, third]}]}},
                  {'person_ids': who})
    data['policies'], data['_distribution_goals'] = [goal], [goal]
    data['generation_options'] = {'scoring': {'fairness_worst_off_emphasis': emphasis}}
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    data['fairness_history'] = {f.goal_identity(goal): {'pairs': {A: {B: .125, third: .5}, B: {third: .25}}}}
    shift = data['shifts'][-1]
    participant = {'person_id': candidate, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
    expected = original_trial_cost(data, goal, shift, participant)
    assert expected < 0
    assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected, abs=1e-12)
    if candidate_role == 'partner':
        assert people_pair_affected(data, goal, shift, candidate)
    priority, _, _ = allocation_settings(data, shift, candidate)
    assert priority == pytest.approx(-expected * 3.125, abs=1e-12)
    # Accepted partner additions must invalidate counts, without rebuilding immutable goal context.
    shift['participants'].append(participant)
    data['_distribution_revision'] += 1
    other = data['shifts'][0]
    again = {**participant, 'start': other['start'], 'end': other['end']}
    assert people_marginal_cost(data, goal, other, again) == pytest.approx(
        original_trial_cost(data, goal, other, again), abs=1e-12)


@pytest.fixture(scope='module')
def greedy_fairness_department_month(scale_harness):
    h = scale_harness('h')
    data, info = department(h, 31, people=150, days=28, rules='none', leave_rate=0)
    people = sorted(data['people'])
    data['rotations'] = []
    data['availability'] = []
    for row in data['memberships']:
        row['category_id'] = h.seed['category']
    service = copy.deepcopy(data['services'][h.seed['services'][0]])
    services = [uid(8000 + n) for n in range(6)]
    data['services'] = {sid: {**service, 'id': sid} for sid in services}
    eligibility = copy.deepcopy(next(row for row in data['eligibilities'] if row['person_id'] == h.seed['people'][0]))
    data['eligibilities'] = [{**eligibility, 'id': uid(9000 + n), 'person_id': pid, 'service_id': services[n // 25]}
                             for n, pid in enumerate(people)]
    template = copy.deepcopy(next(s for s in data['shifts'] if s['assignment_type_id'] == info['types']['am']))
    definition = template['type_snapshot']['definition']
    for requirement in definition['requirements']:
        requirement.update(minimum=1, target=1, maximum=1)
    shifts = []
    for day in range(28):
        on = (START + timedelta(days=day)).isoformat()
        for hour in (8, 13, 18):
            for group, sid in enumerate(services):
                shift = copy.deepcopy(template)
                typ = info['types']['pm'] if hour == 13 else info['types']['am']
                shift.update(id=uid(10000 + len(shifts)), start=f'{on}T{hour:02}:00:00+03:00',
                             end=f'{on}T{hour + 4:02}:00:00+03:00', service_ids=[sid], assignment_type_id=typ,
                             participants=[], series_id=None, locked=False)
                shift['type_snapshot']['id'] = typ
                shifts.append(shift)
    data['shifts'] = shifts[:500]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    ordinary = policy({'metric': 'duties', 'optimize_greedy': True, 'weight': 1.25})
    shared = policy({'metric': 'duties', 'distribution': 'share', 'weight': .75,
                     'within': {'match': 'all', 'conditions': [{'field': 'assignment_type_ids',
                                 'values': [info['types']['pm']]}]}})
    department_id = data['workspace']['department_id']
    data['policies'] = [{**ordinary, 'department_id': department_id, 'kind': 'balance_goal'},
                        {**shared, 'id': uid(6000), 'department_id': department_id, 'kind': 'balance_goal'}]
    started = time.process_time()
    result = generate(data, _budget(120))
    elapsed = time.process_time() - started
    print('greedy fairness department budget', {'cpu_seconds': round(elapsed, 3), 'people': 150,
          'sessions': 500, 'additions': len(result['additions']), 'candidates_per_service': 25})
    return data, info, ordinary, result, elapsed


def test_greedy_fairness_opt_ins_complete_a_department_month_within_cpu_budget(greedy_fairness_department_month):
    data, info, ordinary, result, _ = greedy_fairness_department_month
    assert result['status'] == 'candidate_feasible', result
    assert len(data['people']) == 150 and len(data['shifts']) == 500
    assert len(result['additions']) == 500
    reports = {report['policy_id']: report for report in result['validation']['goals']}
    assert sum(row['planned'] for row in reports[ordinary['id']]['people']) == 500
    selected = sum(s['assignment_type_id'] == info['types']['pm'] for s in data['shifts'])
    assert sum(row['planned'] for row in reports[uid(6000)]['people']) == selected


@pytest.mark.cpu_budget
def test_greedy_fairness_department_month_cpu_budget(greedy_fairness_department_month):
    elapsed = greedy_fairness_department_month[4]
    assert elapsed < _budget(60), elapsed
