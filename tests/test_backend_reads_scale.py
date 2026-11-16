"""Round-two department reads reuse the persisted scheduling workload and shared outputs."""
from datetime import date
import pytest
from sqlalchemy import insert

from apps.api import main, models as m
from apps.api.db import transaction
from apps.api.services import person_fairness
from packages.domain import fairness
from packages.domain.rule_map import rule_map
from tests.test_backend_scale import measure, scale_department
from tests.test_performance_budget import python_budget
from tests.test_rule_map import rule


@pytest.mark.parametrize('changed', ['input_revision', 'availability_revision', 'content_revision', 'toolchain'])
def test_fingerprint_reuses_issued_manifest_and_rebuilds_after_invalidation(h, monkeypatch, changed):
    from apps.api import snapshots

    w = h.shift(h.workspace())['workspace']
    calls, original = [], snapshots.domain_snapshot

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshots, 'domain_snapshot', counted)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        current = workspace.validation['fingerprint']
        captured = []
        assert snapshots.read_fingerprint(db, workspace, data_out=captured) == current
        assert snapshots.fingerprints(db, [workspace]) == {workspace.id: current}
        assert calls == captured == []
        if changed == 'toolchain':
            limit = snapshots.quota_policy_limit()
            monkeypatch.setattr(snapshots, 'quota_policy_limit', lambda: limit + 1)
        else:
            row = workspace if changed == 'content_revision' else db.get(m.Organisation, h.seed['organisation_id'])
            setattr(row, changed, getattr(row, changed) + 1)
        rebuilt = snapshots.read_fingerprint(db, workspace, data_out=captured)
        assert calls == [1] and len(captured) == 1
        assert rebuilt == captured[0]['input_manifest']['token']
        assert (rebuilt != current) == (changed in ('content_revision', 'toolchain'))
        calls.clear()
        assert snapshots.fingerprint(db, workspace) == rebuilt
        assert calls == [1], 'Writer comparisons always acquire authoritative inputs'


def read_department(h, goals=False):
    w, data, _, _ = scale_department(h)
    people = list(data['people'])
    policies = [rule(data, f'Scale read rule {n}', {'kind': 'max_hours', 'hours': 40 + n % 10},
                     {'person_ids': [people[n % len(people)]]} if n < 850 else
                     {'service_ids': [h.seed['services'][n % 2]]} if n < 930 else {})
                for n in range(1000)]
    if goals:
        policies += [rule(data, f'Scale {period} {metric}', {'kind': 'balance_goal', 'metric': metric,
                     'aim': 'equal_share', 'period': period, **({'period_days': 84} if period == 'rolling' else {})},
                     severity='advisory') for period in ('rota', 'month', 'rolling')
                     for metric in ('duties', 'hours', 'session_units', 'weekends')]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Policy), [{**{k: v for k, v in p.items() if k in m.Policy.__table__.columns},
            'starts_on': date(2020, 1, 1), 'created_at': m.now(),
            'approved_by': h.seed['identities']['approver'], 'owner': 'Synthetic scale owner',
            'source': 'Synthetic scale fixture', 'created_by': h.seed['identities']['coordinator']}
            for p in policies])
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    return w


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def person_fairness_measurement(request, scale_harness):
    h = scale_harness(request.param)
    w = read_department(h, goals=True)
    data = h.snapshot(w)
    person = data['shifts'][0]['participants'][0]['person_id']
    h.client('coordinator')
    # Each request rebuilds its snapshot: .69-.83 CPU s locally; one CI sample narrowly crossed 8.75 s at 8.773 s.
    with pytest.MonkeyPatch.context() as monkeypatch:
        samples = [measure(h, 'person-fairness', lambda: h.get('coordinator',
            f'/api/people/{person}/fairness?workspace_id={w["id"]}'), monkeypatch,
            target=(person_fairness, 'standing')) for _ in range(3)]
    return data, person, samples


def test_department_person_fairness(person_fairness_measurement):
    data, person, samples = person_fairness_measurement
    for sampled, measured in samples:
        assert sampled == samples[0][0], 'Repeated reads preserve the complete fairness result'
        assert measured['sql'].get('SELECT', 0) < 60, measured
    result, metrics = min(samples, key=lambda sample: sample[1]['cpu_seconds'])
    expected = fairness.goal_reports(data)
    assert len(result['goals']) == 13  # twelve authored goals plus seeded workload
    for actual, report in zip(result['goals'], expected):
        for field in ('goal_id', 'metric', 'score', 'history'):
            assert actual[field] == report[field]
        [row] = actual['people']
        original = next(r for r in report['people'] if r['person_id'] == person)
        assert {field: row[field] for field in original} == original
        assert row['peers'] == row['peer_count'] == 163
        assert row['rank'] == 1 + sum(r['cost'] < row['cost'] for r in report['people'])
        assert actual['trend'] == [] and actual['trend_complete']


@pytest.mark.cpu_budget
def test_department_person_fairness_time(person_fairness_measurement):
    _, _, samples = person_fairness_measurement
    _, metrics = min(samples, key=lambda sample: sample[1]['cpu_seconds'])
    assert metrics['cpu_seconds'] < python_budget(1.25), metrics


@pytest.mark.wall_benchmark
def test_department_person_fairness_wall_benchmark(person_fairness_measurement):
    _, _, samples = person_fairness_measurement
    _, metrics = min(samples, key=lambda sample: sample[1]['cpu_seconds'])
    assert metrics['wall_seconds'] < python_budget(5), metrics


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def department_rule_map_measurement(request, scale_harness):
    h = scale_harness(request.param)
    w = read_department(h, goals=True)
    data = h.snapshot(w)
    h.client('coordinator')
    with pytest.MonkeyPatch.context() as monkeypatch:
        result, metrics = measure(h, 'rule-map', lambda: h.get('coordinator',
            f'/api/workspaces/{w["id"]}/rule-map'), monkeypatch, target=(main, 'domain_snapshot'))
    return data, result, metrics


def test_department_rule_map(department_rule_map_measurement):
    data, result, metrics = department_rule_map_measurement
    expected = rule_map(data)
    assert len(result['rules']) == 1016  # the seed remains beside the authored scale rules
    for actual, original in zip(result['rules'], expected['rules']):
        assert {field: actual[field] for field in original} == original
    for field in ('shifts', 'markers', 'people'):
        if field in expected:
            assert result[field] == expected[field]
    assert metrics['sql'].get('SELECT', 0) < 60


@pytest.mark.cpu_budget
def test_department_rule_map_time(department_rule_map_measurement):
    _, _, metrics = department_rule_map_measurement
    assert metrics['cpu_seconds'] < python_budget(1.5), metrics


@pytest.mark.wall_benchmark
def test_department_rule_map_wall_benchmark(department_rule_map_measurement):
    _, _, metrics = department_rule_map_measurement
    assert metrics['wall_seconds'] < python_budget(6), metrics


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_department_boot_catalogues(database, request):
    h = request.getfixturevalue(database)
    read_department(h)
    client = h.client('coordinator')
    boot, metrics = measure(h, 'catalogues-boot', lambda: client.get('/api/catalogues?boot=true'))
    assert boot.status_code == 200
    full = client.get('/api/catalogues')
    assert full.status_code == 200
    compact = boot.json()
    assert len(compact['people']) == 163
    known = ','.join(compact['_versions'].values())
    hydrated = h.get('coordinator', '/api/catalogues?known=' + known)
    assert {**compact, **hydrated} == full.json()
    assert len(boot.content) < 150_000 and len(boot.content) < len(full.content) * .15
    assert metrics['sql'].get('SELECT', 0) < 60
