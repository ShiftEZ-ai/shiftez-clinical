"""Request-scoped draft baseline verification reuse."""
from datetime import date
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from apps.api import models as m, snapshots, workspace_baselines
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.settings import Settings
from tests.conftest import TEST_PASSWORD
from tests.test_rule_catalogue_api import approve, line, propose


def _drafts(h, count):
    return [h.shift(h.workspace(f"Baseline memo draft {n}"))["workspace"] for n in range(count)]


def _assignment_type_change(h, colour):
    row = next(item for item in h.get("admin", "/api/catalogues")["assignment_types"]
               if item["id"] == h.seed["type"])
    value = {key: row[key] for key in ("name", "description", "starts_on", "definition") if key in row}
    value["definition"] = {**value["definition"], "colour": colour}
    return {
        "kind": "catalogue.update", "target_id": row["id"], "expected_revision": row["revision"],
        "payload": {"catalogue": "assignment_types", "value": value, "reason": "Baseline memo proof"},
    }


def _profile_listener(engine):
    statements = []

    def listener(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "from workspace_baselines" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    return statements, listener


def _uncached_baseline(db, workspace):
    from apps.api.workspace_baselines import latest, verify
    baseline = latest(db, workspace.id)
    return baseline, verify(baseline) if baseline is not None else None


def _mutate_snapshot(snapshot):
    policy = next(policy for policy in snapshot["policies"] if policy.get("definition"))
    policy["definition"]["rule"]["memo_probe"] = True
    from apps.api.workspace_baselines import LIVE_VALIDITY_SOURCES
    for key in LIVE_VALIDITY_SOURCES:
        rows = snapshot.get(key) or {}
        if rows:
            next(iter(rows.values()))["name"] = "memo probe"
            return
    pytest.fail("fixture has no baseline validity rows")


def _track_baseline_memo(monkeypatch):
    original = snapshots._verified_baseline
    sizes, hashes = [], []

    def tracked(db, workspace):
        result = original(db, workspace)
        rows = {row.id: row for row in db.info.get("_baseline_rows", {}).values() if row is not None}
        memo = db.info.get("_verified_baselines", {})
        sizes.append(len(memo))
        for (baseline_id, payload_hash), payload in memo.items():
            row = rows[baseline_id]
            assert snapshots.snapshot_hash(payload) == payload_hash == row.payload_hash
            hashes.append(True)
        return result

    monkeypatch.setattr(snapshots, "_verified_baseline", tracked)
    return sizes, hashes


def test_verified_baseline_cache_setting_is_env_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("ROTA_VERIFIED_BASELINE_CACHE_ENTRIES", "4")
    assert Settings.from_env().verified_baseline_cache_entries == 4
    with pytest.raises(RuntimeError, match="ROTA_VERIFIED_BASELINE_CACHE_ENTRIES"):
        replace(Settings(environment="test"), verified_baseline_cache_entries=0).check()


def test_app_configured_baseline_cache_size_caps_memo(h, monkeypatch):
    _drafts(h, 8)
    settings = replace(h.settings, verified_baseline_cache_entries=2)
    app = create_app(settings)
    client = TestClient(app, base_url=settings.public_origin)
    try:
        client.headers["origin"] = settings.public_origin
        login = client.post("/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
        assert login.status_code == 200, login.text
        client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
        sizes, hashes = _track_baseline_memo(monkeypatch)
        response = client.post("/api/catalogue/preview", json=_assignment_type_change(h, "violet"))
        assert response.status_code == 200, response.text
        assert sizes and max(sizes) <= 2 and sizes[-1] <= 2 and hashes
    finally:
        client.close()
        app.state.engine.dispose()


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize("count", [2, 8])
def test_catalogue_preview_verifies_each_draft_baseline_once(h, monkeypatch, count):
    _drafts(h, count)
    h.client("admin")
    monkeypatch.setenv("ROTA_VERIFIED_BASELINE_CACHE_ENTRIES", "4")
    sizes, hashes = _track_baseline_memo(monkeypatch)
    calls = []
    original = workspace_baselines.verify

    def verified(row):
        calls.append(row.id)
        return original(row)

    monkeypatch.setattr(workspace_baselines, "verify", verified)
    statements, listener = _profile_listener(h.app.state.engine)
    try:
        response = h.client("admin").post("/api/catalogue/preview", json=_assignment_type_change(h, "violet"))
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", listener)
    assert response.status_code == 200, response.text
    assert len(calls) == count and len(set(calls)) == count
    assert len(statements) == count
    assert sizes and max(sizes) <= 4 and sizes[-1] <= 4
    assert hashes


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize("count", [2, 8])
def test_rule_review_verifies_each_draft_baseline_once(h, monkeypatch, count):
    _drafts(h, count)
    current = approve(h, propose(h, "duties_per_week", [line(numbers={"max": {"value": 4}})])['record'])
    pending = propose(h, "duties_per_week", [line(numbers={"max": {"value": 3}})],
                      policy_id=current["id"], revision=current["revision"])["record"]
    h.client("approver")
    monkeypatch.setenv("ROTA_VERIFIED_BASELINE_CACHE_ENTRIES", "4")
    sizes, hashes = _track_baseline_memo(monkeypatch)
    calls = []
    original = workspace_baselines.verify

    def verified(row):
        calls.append(row.id)
        return original(row)

    monkeypatch.setattr(workspace_baselines, "verify", verified)
    statements, listener = _profile_listener(h.app.state.engine)
    try:
        response = h.client("approver").get(f"/api/policies/{pending['id']}/review")
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", listener)
    assert response.status_code == 200, response.text
    assert calls == []
    assert len(statements) == 1
    assert "canonical_payload" not in statements[0].lower()
    assert sizes == []
    assert hashes == []


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_snapshot_and_fingerprint_equal_with_and_without_baseline_memo(h, monkeypatch):
    workspace = h.shift(h.workspace("Baseline memo equality"))["workspace"]
    original = snapshots._verified_baseline

    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        monkeypatch.setattr(snapshots, "_verified_baseline", _uncached_baseline)
        expected = snapshots.domain_snapshot(db, row)
        expected_fingerprint = snapshots.fingerprint(db, row)
        monkeypatch.setattr(snapshots, "_verified_baseline", original)
        actual = snapshots.domain_snapshot(db, row)
        assert actual == expected
        assert snapshots.fingerprint(db, row) == expected_fingerprint


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_nested_snapshot_mutations_do_not_leak_between_memoized_calls(h, monkeypatch):
    workspace = h.shift(h.workspace("Baseline memo nested isolation"))["workspace"]
    original = snapshots._verified_baseline
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        first = snapshots.domain_snapshot(db, row)
        expected = snapshots.domain_snapshot(db, row)
        _mutate_snapshot(first)
        second = snapshots.domain_snapshot(db, row)
        monkeypatch.setattr(snapshots, "_verified_baseline", _uncached_baseline)
        uncached = snapshots.domain_snapshot(db, row)
        monkeypatch.setattr(snapshots, "_verified_baseline", original)
    assert second == expected == uncached


def test_catalogue_preview_snapshots_match_unmemoized_run_after_nested_mutation(h, monkeypatch):
    _drafts(h, 2)
    from apps.api.services import catalogue_preview
    captures = []
    original_evaluate = catalogue_preview.evaluate_preview

    def capture(prepared):
        captures.append(prepared)
        return original_evaluate(prepared)

    monkeypatch.setattr(catalogue_preview, "evaluate_preview", capture)
    client = h.client("admin")
    body = _assignment_type_change(h, "violet")
    assert client.post("/api/catalogue/preview", json=body).status_code == 200
    memo_clean = captures[-1]
    assert client.post("/api/catalogue/preview", json=body).status_code == 200
    memo_again = captures[-1]
    for workspace in memo_clean["workspaces"]:
        _mutate_snapshot(workspace["before"])
        _mutate_snapshot(workspace["after"])
    monkeypatch.setattr(snapshots, "_verified_baseline", _uncached_baseline)
    assert client.post("/api/catalogue/preview", json=body).status_code == 200
    uncached = captures[-1]
    assert [(row["before"], row["after"], row["saved"]) for row in memo_again["workspaces"]] == [
        (row["before"], row["after"], row["saved"]) for row in uncached["workspaces"]]


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_capture_invalidates_same_session_baseline_row_cache(h, monkeypatch):
    with transaction(h.app.state.engine, write=True) as db:
        actor_id = h.seed["identities"]["coordinator"]
        row = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed["department_id"],
                          created_by=actor_id, last_edited_by=actor_id, name="Baseline memo recapture",
                          starts_on=date(2026, 10, 3), ends_on=date(2026, 10, 10), status="draft",
                          revision=1, content_revision=1)
        db.add(row)
        db.flush()
        snapshots.domain_snapshot(db, row)
        new = workspace_baselines.capture(db, row, actor_id, workspace_revision=row.revision,
                                          reason="Capture same-revision baseline for memo proof")
        second = snapshots.domain_snapshot(db, row)
        monkeypatch.setattr(snapshots, "_verified_baseline", _uncached_baseline)
        uncached = snapshots.domain_snapshot(db, row)
    assert second["baseline"]["id"] == new.id
    assert second == uncached


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_baseline_verification_does_not_cross_request_sessions(h, monkeypatch):
    workspace = h.shift(h.workspace("Baseline memo session boundary"))["workspace"]
    calls = []
    original = workspace_baselines.verify
    monkeypatch.setattr(workspace_baselines, "verify", lambda row: (calls.append(row.id), original(row))[1])
    for _ in range(2):
        with transaction(h.app.state.engine) as db:
            snapshots.domain_snapshot(db, db.get(m.Workspace, workspace["id"]))
    assert len(calls) == 2
