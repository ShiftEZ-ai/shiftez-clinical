"""Benchmark demand, scoring parity, timing provenance and department isolation."""
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
from sqlalchemy import event, func, select

from apps.api import models as m, snapshots
from apps.api.auth import Actor, _effective
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.main import create_app
from apps.api.services import planning, score_check_benchmark as benchmark
from packages.contracts import schemas as s
from tests.legacy_lead import historical_type


def actor(h, role='coordinator'):
    return Actor(h.seed['identities'][role], h.seed['department_id'], h.seed['organisation_id'],
                 'Synthetic benchmark operator', _effective({role}), None, 'benchmark-test')


def command(kind, **payload):
    return s.Command(kind=kind, payload=payload)


def imported(h, base=None):
    workspace = h.workspace('Imported benchmark fixture', base=base)
    if base is None:
        workspace = h.shift(workspace)['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace['id']).origin = 'import'
    return workspace


def clone(db, h, workspace, name='Other draft'):
    source = db.get(m.Workspace, workspace['id'])
    result = planning.create_workspace(db, actor(h), command('workspace.create', name=name,
        starts_on=str(source.starts_on), ends_on=str(source.ends_on)), h.settings)
    row = db.get(m.Workspace, result['workspace']['id'])
    row.base_publication_id = source.base_publication_id
    planning.copy_snapshot_into(db, actor(h), row, snapshots.workspace_content(db, source))
    return row


@pytest.mark.parametrize('role', ['coordinator', 'admin'])
def test_auto_copies_exact_sessions_without_people_or_global_engine_change(h, role):
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        original = db.scalar(select(m.Shift).where(m.Shift.workspace_id == source['id']))
        original.type_snapshot = {**original.type_snapshot, 'name': 'Frozen imported duty'}
        original.attributes = {'imported': ['exact', 4]}
        original.locked = True
        before = snapshots.workspace_shifts(db, source['id'])
        request = command('score_check.auto', workspace_id=source['id'])
        request.expected_revision = source['revision']
        result = benchmark.create_auto_draft(db, actor(h, role), request,
                                             replace(h.settings, generation_engine='cp_sat'))
        auto_id = result['workspace']['id']
        after = snapshots.workspace_shifts(db, auto_id)
        assert [benchmark.session_key(row) for row in before] == [benchmark.session_key(row) for row in after]
        assert after[0]['type_snapshot']['name'] == 'Frozen imported duty'
        assert after[0]['attributes'] == original.attributes
        assert after[0]['participants'] == [] and after[0]['locked'] is False
        assert before[0]['participants']
        job = db.get(m.Job, result['job']['id'])
        assert job.kind == 'generation' and job.status == 'queued'
        assert job.input_snapshot['generation_engine'] == 'cp_sat'
        assert job.input_snapshot['generation_request']['workspace_id'] == auto_id
        assert job.input_snapshot['generation_request']['kind'] == 'generation'
        assert result['workspace']['origin'] == 'auto'
        assert h.settings.generation_engine == 'legacy'


def test_auto_requires_current_source_revision_and_import_origin(h):
    source = imported(h)
    request = command('score_check.auto', workspace_id=source['id'])
    request.expected_revision = source['revision'] - 1
    with transaction(h.app.state.engine) as db, pytest.raises(Problem, match='reload and reconcile') as error:
        benchmark.create_auto_draft(db, actor(h), request, h.settings)
    assert error.value.code == 'STALE_REVISION'
    request.expected_revision = source['revision']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, source['id']).origin = 'planner'
    with transaction(h.app.state.engine) as db, pytest.raises(Problem) as error:
        benchmark.create_auto_draft(db, actor(h), request, h.settings)
    assert error.value.code == 'WORKFLOW_STATE'


def test_identical_n_drafts_share_every_score_component_and_persist_snapshot(h):
    source = imported(h)
    source = h.shift(source, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                     people=[h.seed['people'][1]])['workspace']
    wish = h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'first_day': '2026-10-04',
        'last_day': '2026-10-04', 'kind': 'prefer_off', 'points': 1})['wish']
    advisory = h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week'},
                        {'person_id': h.seed['people'][0]}, severity='advisory')
    mandatory = h.policy({'kind': 'restriction', 'weekdays': [0], 'start_time': '00:00', 'end_time': '24:00'},
                         {'person_id': h.seed['people'][1]})
    with transaction(h.app.state.engine, write=True) as db:
        second = clone(db, h, source)
        third = clone(db, h, source, 'Third draft')
        original = db.get(m.Workspace, source['id'])
        prior = copy.deepcopy(original.validation)
        result = benchmark.compare(db, actor(h), command('score_check.benchmark',
            workspace_ids=[original.id, second.id, third.id]), h.settings)['benchmark']
        assert len(result['rotas']) == 3
        scores = [row['score'] for row in result['rotas']]
        assert all(row['components'] == scores[0]['components'] for row in scores)
        assert all(row['score'] == scores[0]['score'] for row in scores)
        assert set(scores[0]['components']) == {'coverage', 'rules', 'fairness', 'preferences', 'stability'}
        assert all(row['stability'] is None for row in result['rotas'])
        assert all(row['fairness']['goals'] for row in result['rotas'])
        assert all(row['fairness']['spreads'] and row['fairness']['spreads'][0]['value'] > 0
                   for row in result['rotas']), result['rotas'][0]['fairness']
        assert all(row['preferences']['wishes'] == [{'wish_id': wish['id'], 'honoured': 0, 'days': 1}]
                   for row in result['rotas'])
        assert all(row['score']['components']['preferences']['value'] == 0 for row in result['rotas'])
        assert all(row['score']['components']['rules']['value'] < 1 for row in result['rotas'])
        assert all(row['blocking_count'] > 0 and row['warnings_count'] > 0 and row['places_open'] == 1 for row in result['rotas'])
        assert all({advisory['id'], mandatory['id']} <= {finding['rule_id'] for finding in row['findings']}
                   for row in result['rotas'])
        assert original.validation == prior
        second.name = 'Changed after benchmark'
        saved_id = result['id']
    with transaction(h.app.state.engine) as db:
        saved = benchmark.detail(db, actor(h), saved_id)['benchmark']
        assert saved == result
        assert benchmark.list_results(db, actor(h), date(2026, 10, 4), date(2026, 10, 11))['benchmarks'] == [result]
        assert saved['rotas'][1]['name'] == 'Other draft'


@pytest.mark.parametrize('field,value', [('attributes', {'changed': True}), ('cancelled', True),
                                        ('location_id', None), ('start', None)])
def test_comparison_rejects_any_changed_demand(h, field, value):
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        second = clone(db, h, source)
        shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == second.id))
        if field == 'location_id':
            value = h.seed['locations'][1]
        if field == 'start':
            value = shift.start + timedelta(minutes=1)
        setattr(shift, field, value)
        with pytest.raises(Problem) as error:
            benchmark.compare(db, actor(h), command('score_check.benchmark',
                workspace_ids=[source['id'], second.id]), h.settings)
        assert error.value.code == 'BENCHMARK_SESSIONS'
        assert db.scalar(select(func.count()).select_from(m.BenchmarkResult)) == 0


def test_comparison_rejects_different_period_and_rule_overrides(h):
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        second = clone(db, h, source)
        second.ends_on += timedelta(days=1)
        with pytest.raises(Problem) as error:
            benchmark.compare(db, actor(h), command('score_check.benchmark', workspace_ids=[source['id'], second.id]), h.settings)
        assert error.value.code == 'BENCHMARK_PERIOD'
        second.ends_on -= timedelta(days=1)
        second.target_overrides = [{'policy_id': m.uid(), 'policy_revision': 1,
                                    'person_id': h.seed['people'][0], 'amount': 5}]
        with pytest.raises(Problem) as error:
            benchmark.compare(db, actor(h), command('score_check.benchmark', workspace_ids=[source['id'], second.id]), h.settings)
        assert error.value.code == 'BENCHMARK_INPUTS'


def test_common_publication_baseline_maps_clones_without_seeding_its_people(h):
    original = h.shift(h.workspace('Published baseline'))['workspace']
    publication_id = h.publish(original)['publication_id']
    source = imported(h, publication_id)
    with transaction(h.app.state.engine, write=True) as db:
        request = command('score_check.auto', workspace_id=source['id'])
        request.expected_revision = source['revision']
        result = benchmark.create_auto_draft(db, actor(h), request,
                                             replace(h.settings, generation_engine='cp_sat'))
        auto = db.get(m.Workspace, result['workspace']['id'])
        assert auto.base_publication_id == publication_id
        assert all(not row['participants'] for row in snapshots.workspace_shifts(db, auto.id))
        assert benchmark._baseline(db, actor(h), db.get(m.Workspace, source['id'])).id == publication_id
        ordinary = clone(db, h, source, 'Ordinary amendment')
        for other in (auto, ordinary):
            check = planning.revalidate(db, other, store=False)
            assert check['scope'] == 'published_changes' and 'score' not in check
            with pytest.raises(Problem) as error:
                benchmark.compare(db, actor(h), command('score_check.benchmark',
                    workspace_ids=[source['id'], other.id]), h.settings)
            assert error.value.code == 'BENCHMARK_SCOPE'


def test_baseline_reports_partial_or_unavailable_mapping_without_inventing_stability():
    row = {'id': 'one', 'assignment_type_id': 'type', 'type_revision': 1, 'type_snapshot': {},
           'location_id': 'place', 'attributes': {}, 'cancelled': False, 'service_ids': [],
           'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participants': []}
    details = {}
    assert benchmark._mapped_baseline([row, {**row, 'id': 'two'}], [{**row, 'id': 'published'}], details) is None
    assert details == {'state': 'unavailable', 'matched_sessions': 0, 'unmatched_sessions': 0, 'ambiguous_sessions': 1}
    assert benchmark._mapped_baseline([row], [{**row, 'location_id': 'different-place'}], details) is None
    assert details['state'] == 'unavailable' and details['unmatched_sessions'] == 1
    assert benchmark._mapped_baseline([row], [row, {**row, 'location_id': 'different-place'}], details) == {}
    assert details['state'] == 'partial' and details['matched_sessions'] == details['unmatched_sessions'] == 1


def test_permissions_and_department_isolation_cover_all_entry_points(h):
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        result = benchmark.compare(db, actor(h), command('score_check.benchmark', workspace_ids=[source['id']]), h.settings)
        result_id = result['benchmark']['id']
        for read in (lambda: benchmark.detail(db, actor(h, 'staff'), result_id),
                     lambda: benchmark.list_results(db, actor(h, 'staff')),
                     lambda: benchmark.compare(db, actor(h, 'staff'), command('score_check.benchmark', workspace_ids=[source['id']]), h.settings),
                     lambda: benchmark.create_auto_draft(db, actor(h, 'staff'), command('score_check.auto', workspace_id=source['id']), h.settings)):
            with pytest.raises(Problem) as error:
                read()
            assert error.value.status == 403
        outsider = replace(actor(h), department_id=m.uid())
        assert benchmark.list_results(db, outsider)['benchmarks'] == []
        with pytest.raises(Problem) as error:
            benchmark.detail(db, outsider, result_id)
        assert error.value.status == 404
        with pytest.raises(Problem) as error:
            benchmark.compare(db, outsider, command('score_check.benchmark', workspace_ids=[source['id']]), h.settings)
        assert error.value.status == 404


def test_entered_time_counts_work_hours_and_distinct_people_without_inventing_blanks(h):
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, source['id'])
        batch = m.ScoreCheckImportBatch(department_id=actor(h).department_id, workspace_id=workspace.id,
                                       created_by=actor(h).identity_id, status='saved')
        db.add(batch)
        db.flush()
        for index, (hours, people) in enumerate(((12, h.seed['people'][:2]), (8, h.seed['people'][:1]), (None, []))):
            db.add(m.ImportSource(department_id=actor(h).department_id, batch_id=batch.id, workspace_id=workspace.id,
                file_name=f'File {index}.csv', file_ref=str(index), sha256=str(index) * 64, format='csv',
                work_hours_each=hours, made_by=people))
        db.flush()
        result = benchmark._time(db, workspace)
        assert result == {'work_hours': 32, 'people_count': 2, 'missing_files': 1, 'source': 'entered'}
        batch.manual_time_hours, batch.manual_time_people = 40, 4
        assert benchmark._time(db, workspace) == {'work_hours': 40, 'people_count': 4, 'source': 'entered'}


def test_benchmark_reuses_import_gap_decisions_and_blocks_unacknowledged_alarms(h, monkeypatch):
    from apps.api.services import score_check
    source = imported(h)
    counts = s.ScoreCheckGapCounts(missing=1, unresolved=1).model_dump()
    monkeypatch.setattr(score_check, 'gap_counts', lambda *args: counts)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.ScoreCheckImportBatch(department_id=actor(h).department_id, workspace_id=source['id'],
                                      created_by=actor(h).identity_id, status='saved'))
        db.flush()
        request = command('score_check.auto', workspace_id=source['id'])
        request.expected_revision = source['revision']
        with pytest.raises(Problem) as error:
            benchmark.create_auto_draft(db, actor(h), request, h.settings)
        assert error.value.code == 'IMPORT_ALARMS'
        with pytest.raises(Problem) as error:
            benchmark.compare(db, actor(h), command('score_check.benchmark', workspace_ids=[source['id']]), h.settings)
        assert error.value.code == 'IMPORT_ALARMS'
        counts.update(missing=0, unresolved=0, acknowledged=1)
        result = benchmark.compare(db, actor(h), command('score_check.benchmark', workspace_ids=[source['id']]), h.settings)
        assert result['benchmark']['rotas'][0]['import_gaps']['acknowledged'] == 1


def test_auto_time_uses_measured_jobs_and_per_reviewer_activity_excluding_long_gaps(h):
    source = imported(h)
    start = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, source['id'])
        workspace.origin = 'auto'
        workspace.submitted_at = start + timedelta(minutes=42)
        db.add(m.Job(department_id=actor(h).department_id, workspace_id=workspace.id, requested_by=actor(h).identity_id,
            kind='generation', status='applied', input_fingerprint='a' * 64, input_snapshot={},
            started_at=start - timedelta(seconds=30), completed_at=start,
            result={'applied_content_revision': 3}))
        for revision, minutes, role in ((3, 0, 'coordinator'), (4, 5, 'coordinator'),
                                         (5, 8, 'admin'), (6, 40, 'coordinator')):
            db.add(m.ChangeSet(workspace_id=workspace.id, before_snapshot={}, resulting_content_revision=revision,
                actor_id=actor(h, role).identity_id, reason='Synthetic review', created_at=start + timedelta(minutes=minutes)))
        db.flush()
        result = benchmark._time(db, workspace)
        # Coordinator 0..5 + 40..42, admin 0..8; gaps over 15 minutes count as no activity.
        assert result['solver_seconds'] == 30
        assert result['review_hours'] == pytest.approx(15 / 60)
        assert result['people_count'] == 2 and result['source'] == 'mixed'
        assert result['work_hours'] == pytest.approx((15 * 60 + 30) / 3600)


def test_comparison_handles_more_than_500_sessions_without_truncation(h):
    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, source['id'])
        old = snapshots.workspace_shifts(db, workspace.id)[0]
        empty = {**old, 'participants': []}
        planning.copy_snapshot_into(db, actor(h), workspace, {'shifts': [empty] * 500})
        second = clone(db, h, source)
        report = benchmark.compare(db, actor(h), command('score_check.benchmark',
            workspace_ids=[workspace.id, second.id]), h.settings)['benchmark']
        assert len(snapshots.workspace_shifts(db, second.id)) == 501
        assert report['rotas'][0]['places_open'] == report['rotas'][1]['places_open'] == 500


@pytest.mark.parametrize('role', benchmark.READ_ROLES)
def test_frontend_view_all_read_roles_without_auto_or_database_writes(h, role):
    source = imported(h)
    reader = replace(actor(h), roles=frozenset({role}))
    writes = []
    def capture(_connection, _cursor, statement, *_args):
        if statement.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}:
            writes.append(statement)
    with transaction(h.app.state.engine) as db:
        original = db.get(m.Workspace, source['id'])
        validation = copy.deepcopy(original.validation)
        event.listen(h.app.state.engine, 'before_cursor_execute', capture)
        try:
            result = benchmark.frontend_view(db, reader, source['id'])
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', capture)
        assert len(result['sides']) == 1 and result['sides'][0]['workspace_id'] == source['id']
        assert result['period_start'] == '2026-10-04' and result['period_end'] == '2026-10-11'
        assert result['sessions'] == 1 and result['fairness_unit'] == 'h'
        assert result['sides'][0]['time_hours'] is None and result['sides'][0]['time_basis'] is None
        assert result['sides'][0]['wishes_met'] is None
        assert result['sides'][0]['stability_detail'] == 'No baseline'
        assert all(len(row['within']) == len(row['counts']) == 1 for row in result['fairness'])
        assert original.validation == validation and not db.dirty and not db.new
        assert db.scalar(select(func.count()).select_from(m.BenchmarkResult)) == 0
        assert writes == []
        if role != 'coordinator':
            with pytest.raises(Problem) as error:
                benchmark.compare(db, reader, command('score_check.benchmark', workspace_ids=[source['id']]), h.settings)
            assert error.value.status == 403


def test_frontend_projection_aligns_n_drafts_rules_fairness_gaps_and_preference_points(h):
    source = imported(h)
    advisory = h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week'},
                        {'person_id': h.seed['people'][0]}, severity='advisory')
    for person, points in zip(h.seed['people'][:2], (1, 3)):
        h.command('coordinator', 'wish.create', {'person_id': person, 'first_day': '2026-10-04',
            'last_day': '2026-10-04', 'kind': 'prefer_off', 'points': points})
    with transaction(h.app.state.engine, write=True) as db:
        request = command('score_check.auto', workspace_id=source['id'])
        request.expected_revision = source['revision']
        auto = benchmark.create_auto_draft(db, actor(h), request,
                                           replace(h.settings, generation_engine='cp_sat'))['workspace']
        extra = clone(db, h, source)
        report = benchmark.compare(db, actor(h), command('score_check.benchmark',
            workspace_ids=[source['id'], auto['id'], extra.id]), h.settings)['benchmark']
        result = report['frontend']
        assert [side['workspace_id'] for side in result['sides']] == [source['id'], auto['id'], extra.id]
        assert [side['wishes_met'] for side in result['sides']] == [75, 100, 75]
        rule = next(row for row in result['rules'] if row['rule_id'] == advisory['id'])
        assert rule['counts'] == [1, 0, 1] and rule['mandatory'] is False
        assert rule['number'] == '1 hours'
        assert result['gaps_total'] == 1 and result['gaps'][0]['open'] == [0, 1, 0]
        assert result['fairness_unit'] == 'h'
        for side, rota in zip(result['sides'], report['rotas']):
            for part, component in zip(side['parts'], benchmark.COMPONENTS):
                value = rota['score']['components'][component]
                assert part['value'] == (-value['lost'] if value['value'] is not None else None)
        assert benchmark.frontend_view(db, replace(actor(h), roles=frozenset({'auditor'})), source['id']) == result
        assert db.scalar(select(func.count()).select_from(m.BenchmarkResult)) == 1


def test_benchmark_keeps_lead_only_gap_blocking_without_counting_an_open_seat(h):
    typ = historical_type(h, {'name': 'Benchmark lead-only duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
                       'lead': {'minimum': 1, 'maximum': 1,
                                'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []}}})['id']
    source = h.workspace('Imported lead benchmark fixture')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, source['id']).origin = 'import'
    source = h.shift(source, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                    typ=typ, people=h.seed['people'][:2])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        report = benchmark.compare(db, actor(h), command('score_check.benchmark',
            workspace_ids=[source['id']]), h.settings)['benchmark']
    assert [side['not_covered'] for side in report['frontend']['sides']] == [0]
    assert all(side['blocking'] > 0 and not side['publishable'] for side in report['frontend']['sides'])
    assert report['frontend']['gaps'] == []
    assert all(any(finding['code'] == 'LEAD_GAP' for finding in rota['findings'])
               for rota in report['rotas'])


def test_frontend_view_empty_and_denied_roles_keep_missing_values_unknown(h):
    source = h.workspace('Empty imported rota')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, source['id']).origin = 'import'
        result = benchmark.frontend_view(db, actor(h), source['id'])
        side = result['sides'][0]
        assert result['sessions'] == 0 and result['gaps'] == []
        assert side['score'] is None and side['publishable'] is False
        assert all(part['value'] is None for part in side['parts'])
        for role in ('staff', 'credentialer'):
            denied = replace(actor(h), roles=frozenset({role}))
            for operation in (lambda: benchmark.frontend_view(db, denied, source['id']),
                              lambda: benchmark.compare(db, denied, command('score_check.benchmark', workspace_ids=[source['id']]), h.settings),
                              lambda: benchmark.create_auto_draft(db, denied, command('score_check.auto', workspace_id=source['id']), h.settings)):
                with pytest.raises(Problem) as error:
                    operation()
                assert error.value.status == 403
        with pytest.raises(Problem) as error:
            benchmark.frontend_view(db, replace(actor(h), department_id=m.uid()), source['id'])
        assert error.value.status == 404
    assert h.get('admin', '/api/benchmark?workspace=' + source['id'])['sides'][0]['score'] is None


def test_frontend_time_description_distinguishes_measured_and_estimated_parts():
    assert benchmark._time_description(None) == (None, 'Not entered')
    basis, detail = benchmark._time_description({'source': 'measured', 'work_hours': 1 / 120, 'solver_seconds': 30})
    assert basis is None and detail == 'Solver 30 seconds (measured)'
    basis, detail = benchmark._time_description({'source': 'mixed', 'work_hours': 0.5,
        'solver_seconds': 30, 'review_hours': 0.49})
    assert basis == 'estimated' and '(measured)' in detail and '(estimated)' in detail
    assert '1 files have no time entered' in benchmark._time_description({'source': 'entered', 'work_hours': 40,
        'people_count': 4, 'missing_files': 1})[1]


def test_auto_refuses_legacy_engine_before_queueing(h):
    source = imported(h)
    refused = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']},
                        revision=source['revision'], status=422)
    assert refused['error']['code'] == 'SCORE_CHECK_ENGINE_REQUIRED'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Job)) == 0
        assert db.scalar(select(func.count()).select_from(m.Workspace).where(m.Workspace.origin == 'auto')) == 0


def test_score_check_cp_sat_worker_completes_and_apply_accepts(h, cp_sat_engine):
    from apps.worker.main import process_one

    source = imported(h)
    queued = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']},
                       revision=source['revision'])
    auto, job_id = queued['workspace'], queued['job']['id']
    assert queued['job']['generation_engine'] == h.settings.generation_engine == 'cp_sat'
    assert process_one(h.app.state.engine, solver_workers=1, settings=h.settings)
    completed = h.get('coordinator', '/api/jobs/' + job_id)['job']
    assert completed['status'] == 'succeeded' and completed['result']['validation']['valid']
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': auto['id'], 'job_id': job_id,
        'reason': 'Reviewed score-check solver candidate'}, revision=auto['revision'])
    assert applied['workspace']['status'] == 'draft' and applied['validation']['valid']


def test_real_cp_sat_worker_apply_and_persisted_benchmark_journey(h, cp_sat_engine):
    from importlib.metadata import version
    from apps.worker.main import process_one
    from packages.domain.solver_rules import COMPILER_VERSION

    source = imported(h)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.generation_defaults = {**(department.generation_defaults or {}),
            'search_minutes': 1, 'stop_without_improvement_seconds': 1}
        source_before = snapshots.workspace_content(db, db.get(m.Workspace, source['id']))
        validation_before = copy.deepcopy(db.get(m.Workspace, source['id']).validation)
        assert db.scalar(select(func.count()).select_from(m.ScoreCheckExtractionJob)) == 0
        assert db.scalar(select(func.count()).select_from(m.AiCall)) == 0
    queued = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']},
                       revision=source['revision'])
    auto, job_id = queued['workspace'], queued['job']['id']
    assert queued['job']['generation_engine'] == 'cp_sat'
    assert h.detail(auto)['shifts'][0]['participants'] == []
    assert process_one(h.app.state.engine, solver_workers=1, settings=h.settings)
    completed = h.get('coordinator', '/api/jobs/' + job_id)['job']
    result = completed['result']
    assert completed['status'] == 'succeeded', completed
    assert result['status'] == 'candidate_feasible' and result['validation']['valid']
    assert result['solver_workers'] == 1 and result['solver_version'] == version('ortools')
    assert result['algorithm_version'] == COMPILER_VERSION
    assert result['weighted_status'] in {'FEASIBLE', 'OPTIMAL'}
    if result['optimality_proven']:
        assert result['weighted_status'] == 'OPTIMAL'
        assert result['objectives'] and all(row['proven_optimal'] for row in result['objectives'])
    if result['weighted_status'] == 'FEASIBLE':
        assert result['optimality_proven'] is False
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': auto['id'], 'job_id': job_id,
        'reason': 'Reviewed real benchmark solver candidate'}, revision=auto['revision'])
    assert applied['validation']['valid'] and applied['workspace']['status'] == 'draft'
    automatic = h.detail(auto)['shifts']
    assert len(automatic) == 1
    required = automatic[0]['type_snapshot']['definition']['requirements'][0]['minimum']
    people = automatic[0]['participants']
    assert required == 1 and len({person['person_id'] for person in people}) >= required
    assert all(person['person_id'] in h.seed['people'] and person['start'] == automatic[0]['start']
               and person['end'] == automatic[0]['end'] for person in people)
    assert all(row['available_eligible'] >= row['required'] for row in applied['validation']['coverage'])
    persisted = h.command('coordinator', 'score_check.benchmark',
        {'workspace_ids': [source['id'], auto['id']]})['benchmark']
    canonical = h.get('auditor', '/api/benchmark?workspace=' + source['id'])
    assert canonical == persisted['frontend']
    assert canonical['sessions'] == 1 and [side['not_covered'] for side in canonical['sides']] == [0, 0]
    assert canonical['sides'][1]['publishable'] is True
    assert canonical['sides'][1]['time_hours'] is not None and '(measured)' in canonical['sides'][1]['time_detail']
    with transaction(h.app.state.engine) as db:
        source_after = snapshots.workspace_content(db, db.get(m.Workspace, source['id']))
        assert source_after == source_before
        assert db.get(m.Workspace, source['id']).validation == validation_before
        automatic_rows = snapshots.workspace_shifts(db, auto['id'])
        assert sorted(map(benchmark.session_key, automatic_rows)) == sorted(map(benchmark.session_key, source_before['shifts']))
        job = db.get(m.Job, job_id)
        assert job.status == 'applied' and job.started_at is not None and job.completed_at is not None
        elapsed = (job.completed_at - job.started_at).total_seconds()
        assert persisted['rotas'][1]['time']['solver_seconds'] == pytest.approx(elapsed)
        assert db.get(m.BenchmarkResult, persisted['id']).report == persisted
        assert db.scalar(select(func.count()).select_from(m.ScoreCheckExtractionJob)) == 0
        assert db.scalar(select(func.count()).select_from(m.AiCall)) == 0
    history = h.get('auditor', '/api/benchmarks?starts_on=2026-10-04&ends_on=2026-10-11')['benchmarks']
    assert [report['id'] for report in history] == [persisted['id']]
