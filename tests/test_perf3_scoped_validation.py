"""Exact incremental checks, with whole-kind fallbacks for cross-person reports."""
from collections import Counter
import copy
from datetime import date, timedelta
import json
import random
import time

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from packages.contracts.schemas import PolicyDefinition
from packages.domain import generation, validation
from packages.domain.score import schedule_score
from packages.domain.validation_scope import assignment_scope, bounded_assignment_scope
from tests.test_be18_command_latency import edit
from tests.test_schedule_score_scale import department, _id


def representative_month():
    data = department()
    for original in list(data['shifts'][-90:]):
        row = copy.deepcopy(original)
        row['id'] = _id('extra-' + original['id'])
        for participant in row['participants']:
            participant['id'] = _id(row['id'] + participant['person_id'])
        data['shifts'].append(row)
        data['focus_ids'].append(row['id'])
    assert len(data['people']) == 150 and len(data['focus_ids']) == 540
    return data


def public(check):
    return {key: value for key, value in check.items() if key not in validation.INTERNAL_CHECK_KEYS}


def oracle(data, edits=8):
    randomizer = random.Random(38419)
    data = copy.deepcopy(data)
    previous = validation.evaluate(data, session_parts=True)
    available = [row for row in data['shifts'] if row['id'] in data['focus_ids'] and row['participants']]
    originals = {row['id']: copy.deepcopy(row['participants']) for row in available}
    for n in range(edits):
        row = randomizer.choice(available)
        before = copy.deepcopy(row)
        if n % 3 == 0:
            row['participants'] = []
        elif n % 3 == 1:
            row['participants'] = copy.deepcopy(originals[row['id']])
        elif row['participants']:
            row['participants'][0]['overtime'] = not row['participants'][0].get('overtime', False)
        scope = bounded_assignment_scope(data, [before], [row])
        assert scope is not None
        partial = validation.evaluate({**data, '_partial': {**scope, 'complete': True, 'previous': previous}},
                                      session_parts=True)
        full = validation.evaluate(data, session_parts=True)
        assert public(partial) == public(full)
        assert schedule_score(data, partial) == schedule_score(data, full)
        # Receipts survive the database's JSON boundary and a chain of edits, including remove/restore.
        previous = json.loads(json.dumps(partial))


def test_randomized_department_edits_match_whole_findings_coverage_fairness_and_score():
    data = representative_month()
    dept = data['workspace']['department_id']
    rules = (({'kind': 'min_rest', 'hours': 12}, 'mandatory'),
             ({'kind': 'max_duties', 'maximum': 3, 'window': 'week'}, 'mandatory'),
             ({'kind': 'max_consecutive_days', 'maximum': 2}, 'mandatory'),
             ({'kind': 'run_length', 'minimum': 2}, 'advisory'),
             ({'kind': 'together_apart', 'person_ids': list(data['people'])[:3], 'mode': 'together'}, 'advisory'),
             ({'kind': 'minimum_cover', 'minimum': 3, 'aggregation': 'service_group'}, 'mandatory'))
    for n, (rule, severity) in enumerate(rules):
        data['policies'].append({'id': f'oracle-{n}', 'name': f'Oracle {n}', 'department_id': dept,
            'status': 'active', 'starts_on': '2020-01-01', 'revision': 1,
            'definition': PolicyDefinition.model_validate({'rule': rule, 'severity': severity,
                **({'scope': {'service_ids': list(data['services'])}} if rule['kind'] == 'minimum_cover' else {})
            }).model_dump(mode='json')})
    oracle(data)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_randomized_relational_fixture_matches_full_and_edit_stores_current_complete_check(h, monkeypatch):
    pid = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, {'person_id': pid})
    h.policy({'kind': 'min_rest', 'hours': 24})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 2})
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2]})
    w = h.workspace()
    for n in range(6):
        day = date(2026, 10, 4) + timedelta(days=n // 2)
        w = h.shift(w, people=[h.seed['people'][n % 2]], service=n % 2,
                    start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00')['workspace']
    oracle(h.snapshot(w), edits=9)
    target = h.detail(w)['shifts'][0]
    modes, real = [], planning.evaluate
    def counted(data, **kwargs):
        modes.append(data.get('_partial'))
        return real(data, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', counted)
    statements = Counter()
    def count(_conn, _cursor, statement, *_args):
        statements[statement.split()[0].upper()] += 1
    event.listen(h.app.state.engine, 'before_cursor_execute', count)
    try:
        # Keep required cover: D65 refuses removing this session's sole clinician.
        result = edit(h, w, target, [{**p, 'fixed': True} for p in target['participants']])
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', count)
    assert modes and modes[-1]['complete']
    assert modes[-1]['people'] == set(h.seed['people'])
    assert len(modes[-1]['shifts']) == 6 and len(modes[-1]['dates']) == 7
    assert not result['validation'].get('scope') and 'score' in result['validation']
    assert result['validation']['fingerprint'] == h.snapshot(result['workspace'])['fingerprint']
    assert not validation.INTERNAL_CHECK_KEYS.intersection(result['validation'])
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id']))
    print('PERF-3 edit SQL', dict(statements))
    assert statements['SELECT'] <= 300, statements
    assert sum(statements.values()) <= 350, statements


def test_duplicate_allocation_copies_share_canonical_matching_once(monkeypatch):
    data = department()
    data['_allocation_match_cache'] = {}
    policy = data['policies'][20]
    duplicate = copy.deepcopy(policy)
    duplicate.update(id='duplicate', name='Duplicate')
    calls, real = [], generation._allocation_policy_matches
    def counted(*args):
        calls.append(args)
        return real(*args)
    monkeypatch.setattr(generation, '_allocation_policy_matches', counted)
    for pid in data['people']:
        assert generation.allocation_policy_matches(data, policy, data['shifts'][0], pid) == generation.allocation_policy_matches(
            data, duplicate, data['shifts'][0], pid)
    assert len(calls) <= 1


@pytest.fixture
def scoped_work(monkeypatch):
    data = representative_month()
    for n, rule in enumerate(({'kind': 'max_duties', 'maximum': 2, 'window': 'week'},
                             {'kind': 'min_rest', 'hours': 24})):
        data['policies'].append({'id': f'budget-{n}', 'name': f'Budget {n}',
            'department_id': data['workspace']['department_id'], 'status': 'active', 'starts_on': '2020-01-01',
            'definition': PolicyDefinition.model_validate({'rule': rule}).model_dump(mode='json')})
    previous = validation.evaluate(data, session_parts=True)
    before = copy.deepcopy(data['shifts'][0])
    data['shifts'][0]['participants'].pop()
    scope = assignment_scope(data, [before], [data['shifts'][0]])
    calls = Counter()
    real_workload, real_session = validation.workload_selected, validation.validate_session_setup
    def workload(snapshot, policy, pid, entries):
        calls['person_rule_subjects'] += 1
        assert pid in scope['people']
        return real_workload(snapshot, policy, pid, entries)
    def session(snapshot, shift, *args):
        calls['session_subjects'] += 1
        assert shift['id'] in scope['shifts']
        return real_session(snapshot, shift, *args)
    monkeypatch.setattr(validation, 'workload_selected', workload)
    monkeypatch.setattr(validation, 'validate_session_setup', session)
    started = time.process_time()
    result = validation.evaluate({**data, '_partial': {**scope, 'complete': True, 'previous': previous}}, session_parts=True)
    result['score'] = schedule_score(data, result)
    return calls, scope, time.process_time() - started


def test_edit_visits_only_affected_people_and_simultaneous_sessions(scoped_work):
    calls, scope, _ = scoped_work
    assert calls['person_rule_subjects'] <= 2 * len(scope['people'])
    assert calls['session_subjects'] == len(scope['shifts']) < 10
    print('PERF-3 scoped work', dict(calls), 'CPU', scoped_work[2])


@pytest.mark.parametrize('service_matches', [True, False])
def test_complete_scope_predicate_is_evaluated_once_per_immutable_input(monkeypatch, service_matches):
    from packages.domain import conditions
    from packages.domain.time import aware
    data = department()
    data['_scope_matches_cache'] = {}
    shift = data['shifts'][0]
    scope = {'service_ids': shift['service_ids'] if service_matches else ['missing-service']}
    pid, a, b = next(iter(data['people'])), aware(shift['start']), aware(shift['end'])
    calls, canonical = [], conditions.scope_segments
    def counted(*args, **kwargs):
        calls.append(args)
        yield from canonical(*args, **kwargs)
    monkeypatch.setattr(conditions, 'scope_segments', counted)
    for _ in range(20):
        assert conditions.scope_matches_throughout(data, scope, pid, shift, a, b) is service_matches
    assert len(calls) == 1
    assert conditions.scope_matches_throughout({**data, '_scope_matches_cache': {}}, scope, pid, shift, a, b) is service_matches
    assert len(calls) == 2


def test_empty_person_receipt_is_retained_but_never_exposed():
    data = department()
    check = validation.evaluate(data, session_parts=True)
    assert check['_person_parts'] == {}
    assert not validation.INTERNAL_CHECK_KEYS.intersection(public(check))


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('stale', [False, True])
def test_merge_scopes_candidate_after_one_authoritative_baseline_and_matches_full(h, monkeypatch, stale):
    from tests.test_p2_groups import fork, preview, apply
    from tests.test_planning_groups_drafts import integrated, edit as group_edit
    w, shift, _ = integrated(h, people=[h.seed['people'][0]])
    w = h.shift(w, people=[h.seed['people'][0]], start='2026-10-05T08:00:00+03:00',
                end='2026-10-05T12:00:00+03:00', role='admin')['workspace']
    source = fork(h, w)
    local = next(row for row in h.detail(source)['shifts'] if row['start'] == shift['start'])
    source = group_edit(h, source, local, [{**local['participants'][0], 'overtime': True}])['workspace']
    proposal = preview(h, source, w)
    baseline_shifts = h.detail(w)['shifts']
    if stale:
        with transaction(h.app.state.engine) as db:
            target = db.get(m.Workspace, w['id'])
            target.validation = {**target.validation, 'fingerprint': 'stale'}
    calls, real = [], validation.evaluate
    def counted(data, **options):
        calls.append(data.get('_partial'))
        return real(data, **options)
    monkeypatch.setattr(validation, 'evaluate', counted)
    result = apply(h, w, proposal)
    assert len(calls) == (2 if stale else 1)
    expected_shifts = {row['id'] for row in baseline_shifts}
    assert calls[-1]['complete'] and calls[-1]['shifts'] == expected_shifts
    assert calls[-1]['people'] == set(h.seed['people']) and len(calls[-1]['dates']) == 7
    data = h.snapshot(result['workspace'])
    full = real(data)
    checked = h.detail(result['workspace'])['workspace']['validation']
    for key in ('findings', 'coverage', 'valid', 'error_count', 'warning_count', 'quota_report', 'goals'):
        assert checked.get(key) == full.get(key)
    with transaction(h.app.state.engine) as db:
        defaults = db.get(m.Department, data['workspace']['department_id']).generation_defaults
    assert checked['score'] == schedule_score(data, full, defaults)


@pytest.mark.parametrize('change', ['dates', 'adjustments', 'session_setup', 'rules', 'missing_workspace'])
def test_shared_receipt_guard_rejects_nonparticipant_changes(change):
    from packages.domain.input_manifest import manifest
    data = department()
    data['input_manifest'] = manifest(data)
    previous = {**validation.evaluate(data, session_parts=True), 'input_manifest': copy.deepcopy(data['input_manifest'])}
    before = copy.deepcopy({'workspace': data['workspace'], 'shifts': data['shifts']})
    assert planning.assignment_validation_scope(data, before, previous) is not None
    if change == 'dates':
        data['workspace']['ends_on'] = '2027-01-01'
    elif change == 'adjustments':
        data['workspace']['rule_adjustments'] = [{'changed': True}]
    elif change == 'session_setup':
        data['shifts'][0]['attributes'] = {'changed': True}
    elif change == 'rules':
        data['input_manifest']['components']['mandatory_inputs'] = 'changed'
    else:
        before.pop('workspace')
    assert planning.assignment_validation_scope(data, before, previous) is None


@pytest.mark.cpu_budget
def test_scoped_540_session_edit_cpu_budget(scoped_work, record_property):
    from tests.test_scheduler_performance import _budget
    elapsed = scoped_work[2]
    record_property('scoped_edit_cpu_seconds', elapsed)
    # Measured 1.04 process-CPU s on the native Mac; retain headroom for ordinary runner variation.
    assert elapsed < _budget(4), elapsed
