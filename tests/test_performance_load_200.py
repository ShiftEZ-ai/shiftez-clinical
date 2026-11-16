from __future__ import annotations

import pytest

from scripts.performance_load_200 import SafetyRefusal, percentile, validate_plan


def _plan(date: str = "2027-10-15") -> dict:
    return {
        "qa_data_id": "qa-2027-october-synthetic",
        "qa_date": date,
        "telemetry": {"path": "/api/operations"},
        "reads": [{"name": "home", "method": "GET", "path": "/api/home"}],
        "writes": [],
    }


def test_load_200_plan_refuses_december_and_reports_percentiles() -> None:
    with pytest.raises(SafetyRefusal):
        validate_plan(_plan("2027-12-01"))
    december_id = _plan()
    december_id["qa_data_id"] = "qa-2027-december-synthetic"
    with pytest.raises(SafetyRefusal):
        validate_plan(december_id)
    assert percentile([100.0, 200.0, 300.0, 400.0], 0.50) == 250.0
    assert percentile([], 0.99) is None
