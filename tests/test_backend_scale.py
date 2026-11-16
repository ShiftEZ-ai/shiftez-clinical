"""API regression workload: 163 people, 768 sessions and 2,304 assignments in four weeks."""
import cProfile
import gc
import io
import json
import os
import pstats
import time
import uuid
import coverage
import pytest
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Date, DateTime, delete, event, func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import fingerprint
from packages.contracts.schemas import SeriesDefinition
from packages.domain.recurrence import expand
from tests.scheduler_departments import START, department
from tests.test_performance_budget import python_budget


def scale_department(h, assigned=True):
    """Reuse the scheduler department's catalogue and people, persisted to the real migrated test DB.

    27 daily and three weekly templates give 192 sessions / 576 assignments each week. They use
    disjoint groups of assignees, so publication exercises a valid department, not an empty check.
    """
    data, _ = department(h, 51, people=163, days=28, rules='none', leave_rate=0, clinics=1)
    typ = h.configure('assignment_types', {'name': 'Scale clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 3, 'maximum': 3}]}})['record']
    # Non-safety policy inputs must be frozen in the draft baseline; min_rest is safety and remains live.
    h.policy({'kind': 'max_hours', 'hours': 40, 'window': 'week'})
    h.policy({'kind': 'max_duties', 'maximum': 4, 'window': 'week'}, severity='advisory')
    wid = data['workspace']['id']
    with transaction(h.app.state.engine, write=True) as db:
        for cls, values in ((m.Person, data['people'].values()), (m.Membership, data['memberships']),
                            (m.Eligibility, data['eligibilities']), (m.CapabilityGrant, data['grants'])):
            known = set(db.scalars(select(cls.id)))
            for value in values:
                if value['id'] in known:
                    continue
                fields = {c.name: value[c.name] for c in cls.__table__.columns if c.name in value}
                for column in cls.__table__.columns:
                    if fields.get(column.name) is not None and isinstance(fields[column.name], str):
                        if isinstance(column.type, DateTime):
                            fields[column.name] = datetime.fromisoformat(fields[column.name])
                        elif isinstance(column.type, Date):
                            fields[column.name] = date.fromisoformat(fields[column.name])
                db.add(cls(**fields))
            db.flush()
        ids = list(db.scalars(select(m.Shift.id).where(m.Shift.workspace_id == wid)))
        db.execute(delete(m.Assignment).where(m.Assignment.shift_id.in_(ids)))
        db.execute(delete(m.ShiftService).where(m.ShiftService.shift_id.in_(ids)))
        db.execute(delete(m.Shift).where(m.Shift.id.in_(ids)))
        for location in db.scalars(select(m.Location)):
            location.capacity = 1000
        people = list(data['people'])
        series_ids, shifts, links, additions = [], [], [], []
        for n in range(30):
            definition = SeriesDefinition(anchor=START, service_ids=[h.seed['services'][0]],
                days=[{'weekday': day, 'start_time': '08:00', 'end_time': '12:00'}
                      for day in (range(7) if n < 27 else [6])], default_person_ids=people[n * 3:n * 3 + 3])
            series = m.RecurringSeries(id=m.uid(), department_id=h.seed['department_id'], name=f'Scale template {n}',
                assignment_type_id=typ['id'], location_id=h.seed['locations'][0], starts_on=START,
                ends_on=date(2026, 11, 29), definition=definition.model_dump(mode='json'))
            db.add(series)
            series_ids.append(series.id)
            for occurrence in expand(definition, START, date(2026, 11, 1)):
                shift = m.Shift(id=m.uid(), workspace_id=wid, assignment_type_id=typ['id'], type_revision=typ['revision'],
                    type_snapshot=typ, location_id=series.location_id, **occurrence)
                shifts.append(shift)
                links.append(m.ShiftService(shift_id=shift.id, service_id=h.seed['services'][0]))
                for pid in definition.default_person_ids:
                    participant = {'person_id': pid, 'start': occurrence['start'].isoformat(),
                                   'end': occurrence['end'].isoformat()}
                    additions.append({'shift_id': shift.id, 'participant': participant})
                    if assigned:
                        links.append(m.Assignment(shift_id=shift.id, person_id=pid, **occurrence))
        db.flush()
        db.add_all(shifts)
        db.flush()
        db.add_all(links)
    w = h.refresh_draft_baseline({'id': wid, 'revision': h.detail(wid)['workspace']['revision']})
    h.policy({'kind': 'min_rest', 'hours': 11})
    data = h.snapshot(w)
    assert len(data['people']) == 163 and len(data['focus_ids']) == 768 and len(additions) == 2304
    return w, data, additions, series_ids


def measure(h, name, operation, monkeypatch=None, target=None):
    """Count SQL without values; profile in the API thread where TestClient executes the service."""
    counts, profile = Counter(), cProfile.Profile()
    if not os.getenv('SCALE_CPROFILE'):
        target = None
    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        counts[statement.split()[0].upper()] += 1
    if target:
        targets = target if isinstance(target, list) else [target]
        originals = [(module, attribute, getattr(module, attribute)) for module, attribute in targets]
        for module, attribute, real in originals:
            def profiled(*args, _real=real, **kwargs):
                return profile.runcall(_real, *args, **kwargs)
            monkeypatch.setattr(module, attribute, profiled)
    # Exclude collection of earlier fixtures in a reused pytest worker, as in scheduler performance tests.
    # GC remains enabled during the request, so its own allocation/collection costs are measured.
    gc.collect()
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    began, cpu = time.perf_counter(), time.process_time()
    try:
        result = operation()
    finally:
        elapsed, cpu_elapsed = time.perf_counter() - began, time.process_time() - cpu
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
        if target:
            for module, attribute, real in originals:
                monkeypatch.setattr(module, attribute, real)
    metrics = {'wall_seconds': round(elapsed, 4), 'cpu_seconds': round(cpu_elapsed, 4), 'sql': dict(counts)}
    print(name, metrics)
    if label := os.getenv('SCALE_PROFILE'):
        out = Path('.run') / ('scale-' + label + '-' + h.app.state.engine.dialect.name)
        out.mkdir(parents=True, exist_ok=True)
        (out / (name + '.json')).write_text(json.dumps(metrics, indent=2) + '\n')
        stream = io.StringIO()
        if target:
            pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(40)
        (out / (name + '.txt')).write_text(stream.getvalue())
    return result, metrics


def post(h, kind, payload, target=None, revision=None, expected_fingerprint=None):
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={'kind': kind, 'payload': payload, 'target_id': target, 'expected_revision': revision,
              'expected_fingerprint': expected_fingerprint})
    assert response.status_code == 200, response.text
    return response.json()


def interactive_path_measurement(h, monkeypatch):
    observations = {}
    # Benchmark one application instant, independent of minute-based rate-limit cleanup and session renewal.
    moment = m.now()
    monkeypatch.setattr(m, 'clock_now', lambda: moment)
    from apps.api import main as api, snapshots
    from apps.api.services import jobs, planning, planner_tools
    from packages.domain import fairness, feasibility
    def no_rule_comparison(*args, **kwargs):
        pytest.fail('Everyday rota commands must not run activation checks or comparisons')
    for name in ('check', 'compare_findings', 'evaluate'):
        monkeypatch.setattr(feasibility, name, no_rule_comparison)
    w, data, additions, series_ids = scale_department(h, assigned=False)
    h.client('coordinator')
    # Warm pure validation without replaying state-changing commands.
    planning.evaluate(data)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        job = m.Job(id=m.uid(), department_id=workspace.department_id, workspace_id=workspace.id,
            kind='generation', status='succeeded', requested_by=h.seed['identities']['coordinator'],
            input_fingerprint=fingerprint(db, workspace), input_snapshot=data,
            result={'status': 'candidate_feasible', 'additions': additions, 'removals': []})
        db.add(job)
        job_id = job.id
    checks, evaluate = [], planning.evaluate
    def checked(snapshot, **kwargs):
        checks.append(len(snapshot['focus_ids']))
        return evaluate(snapshot, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', checked)
    applied, apply_metrics = measure(h, 'apply', lambda: post(h, 'generation.apply',
        {'workspace_id': w['id'], 'job_id': job_id, 'reason': 'Scale proposal'}, revision=w['revision']),
        monkeypatch, (jobs, 'apply_generation'))
    observations['apply_checks'] = checks[:]
    monkeypatch.setattr(planning, 'evaluate', evaluate)
    w = applied['workspace']
    observations['applied'] = applied
    # The publication ledger needs the populated rota, including all 2,304 assignments.
    fairness.ledger_facts(h.snapshot(w))
    boot_paths = ('/api/me', '/api/catalogues', '/api/leave-types', '/api/pulse', '/api/home')
    for path in boot_paths:
        h.get('coordinator', path)
    _, boot_metrics = measure(h, 'first_page', lambda: [h.get('coordinator', path) for path in boot_paths],
        monkeypatch, [(route.dependant, 'call') for route in h.app.routes if route.path in boot_paths])
    h.get('coordinator', '/api/jobs/' + job_id)
    _, proposal_metrics = measure(h, 'proposal', lambda: h.get('coordinator', '/api/jobs/' + job_id),
        monkeypatch, [(route.dependant, 'call') for route in h.app.routes if route.path == '/api/jobs/{job_id}'])
    w = h.lifecycle(w, 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    checks.clear()
    monkeypatch.setattr(planning, 'evaluate', checked)
    ledger_checks, ledger_facts = [], fairness.ledger_facts
    def ledger(snapshot):
        facts = ledger_facts(snapshot)
        ledger_checks.append(len(facts))
        return facts
    monkeypatch.setattr(fairness, 'ledger_facts', ledger)
    command_cpu, execute = [], api.execute
    def timed_command(*args, **kwargs):
        started, process_started = time.thread_time(), time.process_time()
        try:
            return execute(*args, **kwargs)
        finally:
            command_cpu.append({'command_cpu_seconds': time.thread_time() - started,
                                'command_process_cpu_seconds': time.process_time() - process_started})
    with monkeypatch.context() as command_patch:
        command_patch.setattr(api, 'execute', timed_command)
        published, publish_metrics = measure(h, 'publish', lambda: post(h, 'workspace.publish',
            {'reason': 'Scale publication'}, w['id'], w['revision']), monkeypatch, (planning, 'lifecycle'))
    observations['publish_commands'] = len(command_cpu)
    publish_metrics.update(command_cpu[0])
    print('publish command CPU', command_cpu[0])
    observations['publish_checks'] = checks[:], ledger_checks[:]
    monkeypatch.setattr(planning, 'evaluate', evaluate)
    monkeypatch.setattr(fairness, 'ledger_facts', ledger_facts)
    observations['published'] = published
    snapshot_work = Counter()
    snapshot_inputs, shift_records = snapshots.snapshot_inputs, snapshots.shift_records
    def loaded_inputs(*args, **kwargs):
        snapshot_work['snapshot_inputs'] += 1
        return snapshot_inputs(*args, **kwargs)
    def serialized_shifts(db, rows):
        records = shift_records(db, rows)
        external = [row for row in records if row['workspace_id'] == w['id']]
        snapshot_work['external_sessions'] += len(external)
        snapshot_work['external_assignments'] += sum(len(row['participants']) for row in external)
        return records
    work = Counter()
    serialized, original_record = [], planning.record
    def recorded(row, *args, **kwargs):
        if isinstance(row, m.RecurringSeries):
            serialized.append(row.id)
        return original_record(row, *args, **kwargs)
    monkeypatch.setattr(planning, 'record', recorded)
    for name in ('expand', 'materialise_occurrence'):
        real = getattr(planning, name)
        def counted(*args, _real=real, _name=name, **kwargs):
            work[_name] += 1
            return _real(*args, **kwargs)
        monkeypatch.setattr(planning, name, counted)
    checks.clear()
    monkeypatch.setattr(planning, 'evaluate', checked)
    # A different month keeps the new draft independent from the published rota.
    with monkeypatch.context() as snapshot_patch:
        snapshot_patch.setattr(snapshots, 'snapshot_inputs', loaded_inputs)
        snapshot_patch.setattr(snapshots, 'shift_records', serialized_shifts)
        created, create_metrics = measure(h, 'create', lambda: post(h, 'workspace.create',
            {'name': 'Scale recurring draft', 'starts_on': '2026-11-01', 'ends_on': '2026-11-29', 'series_ids': series_ids}),
            monkeypatch, (planning, 'create_workspace'))
    observations['created'] = created
    observations['create_series_snapshots'] = serialized[:]
    serialized.clear()
    observations['create_checks'] = checks[:], work.copy(), snapshot_work.copy()
    create_metrics['work'] = dict(work, validations=len(checks), **snapshot_work)
    checks.clear()
    work.clear()
    copied, copy_metrics = measure(h, 'copy', lambda: post(h, 'workspace.create',
        {'name': 'Scale copied draft', 'starts_on': '2026-11-29', 'ends_on': '2026-12-27',
         'source_workspace_id': w['id'], 'source_revision': published['workspace']['revision'], 'copy_people': True}),
        monkeypatch, (planning, 'create_workspace'))
    observations['copied'] = copied
    observations['copy_checks'] = checks[:], work.copy()
    copy_metrics['work'] = dict(work, validations=len(checks))
    copied_shifts = h.detail(copied['workspace'])['shifts']
    observations['copied_shifts'] = copied_shifts
    preview_w = h.workspace('Scale preview', start='2026-10-04', end='2026-11-01')
    def preview():
        response = h.client('coordinator').post('/api/planner/preview', json={'kind': 'series.expand',
            'payload': {'workspace_id': preview_w['id'], 'series_ids': series_ids},
            'expected_revision': preview_w['revision']})
        assert response.status_code == 200, response.text
        return response.json()
    preview()
    snapshot_work.clear()
    checks.clear()
    work.clear()
    with monkeypatch.context() as snapshot_patch:
        snapshot_patch.setattr(snapshots, 'snapshot_inputs', loaded_inputs)
        snapshot_patch.setattr(snapshots, 'shift_records', serialized_shifts)
        previewed, preview_metrics = measure(h, 'preview', preview, monkeypatch, (planner_tools, 'preview'))
    observations['previewed'] = previewed
    observations['preview_checks'] = checks[:], work.copy(), snapshot_work.copy()
    preview_metrics['work'] = dict(work, validations=len(checks), **snapshot_work)
    observations['preview_series_snapshots'] = serialized[:]
    serialized.clear()
    before_add = h.detail(preview_w)['shifts']
    observations['before_add'] = before_add
    checks.clear()
    work.clear()
    snapshot_work.clear()
    with monkeypatch.context() as snapshot_patch:
        snapshot_patch.setattr(snapshots, 'snapshot_inputs', loaded_inputs)
        snapshot_patch.setattr(snapshots, 'shift_records', serialized_shifts)
        added, add_metrics = measure(h, 'add', lambda: post(h, 'series.expand',
            {'workspace_id': preview_w['id'], 'series_ids': series_ids}, revision=preview_w['revision'],
            expected_fingerprint=previewed['source_fingerprint']), monkeypatch, (planning, 'expand_series'))
    observations['add_series_snapshots'] = serialized[:]
    observations['added'] = added
    observations['added_count'] = len(h.detail(added['workspace'])['shifts'])
    observations['add_checks'] = checks[:], work.copy(), snapshot_work.copy()
    add_metrics['work'] = dict(work, validations=len(checks), **snapshot_work)
    post(h, 'workspace.undo', {'reason': 'Restore the scale draft before adding recurring sessions'},
         target=preview_w['id'], revision=added['workspace']['revision'])
    observations['after_undo'] = h.detail(preview_w)['shifts']
    metrics = dict(apply=apply_metrics, publish=publish_metrics, create=create_metrics, copy=copy_metrics,
                   preview=preview_metrics, add=add_metrics, first_page=boot_metrics, proposal=proposal_metrics)
    # CPU and structural budgets catch regressions without charging this test for other processes' scheduling delay.
    sessions, assignments = len(data['focus_ids']), len(additions)
    cpu_budgets = dict(apply=assignments / 768, publish=assignments / 768, create=sessions / 256,
                       copy=sessions / 256, preview=sessions / 256, add=sessions / 256, first_page=1, proposal=0.8)
    if h.app.state.engine.dialect.name == 'sqlite':
        # 59,860 batched facts cost 2.3-2.53 CPU seconds normally, 3.156 in the full affected run; 4 still rejects 2x.
        cpu_budgets['publish'] = assignments / 576
    wall_budgets = {name: budget * 4 for name, budget in cpu_budgets.items()}
    # Creation reached 3.44 CPU seconds with eight workers on two CPUs and unchanged SQL/session counts.
    # Exact expansion, materialisation and validation guards above reject repeated work; retain the 12s wall guard.
    cpu_budgets['create'] = sessions / 192
    return observations, metrics, cpu_budgets, wall_budgets


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def interactive_paths_measurement(request, scale_harness):
    with pytest.MonkeyPatch.context() as monkeypatch:
        return interactive_path_measurement(scale_harness(request.param), monkeypatch)


def test_department_interactive_paths(interactive_paths_measurement, request):
    observations, metrics, _, _ = interactive_paths_measurement
    assert observations['apply_checks'] == [768], 'Applying one batch checks the complete rota once'
    applied = observations['applied']
    assert applied['validation']['valid'] and sum(len(s['participants']) for s in applied['shifts']) == 2304
    assert observations['publish_checks'] == ([768], [59860]), 'Publication checks the rota and builds its ledger once'
    assert observations['publish_commands'] == 1, 'Publication executes one complete command, including its commit'
    assert observations['published']['publication_id']
    assert observations['created']['setup']['created'] == 768
    expected_work = ([768], Counter(expand=30, materialise_occurrence=768),
                     Counter(snapshot_inputs=1, external_sessions=768, external_assignments=2304))
    # Prepared writes independently reconstruct the candidate in each transaction;
    # only the pure validation is reused after the writer reacquires current inputs.
    prepared_work = (expected_work[0], *(Counter({key: value * 2 for key, value in part.items()})
                                       for part in expected_work[1:]))
    assert observations['create_checks'] == prepared_work
    copied = observations['copied']
    assert copied['setup']['created'] == 768 and copied['validation']['valid']
    assert observations['copy_checks'] == ([768], Counter())
    copied_shifts = observations['copied_shifts']
    assert sum(len(row['participants']) for row in copied_shifts) == 2304
    assert all(person['start'] == row['start'] and person['end'] == row['end']
               for row in copied_shifts for person in row['participants'])
    assert observations['previewed']['counts']['created_count'] == 768
    for key, repeats in (('create_series_snapshots', 2), ('preview_series_snapshots', 2), ('add_series_snapshots', 2)):
        assert Counter(observations[key]) == Counter({sid: repeats for sid in set(observations[key])})
        assert len(set(observations[key])) == 30, 'Serialize each template once per expansion, not per occurrence'
    assert observations['preview_checks'] == expected_work
    assert not observations['before_add'], 'Preview never saves sessions'
    assert observations['added']['created_count'] == observations['added_count'] == 768
    assert observations['add_checks'] == prepared_work
    assert observations['added']['validation']['error_count'] == observations['previewed']['validation']['error_count']
    assert observations['after_undo'] == observations['before_add'], 'One Undo restores the entire recurring-session batch'
    request.node.user_properties.append(('backend_scale', json.dumps(metrics)))
    sql_budgets = dict(apply=90, publish=160, create=115, copy=100, preview=110, add=125, first_page=180, proposal=20)
    for name, measured in metrics.items():
        assert sum(measured['sql'].values()) <= sql_budgets[name], (name, measured)


@pytest.mark.cpu_budget
def test_department_interactive_paths_time(interactive_paths_measurement):
    _, metrics, cpu_budgets, _ = interactive_paths_measurement
    # Apply: 1.05-1.33 CPU seconds native, 2.48-2.58 traced locally, 10.55 in CI; use the shared tracing allowance.
    # Apply's unchanged native/SQL ceilings and single whole-batch check above still reject duplicated work.
    for name, measured in metrics.items():
        assert measured['cpu_seconds'] < python_budget(cpu_budgets[name]), (name, measured)


@pytest.mark.wall_benchmark
def test_department_interactive_paths_wall_benchmark(interactive_paths_measurement):
    _, metrics, _, wall_budgets = interactive_paths_measurement
    for name, measured in metrics.items():
        assert measured['wall_seconds'] < python_budget(wall_budgets[name]), (name, measured)


@pytest.fixture(scope='module')
def scale_fairness_measurement(scale_harness):
    from packages.domain.fairness import ledger_facts
    _, data, _, _ = scale_department(scale_harness('h'))
    gc.collect()
    started = time.process_time()
    facts = ledger_facts(data)
    elapsed = time.process_time() - started
    return data, facts, elapsed


def test_scale_fairness_cpu(scale_fairness_measurement):
    _, facts, _ = scale_fairness_measurement
    # Includes all metric/clinic/location/type facts and available member-days, without database I/O.
    assert sum(row[4] for row in facts if row[2] == 'duties') == 2304


@pytest.mark.cpu_budget
def test_scale_fairness_time(scale_fairness_measurement):
    data, _, elapsed = scale_fairness_measurement
    assert elapsed < len(data['focus_ids']) / 768 * (3 if coverage.Coverage.current() else 1), elapsed



def test_revalidation_reuses_department_policy_context(h):
    import re
    from apps.api.services.planning import revalidate
    workspace = h.shift(h.workspace())['workspace']
    reads = []

    def department_read(_connection, _cursor, statement, _parameters, _context, _many):
        if re.search(r'FROM departments\s+WHERE departments\.id =', statement):
            reads.append(statement)

    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        event.listen(h.app.state.engine, 'before_cursor_execute', department_read)
        try:
            result = revalidate(db, row)
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', department_read)
    assert result['valid'] and result['score']['score'] == 100
    assert len(reads) == 1, 'Snapshot, swap scope and score reuse one department policy context'


@pytest.mark.parametrize('action,count', [
    ('unassign', 128), pytest.param('unassign', 2000, marks=pytest.mark.cpu_budget),
    ('remove', 128), pytest.param('remove', 2000, marks=pytest.mark.cpu_budget), ('lock', 500), ('copy', 100)])
def test_resolve_batch_sql_budget(h, monkeypatch, request, action, count):
    from apps.api.services import planning
    from apps.api.services.issues import issue_key
    from apps.api.snapshots import snapshot_hash
    from packages.domain.time import aware

    workspace, data, _, _ = scale_department(h)
    before = h.detail(workspace)['shifts']
    if action in {'unassign', 'remove'}:
        targets = sorted(((row, person) for row in before for person in row['participants']),
                         key=lambda pair: (pair[0]['start'], pair[0]['end'],
                                           data['people'][pair[1]['person_id']]['display_name']))[:count]
        with transaction(h.app.state.engine, write=True) as db:
            db.add_all(m.Availability(person_id=person['person_id'], department_id=h.seed['department_id'],
                start=aware(person['start']), end=aware(person['end']),
                kind='recorded_leave', private_reason='Synthetic batch absence',
                recorded_by=h.seed['identities']['coordinator'])
                for _, person in targets)
            db.get(m.Organisation, h.seed['organisation_id']).availability_revision += 1
        workspace = h.lifecycle(workspace, 'validate')['workspace']
        before = h.detail(workspace)['shifts']
        findings = sorted((f for f in workspace['validation']['findings'] if f['code'] == 'UNAVAILABLE'),
                          key=lambda f: (f['start'], f['end'], data['people'][f['person_id']]['display_name']))[:count]
        assert len(findings) == count
        selected = {f['shift_id'] for f in findings}
        kind, payload = 'issues.resolve', {'workspace_id': workspace['id'], 'action': action,
            'keys': [issue_key(f) for f in findings], 'reason': 'Resolve the selected synthetic absences'}
    else:
        selected = {row['id'] for row in before[:count]}
        kind = 'shift.copy' if action == 'copy' else 'shift.action'
        payload = {'workspace_id': workspace['id'], 'shift_ids': sorted(selected), 'reason': 'Review synthetic batch'}
        if action == 'copy':
            payload['offset_days'] = 1
        else:
            payload['action'] = 'lock'
    fixture = snapshot_hash(sorted((row['start'], row['end'],
        sorted(data['people'][p['person_id']]['display_name'] for p in row['participants'])) for row in before))
    calls, real = [], planning.evaluate

    def checked(snapshot, **kwargs):
        calls.append(len(snapshot['focus_ids']))
        return real(snapshot, **kwargs)

    monkeypatch.setattr(planning, 'evaluate', checked)
    with transaction(h.app.state.engine) as db:
        history = db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id']))
        audit = db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == kind))
    done, metrics = measure(h, f'{action}-{count}', lambda: post(h, kind, payload, revision=workspace['revision']))
    request.node.user_properties.append(('resolve_batch', json.dumps({
        'action': action, 'fixture': fixture, 'sessions': len(before), 'people': len(data['people']),
        'selected': len(selected), 'issues': count if action in {'unassign', 'remove'} else 0, 'metrics': metrics})))
    assert calls == [len(before) - len(selected) if action == 'remove' else len(before) + len(selected)
                     if action == 'copy' else len(before)], 'One complete check per batch'
    assert sum(metrics['sql'].values()) <= 150, metrics
    after = h.detail(done['workspace'])['shifts']
    if action == 'unassign':
        assert sum(len(row['participants']) for row in after) == 2304 - count
    elif action == 'remove':
        assert {row['id'] for row in after} == {row['id'] for row in before} - selected
        assert set(done['removed_shift_ids']) == selected
    elif action == 'lock':
        assert {row['id'] for row in after if row['locked']} == selected
    else:
        assert len(after) == len(before) + len(selected)
        assert sum(len(row['participants']) for row in after) == 2304 + 3 * len(selected)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id'])) == history + 1
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == kind)) == audit + 1
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the complete synthetic batch'},
                      workspace['id'], done['workspace']['revision'])
    assert h.detail(undone['workspace'])['shifts'] == before


def test_recurring_creation_reuses_immutable_snapshot_inputs(h, monkeypatch):
    from copy import deepcopy
    from apps.api import snapshots
    from packages.domain.score import schedule_score
    from packages.domain.validation import evaluate

    with transaction(h.app.state.engine, write=True) as db:
        series = m.RecurringSeries(department_id=h.seed['department_id'], name='Synthetic shared-input template',
            assignment_type_id=h.seed['type'], location_id=h.seed['locations'][0], starts_on=date(2026, 10, 4),
            definition=SeriesDefinition(anchor=date(2026, 10, 4), service_ids=[h.seed['services'][0]],
                days=[{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}],
                default_person_ids=[h.seed['people'][0]]).model_dump(mode='json'))
        db.add(series)
        db.flush()
        series_id = series.id
    calls, original = [], snapshots.snapshot_inputs

    def counted(*args):
        data = original(*args)
        calls.append((data, deepcopy(data)))
        return data

    monkeypatch.setattr(snapshots, 'snapshot_inputs', counted)
    # Harness.command also GETs the full check; measure only the recurring-creation request.
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={'kind': 'workspace.create', 'payload': {'name': 'Synthetic shared-input creation',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [series_id]}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['setup']['created'] == 1
    # Preparation and the authoritative writer are separate transactions; each acquires its own raw inputs once.
    assert len(calls) == 2
    assert calls[0][0] is not calls[1][0]
    assert all(data == frozen for data, frozen in calls), 'Template and final checking leave raw inputs unchanged'
    validation = h.detail(result['workspace'])['workspace']['validation']
    canonical = h.snapshot(result['workspace'])
    checked = evaluate(canonical)
    for key in ('valid', 'findings', 'goals', 'coverage'):
        assert validation[key] == checked[key]
    assert validation['score'] == schedule_score(canonical, checked)
