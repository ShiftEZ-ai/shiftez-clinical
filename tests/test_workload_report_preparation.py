"""Report-local preparation reuses fixed facts without carrying them across edits."""
from collections import Counter
from copy import deepcopy

import pytest

from packages.domain import candidate_evidence, fairness as f
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world
from tests.test_fairness_context_cache import modern


PREPARATION_CACHES = ('_distribution_cache', '_policy_applies_cache', '_workload_normalization_cache')


def without_preparation(data):
    return {**data, **dict.fromkeys(PREPARATION_CACHES)}


def workload_world():
    clinic, skill = uid(44), uid(45)
    data = modern(world([duty(1, 5, [A], [clinic]), duty(2, 6, [B], [clinic])]))
    data['services'] = {clinic: {'name': 'Synthetic clinic'}}
    data['capabilities'] = {skill: {}}
    data['eligibilities'] = [{'id': 'eligibility-' + pid, 'person_id': pid, 'service_id': clinic,
                             'starts_on': '2020-01-01', 'ends_on': None} for pid in (A, B)]
    data['grants'] = [{'id': 'grant-' + pid, 'person_id': pid, 'department_id': data['workspace']['department_id'],
                      'capability_id': skill, 'service_id': None, 'starts_on': '2020-01-01',
                      'ends_on': None, 'revoked': False} for pid in (A, B)]
    goal = policy({'metric': 'workload', 'aim': 'workload_spread',
                   'tolerance_percent': 0, 'tolerance_units': 0})
    data.update(policies=[goal], generation_options={'balance_measure': 'reserved_hours'})
    return data, goal


def test_workload_report_reuses_fixed_preparation_within_one_call(monkeypatch):
    data, goal = workload_world()
    original = deepcopy(data)
    expected = f.goal_report(without_preparation(data), goal)
    calls = Counter()
    for module, name, label in ((f, '_goal_people', 'audience'), (f, '_goal_applies', 'scope'),
                                (candidate_evidence, 'candidate_eligible', 'eligibility')):
        real = getattr(module, name)

        def counted(*args, _real=real, _label=label, **kwargs):
            calls[_label] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, name, counted)
    assert f.goal_report(data, goal) == expected
    assert calls['audience'] == 1
    assert calls['scope'] == len(data['shifts'])
    # One goal audience and one fixed-cover normalization; the identical second view is reused.
    assert calls['eligibility'] == 2 * len(data['people'])
    assert data == original and all(key not in data for key in PREPARATION_CACHES)


@pytest.mark.parametrize('kind', ['modern', 'legacy', 'fixed'])
@pytest.mark.parametrize('cache_mode', ['absent', 'disabled', 'opt-in'])
def test_workload_reports_keep_live_assignments_and_planned_values(kind, cache_mode):
    data, goal = workload_world()
    if kind == 'legacy':
        for person in data['people'].values():
            person.pop('active')
    elif kind == 'fixed':
        for shift in data['shifts']:
            shift['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': [A]}}
    if cache_mode != 'absent':
        data.update({key: {} if cache_mode == 'opt-in' else None for key in PREPARATION_CACHES})
    supplied = {key: data[key] for key in PREPARATION_CACHES if key in data}
    original = deepcopy({key: value for key, value in data.items() if key not in PREPARATION_CACHES})
    changed = {**data, 'shifts': [{**shift, 'participants': []} for shift in data['shifts']]}
    for view in (data, changed, data):
        for values in (None, ({A: 3, B: 1}, {}), ({A: 0, B: 2}, {})):
            assert f.goal_report(view, goal, values) == f.goal_report(without_preparation(view), goal, values)
    assert {key: value for key, value in data.items() if key not in PREPARATION_CACHES} == original
    assert all(data[key] is value for key, value in supplied.items())
    if cache_mode == 'absent':
        assert all(key not in data for key in PREPARATION_CACHES)


@pytest.mark.parametrize('change', ['active', 'membership', 'eligibility', 'grant', 'leave', 'requirements',
                                  'focus', 'empty', 'closed', 'where', 'scope', 'policy-date', 'measure',
                                  'participation', 'usual', 'overtime'])
def test_default_workload_preparation_is_fresh_after_in_place_fact_edits(change):
    data, goal = workload_world()
    if change == 'policy-date':
        data['fairness_semantics'] = 1
    if change == 'grant':
        goal['definition']['scope'] = {'capability_ids': [uid(45)]}
    before = f.goal_report(data, goal)
    if change == 'active':
        data['people'][B]['active'] = False
    elif change == 'membership':
        data['memberships'][1]['starts_on'] = '2026-10-07'
    elif change == 'eligibility':
        data['eligibilities'][1]['ends_on'] = '2026-10-05'
    elif change == 'grant':
        data['grants'][1]['revoked'] = True
    elif change == 'leave':
        data['availability'].append({'id': 'synthetic-leave', 'person_id': B, 'kind': 'recorded_leave',
                                     'start': '2026-10-05T00:00:00+00:00', 'end': '2026-10-07T00:00:00+00:00'})
    elif change == 'requirements':
        data['shifts'][0]['type_snapshot']['definition']['requirements'][0]['minimum'] = 2
    elif change == 'focus':
        data['focus_ids'][:] = data['focus_ids'][:1]
    elif change == 'empty':
        data['focus_ids'].clear()
    elif change == 'closed':
        data['shifts'][1]['closed'] = True
    elif change == 'where':
        goal['definition']['rule']['service_ids'] = [uid(44)]
        data['shifts'][1]['service_ids'] = [uid(46)]
    elif change == 'scope':
        goal['definition']['scope'] = {'person_ids': [A]}
    elif change == 'policy-date':
        goal['starts_on'] = '2026-10-06'
    elif change == 'measure':
        data['generation_options']['balance_measure'] = 'duties'
    elif change == 'participation':
        data['shifts'][0]['participants'][0]['end'] = '2026-10-05T10:00:00+00:00'
    elif change == 'usual':
        for shift in data['shifts']:
            shift['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': [A]}}
            shift['participants'] = []
    else:
        data['shifts'][0]['participants'][0]['overtime'] = True
    edited = deepcopy(data)
    after = f.goal_report(data, goal)
    assert after == f.goal_report(without_preparation(data), goal)
    assert (after == before) is (change == 'overtime')
    assert data == edited and all(key not in data for key in PREPARATION_CACHES)


@pytest.mark.parametrize('explain_findings', [False, True])
def test_candidate_validation_and_score_match_full_uncached_workload_reports(h, monkeypatch, explain_findings):
    from packages.domain import score, validation
    from tests.test_solver_supervision_cache import assignment, snapshot

    data, _, _, _, independent, _, supervisor, _ = snapshot(h)
    original = deepcopy(data)
    changed = {**independent, 'participants': [assignment(independent, supervisor)]}
    trial = {**data, 'shifts': [changed if shift is independent else shift for shift in data['shifts']]}
    cache, canonical = {}, f.workload_report

    def unprepared(view, *args, **kwargs):
        return canonical(without_preparation(view), *args, **kwargs)

    for view in (data, trial, data):
        with monkeypatch.context() as reference:
            reference.setattr(f, 'workload_report', unprepared)
            expected = validation.evaluate(view, explain_findings=explain_findings)
            expected_score = score.schedule_score(view, expected)
        actual = validation.evaluate(view, session_cache=cache, explain_findings=explain_findings)
        assert actual == expected
        assert score.schedule_score(view, actual) == expected_score
    assert data == original
