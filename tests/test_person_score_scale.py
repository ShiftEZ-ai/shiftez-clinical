"""Person attribution and scorer budgets at the shared real-department scale."""
import time
import copy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import person_score
from sqlalchemy import delete
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from packages.domain.validation import index_person_rows
from tests.test_backend_reads_scale import read_department
from tests.test_backend_scale import measure
from tests.test_performance_budget import python_budget
from tests.test_schedule_score import data, policy, shift, warning


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def person_attribution_scale(request, scale_harness):
    h = scale_harness(request.param)
    with pytest.MonkeyPatch.context() as monkeypatch:
        workspace = read_department(h, goals=True)
        source = h.snapshot(workspace)
        person = source['shifts'][0]['participants'][0]['person_id']
        # Live shortages exist; attribution remains the stored check and does not recompute opportunities.
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(delete(m.Assignment).where(m.Assignment.id == source['shifts'][0]['participants'][0]['id']))
        source = h.snapshot(workspace)
        check = evaluate(source)
        start, cpu_started = time.perf_counter(), time.process_time()
        issued = schedule_score(source, check)
        elapsed, cpu_elapsed = time.perf_counter() - start, time.process_time() - cpu_started
        with transaction(h.app.state.engine, write=True) as db:
            stored = db.get(m.Workspace, workspace['id'])
            stored.validation = {**check, 'score': issued, 'input_manifest': {
                key: source['input_manifest'][key] for key in ('format', 'token', 'components', 'dirty_hint')}}
        h.client('coordinator')
        def unexpected_trial(*args, **kwargs):
            pytest.fail('Issued attribution must not calculate live staffing opportunities')
        monkeypatch.setattr(person_score, 'domain_snapshot', unexpected_trial)
        result, metrics = measure(h, 'person-attribution', lambda: h.get('coordinator',
            f'/api/people/{person}/score'))
        return result, issued, person, metrics, elapsed, cpu_elapsed


def test_department_person_attribution_reads_issued_score_without_live_trials(person_attribution_scale):
    result, issued, person, metrics, _, _ = person_attribution_scale
    assert result['draft']['person_score'] == issued['people'].get(person)
    assert result['draft']['open_places'] == []
    assert result['draft']['open_places_total'] is result['draft']['open_has_more'] is None
    assert metrics['sql'].get('SELECT', 0) <= 16  # Authentication plus the fixed person/rota reads.


@pytest.mark.cpu_budget
def test_department_person_attribution_reads_issued_score_without_live_trials_cpu_budget(person_attribution_scale):
    _, _, _, metrics, _, cpu_elapsed = person_attribution_scale
    assert cpu_elapsed < python_budget(1), cpu_elapsed
    assert metrics['cpu_seconds'] < python_budget(1), metrics


@pytest.mark.wall_benchmark
def test_department_person_attribution_reads_issued_score_without_live_trials_wall_benchmark(person_attribution_scale):
    _, _, _, metrics, elapsed, _ = person_attribution_scale
    assert elapsed < python_budget(1), elapsed
    assert metrics['wall_seconds'] < python_budget(1), metrics


def test_score_dated_scope_caches_are_valid_when_trial_participants_change():
    rule = policy('dated-programme')
    rule['definition']['scope'] = {'programme_id': 'programme'}
    source = data([shift('before-membership', 4, ['a']), shift('after-membership', 6, ['b'])], [rule])
    source['programmes'] = {'programme': {'starts_on': '2020-01-01'}}
    source['programme_memberships'] = [
        {'person_id': 'a', 'programme_id': 'programme', 'starts_on': '2020-01-01', 'ends_on': '2026-10-05'},
        {'person_id': 'b', 'programme_id': 'programme', 'starts_on': '2026-10-05', 'ends_on': '2027-01-01'}]
    check = {'findings': [warning(rule['id'], 'a')], 'coverage': []}
    cached = {**source, '_person_rows': index_person_rows(source), '_person_dates_cache': {},
              '_scope_interval_cache': {}, '_line_index_cache': {}}
    assert schedule_score(cached, check) == schedule_score(source, check)
    # The scope's dated facts stay unchanged; only who works changes between trials.
    changed = copy.deepcopy(source['shifts'])
    changed[0]['participants'].append({**changed[0]['participants'][0], 'person_id': 'b'})
    changed[1]['participants'].append({**changed[1]['participants'][0], 'person_id': 'a'})
    trial = {**source, 'shifts': changed}
    original = copy.deepcopy(trial)
    assert schedule_score({**cached, 'shifts': changed}, check) == schedule_score(trial, check)
    assert trial == original
