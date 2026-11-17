"""Coverage needs current evaluation and visible duty IDs, without a full issued-rota projection."""
from types import SimpleNamespace

import pytest

from apps.api import main as api, models as m
from apps.api.db import transaction
from packages.domain.validation import evaluate
from .test_performance_budget import python_budget
from .test_schedule_window import published_rota


def test_publication_coverage_preserves_operator_and_staff_rows_without_copying_the_snapshot(h, monkeypatch):
    publication = published_rota(h)
    visible = {row["id"] for row in h.get("staff", f"/api/publications/{publication}")["snapshot"]["shifts"]}
    copies = []
    deepcopy = api.copy.deepcopy
    def counted(value):
        copies.append(value)
        return deepcopy(value)
    monkeypatch.setattr(api, "copy", SimpleNamespace(deepcopy=counted))
    path = f"/api/publications/{publication}/coverage"
    operator = h.get("coordinator", path)["evaluation"]
    staff = h.get("staff", path)["evaluation"]
    assert operator["coverage"] and staff["coverage"] == [row for row in operator["coverage"] if row["shift_id"] in visible]
    assert staff["findings"] == [] and staff["error_count"] == staff["warning_count"] == 0
    # This work-count ceiling does not depend on CI speed or coverage instrumentation.
    assert len(copies) == 0


def test_publication_coverage_accepts_slim_audit_snapshot_and_enforces_staff_duty_scope(h):
    workspace = h.workspace("Slim audit publication")
    validation = evaluate(h.snapshot(workspace))
    with transaction(h.app.state.engine, write=True) as db:
        w = db.get(m.Workspace, workspace["id"])
        w.status = "published"
        # Same snapshot shape as test_home.big_department, used by the frontend performance audit.
        publication = m.Publication(workspace_id=w.id, department_id=w.department_id, sequence=1,
            starts_on=w.starts_on, ends_on=w.ends_on, current=True, published_by=w.created_by,
            fingerprint="0" * 64, validation=validation,
            snapshot={"timezone": "Asia/Riyadh", "shifts": [], "services": {}, "locations": {}})
        db.add(publication)
        db.flush()
        path = f"/api/publications/{publication.id}/coverage"
    assert h.client("coordinator").get(path).status_code == 200
    refused = h.client("staff").get(path)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.postgres
def test_coverage_projection_reuses_across_apps_and_invalidates_inputs(pg, monkeypatch):
    """Separate API instances share one current projection; staff filtering never changes the stored value."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from tests.test_backend_scale import measure, scale_department
    from tests.test_staff_swaps import ordered_clock
    from packages.domain.validation import check_contracts
    from packages.domain.time import aware

    with ordered_clock(monkeypatch):
        workspace, data, _, _ = scale_department(pg)
        with transaction(pg.app.state.engine, write=True) as db:
            stored = db.get(m.Workspace, workspace['id'])
            stored.status = 'published'
            publication = m.Publication(workspace_id=stored.id, department_id=stored.department_id, sequence=1,
                starts_on=stored.starts_on, ends_on=stored.ends_on, current=True, published_by=stored.created_by,
                snapshot=data, validation={'contracts': check_contracts()}, fingerprint=data['fingerprint'])
            db.add(publication)
            db.flush()
            path = f'/api/publications/{publication.id}/coverage'
        calls, original = [], api.evaluate
        def counted(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(api, 'evaluate', counted)
        pg.client('coordinator')
        initial, cold = measure(pg, 'coverage-cold', lambda: pg.get('coordinator', path))
        other_app = api.create_app(pg.settings)
        try:
            with TestClient(other_app, base_url=pg.settings.public_origin) as client:
                client.cookies.update(pg.client('coordinator').cookies)
                response, warm = measure(SimpleNamespace(app=other_app), 'coverage-other-app', lambda: client.get(path))
                assert response.status_code == 200, response.text
                assert response.json()['evaluation'] == initial['evaluation'] and len(calls) == 1
                assert warm['sql'].get('SELECT', 0) <= 12, warm
                with transaction(pg.app.state.engine) as db:
                    assert db.scalar(select(m.WorkspaceReadProjection.kind).where(
                        m.WorkspaceReadProjection.workspace_id == workspace['id'],
                        m.WorkspaceReadProjection.kind == 'publication_coverage'))
                staff = pg.get('staff', path)['evaluation']
                own = {s['id'] for s in data['shifts'] if any(p['person_id'] == pg.seed['people'][0] for p in s['participants'])}
                assert {r['shift_id'] for r in staff['coverage']} == own and staff['findings'] == []
                before = len(calls)
                assert pg.get('coordinator', path)['evaluation'] == initial['evaluation']
                assert len(calls) == before
                shift = next(s for s in data['shifts'] if any(p['person_id'] == pg.seed['people'][0] for p in s['participants']))
                with transaction(pg.app.state.engine, write=True) as db:
                    db.add(m.Availability(department_id=pg.seed['department_id'], person_id=pg.seed['people'][0],
                        start=aware(shift['start']), end=aware(shift['end']), kind='recorded_leave',
                        recorded_by=pg.seed['identities']['coordinator'], private_reason='Synthetic private absence'))
                    db.get(m.Organisation, pg.seed['organisation_id']).availability_revision += 1
                fresh = client.get(path)
                assert fresh.status_code == 200 and len(calls) == before + 1
                assert fresh.json()['evaluation'] != initial['evaluation']
                assert any(f['code'] == 'UNAVAILABLE' for f in fresh.json()['evaluation']['findings'])
        finally:
            other_app.state.engine.dispose()
    return locals()


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_coverage_projection_cpu_budget(pg, monkeypatch):
    state = test_coverage_projection_reuses_across_apps_and_invalidates_inputs(pg, monkeypatch)
    assert state['warm']['cpu_seconds'] < python_budget(1), state['warm']


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_coverage_projection_wall_budget(pg, monkeypatch):
    state = test_coverage_projection_reuses_across_apps_and_invalidates_inputs(pg, monkeypatch)
    assert state['warm']['wall_seconds'] < python_budget(3), state['warm']
