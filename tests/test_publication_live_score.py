"""Publication live-score parity through real preparation/resume, without solver search."""
from copy import deepcopy
import json

from ortools.sat.python import cp_model
import pytest

from packages.contracts.schemas import GenerationOptions
from packages.domain import cp_sat, score, solver_resume
from packages.domain.cp_sat_score import begin_score_program, finish_score_program, stability_component
from packages.domain.solver_control import SolverControl
from tests.test_schedule_score import data, shift, coverage


@pytest.fixture(autouse=True)
def no_solver_search(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail('This focused test permits preparation and arithmetic only, never solver search')
    monkeypatch.setattr(cp_model.CpSolver, 'solve', refuse)
    monkeypatch.setattr(cp_model.CpSolver, 'Solve', refuse)
    monkeypatch.setattr(SolverControl, 'solve', refuse)


@pytest.mark.parametrize('before', [[], [('a', False, False)], [('b', False, False)],
                                   [('a', True, False)], [('a', False, True)], [('a', True, True)]])
@pytest.mark.parametrize('coverage_weight,stability_weight', [(1, 1), (3, 1), (1, 3), (1, 0), (0, 1)])
def test_recorded_publication_stability_matches_canonical(before, coverage_weight, stability_weight):
    snapshot = data([shift('s', 4, ['a'])])
    snapshot['score_baseline'] = {'publication_id': 'synthetic-publication', 'state': 'available', 'assignments': [
        {'shift_id': 's', 'person_id': pid, 'team_lead': lead, 'overtime': overtime}
        for pid, lead, overtime in before]}
    participants = {('s', 'a'): snapshot['shifts'][0]['participants'][0]}
    model = cp_model.CpModel()
    begin_score_program(model)
    loss, present, _ = stability_component(model, snapshot, {('s', 'a'): 1, ('s', 'b'): 0}, {}, {},
                                          participants=participants)
    program = finish_score_program(model, {'coverage': (0, 1), 'fairness': (0, 1), 'stability': (loss, present)}, 1)
    program['publication_stability'] = True
    weights = dict.fromkeys(score.COMPONENTS, 0)
    weights.update(coverage=coverage_weight, stability=stability_weight)
    options = {'scoring': {'weights': weights}}
    expected = score.progress_score(score.schedule_score(snapshot,
        {'coverage': [coverage('s', 4, 1, 1)], 'findings': []}, options))
    assert score.compiled_progress_score(program, [], options) == expected


@pytest.mark.parametrize('root', [None, [0, 0], [{'offset': 0, 'terms': [[99999, 1]]}, 1]])
def test_required_publication_root_fails_closed(root):
    program = {'prefix_size': 0, 'scale': 1_000_000, 'ops': [], 'goal_cost_checks': [],
               'components': {'coverage': [0, 1], 'stability': root}, 'assigned': 1,
               'publication_stability': True}
    assert score.compiled_progress_score(program, []) is None


def test_optimizer_only_root_stays_excluded():
    program = {'prefix_size': 0, 'scale': 1_000_000, 'ops': [], 'goal_cost_checks': [],
               'components': {'coverage': [0, 1], 'stability': [1_000_000, 1]}, 'assigned': 1}
    actual = score.compiled_progress_score(program, [])
    assert actual['total'] == 100 and actual['components']['stability'] is None
    program['publication_stability'] = False
    assert score.compiled_progress_score(program, []) == actual


def preparation_snapshot(h, state='available'):
    first, second = h.seed['people'][:2]
    snapshot = h.snapshot(h.shift(h.workspace(), people=[first])['workspace'])
    snapshot['shifts'][0]['locked'] = True
    snapshot['generation_options'] = {'shortlist_width': 0, 'diagnostic_seconds': 2}
    if state != 'absent':
        snapshot['score_baseline'] = {'publication_id': 'synthetic-publication', 'state': state,
            'assignments': None if state == 'unavailable' else [] if state == 'empty' else [
                {'shift_id': snapshot['shifts'][0]['id'], 'person_id': second,
                 'team_lead': False, 'overtime': False}]}
    return snapshot


@pytest.mark.parametrize('state', ['available', 'empty', 'partial', 'unavailable', 'absent'])
@pytest.mark.parametrize('footer', ['same', 'weights', 'context'])
def test_real_preparation_and_serialized_recording_preserve_publication_parity(h, monkeypatch, state, footer):
    snapshot = preparation_snapshot(h, state)
    if footer == 'weights':
        snapshot['generation_defaults'] = {'scoring': {'weights': {
            'coverage': 1, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 1}}}
    elif footer == 'context':
        snapshot['score_context'] = {'fairness_history': {'synthetic-unrelated-history': {}}}
    options = GenerationOptions.model_validate(snapshot['generation_options'])
    clones = []
    clone = cp_model.CpModel.clone
    def record_clone(model):
        result = clone(model)
        clones.append((model, str(model.proto)))
        return result
    monkeypatch.setattr(cp_model.CpModel, 'clone', record_clone)
    prepared = cp_sat._prepare(snapshot, 2, 1, SolverControl(), options, False, False)
    assert 'model' in prepared, prepared
    assert bool(clones) == (footer != 'same')
    assert all(str(model.proto) == original for model, original in clones)
    key = solver_resume.resume_key(snapshot, options, False, False)
    envelope = solver_resume.compress_state(solver_resume.prepared_state(key, prepared))
    restored = solver_resume.restore(solver_resume.decompress_state(json.loads(json.dumps(envelope))), key)
    assert restored is not None and restored['score_program'] == prepared['score_program']
    assert str(restored['model'].proto) == str(prepared['model'].proto)
    available = score.publication_baseline(snapshot) is not None
    assert restored['score_program']['publication_stability'] is available
    assert ('stability' in restored['score_program']['components']) is available
    # Locked choices are fixed; use their native domain endpoints, never solve the model.
    solution = [variable.domain[0] for variable in restored['model'].proto.variables]
    expected = score.progress_score(restored['before_score'])
    actual = score.compiled_progress_score(restored['score_program'], solution, restored['scoring'])
    assert actual == expected


def test_locked_only_score_program_keeps_coverage_component(h):
    snapshot = preparation_snapshot(h, 'absent')
    options = GenerationOptions.model_validate(snapshot['generation_options'])
    prepared = cp_sat._prepare(snapshot, 2, 1, SolverControl(), options, False, False)
    expected = score.progress_score(prepared['before_score'])
    actual = score.compiled_progress_score(prepared['score_program'],
                                           [variable.domain[0] for variable in prepared['model'].proto.variables],
                                           prepared['scoring'])
    assert actual['components']['coverage'] == expected['components']['coverage']


@pytest.mark.parametrize('publication', [False, True])
@pytest.mark.parametrize('marker', ['missing', False, True, 'true'])
def test_real_restored_legacy_program_admission_clears_only_incompatible_live_score(h, monkeypatch, publication, marker):
    snapshot = preparation_snapshot(h, 'available' if publication else 'absent')
    options = GenerationOptions.model_validate(snapshot['generation_options'])
    prepared = cp_sat._prepare(snapshot, 2, 1, SolverControl(), options, False, False)
    assert 'model' in prepared, prepared
    if marker == 'missing':
        prepared['score_program'].pop('publication_stability')
    else:
        prepared['score_program']['publication_stability'] = marker
    key = solver_resume.resume_key(snapshot, options, False, False)
    state = json.loads(json.dumps(solver_resume.compress_state(solver_resume.prepared_state(key, prepared))))
    restored = solver_resume.restore(solver_resume.decompress_state(state), key)
    model_before = str(restored['model'].proto)
    updates = []
    control = SolverControl(progress=updates.append)
    class AdmissionObserved(Exception):
        pass
    stage = control.stage
    def stop_before_search(name, **details):
        stage(name, **details)
        raise AdmissionObserved
    monkeypatch.setattr(control, 'stage', stop_before_search)
    with pytest.raises(AdmissionObserved):
        cp_sat._search(snapshot, 2, 1, control, options, False, False, False,
                       hint=[], has_solution=False, **restored)
    compatible = publication == (marker is True)
    assert (control.score_program is not None) is compatible
    assert updates[-1]['stage'] == 'preparing'
    assert ('best_score' not in updates[-1]) if compatible else updates[-1]['best_score'] is None
    assert str(restored['model'].proto) == model_before
