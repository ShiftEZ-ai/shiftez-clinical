"""Bounded collection lists for 163 people, 768 sessions and department history."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_backend_scale import measure, scale_department
from .test_performance_budget import python_budget
from .test_requests_search import request_row


COLLECTION_CPU_BUDGETS = dict(audit=0.15, operations=0.2, requests=0.5, schedule=0.15,
                              audit_filtered=0.25, schedule_filtered=1)


def source_snapshot():
    return {
        'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'dirty_diff_sha256': hashlib.sha256(subprocess.check_output(['git', 'diff', 'HEAD', '--binary'])).hexdigest(),
        'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgres'])
def collection_scale_measurement(request, scale_harness):
    source = source_snapshot() if os.getenv('RC17_LIST_PROFILE') else None
    h = scale_harness(request.param)
    workspace, data, _, _ = scale_department(h)
    people = list(data['people'])
    moment = m.now()
    with transaction(h.app.state.engine, write=True) as db:
        for index in range(1000):
            db.add(m.AuditEvent(department_id=h.seed['department_id'], actor_id=h.seed['identities']['coordinator'],
                action='scale.create' if index % 2 else 'scale.update', reason='Synthetic collection scale check',
                detail={}, correlation_id='scale-history', created_at=moment - timedelta(seconds=index)))
            db.add(m.Outbox(department_id=h.seed['department_id'], event_key=f'scale-event-{index}',
                kind='scale_notice' if index % 2 else 'scale_change', state='failed' if index % 3 else 'queued',
                payload={'private': 'This payload must not reach collection rows'},
                created_at=moment - timedelta(seconds=index)))
        db.add_all(request_row(h, people[index % len(people)], 'Synthetic scale request', day=index % 28,
                              status=('submitted', 'approved', 'rejected')[index % 3]) for index in range(500))
    paths = {
        'audit': ('auditor', '/api/audit', {'people': 'false'}),
        'operations': ('operator', '/api/operations', {}),
        'requests': ('approver', '/api/requests/all', {}),
        'schedule': ('coordinator', '/api/schedule/days', {'start': '2026-10-04', 'end': '2026-11-01'}),
        'audit_filtered': ('auditor', '/api/audit', {'people': 'false', 'filter': json.dumps({
            'conditions': {'conditions': [{'field': 'kind', 'values': ['scale']},
                {'match': 'any', 'conditions': [{'conditions': [{'field': 'action', 'values': ['scale.create']}]},
                                              {'field': 'action', 'values': ['scale.update']}]}]},
            'sort': ['kind', 'asc'], 'then_sort': [['action', 'desc'], ['created_at', 'asc']],
        })}),
    }
    observations, metrics = {}, {}
    def measure_endpoint(name, read):
        read()
        samples = []
        for _ in range(3):
            response, measured = measure(h, 'collection_' + name, read)
            samples.append(measured)
        observations[name] = response.json()
        metrics[name] = {
            'wall_seconds': statistics.median(sample['wall_seconds'] for sample in samples),
            'cpu_seconds': statistics.median(sample['cpu_seconds'] for sample in samples),
            'sql_count': max(sum(sample['sql'].values()) for sample in samples),
            'payload_bytes': len(response.content),
            'samples': samples,
        }
    for name, (role, path, params) in paths.items():
        client = h.client(role)
        def read():
            response = client.get(path, params=params)
            assert response.status_code == 200, response.text
            return response
        measure_endpoint(name, read)

    # Add filter facts after ordinary list measurements, preserving their before/after workload.
    witnesses = [row['participants'][0]['person_id'] for row in data['shifts']
                 if row['id'] in data['focus_ids'] and row['start'].startswith('2026-10-05')][:9]
    assert len(set(witnesses)) == 9
    with transaction(h.app.state.engine, write=True) as db:
        department = h.seed['department_id']
        rank = m.Category(department_id=department, name='Scale filter rank', starts_on=date(2020, 1, 1))
        programme = m.Programme(department_id=department, name='Scale filter programme', starts_on=date(2020, 1, 1))
        tag = m.Tag(department_id=department, name='Scale filter team', labels=['people'])
        room = db.get(m.Location, h.seed['locations'][0])
        building = m.Location(department_id=department, name='Scale filter building',
            location_type_id=room.location_type_id, starts_on=date(2020, 1, 1), capacity=1000)
        db.add_all([rank, programme, tag, building])
        db.flush()
        room.parent_id = building.id
        db.get(m.Service, h.seed['services'][0]).mode = 'physical'
        for index, person_id in enumerate(witnesses):
            db.add(m.Membership(department_id=department, person_id=person_id, category_id=rank.id,
                                starts_on=date(2026, 10, 1)))
            if index % 2:
                db.add(m.TagMember(department_id=department, tag_id=tag.id, kind='people', member_id=person_id))
            else:
                db.add(m.ProgrammeMembership(department_id=department, person_id=person_id,
                    programme_id=programme.id, starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1)))
        tree = {'conditions': [{'field': 'source', 'values': [workspace['id']]},
            {'field': 'category_ids', 'values': [rank.id]},
            {'match': 'any', 'conditions': [{'field': 'programme_ids', 'values': [programme.id]},
                                          {'field': 'tags', 'values': [tag.name]}]},
            {'conditions': [{'field': 'weekday', 'values': ['0']},
                            {'field': 'consultation_mode', 'values': ['physical']},
                            {'field': 'location_ids', 'values': [building.id]}]}]}
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    from apps.api.services import schedule_view
    evaluated, filter_shifts = [], schedule_view._filter_shifts
    def counted(shifts, *args, **kwargs):
        evaluated.append(len(shifts))
        return filter_shifts(shifts, *args, **kwargs)
    with pytest.MonkeyPatch.context() as cache_patch:
        cache_patch.setattr(schedule_view, '_summaries', {})
        cache_patch.setattr(schedule_view, '_filter_shifts', counted)
        def read_filtered_schedule():
            # Measure tree evaluation, rather than the already-computed day-summary cache.
            schedule_view._summaries.clear()
            response = h.client('coordinator').get('/api/schedule/days', params={
                'start': '2026-10-04', 'end': '2026-11-01', 'filter': json.dumps({'conditions': tree})})
            assert response.status_code == 200, response.text
            return response
        measure_endpoint('schedule_filtered', read_filtered_schedule)
    metrics['schedule_filtered']['evaluated_sessions'] = evaluated
    receipt = dict(source=source, database=h.app.state.engine.dialect.name,
                   workload=dict(people=163, sessions=768, assignments=2304, audit_added=1000,
                                 outbox_added=1000, requests=500), metrics=metrics)
    if label := os.getenv('RC17_LIST_PROFILE'):
        destination = Path('.run') / ('rc17-list-' + label)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / (receipt['database'] + '.json')).write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return observations, metrics, receipt


def test_department_collection_lists_stay_paged_and_bounded(collection_scale_measurement, request):
    observed, metrics, receipt = collection_scale_measurement
    request.node.user_properties.append(('collection_scale', json.dumps(receipt)))
    assert len(observed['audit']['events']) == 100 and observed['audit']['has_more']
    assert observed['audit']['people'] == []
    assert len(observed['operations']['outbox']) == 100 and observed['operations']['total'] >= 1000
    assert all('payload' not in row for row in observed['operations']['outbox'])
    assert len(observed['requests']['items']) == 100 and observed['requests']['total'] == 500
    assert observed['requests']['has_more']
    assert sum(counts['sessions'] for day in observed['schedule']['days'].values() for counts in day) == 768
    filtered = observed['audit_filtered']
    assert len(filtered['events']) == 100 and filtered['has_more']
    assert all(row['kind'] == 'scale' and row['action'] == 'scale.update' for row in filtered['events'])
    stamps = [row['created_at'] for row in filtered['events']]
    assert stamps == sorted(stamps)
    assert {day: sum(item['sessions'] for item in entries)
            for day, entries in observed['schedule_filtered']['days'].items()} == {
        f'2026-10-{day:02d}': 9 for day in (5, 12, 19, 26)}
    assert metrics['schedule_filtered']['evaluated_sessions'] == [768] * 4
    for name, sql_limit, payload_limit in (
        ('audit', 16, 150_000), ('operations', 28, 100_000), ('requests', 65, 200_000),
        ('schedule', 24, 50_000), ('audit_filtered', 24, 150_000), ('schedule_filtered', 60, 50_000),
    ):
        assert metrics[name]['sql_count'] <= sql_limit, (name, metrics[name])
        assert metrics[name]['payload_bytes'] <= payload_limit, (name, metrics[name])


@pytest.mark.cpu_budget
def test_department_collection_list_latency(collection_scale_measurement):
    _, metrics, _ = collection_scale_measurement
    # Native CPU: audit/schedule .03s, operations .05s, requests .17s, filtered schedule .33s; allow CI tracing.
    for name, measured in metrics.items():
        assert measured['cpu_seconds'] < python_budget(COLLECTION_CPU_BUDGETS[name]), (name, measured)


@pytest.mark.wall_benchmark
def test_department_collection_list_wall_benchmark(collection_scale_measurement):
    _, metrics, _ = collection_scale_measurement
    for name, measured in metrics.items():
        assert measured['wall_seconds'] < python_budget(max(1, COLLECTION_CPU_BUDGETS[name] * 4)), (name, measured)
