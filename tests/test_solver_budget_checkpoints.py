"""New scoring helpers interrupt preparation after work begins, without changing the snapshot."""
from copy import deepcopy

import pytest
from ortools.sat.python import cp_model

from packages.domain import allocation_score, candidate_evidence, cp_sat_goals, cp_sat_score, cp_sat_score_preferences
from packages.domain import fairness_distribution, score_normalization, validation
from packages.domain.solver_control import TimeLimit
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world
from tests.test_weighted_goals import STRENGTHS


@pytest.mark.parametrize('helper', ['allocation', 'rules', 'workload', 'distribution', 'goals', 'wishes'])
def test_scoring_helper_checks_deadline_inside_preparation(monkeypatch, helper):
    data = world([duty(i, i, [A, B]) for i in range(1, 4)])
    worked = []

    def observed(*args, **kwargs):
        worked.append(True)
        return True

    def check():
        if worked:
            raise TimeLimit()

    if helper == 'allocation':
        data['policies'] = [policy({'kind': 'allocation_preference', 'priority': 1})]
        monkeypatch.setattr(allocation_score, 'allocation_policy_matches', observed)
        run = lambda: allocation_score.allocation_score_items(data)
    elif helper == 'rules':
        rank = uid(90)
        data['categories'][rank] = {'active': True, 'starts_on': '2020-01-01'}
        for member in data['memberships']:
            member['category_id'] = rank
        for person in data['people'].values():
            person['active'] = True
        data['policies'] = [policy({'kind': 'restriction'})]
        monkeypatch.setattr(validation, 'policy_applies', observed)
        run = lambda: score_normalization.rule_opportunities(data)
    elif helper == 'workload':
        for person in data['people'].values():
            person['active'] = True

        def ineligible(*args, **kwargs):
            observed()
            return False

        monkeypatch.setattr(candidate_evidence, 'candidate_eligible', ineligible)
        run = lambda: score_normalization.workload_normalization(data, 'duties')
    elif helper == 'distribution':
        goal = policy({'metric': 'duties', 'distribution': 'share',
                       'within': {'conditions': [{'field': 'service_ids', 'values': [uid(70)]}]}})
        data['policies'] = [goal]
        monkeypatch.setattr(fairness_distribution, 'applies', observed)
        run = lambda: fairness_distribution.counted_groups(data, goal)
    elif helper == 'goals':
        data['policies'] = [policy({'metric': 'duties'})]
        model = cp_model.CpModel()
        participants = {(s['id'], p['person_id']): p for s in data['shifts'] for p in s['participants']}
        choices = {key: model.new_constant(1) for key in participants}
        original = cp_sat_goals.contribution

        def contribution(*args):
            observed()
            return original(*args)

        monkeypatch.setattr(cp_sat_goals, 'contribution', contribution)
        run = lambda: cp_sat_goals.add_score_goals(model, data, choices, participants, {}, {},
                                                  set(data['focus_ids']), STRENGTHS)
    else:
        data['wishes'] = [{'id': uid(80), 'person_id': A, 'kind': 'prefer_on',
                           'first_day': '2026-10-01', 'last_day': '2026-10-03', 'weight': 1}]
        original = cp_sat_score_preferences.wish_days

        def wish_days(*args):
            observed()
            return original(*args)

        monkeypatch.setattr(cp_sat_score_preferences, 'wish_days', wish_days)
        run = lambda: cp_sat_score_preferences.add_score_preferences(cp_model.CpModel(), data, {}, {}, {}, {}, {}, STRENGTHS)

    before = deepcopy(data)
    data['_check_budget'] = check
    with pytest.raises(TimeLimit):
        run()
    assert worked
    assert {key: value for key, value in data.items() if key != '_check_budget'} == before


def test_coverage_objective_checks_deadline_between_compiled_rows():
    model = cp_model.CpModel()
    choice = model.new_bool_var('assigned')

    def check():
        if model.proto.constraints:
            raise TimeLimit()

    with pytest.raises(TimeLimit):
        cp_sat_score.coverage_component(model, [(choice, 1, [2], 60)] * 3, check=check)
    assert model.proto.constraints
