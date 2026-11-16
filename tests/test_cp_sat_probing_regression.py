"""Weighted fairness arithmetic must not turn a validator-proven rota infeasible."""
from copy import deepcopy
from random import Random
from unittest.mock import patch

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from tests.scheduler_departments import _id, apply


@pytest.mark.parametrize('weights', [None, {'coverage': 0, 'rules': 0, 'fairness': 0,
                                          'preferences': 0, 'stability': 1}],
                         ids=['default_weights', 'stability_only'])
def test_locked_24h_cover_has_no_false_weighted_conflict(h, weights):
    night_type = h.configure('assignment_types', {'name': 'On-call (24h protected work)',
        'starts_on': '2020-01-01', 'definition': {'maximum_duration_minutes': 1440,
            'requirements': [{'minimum': 1, 'maximum': 2}]}})['record']['id']
    early_type = h.configure('assignment_types', {'name': 'Clinic (two required people)',
        'starts_on': '2020-01-01', 'definition': {
            'requirements': [{'minimum': 1, 'maximum': 3}]}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': 2}, {'assignment_type_id': early_type})
    workspace = h.workspace(start='2026-10-04', end='2026-10-06')
    workspace = h.shift(workspace, typ=night_type, start='2026-10-04T08:00:00+03:00',
        end='2026-10-05T08:00:00+03:00', people=h.seed['people'][:2])['workspace']
    night = h.snapshot(workspace)['shifts'][0]
    workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [night['id']], 'action': 'lock', 'reason': 'Preserve synthetic on-call'},
        revision=workspace['revision'])['workspace']
    workspace = h.shift(workspace, typ=early_type, start='2026-10-05T09:00:00+03:00',
        end='2026-10-05T13:00:00+03:00', people=[])['workspace']
    snapshot = h.snapshot(workspace)
    snapshot['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0}
    if weights is not None:
        snapshot['generation_options']['scoring'] = {'weights': weights}
    early = next(s for s in snapshot['shifts'] if s['assignment_type_id'] == early_type)
    witness = deepcopy(snapshot)
    target = next(s for s in witness['shifts'] if s['id'] == early['id'])
    template = night['participants'][0]
    target['participants'] = [{**template, 'id': _id('probing-witness', pid), 'shift_id': target['id'],
        'person_id': pid, 'start': target['start'], 'end': target['end']}
        for pid in (h.seed['people'][0], h.seed['people'][2])]
    assert evaluate(witness)['valid'], evaluate(witness)

    constructor = cp_model.CpSolver
    for seed in (11, 29, 47):
        data = deepcopy(snapshot)
        rng = Random(seed)
        for key in ('shifts', 'policies', 'memberships', 'eligibilities'):
            rng.shuffle(data[key])
        original = deepcopy(data)

        def seeded_solver():
            solver = constructor()
            solver.parameters.random_seed = seed
            return solver

        with patch('packages.domain.cp_sat.cp_model.CpSolver', seeded_solver):
            result = generate(data, 5, workers=1)
        assert data == original
        assert result['status'] == 'candidate_feasible' and result['optimality_proven'], result
        candidate = apply(data, result)
        assert evaluate(candidate)['valid'], evaluate(candidate)
        assert next(s for s in candidate['shifts'] if s['id'] == night['id']) == next(
            s for s in data['shifts'] if s['id'] == night['id'])
        assert len(next(s for s in candidate['shifts'] if s['id'] == early['id'])['participants']) >= 2
