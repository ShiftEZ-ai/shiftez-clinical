"""Bounded Home and rota-list reads at the department's representative scale."""
import time
from datetime import date, timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.quotas import METRIC_VERSION
from .clock_support import freeze_clock
from .test_home import big_department


@pytest.mark.postgres
def test_home_and_workspace_reads_stay_bounded_at_five_year_scale(pg, monkeypatch):
    """150+ people and retained rotas must not make boot reads grow with history."""
    big_department(pg)
    coordinator = pg.seed["identities"]["coordinator"]
    department = pg.seed["department_id"]
    frozen_now = freeze_clock(monkeypatch)
    rows = []
    stored_check = {"fingerprint": "0" * 64, "valid": True, "error_count": 0,
                    "warning_count": 0, "findings": [], "coverage": [],
                    "contracts": {"block_quota": METRIC_VERSION, "programmes": "programmes-v1"}}
    for index in range(180):
        starts = date(2024, 1, 1) + timedelta(days=index * 14)
        rows.append(m.Workspace(department_id=department, name=f"Scale rota {index}", starts_on=starts,
                                ends_on=starts + timedelta(days=7), status="draft", created_by=coordinator,
                                last_edited_by=coordinator, validation=stored_check,
                                archived_at=(frozen_now if index % 11 == 0 else None)))
    with transaction(pg.app.state.engine, write=True) as db:
        db.add_all(rows)
        db.flush()
        db.add_all(m.Publication(department_id=department, workspace_id=row.id, sequence=100 + index,
                                 starts_on=row.starts_on, ends_on=row.ends_on, current=True, published_by=coordinator,
                                 snapshot={}, validation={'contracts': {'block_quota': METRIC_VERSION,
                                                                        'programmes': 'programmes-v1'}}, fingerprint="0" * 64)
                   for index, row in enumerate(rows[1:41]))

    list_times, home_times = [], []
    for _ in range(3):
        began = time.perf_counter()
        page = pg.get("coordinator", "/api/workspaces?limit=50&offset=0")
        list_times.append(time.perf_counter() - began)
        began = time.perf_counter()
        home = pg.get("coordinator", "/api/home")
        home_times.append(time.perf_counter() - began)
        assert page["total"] >= 170 and page["has_more"] and len(page["workspaces"]) == 50
        assert home["rotas"] >= 170

    # Generous measured budgets: these are API wall times, not solver budgets.
    print(f"read scale seconds: workspaces={list_times} home={home_times}")
    assert page["workspaces"][0]["starts_on"] <= "2026-10-03" <= page["workspaces"][0]["ends_on"]
    return locals()


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_home_and_workspace_reads_wall_budget(pg, monkeypatch):
    state = test_home_and_workspace_reads_stay_bounded_at_five_year_scale(pg, monkeypatch)
    assert max(state['list_times']) < 2.0, state['list_times']
    assert max(state['home_times']) < 2.0, state['home_times']
