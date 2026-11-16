"""Reproducible PostgreSQL command timeout calibration on the existing department fixture."""
from datetime import date
from importlib.metadata import version
import json
import logging
from logging.handlers import BufferingHandler
import platform
import time

import pytest
import sqlalchemy
from sqlalchemy import event, insert, text

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import fingerprint
from .test_backend_scale import measure, post, scale_department
from .test_rule_map import rule
from .test_rule_templates import propose
from .test_staff_import import csv_of, row
from .test_performance_budget import python_budget


def command_write_measurement(h, sample):
    observations = {}
    workspace, data, additions, _ = scale_department(h, assigned=False)
    people = list(data['people'])
    # Keep current person-scoped rules and the retired versions returned by real catalogue reads.
    policies = [rule(data, f'Command scale rule {n}', {'kind': 'max_hours', 'hours': 168},
                     {'person_ids': [people[n % len(people)]]}) for n in range(997)]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Policy), [{**{k: v for k, v in policy.items() if k in m.Policy.__table__.columns},
            'starts_on': date(2020, 1, 1), 'created_at': m.now(),
            'status': 'active' if n < 163 else 'retired', 'active': n < 163,
            'approved_by': h.seed['identities']['approver'], 'owner': 'Synthetic scale owner',
            'source': 'Synthetic scale fixture', 'created_by': h.seed['identities']['coordinator']}
            for n, policy in enumerate(policies)])
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        server = db.scalar(text('SHOW server_version'))
    data = h.snapshot(workspace)
    observations['shape'] = len(data['people']), len(data['focus_ids'])
    with transaction(h.app.state.engine, write=True) as db:
        saved = db.get(m.Workspace, workspace['id'])
        job = m.Job(id=m.uid(), department_id=saved.department_id, workspace_id=saved.id,
            kind='generation', status='succeeded', requested_by=h.seed['identities']['coordinator'],
            input_fingerprint=fingerprint(db, saved), input_snapshot=data,
            result={'status': 'candidate_feasible', 'additions': additions, 'removals': []})
        db.add(job)
        job_id = job.id
    for role in ('coordinator', 'approver', 'admin'):
        h.client(role)

    metrics = {}

    def timed(name, operation):
        statement_times = []

        def before(connection, cursor, statement, parameters, context, many):
            if connection.info.get('rota_write'):
                context.scale_started = time.perf_counter()

        def after(connection, cursor, statement, parameters, context, many):
            if hasattr(context, 'scale_started'):
                statement_times.append(time.perf_counter() - context.scale_started)

        engine = h.app.state.engine
        event.listen(engine, 'before_cursor_execute', before)
        event.listen(engine, 'after_cursor_execute', after)
        logger, captured = logging.getLogger('rota.db'), BufferingHandler(10000)
        previous_level = logger.level
        logger.addHandler(captured)
        logger.setLevel(logging.INFO)
        try:
            result, measured = measure(h, name, operation)
        finally:
            logger.removeHandler(captured)
            logger.setLevel(previous_level)
            event.remove(engine, 'before_cursor_execute', before)
            event.remove(engine, 'after_cursor_execute', after)
        completed = [record.duration_ms for record in captured.buffer if record.msg == 'write_completed']
        observations.setdefault('write_counts', {})[name] = len(completed)
        measured.update(write_seconds=completed[0] / 1000,
                        slowest_statement_seconds=round(max(statement_times), 6))
        metrics[name] = measured
        return result

    applied = timed('generation.apply', lambda: post(h, 'generation.apply',
        {'workspace_id': workspace['id'], 'job_id': job_id, 'reason': 'Synthetic scale application'},
        revision=workspace['revision']))
    observations['applied'] = applied
    workspace = applied['workspace']

    proposal = propose(h, {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01',
        'metric': 'session_units', 'comparison': 'maximum', 'amount': 28},
        severity='advisory', listed_only=True,
        per_person=[{'person_id': pid, 'amount': 28} for pid in people])
    activated = timed('policy.activate', lambda: h.command('approver', 'policy.activate',
        {'reason': 'Independent synthetic scale approval'}, proposal['id'], proposal['revision']))
    observations['activated'] = activated
    observations['approver'] = h.seed['identities']['approver']

    workspace = h.lifecycle(workspace, 'submit')['workspace']
    workspace = h.lifecycle(workspace, 'approve', 'approver')['workspace']
    published = timed('workspace.publish', lambda: post(h, 'workspace.publish',
        {'reason': 'Synthetic scale publication'}, workspace['id'], workspace['revision']))
    observations['published'] = published

    copied = timed('workspace.copy-next-year', lambda: post(h, 'workspace.create',
        {'name': 'Synthetic next-year copy', 'starts_on': '2027-10-04', 'ends_on': '2027-11-01',
         'source_workspace_id': workspace['id'], 'source_revision': published['workspace']['revision'],
         'copy_people': True}))
    observations['copied'] = copied
    observations['copied_assignments'] = sum(len(s['participants']) for s in h.detail(copied['workspace'])['shifts'])

    records = h.get('coordinator', '/api/catalogues')['people']
    imported = timed('staff_import.preview', lambda: h.command('coordinator', 'staff_import.preview',
        {'source_name': 'Synthetic department catalogue', 'csv_text': csv_of(*[
            row(display_name=person['display_name'], title='Synthetic department import') for person in records])}))
    observations['import_preview'] = imported
    imported = timed('staff_import.apply', lambda: h.command('coordinator', 'staff_import.apply',
        {'reason': 'Synthetic catalogue import'}, imported['batch']['id'], imported['batch']['revision'],
        expected_fingerprint=imported['source_fingerprint']))
    observations['imported'] = imported

    records = h.get('coordinator', '/api/catalogues')['people']
    updated = timed('catalogue.bulk_update', lambda: h.command('coordinator', 'catalogue.bulk_update',
        {'catalogue': 'people', 'items': [{'id': person['id'], 'revision': person['revision']} for person in records],
         'add_tags': ['Synthetic scale batch'], 'reason': 'Synthetic department catalogue update'}))
    observations['updated'] = updated
    evidence = {'sample': sample + 1, 'platform': platform.platform(), 'python': platform.python_version(),
                'sqlalchemy': sqlalchemy.__version__, 'psycopg': version('psycopg'),
                'postgresql': server, 'people': 163, 'sessions': 768, 'assignments': 2304,
                'policies_before_activation': 1000, 'active_before_activation': 166,
                'statement_timeout_ms': h.settings.command_statement_timeout_ms,
                'write_budget_ms': h.settings.command_write_budget_ms,
                'metrics': metrics}
    print('command_write_scale', json.dumps(evidence, sort_keys=True))
    return observations, evidence


@pytest.fixture(scope='module', params=[pytest.param(n, marks=pytest.mark.postgres) for n in range(3)])
def command_write_scale_measurement(request, scale_harness):
    return command_write_measurement(scale_harness('pg'), request.param)


def test_department_command_write_headroom(command_write_scale_measurement, request):
    observations, evidence = command_write_scale_measurement
    assert observations['shape'] == (163, 768)
    assert all(count == 1 for count in observations['write_counts'].values()), 'Measure one successful serialized command, including commit'
    applied = observations['applied']
    assert applied['validation']['valid'] and sum(len(s['participants']) for s in applied['shifts']) == 2304
    activated = observations['activated']
    assert activated['record']['status'] == 'active' and len(activated['record']['definition']['per_person']) == 163
    assert activated['record']['approved_by'] == observations['approver']
    assert observations['published']['publication_id']
    activation, publication = (evidence['metrics'][kind] for kind in ('policy.activate', 'workspace.publish'))
    assert publication['sql']['SELECT'] <= 125, publication
    copied = observations['copied']
    assert copied['setup']['created'] == 768 and copied['validation']['valid']
    assert observations['copied_assignments'] == 2304
    imported = observations['import_preview']
    assert imported['summary']['rows'] == 163 and imported['summary']['error_rows'] == 0
    assert observations['imported']['applied']['updated_people'] == 163
    updated = observations['updated']
    assert len(updated['records']) == 163 and all('Synthetic scale batch' in person['tags'] for person in updated['records'])
    request.node.user_properties.append(('command_write_scale', json.dumps(evidence, sort_keys=True)))
    return locals()


@pytest.mark.wall_benchmark
def test_department_command_write_headroom_wall_budget(command_write_scale_measurement, request):
    state = test_department_command_write_headroom(command_write_scale_measurement, request)
    activation, publication = (state['evidence']['metrics'][kind] for kind in ('policy.activate', 'workspace.publish'))
    assert activation['wall_seconds'] < python_budget(10), activation
    assert publication['wall_seconds'] < python_budget(10), publication


@pytest.mark.cpu_budget
def test_department_command_write_headroom_cpu_budget(command_write_scale_measurement, request):
    state = test_department_command_write_headroom(command_write_scale_measurement, request)
    publication = state['evidence']['metrics']['workspace.publish']
    assert publication['cpu_seconds'] < python_budget(4.5), publication


@pytest.mark.wall_benchmark
def test_department_command_write_headroom_time(command_write_scale_measurement):
    _, evidence = command_write_scale_measurement
    for measured in evidence['metrics'].values():
        assert measured['write_seconds'] < evidence['write_budget_ms'] / 1000, measured
        assert measured['slowest_statement_seconds'] < evidence['statement_timeout_ms'] / 1000, measured
