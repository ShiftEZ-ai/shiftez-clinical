"""Record generation queue and worker timings on the existing synthetic scale fixture."""
import cProfile
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.api import main as api, models as m
from apps.api.db import make_engine, transaction
from apps.api.settings import Settings
from apps.worker import main as worker
from packages.domain import cp_sat
from tests.conftest import disposable_postgres_url, postgres_harness, postgres_template
from tests.test_planning_groups_drafts import group
from tests.test_scheduler_performance import department_preparation_163_people


def queue(harness, workspace, role='coordinator', **options):
    profile = cProfile.Profile()
    execute = api.execute
    phases = {}

    class WriterTiming(logging.Handler):
        def emit(self, entry):
            if getattr(entry, 'command_kind', None) != 'job.create':
                return
            if entry.msg == 'write_lock_acquired':
                phases['locked'] = entry.created
            if entry.msg == 'write_phase_completed' and entry.phase == 'commit':
                phases['committed'] = entry.created

    def profiled_execute(*args, **kwargs):
        return profile.runcall(execute, *args, **kwargs)

    logger = logging.getLogger('rota.db')
    previous_level = logger.level
    handler = WriterTiming()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    api.execute = profiled_execute
    started = time.perf_counter()
    try:
        job = harness.command(role, 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation', **options},
                              revision=workspace['revision'])['job']
        wall_seconds = time.perf_counter() - started
    finally:
        api.execute = execute
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return job, {'wall_seconds': wall_seconds, 'writer_seconds': phases['committed'] - phases['locked']}


def profile_worker(job_id):
    settings = Settings(environment='test', generation_engine='cp_sat', database_url=os.environ['TEST_POSTGRES_URL'])
    engine = make_engine(settings.database_url, settings=settings)
    prepare = cp_sat._prepare
    model_seconds = 0.0

    def timed_prepare(*args, **kwargs):
        nonlocal model_seconds
        started = time.perf_counter()
        try:
            return prepare(*args, **kwargs)
        finally:
            model_seconds += time.perf_counter() - started

    cp_sat._prepare = timed_prepare
    try:
        started = time.perf_counter()
        assert cProfile.Profile().runcall(worker.process_one, engine, settings=settings, solver_workers=1)
        wall_seconds = time.perf_counter() - started
        with transaction(engine) as db:
            job = db.get(m.Job, job_id)
            assert job.status == 'succeeded', job.error_code
            return {'wall_seconds': wall_seconds, 'model_preparation_seconds': model_seconds,
                    'status': job.result.get('status'), 'solver_status': job.result.get('solver_status'),
                    'phase_timings': job.result.get('phase_timings')}
    finally:
        cp_sat._prepare = prepare
        engine.dispose()


def measure(harness):
    data = department_preparation_163_people.__wrapped__(lambda _: harness)
    for client in harness.clients.values():
        client.close()
    harness.clients.clear()
    harness.settings = replace(harness.settings, generation_engine='cp_sat')
    harness.app = api.create_app(harness.settings, engine=harness.app.state.engine)
    harness.client('coordinator')
    harness.client('admin')
    workspace = harness.detail(data['workspace']['id'])['workspace']
    population = {'conditions': {'conditions': [{'field': 'person_ids', 'values': harness.seed['people']}]}}
    timings = {}
    for name, options in (('unscoped', {}), ('population', {'generation_scope': population})):
        job, timings[name] = queue(harness, workspace, **options)
        harness.command('coordinator', 'job.cancel', {'reason': 'Profile next request'}, job['id'], job['revision'])

    planning_group = group(harness, people=list(data['people']))
    workspace = harness.detail(workspace['id'])['workspace']
    if workspace['purpose'] != 'integrated':
        workspace = harness.command('admin', 'workspace.designate', {'reason': 'Use the synthetic group rota'},
                                    workspace['id'], workspace['revision'])['workspace']
    job, timings['group'] = queue(harness, workspace, planning_group_version_id=planning_group['id'], lookahead='none')
    harness.command('coordinator', 'job.cancel', {'reason': 'Profile next request'}, job['id'], job['revision'])
    job, timings['worker_request'] = queue(harness, workspace, role='admin', generation_options={'search_minutes': 1})

    env = {**os.environ, 'TEST_POSTGRES_URL': harness.settings.database_url}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'worker', job['id']],
                           check=True, env=env, capture_output=True, text=True)
    return {'people': len(data['people']), 'sessions': len(data['shifts']), 'rule_versions': len(data['policies']),
            'queue': timings, 'worker': json.loads(child.stdout)}


def main():
    if sys.argv[1] == 'worker':
        print(json.dumps(profile_worker(sys.argv[2])))
        return
    url = disposable_postgres_url()
    with postgres_template(url) as template, closing(postgres_harness(url, template)) as fixture:
        report = measure(next(fixture))
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
