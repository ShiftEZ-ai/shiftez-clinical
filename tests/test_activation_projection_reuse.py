"""Activation preparation consumes current durable currencies before rebuilding rotas."""

import pytest

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.auth import authenticate
from apps.api.services import policies
from apps.api.services.read_projections import CANONICAL_AUDIENCE, input_stamp


def test_activation_read_reuses_current_feasibility_projection(h, monkeypatch):
    workspace = h.workspace("Activation projection")
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        row = db.get(m.Workspace, workspace["id"])
        db.add(m.WorkspaceReadProjection(
            workspace_id=row.id, audience=CANONICAL_AUDIENCE,
            kind="feasibility_fingerprint", stamp=input_stamp(db, row),
            value="stored-feasibility-token"))

    monkeypatch.setattr(snapshots, "domain_snapshot",
                        lambda *args, **kwargs: pytest.fail("current activation projection was rebuilt"))
    with transaction(engine) as db:
        db.info["_activation_read"] = True
        row = db.get(m.Workspace, workspace["id"])
        assert snapshots.feasibility_fingerprints(db, [row]) == {
            row.id: "stored-feasibility-token"}


def test_activation_impact_reuses_receipt_validation(h, monkeypatch):
    policy = h.policy({"kind": "restriction"})
    workspace = h.workspace("Receipt impact")
    engine = h.app.state.engine
    monkeypatch.setattr(policies, "domain_snapshot",
                        lambda *args, **kwargs: pytest.fail("receipt impact rebuilt a rota"))
    with transaction(engine) as db:
        actor = authenticate(db, h.client("coordinator").cookies["rota_session"])
        row = db.get(m.Workspace, workspace["id"])
        policy_row = db.get(m.Policy, policy["id"])
        db.info["_activation_receipts"] = {row.id: {
            "checks": {"proposed": {"validation": {
                "valid": False, "error_count": 1, "warning_count": 0,
                "evaluated_at": "2026-11-07T00:00:00+00:00",
                "fingerprint": "receipt-fingerprint", "engine_version": "test",
                "findings": [{"rule_id": policy_row.id, "code": "RESTRICTION",
                               "severity": "error"}]}}}}}
        impact = policies.policy_impact(db, actor, policy_row, details=False,
                                       workspaces=[row])
    assert impact[0]["findings"][0]["rule_id"] == policy["id"]
    assert impact[0]["evaluation"]["error_count"] == 1
