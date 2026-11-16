"""Pair-limit trials include the existing Who rows affected by a new partner."""
import copy
import time
from collections import defaultdict
from datetime import date, timedelta

import pytest

from packages.domain import fairness
from packages.domain.fairness_distribution import distribution_plan, loss, marginal_cost, people_pair_affected, value
from packages.domain.generation import allocation_settings, generate
from tests.test_engine_fairness_review import A, B, DEPT, duty, policy, uid, world
from tests.test_performance_budget import python_budget


def tree(people):
    return {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': people}]}


def total_loss(data, goal):
    return float(sum(loss(plan) / value(plan['denominator']) for plan in distribution_plan(data, goal)))


@pytest.mark.parametrize('candidate_role', ['who', 'partner', 'both'])
def test_pair_limit_trial_matches_all_affected_rows_and_report(candidate_role):
    third = uid(30)
    candidate = A if candidate_role == 'who' else B
    current = duty(3, 7, [B, third] if candidate_role == 'who' else [A, third])
    data = world([duty(1, 5, [A, B]), duty(2, 6, [A, B]), current])
    data['people'][third] = {'display_name': 'Third', 'tags': []}
    data['memberships'].append({'person_id': third, 'department_id': DEPT, 'starts_on': '2020-01-01'})
    goal = policy({'metric': 'pair_exposure', 'distribution': 'pair_limit', 'aim': 'target',
                   'amount': 2, 'partners': tree([B, third]), 'weight': 2.5},
                  {'person_ids': [A, B] if candidate_role == 'both' else [A]})
    data['policies'], data['_distribution_goals'] = [goal], [goal]
    data['_distribution_cache'], data['_distribution_revision'] = {}, 0
    participant = {'person_id': candidate, 'start': current['start'], 'end': current['end'], 'overtime': False}
    history = [(s, p) for s in data['shifts'] for p in s['participants'] if p['person_id'] == candidate]
    trial = copy.deepcopy(data)
    trial['shifts'][-1]['participants'].append(participant)
    expected = total_loss(trial, goal) - total_loss(data, goal)
    assert expected == .5
    assert sum(row['cost'] for row in fairness.goal_report(trial, goal)['people']) - sum(
        row['cost'] for row in fairness.goal_report(data, goal)['people']) == expected
    assert marginal_cost(data, goal, current, participant, history) == pytest.approx(expected)
    index = data['_distribution_cache'][('participations',)]
    assert marginal_cost(data, goal, current, participant, history) == pytest.approx(expected)
    assert data['_distribution_cache'][('participations',)] is index
    if candidate_role == 'partner':
        assert people_pair_affected(data, goal, current, candidate)
    priority, _, _ = allocation_settings(data, current, candidate, history=history)
    assert priority == pytest.approx(-2.5 * expected)

    # Accepted additions invalidate the index; repeated calls reuse it for this revision.
    current['participants'].append(participant)
    data['_distribution_revision'] += 1
    next_shift = duty(4, 8, [B] if candidate_role == 'who' else [A])
    data['shifts'].append(next_shift)
    data['focus_ids'].append(next_shift['id'])
    again = {**participant, 'start': next_shift['start'], 'end': next_shift['end']}
    trial = copy.deepcopy(data)
    trial['shifts'][-1]['participants'].append(again)
    assert marginal_cost(data, goal, next_shift, again, history) == pytest.approx(
        total_loss(trial, goal) - total_loss(data, goal))
    assert data['_distribution_cache'][('participations',)] is not index


def test_greedy_avoids_repeated_outside_who_partner_despite_lower_workload(h):
    who, repeated, alternative = h.seed['people'][:3]
    h.policy({'kind': 'balance_goal', 'distribution': 'pair_limit', 'metric': 'pair_exposure',
              'aim': 'target', 'amount': 1, 'partners': tree([repeated, alternative])},
             {'person_id': who}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Synthetic paired work', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']['id']
    workspace = h.workspace()
    for day, people in ((5, [who, repeated]), (6, [alternative]), (7, [alternative]), (8, [who])):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00',
                            typ=typ if day in (5, 8) else h.seed['type'], people=people)['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (who, repeated, alternative)}
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result
    [added] = result['additions']
    assert added['participant']['person_id'] == alternative


@pytest.fixture(scope='module')
def outside_who_department_pair_trials():
    people = [uid(1000 + n) for n in range(150)]
    who, partners = people[:140], people[140:]
    shifts = [duty(n, (date(2026, 10, 5) + timedelta(days=n % 28)).isoformat(),
                   [who[n % len(who)], who[(n + 1) % len(who)], partners[n % len(partners)]])
              for n in range(500)]
    data = world(shifts, '2026-10-05', '2026-11-02')
    data['people'] = {pid: {'display_name': str(n), 'tags': []} for n, pid in enumerate(people)}
    data['memberships'] = [{'person_id': pid, 'department_id': DEPT, 'starts_on': '2020-01-01'} for pid in people]
    goal = policy({'metric': 'pair_exposure', 'distribution': 'pair_limit', 'aim': 'target',
                   'amount': .5, 'partners': tree(partners)}, {'person_ids': who})
    data['policies'], data['_distribution_cache'], data['_distribution_revision'] = [goal], {}, 0
    trials, index, reused, affected = [], None, [], []
    started = time.process_time()
    for shift in shifts[::10]:
        present = {p['person_id'] for p in shift['participants']}
        for pid in partners:
            if pid in present:
                continue
            participant = {'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
            affected.append(people_pair_affected(data, goal, shift, pid))
            cost = marginal_cost(data, goal, shift, participant, [])
            trials.append((shift, participant, cost))
            if index is None:
                index = data['_distribution_cache'][('participations',)]
            reused.append(data['_distribution_cache'][('participations',)] is index)
    elapsed = time.process_time() - started
    print('outside-Who pair trial budget', {'cpu_seconds': elapsed, 'trials': len(trials), 'sessions': len(shifts)})
    return data, goal, shifts, who, trials, reused, affected, elapsed


def test_outside_who_pair_trials_reuse_department_index_within_cpu_budget(outside_who_department_pair_trials):
    data, goal, shifts, who, trials, reused, affected, _ = outside_who_department_pair_trials
    assert len(trials) == 450 and len(data['people']) == 150 and len(shifts) == 500
    assert all(affected)
    assert all(reused)

    def losses(context):
        result = defaultdict(float)
        for plan in distribution_plan(context, goal):
            result[plan['person_id']] += float(loss(plan) / value(plan['denominator']))
        return result

    before = losses(data)
    for shift, participant, cost in (trials[0], trials[len(trials) // 2], trials[-1]):
        trial = {**data, 'shifts': [{**s, 'participants': [*s['participants'], participant]}
                                  if s['id'] == shift['id'] else s for s in shifts]}
        after = losses(trial)
        changed = {pid for pid in who if after[pid] != before[pid]}
        assert changed == {p['person_id'] for p in shift['participants']} & set(who)
        assert cost == pytest.approx(sum(after[pid] - before[pid] for pid in changed))


@pytest.mark.cpu_budget
def test_outside_who_department_pair_trials_cpu_budget(outside_who_department_pair_trials):
    elapsed = outside_who_department_pair_trials[7]
    assert elapsed < python_budget(3), elapsed
