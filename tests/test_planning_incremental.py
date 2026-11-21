"""Assignment edits reuse session checks without changing the complete rota verdict or score."""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from apps.api.services import planning
from packages.domain import validation
from packages.domain.finding_causes import DIAGNOSTIC_VERSION
from packages.domain.input_manifest import manifest
from packages.domain.rota_summary import fix_list_context, normalized_check
from packages.domain.score import schedule_score
from tests.test_schedule_score_scale import department


def canonical(value):
    # Edit-history receipts differ when the same check follows a different preceding edit.
    value = {key: child for key, child in value.items()
             if key not in {'_session_parts', '_person_parts', 'evaluated_at', 'fix_list_state', 'fix_list_delta'}}
    for key in ('findings', 'coverage'):
        value[key] = sorted(value.get(key, []), key=lambda row: json.dumps(row, sort_keys=True))
    return value


@pytest.mark.parametrize('change', ['remove', 'overtime', 'pooled', 'closed', 'shape_resources'])
def test_incremental_validation_keeps_full_findings_coverage_goals_and_score(change, monkeypatch):
    data = department()
    data['shifts'] = data['shifts'][:30]
    data['focus_ids'] = [row['id'] for row in data['shifts']]
    if change == 'pooled':
        policy = copy.deepcopy(data['policies'][0])
        policy.update(id='pooled-cover', name='Pooled cover')
        policy['definition'].update(rule={'kind': 'minimum_cover', 'minimum': 3, 'aggregation': 'service_group'},
                                    scope={'service_ids': list(data['services'])}, severity='mandatory')
        data['policies'].append(policy)
    if change == 'closed':
        data['shifts'][-1]['cancelled'] = True
    if change == 'shape_resources':
        policy = copy.deepcopy(data['policies'][0])
        policy.update(id='run-shape', name='Run shape')
        policy['definition'].update(rule={'kind': 'run_length', 'minimum': 2}, scope={})
        data['policies'].append(policy)
        for row in data['shifts']:
            row['type_snapshot']['definition']['resources'] = [
                {'resource_pool_id': 'missing-pool', 'units': 1, 'from_minute': 0}]
    previous = validation.evaluate(data, session_parts=True)
    before = {'shifts': copy.deepcopy(data['shifts'])}
    changed = copy.deepcopy(data)
    if change == 'overtime':
        changed['shifts'][0]['participants'][0]['overtime'] = not changed['shifts'][0]['participants'][0]['overtime']
    else:
        changed['shifts'][0]['participants'].pop()
    touched = planning.assignment_check_scope(changed, before)
    assert 0 < len(touched) < len(data['shifts'])
    calls, real = [], validation.validate_session_setup
    def counted(data, shift, *args):
        calls.append(shift['id'])
        return real(data, shift, *args)
    monkeypatch.setattr(validation, 'validate_session_setup', counted)
    result = validation.evaluate({**changed, '_partial': {'shifts': touched, 'people': set(changed['people']),
        'complete': True, 'previous': previous}}, session_parts=True)
    assert set(calls) == touched
    monkeypatch.setattr(validation, 'validate_session_setup', real)
    whole = validation.evaluate(changed)
    assert canonical(result) == canonical(whole)
    if change == 'shape_resources':
        assert {'RESOURCE_UNKNOWN', 'RUN_TOO_SHORT'} <= {f['code'] for f in result['findings']}
    assert schedule_score(changed, result) == schedule_score(changed, whole)
    assert '_session_parts' not in normalized_check(result)


def test_assignment_scope_falls_back_for_session_setup_change():
    data = department()
    before = {'shifts': copy.deepcopy(data['shifts'])}
    data['shifts'][0]['service_ids'] = list(data['services'])[:2]
    assert planning.assignment_check_scope(data, before) is None


def test_manual_assignment_uses_partial_check_and_matches_fresh_full_check(h, monkeypatch):
    pid = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, {'person_id': pid})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 2}, {'person_id': pid})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[pid])['workspace']
    target = sorted(h.detail(w)['shifts'], key=lambda row: row['start'])[0]
    modes, evaluate = [], planning.evaluate
    def counted(data, **kwargs):
        modes.append(data.get('_partial'))
        return evaluate(data, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', counted)
    payload = {key: target[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=target['id'],
                   participants=[{'person_id': pid, 'start': target['start'], 'end': target['end']}])
    result = h.command('coordinator', 'shift.update', payload, revision=w['revision'])
    assert modes[-1] and modes[-1]['complete']
    data = h.snapshot(result['workspace'])
    whole = validation.evaluate(data)
    whole['score'] = schedule_score(data, whole)
    assert canonical({key: result['validation'][key] for key in whole}) == canonical(whole)
    assert '_session_parts' not in result['validation']
    # Any changed rule/fact fingerprint forces a new full session check.
    h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, {'person_id': pid})
    payload['participants'] = []
    h.command('coordinator', 'shift.update', payload, revision=result['workspace']['revision'])
    assert modes[-1] is None


def test_assignment_removal_updates_overlap_flags_on_the_unchanged_peer(h):
    pid = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[pid])['workspace']
    w = h.shift(w, people=[pid], service=1)['workspace']
    rows = h.detail(w)['shifts']
    target, peer = rows
    payload = {key: target[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=target['id'], participants=[])
    result = h.command('coordinator', 'shift.update', payload, revision=w['revision'])
    changed = {row['id']: row for row in result['shifts']}
    assert {target['id'], peer['id']} <= set(changed)
    assert changed[peer['id']]['double_booking_conflicts'] == []
    assert not [f for f in result['validation']['findings'] if f['code'] == 'TIME_OVERLAP']


def test_shared_allocation_match_caches_session_scopes_and_rejects_other_named_people(monkeypatch):
    from packages.domain import generation
    data = department()
    data['_allocation_match_cache'] = {}
    calls, real = [], generation._allocation_policy_matches
    def counted(*args):
        calls.append(args[-1])
        return real(*args)
    monkeypatch.setattr(generation, '_allocation_policy_matches', counted)
    shift = data['shifts'][0]
    for policy in (data['policies'][0], data['policies'][20]):
        for pid in data['people']:
            assert generation.allocation_policy_matches(data, policy, shift, pid) == real(data, policy, shift, pid)
    assert len(calls) <= 2


@pytest.fixture
def department_assignment_check(monkeypatch, request):
    data = department()
    if getattr(request, 'param', None) == 'bounded':
        # Global rules deliberately close over the whole rota; use a dated local rule to test session reuse.
        policy = next(p for p in data['policies'] if p['definition']['rule']['kind'] == 'restriction')
        policy.update(starts_on='2026-10-01', ends_on='2026-10-02')
        policy['definition']['scope']['service_ids'] = data['shifts'][0]['service_ids']
        data['policies'] = [policy]
    data['input_manifest'] = manifest(data)
    data['fingerprint'] = data['input_manifest']['token']
    previous = validation.evaluate(data, session_parts=True)
    previous.update(input_manifest=data['input_manifest'], evaluated_at='2026-10-01T00:00:00+00:00',
                    diagnostic_version=DIAGNOSTIC_VERSION, fix_list_context=fix_list_context(data))
    before = {'workspace': copy.deepcopy(data['workspace']), 'shifts': copy.deepcopy(data['shifts'])}
    data['shifts'][0]['participants'].pop()
    data['workspace']['content_revision'] += 1
    data['input_manifest'] = manifest(data)
    data['fingerprint'] = data['input_manifest']['token']
    workspace = SimpleNamespace(department_id=data['workspace']['department_id'], status='draft', validation=previous,
                                accepted_issues=[])
    db = SimpleNamespace(info={}, flush=lambda: None, get=lambda *args: SimpleNamespace(generation_defaults={}))
    monkeypatch.setattr(planning, 'domain_snapshot', lambda *args, **kwargs: data)
    started = time.process_time()
    full = planning.revalidate(db, workspace)
    full_cpu = time.process_time() - started
    calls, setups, evaluate = [], [], planning.evaluate
    setup = validation.validate_session_setup

    def counted(snapshot, **kwargs):
        calls.append(snapshot.get('_partial'))
        return evaluate(snapshot, **kwargs)

    def counted_setup(snapshot, shift, *args):
        setups.append(shift['id'])
        return setup(snapshot, shift, *args)

    monkeypatch.setattr(planning, 'evaluate', counted)
    monkeypatch.setattr(validation, 'validate_session_setup', counted_setup)
    started = time.process_time()
    result = planning.revalidate(db, workspace, _incremental=(before, previous))
    partial_cpu = time.process_time() - started
    return full, result, calls, full_cpu, partial_cpu, setups


@pytest.mark.parametrize('department_assignment_check', ['bounded'], indirect=True)
def test_department_assignment_check_reuses_sessions_and_matches_full_evaluation(department_assignment_check):
    full, result, calls, _, _, setups = department_assignment_check
    assert calls[-1] and 0 < len(setups) == len(calls[-1]['shifts']) < 10
    assert canonical(result) == canonical(full)


def test_department_assignment_check_closes_over_global_rules_and_matches_full_evaluation(department_assignment_check):
    full, result, calls, _, _, setups = department_assignment_check
    assert calls[-1] and 0 < len(calls[-1]['people']) < 10
    assert 0 < len(setups) == len(calls[-1]['shifts']) < 10
    assert canonical(result) == canonical(full)


@pytest.mark.cpu_budget
def test_department_assignment_check_cpu_budget(department_assignment_check, record_property):
    import coverage
    _, _, _, full_cpu, partial_cpu, _ = department_assignment_check
    record_property('full_cpu_seconds', full_cpu)
    record_property('partial_cpu_seconds', partial_cpu)
    print(f'150 people / 450 sessions edit: full={full_cpu:.3f} CPU s; partial={partial_cpu:.3f} CPU s')
    assert partial_cpu < 2 * (7 if coverage.Coverage.current() else 1), partial_cpu
