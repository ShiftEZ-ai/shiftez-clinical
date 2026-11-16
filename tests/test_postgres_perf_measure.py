"""Static parser and redaction guards for the read-only PostgreSQL collector."""

import json

import pytest

from scripts.postgres_perf_measure import (
    ACKNOWLEDGEMENT,
    CollectionRefusal,
    _plan_fingerprint,
    load_fingerprints,
    validate_gate,
    validate_select,
)


def test_gate_accepts_dev_and_rejects_production_or_december():
    assert validate_gate("dev-nov", "LEAD-PERFORMANCE", ACKNOWLEDGEMENT) == ("dev-nov", "LEAD-PERFORMANCE")
    with pytest.raises(CollectionRefusal):
        validate_gate("dev-nov", "LEAD-PERFORMANCE", "")
    for target in ("production", "dev-december", "qa-2026-12"):
        with pytest.raises(CollectionRefusal):
            validate_gate(target, "LEAD-PERFORMANCE", ACKNOWLEDGEMENT)


def test_select_gate_rejects_writes_and_december_literals():
    with pytest.raises(CollectionRefusal):
        validate_select("UPDATE public.people SET name = 'x'")
    with pytest.raises(CollectionRefusal):
        validate_select("SELECT * FROM public.shifts WHERE starts_at >= '2026-12-01'")


def test_fingerprint_manifest_requires_approval_and_safe_select(tmp_path):
    path = tmp_path / "fingerprints.json"
    path.write_text(json.dumps({"fingerprints": [{"name": "calendar_feed", "approved": True,
                                                    "sql": "SELECT id FROM public.shifts LIMIT 10"}]}), encoding="utf-8")
    assert load_fingerprints(path)[0]["name"] == "calendar_feed"


def test_plan_fingerprint_drops_predicates_and_keeps_shape():
    result = _plan_fingerprint({"Plan": {"Node Type": "Index Scan", "Filter": "name = 'secret'",
                                          "Actual Rows": 2, "Plans": []}})
    assert result["nodes"] == [{"node_type": "Index Scan", "actual_rows": 2}]
    assert "secret" not in json.dumps(result)
