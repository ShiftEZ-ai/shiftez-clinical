"""Creation Undo prefilters retained JSON evidence before decoding candidates."""
from datetime import date, timedelta
from time import perf_counter, process_time

import coverage
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import catalogue
from packages.domain.quotas import METRIC_VERSION


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def reference_h(request):
    return request.getfixturevalue(request.param)


def test_json_candidate_prefilter_preserves_exact_strings_and_pending_overlays(reference_h):
    h = reference_h
    with transaction(h.app.state.engine, write=True) as db:
        target = m.Service(department_id=h.seed['department_id'], name='Synthetic exact JSON reference', starts_on=date(2020, 1, 1))
        db.add(target)
        db.flush()
        view = m.TableView(department_id=h.seed['department_id'], owner_id=h.seed['identities']['admin'],
            table_key='services', name='Synthetic exact JSON candidate', definition={'value': 'prefix-' + target.id})
        db.add(view)
        db.flush()
        db.info['bulk'] = {'test': True}
        catalogue._require_unreferenced(db, target)
        view.definition = {'nested': [{target.id: True}]}
        with db.no_autoflush, pytest.raises(Problem):
            catalogue._require_unreferenced(db, target)
        db.flush()
        with pytest.raises(Problem):
            catalogue._require_unreferenced(db, target)
        db.delete(view)
        with db.no_autoflush:
            catalogue._require_unreferenced(db, target)


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def retained_publications_reference_measurement(request, scale_harness):
    h = scale_harness(request.param)
    first = date(2016, 1, 1)
    snapshot = {'people': [{'id': m.uid(), 'display_name': f'Synthetic retained colleague {n}'} for n in range(150)],
                'shifts': [{'id': m.uid(), 'service_ids': [m.uid()], 'participants': [{'person_id': m.uid()}]}
                           for _ in range(500)]}
    with pytest.MonkeyPatch.context() as monkeypatch, transaction(h.app.state.engine, write=True) as db:
        def publication(n, stored_snapshot):
            start = first + timedelta(days=31 * n)
            workspace = m.Workspace(department_id=h.seed['department_id'], name=f'Synthetic retained month {n}',
                starts_on=start, ends_on=start + timedelta(days=28), status='published',
                created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
            db.add(workspace)
            db.flush()
            db.add(m.Publication(department_id=h.seed['department_id'], workspace_id=workspace.id, sequence=n + 1,
                starts_on=workspace.starts_on, ends_on=workspace.ends_on, published_by=h.seed['identities']['coordinator'],
                snapshot=stored_snapshot, validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}},
                fingerprint='f' * 64))
        for n in range(120):
            publication(n, snapshot)
        target = m.Service(department_id=h.seed['department_id'], name='Synthetic unrelated new clinic', starts_on=first)
        db.add(target)
        db.flush()
        decoded = []
        original = catalogue._json_strings
        def tracked(value):
            decoded.append(value)
            return original(value)
        monkeypatch.setattr(catalogue, '_json_strings', tracked)
        started, cpu_started = perf_counter(), process_time()
        catalogue._require_unreferenced(db, target)
        elapsed, cpu = perf_counter() - started, process_time() - cpu_started
        print({'creation_reference_seconds': elapsed, 'creation_reference_cpu_seconds': cpu, 'decoded_documents': len(decoded)})
        unrelated_decoded = len(decoded)
        publication(120, {**snapshot, 'retained_service': target.id})
        db.flush()
        with pytest.raises(Problem) as error:
            catalogue._require_unreferenced(db, target)
        return {'unrelated_decoded': unrelated_decoded, 'reference_error': error.value.code,
                'decoded_documents': len(decoded), 'wall_seconds': elapsed, 'cpu_seconds': cpu}


def test_creation_reference_budget_with_ten_years_of_department_publications(retained_publications_reference_measurement, request):
    measured = retained_publications_reference_measurement
    request.node.user_properties.extend([('creation_reference_seconds', measured['wall_seconds']),
        ('creation_reference_cpu_seconds', measured['cpu_seconds']),
        ('creation_reference_decoded_documents', measured['decoded_documents'])])
    assert measured['unrelated_decoded'] == 0
    assert measured['reference_error'] == 'INTEGRITY_CONFLICT'
    assert measured['decoded_documents'] == 1


@pytest.mark.cpu_budget
def test_creation_reference_cpu_budget_with_ten_years_of_department_publications(retained_publications_reference_measurement):
    measured = retained_publications_reference_measurement
    seconds = 1.5 * (3 if coverage.Coverage.current() else 1)
    assert measured['cpu_seconds'] < seconds, measured


@pytest.mark.wall_benchmark
def test_creation_reference_wall_benchmark_with_ten_years_of_department_publications(retained_publications_reference_measurement):
    measured = retained_publications_reference_measurement
    seconds = 1.5 * (3 if coverage.Coverage.current() else 1)
    assert measured['wall_seconds'] < seconds * 4, measured
