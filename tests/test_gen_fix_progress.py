"""A heartbeat reports elapsed time even when CP-SAT has no new incumbent."""
import threading
import pytest
from sqlalchemy import event
from apps.worker import main as worker
from .test_worker_write_timeouts import generation_job


def test_elapsed_progress_keeps_stage_and_does_not_compound():
    update = {'stage': 'improving', 'elapsed_seconds': 80, 'search_started_seconds': 75,
              'improvements_found': 4, 'events': [{'seq': 4}]}
    assert worker.elapsed_progress(update, 20) == {**update, 'elapsed_seconds': 100}
    assert worker.elapsed_progress(update, 30)['elapsed_seconds'] == 110
    assert update['elapsed_seconds'] == 80


@pytest.mark.postgres
def test_worker_heartbeat_and_status_use_latest_stage_with_advancing_clock(pg, monkeypatch):
    jid = generation_job(pg)
    observed = threading.Event()
    status_seen = {}
    heartbeat = worker.heartbeat_job
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)
    def record(db, claim, progress):
        alive = heartbeat(db, claim, progress)
        if progress.get('stage') == 'improving' and progress['elapsed_seconds'] > 80:
            event.listen(db, 'after_commit', lambda session: observed.set(), once=True)
        return alive
    monkeypatch.setattr(worker, 'heartbeat_job', record)
    def solve(snapshot, *, progress, cancelled):
        progress({'stage': 'improving', 'elapsed_seconds': 80, 'search_started_seconds': 75,
                  'time_limit_seconds': 120})
        assert observed.wait(5)
        status = pg.get('coordinator', f'/api/jobs/{jid}/status?events=true')['job']
        status_seen['job'] = status
        assert status['progress']['stage'] == 'improving'
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}
    assert worker.process_one(pg.app.state.engine, scheduler=solve)
    assert observed.is_set()
    assert pg.get('coordinator', f'/api/jobs/{jid}/status')['job']['status'] == 'succeeded'
    return locals()


@pytest.mark.wall_benchmark
@pytest.mark.postgres
def test_worker_heartbeat_reports_advancing_elapsed_time(pg, monkeypatch):
    state = test_worker_heartbeat_and_status_use_latest_stage_with_advancing_clock(pg, monkeypatch)
    assert state['status_seen']['job']['progress']['elapsed_seconds'] > 80
