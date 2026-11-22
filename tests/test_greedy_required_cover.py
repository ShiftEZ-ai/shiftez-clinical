"""Required cover must survive choices that need more than a different sweep order."""
import copy
import threading
import uuid

import pytest

from packages.domain import generation
from tests.scheduler_departments import apply
from tests.test_source_parity_staffing import policy


def stranding_world(h):
    workspace = h.workspace()
    for index in range(3):
        typ = h.configure('assignment_types', {'name': f'Synthetic required duty {index}', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
        workspace = h.shift(workspace, typ=typ, people=[], start=f'2026-10-{5+index:02}T08:00:00+03:00',
                            end=f'2026-10-{5+index:02}T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    people = sorted(h.seed['people'])
    data['people'] = {pid: data['people'][pid] for pid in people}
    data['policies'] = [policy(data, {'kind': 'max_duties', 'maximum': 1, 'window': 'week'})]
    for index, allowed in enumerate(((0,), (1, 2), (0, 1))):
        shift = data['shifts'][index]
        data['policies'].append(policy(data, {'kind': 'restriction'}, {
            'assignment_type_id': shift['assignment_type_id'],
            'person_ids': [pid for n, pid in enumerate(people) if n not in allowed]}))
    return data, people


def test_greedy_repairs_required_cover_when_both_existing_orders_strand_a_seat(h):
    from apps.api.services.job_reads import result_stale
    from apps.api.services.jobs import application_recheckable

    data, people = stranding_world(h)
    original = copy.deepcopy(data)
    result = generation.generate(data, 10)
    assert result['status'] == 'candidate_feasible', (result['status'], [
        (f['code'], f['message']) for f in result['validation']['findings'] if f['severity'] == 'error'])
    draft = apply(data, result)
    assert result['validation']['valid']
    assert result['algorithm_version'] == generation.ALGORITHM_VERSION
    assert result['required_cover_repair']['solver_status'] == 'FEASIBLE'
    # The repair model is proven best, but the saved greedy proposal is not proven optimal (#34).
    assert result['required_cover_repair']['search_stop']['reason'] == 'model_proven'
    assert result['required_cover_repair']['moved'] == 1
    assert any('moved 1 greedy suggestion.' in warning for warning in result['warnings'])
    assert 'search_stop' not in result and 'first_solution_seconds' not in result
    assert result['optimality_proven'] is False
    assert result['unmet_requirements'] == []
    assert application_recheckable({**data, 'generation_engine': 'legacy'}, result)
    assert result_stale(None, {'input_snapshot': {**data, 'generation_engine': 'legacy'}, 'result': result},
                        data['fingerprint'], None, input_fingerprint=data['fingerprint']) is False
    assert [s['participants'][0]['person_id'] for s in draft['shifts']] == [people[0], people[2], people[1]]
    assert data == original
    for deterministic in (False, True):
        repeated = generation.generate(data, 10, deterministic=deterministic)
        assert (repeated['additions'], repeated['removals']) == (result['additions'], result['removals'])


def test_greedy_uses_frozen_search_time_unless_an_explicit_budget_is_supplied(monkeypatch):
    from packages.contracts.schemas import GenerationOptions

    allowances = []
    def local(_snapshot, budget, **kwargs):
        allowances.append(budget)
        return {'status': 'candidate_feasible'}
    monkeypatch.setattr(generation, '_generate_with_redistribution', local)
    for snapshot, explicit, expected in (
        ({}, None, GenerationOptions().search_minutes * 60),
        ({'generation_defaults': {'search_minutes': 2}}, None, 120),
        ({'generation_options': {'search_minutes': 3}}, None, 180),
        ({'generation_options': {'search_minutes': 1}}, None, 90),
        ({'generation_options': {'search_minutes': 3}}, 0, 0),
        ({'generation_options': {'search_minutes': 3}}, 10, 10),
    ):
        result = generation.generate(snapshot, explicit)
        assert allowances[-1] == (90 if explicit is None else expected)
        assert result['time_limit_seconds'] == expected
        assert result['elapsed_seconds'] >= 0


@pytest.mark.parametrize('repair_status', ['unknown_time_limit', 'cancelled', 'requirements_conflict', 'translation_error',
                                          'unavailable', 'candidate_feasible'])
def test_failed_required_cover_repair_retains_the_validated_partial_and_its_gaps(h, monkeypatch, repair_status):
    from packages.domain import cp_sat

    data, _ = stranding_world(h)
    original = generation._generate_with_redistribution(data, 5)
    assert original['status'] == 'candidate_with_gaps'
    monkeypatch.setattr(generation, '_generate_with_redistribution', lambda *_, **kwargs: copy.deepcopy(original))
    def repair(_snapshot, budget, **options):
        placements = _snapshot.pop('_greedy_placements')
        assert placements == {(a['shift_id'], a['participant']['person_id']) for a in original['additions']}
        assert _snapshot == {**data, '_greedy_required_cover': True} and 0 < budget <= 10
        assert options['workers'] == 1 and options['feasibility_only'] is True
        assert options['_control'].cancelled() is False
        if repair_status == 'unavailable':
            raise ImportError('Synthetic unavailable repair')
        return {'status': repair_status, 'additions': [], 'removals': []}
    monkeypatch.setattr(cp_sat, 'generate', repair)
    result = generation.generate(data, 10)
    assert result['additions'] == original['additions']
    assert result['removals'] == original['removals']
    assert result['unmet_requirements'] == original['unmet_requirements']
    assert result['validation'] == original['validation']
    assert result['status'] == ('unknown_time_limit' if repair_status in {'unknown_time_limit', 'cancelled'}
                                else 'candidate_with_gaps')
    assert result['warnings']


def test_required_cover_repair_rejects_an_invalid_candidate(h, monkeypatch):
    from packages.domain import cp_sat

    data, _ = stranding_world(h)
    partial = generation._generate_with_redistribution(data, 5)
    invalid = generation._generate(data, 0)
    assert not invalid['validation']['valid'] and invalid['additions'] != partial['additions']
    monkeypatch.setattr(cp_sat, 'generate', lambda *args, **kwargs: {**invalid, 'status': 'candidate_feasible'})
    result = generation.generate(data, 10)
    assert result['status'] == 'candidate_with_gaps'
    for key in ('additions', 'removals', 'validation', 'unmet_requirements'):
        assert result[key] == partial[key]
    assert 'required_cover_repair' not in result
    assert any('found no valid complete rota' in warning for warning in result['warnings'])


def test_required_cover_repair_can_use_a_desired_seat(h, monkeypatch):
    from packages.domain.validation import evaluate

    data, people = stranding_world(h)
    partial = generation._generate_with_redistribution(data, 5)
    original = data['shifts'][-1]
    duty = h.configure('assignment_types', {'name': 'Synthetic desired duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'target': 1, 'maximum': 1}]}})['record']
    sid = str(uuid.uuid4())
    data['types'][duty['id']] = duty
    data['focus_ids'].append(sid)
    data['shifts'].append(dict(original, id=sid, assignment_type_id=duty['id'], type_snapshot=duty))
    data['policies'].append(policy(data, {'kind': 'restriction'},
                                 {'assignment_type_id': duty['id'], 'person_ids': people[:2]}))
    # Start with a validated partial that used the person needed for repair on a desired seat.
    partial['additions'].append({'shift_id': sid, 'participant': {'id': str(uuid.uuid4()), 'shift_id': sid,
        'person_id': people[2], 'start': original['start'], 'end': original['end'], 'participation': 'staff',
        'overtime': False, 'revision': 1}})
    partial['validation'] = evaluate(apply(data, partial))
    partial['unmet_requirements'] = generation.unmet_requirements(partial['validation'])
    assert generation.deficit_score(partial['validation']) == (1, 0, 0)
    monkeypatch.setattr(generation, '_generate_with_redistribution', lambda *args, **kwargs: copy.deepcopy(partial))
    result = generation.generate(data, 10)
    assert result['status'] == 'candidate_feasible'
    assert result['validation']['valid']
    assert generation.deficit_score(result['validation']) == (0, 0, 1)
    assert result['unmet_requirements'] and all(f['code'] == 'COVERAGE_TARGET' for f in result['unmet_requirements'])


def test_required_cover_repair_skips_many_open_places(h, monkeypatch):
    from packages.domain import cp_sat

    data, _ = stranding_world(h)
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['requirements'][0].update(minimum=2, maximum=2)
    data['planning_population'] = {s['id']: [] for s in data['shifts']}
    monkeypatch.setattr(cp_sat, 'generate', lambda *args, **kwargs: pytest.fail('Large shortfall must skip repair'))
    result = generation.generate(data, 10)
    assert result['status'] == 'candidate_with_gaps'
    assert result['unmet_requirements']
    assert generation.deficit_score(result['validation'])[0] == 6
    assert any('skipped because 6 required places' in warning for warning in result['warnings'])


def test_expired_greedy_budget_keeps_partial_and_lists_missing_required_places(h):
    data, _ = stranding_world(h)
    result = generation.generate(data, 0)
    assert result['status'] == 'unknown_time_limit'
    assert result['additions'] == []
    assert result['validation']['valid'] is False
    assert len(result['unmet_requirements']) == 3
    assert 'does not prove' in result['message']


def test_exhausted_placement_does_not_use_frozen_search_time_for_repair(h, monkeypatch):
    from types import SimpleNamespace

    from packages.domain import cp_sat

    data, _ = stranding_world(h)
    data['generation_options'] = {'search_minutes': 5}
    partial = generation._generate_with_redistribution(data, 5)
    assert partial['status'] == 'candidate_with_gaps'

    def unexpected_repair(*args, **kwargs):
        pytest.fail('Placement used its allowance; repair must not start')

    clock = iter((0, 90, 90))
    monkeypatch.setattr(generation, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(generation, '_generate_with_redistribution', lambda *_, **kwargs: copy.deepcopy(partial))
    monkeypatch.setattr(cp_sat, 'generate', unexpected_repair)
    result = generation.generate(data)
    assert result['status'] == 'candidate_with_gaps'
    assert result['unmet_requirements'] == partial['unmet_requirements']


@pytest.mark.parametrize('protection', ['existing', 'population', 'series'])
def test_required_cover_repair_keeps_generation_preservation_and_candidate_boundaries(h, protection):
    data, people = stranding_world(h)
    if protection == 'existing':
        shift = data['shifts'][-1]
        shift['participants'] = [{'id': str(uuid.uuid4()), 'person_id': people[0], 'start': shift['start'],
                                  'end': shift['end'], 'participation': 'staff', 'overtime': False, 'fixed': False}]
    elif protection == 'population':
        data['planning_population'] = {s['id']: people[:2] for s in data['shifts']}
    else:
        data['shifts'][1]['series_snapshot'] = {'definition': {
            'default_person_ids': [people[1]], 'usual_assignees_fixed': True}}
    original = copy.deepcopy(data)
    result = generation.generate(data, 10)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['unmet_requirements']
    assert not result['removals']
    if protection != 'existing':
        assert all(row['participant']['person_id'] != people[2] for row in result['additions'])
    assert data == original


@pytest.mark.parametrize('status', ['cancelled', 'unknown_time_limit', 'needs_more_time'])
def test_required_cover_timeout_keeps_an_already_validated_repair(h, monkeypatch, status):
    from packages.domain import cp_sat

    data, _ = stranding_world(h)
    complete = cp_sat.generate(data, 10, workers=1, feasibility_only=True)
    assert complete['validation']['valid']
    partial = generation._generate_with_redistribution(data, 5)
    monkeypatch.setattr(generation, '_generate_with_redistribution', lambda *_, **kwargs: copy.deepcopy(partial))
    def stopped(*_args, **kwargs):
        if kwargs.get('_control') is not None:
            kwargs['_control'].validated_result = copy.deepcopy(complete)
        return {'status': status, 'additions': [], 'removals': []}
    monkeypatch.setattr(cp_sat, 'generate', stopped)
    result = generation.generate(data, 10)
    assert result['status'] == 'unknown_time_limit'
    assert result['validation']['valid']
    assert len(result['additions']) == 3
    assert result['unmet_requirements'] == []
    assert result['message'] == 'Search time expired. The complete validated rota is retained; score optimization is unfinished.'


@pytest.mark.parametrize('phase', ['placement', 'repair'])
def test_cancel_keeps_validated_partial_and_lists_gaps(h, monkeypatch, phase):
    from packages.domain import cp_sat
    from packages.domain.validation import evaluate

    data, _ = stranding_world(h)
    original = copy.deepcopy(data)
    stopped = threading.Event()
    trials = []
    trial = generation.Recheck.trial

    def cancel_after_trial(self, *args, **kwargs):
        checked = trial(self, *args, **kwargs)
        trials.append(checked)
        if phase == 'placement':
            stopped.set()
        return checked

    def cancel_repair(*args, **kwargs):
        from packages.domain.solver_control import Cancelled
        assert phase == 'repair', 'Cancellation during placement must skip the repair'
        assert trials and not stopped.is_set()
        stopped.set()
        with pytest.raises(Cancelled):
            kwargs['_control'].check()
        return {'status': 'cancelled'}

    monkeypatch.setattr(generation.Recheck, 'trial', cancel_after_trial)
    monkeypatch.setattr(cp_sat, 'generate', cancel_repair)
    result = generation.generate(data, 10, cancelled=stopped.is_set)
    assert trials and stopped.is_set()
    assert result['status'] == 'cancelled'
    assert result['message'].startswith('Search cancelled.')
    assert result['additions'] and result['unmet_requirements']
    assert result['validation'] == evaluate(apply(data, result))
    assert all(f['code'] in generation.COVERAGE_CODES for f in result['validation']['findings']
               if f['severity'] == 'error')
    assert result['unmet_requirements'] == generation.unmet_requirements(result['validation'])
    assert data == original
