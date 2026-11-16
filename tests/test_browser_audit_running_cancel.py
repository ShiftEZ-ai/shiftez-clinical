"""Real running worker stop-and-keep, isolated to this test's database."""
import json
import os
import threading
from pathlib import Path

import re
import pytest
from playwright.sync_api import expect

from apps.worker import main as worker
from packages.domain.cp_sat import generate
from .test_browser import auto_result, wait_run_created, browser, live_server, sign_in, open_rota, open_scheduling  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width', [1440, 390])
def test_stop_running_worker_keeps_preparation_after_terminal_and_reload(browser, cp_server, h, monkeypatch, tmp_path, width):
    from apps.api import models as m
    from apps.api.db import transaction
    from packages.domain import cp_sat
    workspace = h.shift(h.workspace('Running stop-and-keep audit'), people=[])['workspace']
    before = h.detail(workspace)
    context, page = sign_in(browser, cp_server, 'coordinator', viewport={'width': width, 'height': 900})
    checkpoint, release, stop_seen, finished = (threading.Event() for _ in range(4))
    thread = None
    outcomes, failures, native_results = [], [], []
    try:
        open_rota(page, workspace['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()
        wait_run_created(page)
        jobs = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
        assert len(jobs) == 1 and jobs[0]['status'] == 'queued'
        job = jobs[0]
        assert job['generation_engine'] == 'cp_sat'

        # Observe the real heartbeat reading the operator's persisted stop request.
        heartbeat_job = worker.heartbeat_job
        def observed_heartbeat(db, claim, progress):
            alive = heartbeat_job(db, claim, progress)
            if claim['id'] == job['id'] and (db.get(m.Job, claim['id']).search_control or {}).get('stop'):
                stop_seen.set()
            return alive
        monkeypatch.setattr(worker, 'heartbeat_job', observed_heartbeat)

        def gated_scheduler(snapshot, *, progress, cancelled, **options):
            assert snapshot['workspace']['id'] == workspace['id']
            def report(update):
                progress(update)
                if update['stage'] == 'preparing' and not checkpoint.is_set():
                    checkpoint.set()
                    assert release.wait(30), 'Browser did not release the owned preparation checkpoint'
            result = generate(snapshot, time_budget_seconds=5,
                              progress=report, cancelled=cancelled, **options)
            native_results.append(result['status'])
            return result
        monkeypatch.setattr(cp_sat, 'generate', gated_scheduler)

        def run():
            try:
                outcomes.append(worker.process_one(h.app.state.engine))
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=run, name='owned-stop-checkpoint', daemon=True)
        thread.start()
        assert checkpoint.wait(15), 'Real CP-SAT did not reach its preparation checkpoint'
        running = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert running['status'] == 'running' and running['started_at']
        assert running['revision'] > job['revision'] and not finished.is_set()

        panel.get_by_role('button', name='Stop and keep best', exact=True).click()
        confirmation = page.get_by_role('dialog', name='Stop and keep best', exact=True)
        confirmation.get_by_label('Reason', exact=True).fill('Keep the isolated running-worker preparation')
        with page.expect_response(lambda r: r.url.endswith('/api/commands')
                                  and (r.request.post_data_json or {}).get('kind') == 'job.stop_keep_best') as stopped:
            confirmation.get_by_role('button', name='Stop and keep best', exact=True).click()
        assert stopped.value.status == 200
        expect(confirmation).to_have_count(0)
        terminal = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert terminal['status'] == 'running' and terminal['search_control']['stop']
        assert not finished.is_set(), 'Stop must be confirmed while the worker is still running'
        assert stop_seen.wait(10), 'The running worker did not observe persisted operator stop'
        release.set()
        assert finished.wait(30), 'Stopped worker did not terminate after the checkpoint was released'
        thread.join()
        assert not failures and outcomes == [True]
        assert len(native_results) == 1
        assert native_results == ['needs_more_time']
        latest = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert latest['status'] == 'succeeded' and latest['result']['status'] == 'needs_more_time'
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job['id']).input_snapshot['fingerprint'] == latest['input_fingerprint']
        after = h.detail(workspace)
        assert after['workspace']['revision'] == before['workspace']['revision']
        assert after['workspace']['status'] == 'draft' and after['shifts'] == before['shifts']

        page.reload()
        open_rota(page, workspace['id'])
        open_scheduling(page)
        auto_result(panel)
        expect(panel.get_by_text('More search time needed', exact=True)).to_be_visible()
        expect(panel).to_contain_text('Saved inputs are kept.')
        expect(panel).to_contain_text('preparation stopped early runs again.')
        expect(panel.get_by_role('button', name='Continue for 5 min', exact=True)).to_be_visible()
        expect(panel.get_by_role('progressbar')).to_have_count(0)
        panel.get_by_role('button', name='Review latest result', exact=True).click()
        result = page.get_by_role('dialog', name='Auto-schedule results', exact=True)
        expect(result).to_contain_text('More search time needed')
        expect(result).to_contain_text('Saved inputs are kept.')
        expect(result).to_contain_text('preparation stopped early runs again.')
        expect(result).not_to_contain_text('read-only')
        expect(page.get_by_role('button', name=re.compile(r'^Apply (the change|all \d+ changes)$'))).to_have_count(0)
        assert h.detail(workspace)['shifts'] == before['shifts']
        artifact = Path(os.getenv('PW_COVERAGE_ARTIFACT_DIR', str(tmp_path))) / f'running-stop-keep-{width}'
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / 'receipt.json').write_text(json.dumps({'job_id': job['id'], 'queued_revision': job['revision'],
            'running_revision': running['revision'], 'worker_observed_stop': stop_seen.is_set(),
            'native_terminal_status': native_results[0], 'stored_terminal_status': latest['status'],
            'stored_result': latest['result'], 'unchanged_draft': True}, indent=2), encoding='utf8')
        page.screenshot(path=str(artifact / 'kept-after-reload.png'), full_page=True)
    finally:
        release.set()
        if thread:
            thread.join(timeout=20)
        context.close()
        assert thread is None or not thread.is_alive(), 'Owned worker must finish before disposable database teardown'
