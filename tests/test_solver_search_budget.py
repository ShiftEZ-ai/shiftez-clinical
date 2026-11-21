"""Search-only budgets, durable preparation and cooperative stop/extension."""
import base64
import gzip
import io
import json
import threading
import time
import weakref
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from packages.domain import cp_sat, solver_control, solver_summary, solver_resume
from packages.domain.solver_control import SolverControl


def snapshot(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    return h.snapshot(workspace)


def store_checkpoints(saved):
    def checkpoint(state):
        # Exercise the actual worker boundary: no Python objects may hide in a checkpoint.
        previous = saved[0] if saved and saved[0].get('key') == state.get('key') else {}
        saved[:] = [json.loads(json.dumps({**previous, **state}))]
    return checkpoint


def test_checkpoint_preserves_clipped_rolling_quota_validation_and_score(h):
    from packages.contracts.schemas import GenerationOptions
    from packages.domain.score import schedule_score
    from tests.test_rule_lines import line, person, rule

    workspace = h.workspace(start='2026-09-27', end='2026-10-04')
    for day in ('2026-10-01', '2026-10-03'):
        workspace = h.shift(workspace, start=day + 'T08:00:00+03:00',
                            end=day + 'T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    data['policies'] = [rule('duties_per_week',
        line(who=person(h.seed['people'][0]), starts_on='2026-10-01', ends_on='2026-10-04',
             numbers={'min': {'value': 6, 'level': 'mandatory'}}),
        settings={'block_quota': {'starts_on': '2026-09-27', 'ends_on': '2026-10-04',
                                  'window_alignment': 'rolling'}},
        department_id=workspace['department_id'])]
    data['quota_context'] = {'complete': True, 'start': '2026-09-27T00:00:00+03:00',
                             'end': '2026-10-04T00:00:00+03:00', 'publications': []}
    options = GenerationOptions()
    prepared = cp_sat._prepare(data, 3, 1, SolverControl(), options, False, False)
    assert 'model' in prepared, json.dumps(prepared, indent=2)
    original = cp_sat.evaluate(prepared['data'])
    assert original['quota_report']['rows'][0]['amount'] == 2
    assert original['quota_report']['rows'][0]['actual'] == 2
    score = schedule_score(prepared['data'], original, prepared['scoring'])

    key = solver_resume.resume_key(data, options, False, False)
    checkpoint = solver_resume.compress_state(solver_resume.prepared_state(key, prepared))
    restored = solver_resume.restore(solver_resume.decompress_state(checkpoint), key)
    assert restored['data']['policies'][0]['_quota_full_window'] == ('2026-09-27', '2026-10-04')
    validation = cp_sat.evaluate(restored['data'])
    assert validation['quota_report']['rows'][0]['amount'] == 2
    assert validation == original
    assert schedule_score(restored['data'], validation, restored['scoring']) == score
    assert str(restored['model'].proto) == str(prepared['model'].proto)


def test_overtime_preparation_checkpoints_and_resumes_an_editable_duty(h, monkeypatch):
    h.policy({'kind': 'allocation_overtime'}, {'person_id': h.seed['people'][0]}, severity='advisory')
    data, saved = snapshot(h), []
    original = SolverControl.solve
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    result = cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    assert result['status'] == 'needs_more_time', result
    assert saved[0]['model'] and saved[0]['prepared']
    assert '_allocation_index' not in solver_resume.restore(saved[0], saved[0]['key'])['data']

    monkeypatch.setattr(SolverControl, 'solve', original)
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Continuation rebuilt its prepared model'))
    result = cp_sat.generate(data, 3, resume_state=solver_resume.compress_state(saved[0]),
                             checkpoint=store_checkpoints(saved))
    assert result['status'] == 'candidate_feasible', result
    assert result['preparation_reused'] and result['validation']['valid']
    assert result['additions']


def test_pack_preserves_semantic_metadata_and_strips_transient_state():
    model = cp_model.CpModel()
    data = {'policies': [{'_quota_full_window': ('2026-09-27', '2026-10-04'),
                          '_count_scope': {'person_ids': ['synthetic']},
                          '_catalogue_sources': [{'id': 'source'}],
                          '_overtime_request': True, '_overtime_dates': ['2026-10-01'],
                          '_v2_source_lines': {0: {'_source_identity': 'line'}}}],
            '_check_budget': lambda: None, '_condition_contexts': {'runtime': object()},
            '_resource_sessions': (object(),), '_allocation_index': {'first': date(2026, 10, 1)},
            '_future_cache': object()}
    restored = solver_resume.unpack(solver_resume.pack(data), model)
    assert restored == {'policies': data['policies']}
    with pytest.raises(TypeError, match='Unsupported prepared-model data'):
        solver_resume.pack({'_count_scope': object()})
    with pytest.raises(TypeError, match='Unsupported prepared-model data'):
        solver_resume.pack({'public_field': object()})


@pytest.mark.parametrize('legacy_format', [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_legacy_preparation_is_not_reused(monkeypatch, legacy_format):
    from packages.contracts.schemas import GenerationOptions

    data, options = {'fingerprint': 'synthetic'}, GenerationOptions()
    current_key = solver_resume.resume_key(data, options, False, False)
    with monkeypatch.context() as legacy:
        legacy.setattr(solver_resume, 'PREPARATION_FORMAT', legacy_format)
        old_key = solver_resume.resume_key(data, options, False, False)
    model = cp_model.CpModel()
    model.new_bool_var('retained')
    state = solver_resume.prepared_state(old_key, {'model': model})
    assert old_key != current_key
    assert solver_resume.restore(state, current_key) is None


def test_preparation_validation_and_summary_do_not_spend_search_budget(h, monkeypatch):
    data = snapshot(h)
    now, stages = [100.0], []
    advances = {'preparation_seconds': 0, 'validation_seconds': 0, 'summary_seconds': 0, 'search_seconds': 0}
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    candidate, evaluate, summarise = cp_sat.candidate_evidence, cp_sat.evaluate, solver_summary.summarise

    def prepare(*args):
        now[0] += 180
        advances['preparation_seconds'] += 180
        return candidate(*args)

    def validate(*args, **kwargs):
        if stages and stages[-1]['stage'] == 'validating':
            now[0] += 68
            advances['validation_seconds'] += 68
        return evaluate(*args, **kwargs)

    def summary(*args, **kwargs):
        now[0] += 52
        advances['summary_seconds'] += 52
        return summarise(*args, **kwargs)

    monkeypatch.setattr(cp_sat, 'candidate_evidence', prepare)
    monkeypatch.setattr(cp_sat, 'evaluate', validate)
    monkeypatch.setattr(solver_summary, 'summarise', summary)
    result = cp_sat.generate(data, 3, progress=stages.append)

    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid']
    assert result['phase_timings'] == pytest.approx(advances)
    assert result['elapsed_seconds'] == pytest.approx(sum(advances.values()))
    assert any(row['stage'] == 'summarizing' and 'phase_timings' in row for row in stages)


def test_no_solution_is_resumable_and_reuses_prepared_model(h, monkeypatch):
    data, saved = snapshot(h), []
    original = SolverControl.solve
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    result = cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    assert result['status'] == 'needs_more_time'
    assert saved[0]['model'] and not saved[0]['hint']
    assert not result['additions'] and not result['removals']

    monkeypatch.setattr(SolverControl, 'solve', original)
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Continuation rebuilt its prepared model'))
    result = cp_sat.generate(data, 3, workers=2, resume_state=saved[0], checkpoint=store_checkpoints(saved))
    assert result['status'] == 'candidate_feasible', result
    assert result['preparation_reused'] and result['validation']['valid']
    assert result['solver_workers'] == 2
    assert saved[0]['result']['status'] == 'candidate_feasible'


def test_retry_keeps_spent_search_budget_but_continue_starts_fresh(h, monkeypatch):
    data, saved = snapshot(h), []
    original = SolverControl.solve
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    state = solver_resume.compress_state(saved[0])
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Retry rebuilt its prepared model'))
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: pytest.fail('Retry received a fresh search budget'))
    exhausted = cp_sat.generate(data, 3, resume_state=state, search_control=lambda: {'spent_seconds': 3})
    assert exhausted['status'] == 'needs_more_time' and exhausted['preparation_reused']
    assert exhausted['search_elapsed_seconds'] == 3

    monkeypatch.setattr(SolverControl, 'solve', original)
    continued = cp_sat.generate(data, 3, resume_state=state, search_control=lambda: {'spent_seconds': 0})
    assert continued['status'] == 'candidate_feasible' and continued['preparation_reused']
    assert continued['validation']['valid']


def test_retry_seeds_spent_search_once_and_does_not_double_count_heartbeat(monkeypatch):
    now, update = [100.0], {'minutes': 1, 'spent_seconds': 55}
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    control = SolverControl(search_control=lambda: update)
    control.search_budget = 60
    assert control.remaining() == 5
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=5), stop_search=lambda: None)

    def solve(model, callback):
        now[0] += 2
        update['spent_seconds'] = 57
        assert control.remaining() == 3
        return cp_model.UNKNOWN

    solver.solve = solve
    assert control.solve(solver, None, 0) == cp_model.UNKNOWN
    assert control.search_spent == 57 and control.remaining() == 3
    restarted = SolverControl(search_control=lambda: update)
    restarted.search_budget = 60
    assert restarted.search_spent == 57 and restarted.remaining() == 3


@pytest.mark.parametrize('spent', [-1, float('inf'), float('nan')])
def test_invalid_persisted_search_duration_is_rejected(spent):
    with pytest.raises(ValueError, match='finite and nonnegative'):
        SolverControl(search_control=lambda: {'spent_seconds': spent})


def test_prepared_state_compression_preserves_legacy_envelope_bytes(h):
    from packages.contracts.schemas import GenerationOptions

    data = snapshot(h)
    options = GenerationOptions()
    prepared = cp_sat._prepare(data, 3, 1, SolverControl(), options, False, False)
    assert 'model' in prepared, prepared
    prepared['data']['checkpoint_test_label'] = 'عيادة تجريبية'
    key = solver_resume.resume_key(data, options, False, False)
    state = solver_resume.prepared_state(key, prepared)

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', compresslevel=1, mtime=0) as compressed:
        with io.TextIOWrapper(compressed, encoding='utf-8') as output:
            json.dump(state, output, ensure_ascii=False, separators=(',', ':'))
    legacy = {'encoding': 'gzip', 'data': base64.b64encode(buffer.getbuffer()).decode('ascii'),
              'key': state.get('key')}
    assert solver_resume.compress_state(state) == legacy


def test_prepared_state_compression_roundtrip_and_legacy_raw_support(monkeypatch):
    state = {'key': 'bound-to-input', 'model': 'variables { domain: 0 domain: 1 }\n' * 200,
             'prepared': {'mapping': [['label', 'عيادة تجريبية'], ['values', list(range(20))]]}}
    monkeypatch.setattr(solver_resume.json, 'dump', lambda *args, **kwargs: pytest.fail('Used the streaming JSON encoder'))
    encoded = solver_resume.compress_state(state)
    assert encoded['encoding'] == 'gzip' and encoded['key'] == state['key']
    assert solver_resume.decompress_state(encoded) == state
    assert solver_resume.decompress_state(state) is state
    assert solver_resume.decompress_state(None) is None
    checkpoint = {'hint': [1], 'has_solution': True}
    assert solver_resume.decompress_state({**encoded, 'checkpoint': checkpoint}) == {**state, **checkpoint}
    raw = {**state, 'checkpoint': checkpoint}
    before = deepcopy(raw)
    assert solver_resume.decompress_state(raw) == {**state, **checkpoint}
    assert raw == before
    assert solver_resume.decompress_state({**encoded, 'checkpoint': {'key': 'changed-input'}})['key'] == 'changed-input'
    assert 'checkpoint' not in state and 'checkpoint' not in encoded
    with pytest.raises(ValueError, match='does not match'):
        solver_resume.decompress_state({**encoded, 'key': 'different-input'})
    with pytest.raises(ValueError, match='Unsupported'):
        solver_resume.decompress_state({'encoding': 'unknown'})


@pytest.mark.parametrize('resumed', [False, True])
def test_serialized_preparation_is_released_before_search_and_later_callbacks_are_small(h, monkeypatch, resumed):
    data, saved, references, fields = snapshot(h), [], [], []
    original_solve = SolverControl.solve
    if resumed:
        monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
        cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    state = solver_resume.compress_state(saved[0]) if resumed else None
    original = solver_resume.decompress_state if resumed else solver_resume.prepared_state

    class Serialized(dict):
        pass

    def track(*args, **kwargs):
        result = original(*args, **kwargs)
        if result is not None:
            result['prepared'] = Serialized(result['prepared'])
            references.append(weakref.ref(result['prepared']))
        return result

    monkeypatch.setattr(solver_resume, 'decompress_state' if resumed else 'prepared_state', track)
    persist = store_checkpoints(saved)

    def checkpoint(update):
        fields.append(set(update))
        persist(update)

    def search(*args):
        assert references and all(reference() is None for reference in references)
        return original_solve(*args)

    monkeypatch.setattr(SolverControl, 'solve', search)
    result = cp_sat.generate(data, 3, resume_state=state, checkpoint=checkpoint)
    assert result['status'] == 'candidate_feasible', result
    assert len(fields) > 1
    mutable_updates = fields if resumed else fields[1:]
    assert all(not {'model', 'prepared'} & field for field in mutable_updates)
    assert saved[0]['model'] and saved[0]['prepared'] and saved[0]['result']['status'] == 'candidate_feasible'


def test_solution_checkpoint_survives_crash_and_stop_after_restart(h, monkeypatch):
    data, saved = snapshot(h), []
    persist = store_checkpoints(saved)

    def crash_after_solution(state):
        persist(state)
        if state.get('hint'):
            raise RuntimeError('Synthetic worker crash')

    with pytest.raises(RuntimeError, match='Synthetic worker crash'):
        cp_sat.generate(data, 3, checkpoint=crash_after_solution)
    assert saved[0]['hint']
    assert saved[0]['checkpoint_assignments']
    original = SolverControl.solve
    searched = []

    def resumed_solve(control, solver, model, tier):
        searched.append(tier)
        assert tier is None, 'A stopped continuation must only complete score arithmetic'
        return original(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', resumed_solve)
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Restart rebuilt its prepared model'))
    result = cp_sat.generate(data, 3, resume_state=saved[0], search_control=lambda: {'stop': True})
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid'] and result['additions']
    assert searched == []  # Stop validates the saved decisions without another derived-score solve.


def test_stopped_resume_checks_incumbent_without_relabelling_unknown_solver_status(h, monkeypatch):
    data, saved = snapshot(h), []
    first = cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    assert first['status'] == 'candidate_feasible' and saved[0]['has_solution']
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    result = cp_sat.generate(data, 3, resume_state=saved[0], search_control=lambda: {'stop': True})
    assert result['status'] == 'candidate_feasible' and result['validation']['valid']
    assert result['solver_status'] == 'UNKNOWN' and not result['optimality_proven']


def test_resumed_full_candidate_is_never_narrowed_or_made_worse(h, monkeypatch):
    from tests.scheduler_departments import department
    h.policy({'kind': 'restriction'}, severity='advisory')
    data, _ = department(h, 7, people=10, days=2, rules='none', leave_rate=0)
    data['generation_options'] = {'shortlist_width': 1}
    saved = []
    first = cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    assert first['status'] == 'candidate_feasible', first
    original, searched = SolverControl.solve, []

    def inspect(control, solver, model, tier):
        if tier is not None:
            searched.append(control.context['candidates'])
            assert control.context['candidates'] == 'all'
            hinted = model.clone()
            hinted.clear_objective()
            witness = cp_model.CpSolver()
            witness.parameters.num_workers = 1
            witness.parameters.fix_variables_to_their_hinted_value = True
            assert witness.solve(hinted) == cp_model.OPTIMAL, 'Continuation invalidated its saved incumbent'
        return original(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', inspect)
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Continuation rebuilt its model'))
    resumed = cp_sat.generate(data, 3, resume_state=saved[0])
    assert searched
    assert resumed['status'] == 'candidate_feasible', resumed
    assert resumed['summary']['score_saved']['uncapped_score'] >= first['summary']['score_saved']['uncapped_score']


@pytest.mark.parametrize('change', ['fingerprint', 'options'])
def test_prepared_cache_is_rejected_when_inputs_or_semantic_options_change(h, monkeypatch, change):
    data, saved = snapshot(h), []
    monkeypatch.setattr(SolverControl, 'solve', lambda *args: cp_model.UNKNOWN)
    cp_sat.generate(data, 3, checkpoint=store_checkpoints(saved))
    state = deepcopy(saved[0])
    if change == 'fingerprint':
        data['fingerprint'] = 'changed-inputs'
    else:
        data['generation_options'] = {**data.get('generation_options', {}), 'mode': 'rebalance'}
    prepared, original = [], cp_sat._prepare

    def prepare(*args):
        prepared.append(True)
        return original(*args)

    monkeypatch.setattr(cp_sat, '_prepare', prepare)
    result = cp_sat.generate(data, 3, resume_state=state)
    assert prepared and not result['preparation_reused']
    assert result['status'] == 'needs_more_time'


def test_add_time_and_stop_are_observed_without_solution_callbacks():
    update = {}
    control = SolverControl(search_control=lambda: update)
    control.search_budget = .001
    interrupted = threading.Event()
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=.001), stop_search=interrupted.set)

    def solve(model, callback):
        update['minutes'] = 1
        assert control.remaining() > 59
        assert control.search_budget == 60
        assert solver.parameters.max_time_in_seconds == float('inf')
        update['stop'] = True
        assert interrupted.wait(2), 'The watcher did not observe stop without an incumbent'
        return cp_model.UNKNOWN

    solver.solve = solve
    assert control.solve(solver, None, 0) == cp_model.UNKNOWN
    assert control.stopped and control.remaining() == 0


def test_search_progress_advances_without_solution_callbacks(monkeypatch):
    now, update = [100.0], {}
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    published = threading.Event()
    control = SolverControl(progress=lambda row: published.set() if row['search_elapsed_seconds'] == 4 else None,
                            search_control=lambda: update)
    control.search_budget = 60
    interrupted = threading.Event()
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=60), stop_search=interrupted.set)

    def solve(model, callback):
        now[0] += 4
        assert published.wait(2), 'Search progress did not advance without a solution'
        update['stop'] = True
        assert interrupted.wait(2)
        return cp_model.UNKNOWN

    solver.solve = solve
    assert control.solve(solver, None, 0) == cp_model.UNKNOWN


def test_diagnostics_have_their_own_budget_and_never_overwrite_solution_hints():
    saved = []
    control = SolverControl(checkpoint_solution=saved.append)
    control.search_budget = control.search_spent = 1
    model, solver = cp_model.CpModel(), cp_model.CpSolver()
    model.new_bool_var('diagnostic_only')
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 2
    assert control.solve(solver, model, 'diagnostics') == cp_model.OPTIMAL
    assert control.search_spent == 1 and not saved
    assert control.first_solution_seconds is None


def test_budget_expiry_after_incumbent_keeps_reviewable_proposal(h, monkeypatch):
    original = SolverControl.solve

    def use_budget(control, solver, model, tier):
        status = original(control, solver, model, tier)
        if tier is not None:
            control.search_spent = control.search_budget
        return status

    monkeypatch.setattr(SolverControl, 'solve', use_budget)
    result = cp_sat.generate(snapshot(h), 3)
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid'] and result['summary']


def test_stop_during_preparation_is_resumable(h):
    update = {}

    def progress(row):
        if row['stage'] == 'preparing':
            update['stop'] = True

    result = cp_sat.generate(snapshot(h), 3, progress=progress, search_control=lambda: update)
    assert result['status'] == 'needs_more_time'
    assert result['phase_timings']['search_seconds'] == 0
    assert result['additions'] == result['removals'] == []


@pytest.mark.cpu_budget
def test_department_preparation_cache_measurement(h, monkeypatch, request):
    from tests.scheduler_departments import department
    data, _ = department(h, 7, people=150, days=28, rules='full', leave_rate=.05, clinics=9)
    assert len(data['people']) == 150 and len(data['shifts']) == 532
    saved = []
    started = time.perf_counter()
    first = cp_sat.generate(data, .001, workers=1, checkpoint=store_checkpoints(saved))
    preparation_wall = time.perf_counter() - started
    assert first['status'] in {'needs_more_time', 'candidate_feasible'}
    monkeypatch.setattr(cp_sat, '_prepare', lambda *args: pytest.fail('Department continuation rebuilt its model'))
    started = time.perf_counter()
    resumed = cp_sat.generate(data, .001, workers=1, resume_state=saved[0])
    receipt = {'preparation_wall_seconds': preparation_wall, 'resume_wall_seconds': time.perf_counter() - started,
               'first_phases': first['phase_timings'], 'resume_phases': resumed['phase_timings'],
               'checkpoint_bytes': len(json.dumps(saved[0])), 'input_fingerprint': data['fingerprint']}
    request.node.user_properties.append(('generation_preparation_cache', receipt))
    print('department preparation/cache measurement', receipt)
    assert resumed['preparation_reused']


@pytest.mark.parametrize('mapping', [
    {'first': {'mapping': {'shared_property_name': 1}}, 'second': {'mapping': {'shared_property_name': 2}}},
    [['first', {'mapping': [['shared_property_name', 1]]}], ['second', {'mapping': [['shared_property_name', 2]]}]]],
    ids=['object', 'legacy-pairs'])
def test_preparation_reads_compact_and_legacy_string_mappings(mapping):
    model = cp_model.CpModel()
    assert solver_resume.unpack({'mapping': mapping}, model) == {
        'first': {'shared_property_name': 1}, 'second': {'shared_property_name': 2}}


def test_preparation_string_maps_avoid_pair_lists_and_share_decoded_property_names():
    model = cp_model.CpModel()
    data = {'rows': [{'shared_resume_property_name': index} for index in range(3)],
            'mixed': {('synthetic', 2): {'shared_resume_property_name': 7}, 3: 'integer-key'},
            'reserved': {'variable': 4, 'mapping': 5, 'expression': 6},
            '_count_scope': {'person_ids': ['synthetic']}, '_check_budget': object()}
    packed = solver_resume.pack(data)
    assert isinstance(packed['mapping'], dict)
    assert isinstance(packed['mapping']['mixed']['mapping'], list)
    restored = solver_resume.decompress_state(solver_resume.compress_state({'key': 'synthetic', 'prepared': packed}))
    rows = restored['prepared']['mapping']['rows']
    assert all(isinstance(row['mapping'], dict) for row in rows)
    assert next(iter(rows[0]['mapping'])) is next(iter(rows[1]['mapping']))
    assert solver_resume.unpack(restored['prepared'], model) == {
        key: item for key, item in data.items() if key != '_check_budget'}
    with pytest.raises(ValueError, match='Invalid prepared-model data'):
        solver_resume.unpack({'mapping': {1: 'not-a-JSON-property'}}, model)


def test_preparation_shares_only_equal_exact_string_values():
    text = 'synthetic Ω\x00\ud800 identifier'
    equal = (text + '!')[:-1]
    assert equal == text and equal is not text
    calls = []
    class CustomString(str):
        def __hash__(self):
            calls.append('hash')
            raise AssertionError('Do not hash a string subclass')
        def __eq__(self, other):
            calls.append('equal')
            raise AssertionError('Do not compare a string subclass')
    class Opaque:
        def __hash__(self):
            calls.append('opaque hash')
            raise AssertionError('Do not hash opaque values')
        def __eq__(self, other):
            calls.append('opaque equal')
            raise AssertionError('Do not compare opaque values')
    custom, opaque = CustomString(text), Opaque()
    scalars = [custom, opaque, False, True, 0, 0.0, None]
    payload = {'mapping': {
        'rows': [{'mapping': {'id': text}}, {'mapping': {'id': equal}}],
        'tuple': {'tuple': [equal]}, 'set': {'set': [equal]}, 'scalars': scalars}}
    restored = solver_resume.unpack(payload, object())
    assert restored['rows'][0]['id'] is restored['rows'][1]['id'] is text
    assert restored['tuple'][0] is text and next(iter(restored['set'])) is text
    assert restored['rows'][0] is not restored['rows'][1]
    restored['rows'][0]['changed'] = True
    assert 'changed' not in restored['rows'][1]
    assert all(value is original for value, original in zip(restored['scalars'], scalars))
    assert type(restored['rows'][0]['id']) is str and type(restored['scalars'][0]) is CustomString
    variants = ['é', 'e\u0301', 'É', '0', '00', '\x00', '\ud800']
    assert solver_resume.unpack(variants, object()) == variants
    assert not calls


def test_preparation_string_sharing_is_local_and_does_not_retain_its_model():
    class ModelMarker:
        pass
    first = ''.join(['synthetic ', 'Ω identifier'])
    second = (first + '!')[:-1]
    model = ModelMarker()
    reference = weakref.ref(model)
    left = solver_resume.unpack([first, (first + '!')[:-1]], model)
    right = solver_resume.unpack([second, (second + '!')[:-1]], model)
    assert left[0] is left[1] is first and right[0] is right[1] is second
    assert left[0] is not right[0]
    assert not vars(model)
    del model
    assert reference() is None


def test_preparation_string_sharing_preserves_decoder_validation():
    model = cp_model.CpModel()
    model.new_bool_var('synthetic')
    def outcome(call):
        try:
            value = call()
        except (TypeError, ValueError, OverflowError) as error:
            return ('error', type(error).__name__)
        return ('variable', type(value).__name__, value.index)
    for index in (-1, 1, 0.0, False, True, '0', None):
        # The current restore rejects non-integer indices before consulting its variable cache.
        expected = (outcome(lambda: model.get_int_var_from_proto_index(index)) if type(index) is int
                    else ('error', 'ValueError'))
        payload = {'mapping': {'valid': {'variable': 0}, 'strings': ['synthetic repeated', 'synthetic repeated'],
                               'tested': {'variable': index}}}
        actual = outcome(lambda: solver_resume.unpack(payload, model)['tested'])
        assert actual == expected
    for invalid in ({'mapping': {1: 'invalid property'}}, {'unknown_tag': 'synthetic repeated'}):
        with pytest.raises(ValueError, match='Invalid prepared-model data'):
            solver_resume.unpack(invalid, model)


def test_preparation_restores_local_strings_without_losing_variable_checks():
    model = cp_model.CpModel()
    variable = model.new_bool_var('resume')
    strings = [''.join(['a repeated ', 'resume label']) for _ in range(2)]
    assert strings[0] == strings[1] and strings[0] is not strings[1]
    checks = []
    restored = solver_resume.unpack(solver_resume.pack([strings, variable, variable]), model,
                                    check=lambda: checks.append(True))
    assert restored[0][0] is restored[0][1]
    assert restored[1] is restored[2]
    assert restored[1].index == variable.index
    assert checks == [True]
