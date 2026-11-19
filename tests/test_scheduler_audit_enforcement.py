"""Adversarial scheduler audit: final evidence, rule strength, scope and expanded quota identity."""
from copy import deepcopy

import pytest

from packages.domain.cp_sat import generate
from packages.domain.score import flags, schedule_score
from packages.domain.validation import evaluate

from .test_rule_catalogue_api import approve as approve_catalogue, propose as propose_catalogue
from .test_rule_catalogue_guard import line
from .test_rule_lines_inherit import n, person
from .test_rule_templates import activate, propose


def test_weighted_tradeoff_reports_final_components_despite_legacy_tolerance(h):
    first, second = h.seed['people'][:2]
    w = h.workspace()
    for day in (4, 5, 6, 7, 8):
        for start, end in (('08', '12'), ('13', '17')):
            w = h.shift(w, start=f'2026-10-{day:02}T{start}:00:00+03:00',
                        end=f'2026-10-{day:02}T{end}:00:00+03:00',
                        people=[first] if day < 7 else [])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['availability'].append({'id': 'leave', 'person_id': first, 'kind': 'recorded_leave',
                                'start': '2026-10-07T00:00:00+03:00', 'end': '2026-10-09T00:00:00+03:00'})
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['requirements'][0]['maximum'] = 1
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0, 'scoring': {'tolerance': {'changes': 50}}}
    result = generate(data, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']
    assert len(result['additions']) == 5 and len(result['removals']) == 1
    assert [row['name'] for row in result['objectives']] == ['coverage', 'weighted']
    assert result['optimality_proven'] and result['weighted_status'] == 'OPTIMAL'
    assert result['compiled_component_values']['stability'] == pytest.approx(5 / 6, abs=0.000001)
    assert result['objective_components']['fairness']['value'] == 1
    baseline = {(s['id'], p['person_id']): flags(p) for s in data['shifts'] for p in s['participants']}
    after = deepcopy(data)
    removed = {row['assignment_id'] for row in result['removals']}
    for shift in after['shifts']:
        shift['participants'] = [p for p in shift['participants'] if p['id'] not in removed]
        shift['participants'] += [row['participant'] for row in result['additions'] if row['shift_id'] == shift['id']]
    assert result['objective_components'] == schedule_score(after, evaluate(after), data['generation_options'],
                                                           baseline=baseline)['components']


def test_allocation_preference_strength_changes_the_assignment(h):
    first, second = h.seed['people'][:2]
    preferences = [activate(h, propose(h, {'kind': 'allocation_preference', 'priority': 1},
                                     {'person_id': pid}, 'advisory', strength='low'))
                   for pid in (first, second)]
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0]['maximum'] = 1
    data['generation_options'] = {'shortlist_width': 0}
    by_id = {p['id']: p for p in data['policies']}
    for preferred, other, expected in ((preferences[0], preferences[1], first), (preferences[1], preferences[0], second)):
        by_id[preferred['id']]['definition']['strength'] = 'critical'
        by_id[other['id']]['definition']['strength'] = 'low'
        result = generate(data, 5, deterministic=True)
        assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
        assert [a['participant']['person_id'] for a in result['additions']] == [expected]


def test_unrelated_closed_session_does_not_block_selected_healthy_session(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-10T08:00:00+03:00', end='2026-10-10T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    closed, selected = sorted(data['shifts'], key=lambda s: s['start'])
    data['closures'].append({'location_id': closed['location_id'], 'start': closed['start'], 'end': closed['end']})
    data['generation_session_ids'] = [selected['id']]
    progress = []
    result = generate(data, 5, deterministic=True, progress=progress.append)
    assert result['status'] == 'candidate_with_gaps', result
    assert {a['shift_id'] for a in result['additions']} == {selected['id']}
    errors = [f for f in result['validation']['findings'] if f['severity'] == 'error']
    assert errors and all(f['shift_id'] == closed['id'] for f in errors)
    assert 'LOCATION_CLOSED' in {f['code'] for f in errors}
    assert progress[-1]['stage'] == 'ready' and not progress[-1]['shared_validation_passed']
    assert progress[-1]['selected_scope_validation_passed']


def test_quota_minimum_keeps_its_scope_when_only_maximum_is_overridden(h):
    first = h.seed['people'][0]
    block = {'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    everyone = line(numbers={'min': n(2), 'max': n(5)})
    mine = line(who=person(first), numbers={'max': n(3)})
    approve_catalogue(h, propose_catalogue(h, 'duties_per_period', [everyone, mine],
                                           settings={'block_quota': block}, acknowledged=[everyone['id']])['record'])
    w = h.workspace()
    for day in (4, 5):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=h.seed['people'])['workspace']
    data = h.snapshot(w)
    witness = evaluate(data)
    assert witness['valid'], witness['findings']
    assert {(r['comparison'], r['amount']) for r in witness['quota_report']['rows'] if r['person_id'] == first} == {
        ('minimum', 2), ('maximum', 3)}
    result = generate(data, 5, feasibility_only=True, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']


def test_advisory_quota_maximum_does_not_erase_a_broken_minimum_in_score(h):
    first = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'range', 'amount': 2, 'maximum_amount': 5},
             {'person_id': first}, severity='advisory')
    w = h.shift(h.workspace(), people=[first])['workspace']
    data = h.snapshot(w)
    validation = evaluate(data)
    assert validation['valid']
    assert 'QUOTA_BELOW_MINIMUM' in {f['code'] for f in validation['findings']}
    result = schedule_score(data, validation)
    assert result['components']['rules']['value'] < 1
    assert any(row['component'] == 'rules' and row['points'] > 0 for row in result['why'])


def test_preference_strength_is_shared_by_legacy_ranking_and_score(h):
    from packages.domain.generation import allocation_settings

    first = h.seed['people'][0]
    preferences = [activate(h, propose(h, {'kind': 'allocation_preference', 'priority': priority},
                                     {'person_id': first}, 'advisory', strength='low')) for priority in (-1, 1)]
    data = h.snapshot(h.shift(h.workspace(), people=[first])['workspace'])
    by_id = {p['id']: p for p in data['policies']}
    scores = []
    for negative, positive in (('critical', 'low'), ('low', 'critical')):
        by_id[preferences[0]['id']]['definition']['strength'] = negative
        by_id[preferences[1]['id']]['definition']['strength'] = positive
        priority = allocation_settings(data, data['shifts'][0], first)[0]
        assert (priority < 0) == (negative == 'critical')
        scores.append(schedule_score(data, evaluate(data))['components']['preferences']['value'])
    assert scores[0] < scores[1]


@pytest.mark.parametrize('pairing', ['present', 'supervised'])
def test_pair_objective_counts_every_present_and_supervised_opportunity(h, pairing):
    from .test_fairness import goal

    first, second = h.seed['people'][:2]
    supervision = {}
    if pairing == 'supervised':
        h.command('coordinator', 'membership.create', {'person_id': first,
                  'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
        supervision = {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                       'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}
    pair_type = h.configure('assignment_types', {'name': 'Audit paired clinic', 'starts_on': '2020-01-01',
                           'definition': {'requirements': [{'minimum': 0, 'maximum': 2}], **supervision}})['record']['id']
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'rolling', 'window_days': 7})
    policy = goal(h, metric='pair_exposure', aim='min_per_pair', amount=1, period='custom',
                  starts_on='2026-10-04', ends_on='2026-10-11', scope={'conditions': person(first)},
                  partners=person(second), pairing=pairing)
    w = h.workspace()
    for day in range(4, 11):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        people = [{'person_id': first, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': second},
                  {'person_id': second, 'start': start, 'end': end, 'participation': 'supervisor'}] if supervision else [first, second]
        w = h.shift(w, typ=pair_type, start=start, end=end, people=people)['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    # Former sampling of seven opportunities kept positions 0,1,2,4,5,6.
    # Only position 3 is worked; the hard duty cap prevents adding another meeting.
    for index, shift in enumerate(sorted(data['shifts'], key=lambda s: s['id'])):
        if index != 3:
            shift['participants'] = []
    data['generation_options'] = {'mode': 'fill_gaps', 'shortlist_width': 0}
    result = generate(data, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert 'objective_approximations' not in result
    assert [row['name'] for row in result['objectives']] == ['coverage', 'weighted']
    assert result['objective_components']['fairness']['value'] == 1
    assert result['compiled_component_values']['fairness'] == 1
    assert result['compiled_component_values']['stability'] == 1
    report = next(row for row in result['validation']['goals'] if row['policy_id'] == policy['id'])
    assert len(report['pairs']) == 1 and report['pairs'][0]['actual'] == 1 and report['pairs'][0]['cost'] == 0
    assert result['additions'] == result['removals'] == []


def test_quota_v6_keeps_historical_v5_reports_readable():
    from packages.domain.quotas import METRIC_VERSION, project_report

    assert METRIC_VERSION == 'block-quota-v6'
    old = {'metric_version': 'block-quota-v5', 'rows': [{'person_id': 'one', 'actual': 2}, {'person_id': 'two', 'actual': 3}]}
    assert project_report(old, 'one') == {'metric_version': 'block-quota-v5', 'rows': [{'person_id': 'one', 'actual': 2}]}
    assert len(old['rows']) == 2


def test_rebalance_does_not_count_an_existing_break_as_cover(h):
    from .test_participation_segments import person as segmented_person

    first = h.seed['people'][0]
    data = h.snapshot(h.shift(h.workspace(), people=[segmented_person(first)])['workspace'])
    data['people'] = {first: data['people'][first]}
    data['generation_options'] = {'shortlist_width': 0}
    assert 'COVERAGE_GAP' in {f['code'] for f in evaluate(data)['findings']}
    result = generate(data, 5, deterministic=True)
    # With no relief person there is no feasible cover; a continuous model followed by reusing
    # the segmented row instead produced a solver/validator translation error.
    assert result['status'] == 'requirements_conflict', result
    assert result['additions'] == result['removals'] == []
# Include this file in hosted supervision and team-lead qualification.
