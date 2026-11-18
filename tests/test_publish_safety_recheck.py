"""Publication always rechecks current inputs before trusting review evidence."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from packages.domain import validation


def _approved(h, mutation):
    workspace = h.workspace("Publish currency matrix")
    policy = None
    if mutation in {"rule", "adjustment", "exception"}:
        policy = h.policy({"kind": "max_duties", "maximum": 99, "window": "week"},
                          severity="advisory", exception=mutation == "exception")
    workspace = h.shift(workspace)["workspace"]
    workspace = h.lifecycle(workspace, "submit")["workspace"]
    workspace = h.lifecycle(workspace, "approve", "approver")["workspace"]
    return workspace, policy


@pytest.mark.parametrize("mutation", ["assignment", "shift", "rule", "exception", "adjustment", "validator", "leave"])
def test_publish_rechecks_every_approved_input_change_before_review_gate(h, monkeypatch, mutation):
    from apps.api.snapshots import fingerprint

    workspace, policy = _approved(h, mutation)
    with transaction(h.app.state.engine) as db:
        approved_fingerprint = fingerprint(db, db.get(m.Workspace, workspace["id"]))
    if mutation == "validator":
        monkeypatch.setattr(validation, "ENGINE_VERSION", validation.ENGINE_VERSION + "-next")
    elif mutation == "leave":
        h.command("coordinator", "availability.record", {
            "person_id": h.seed["people"][0], "start": "2026-10-04T08:00:00+03:00",
            "end": "2026-10-04T12:00:00+03:00", "kind": "actual_absence",
            "private_reason": "Synthetic leave currency change"})
    elif mutation == "exception":
        created = h.command("coordinator", "exception.create", {
            "policy_id": policy["id"], "person_id": h.seed["people"][0],
            "starts_on": "2026-10-04", "ends_on": "2026-10-11",
            "reason": "Synthetic exception currency change"})["record"]
        h.command("approver", "exception.approve", {"reason": "Approve synthetic exception"},
                  target=created["id"], revision=created["revision"])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Workspace, workspace["id"])
            if mutation == "assignment":
                assignment = db.scalar(select(m.Assignment).where(m.Assignment.shift_id.in_(
                    select(m.Shift.id).where(m.Shift.workspace_id == row.id))).limit(1))
                assignment.person_id = h.seed["people"][1]
            elif mutation == "shift":
                shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == row.id).limit(1))
                shift.attributes = {**(shift.attributes or {}), "currency_test": True}
            elif mutation == "rule":
                db.get(m.Policy, policy["id"]).revision += 1
            else:  # adjustment
                row.rule_adjustments = [{"id": m.uid(), "policy_id": policy["id"],
                                         "policy_revision": policy["revision"], "mode": "off", "amount": None,
                                         "starts_on": str(row.starts_on), "ends_on": str(row.ends_on),
                                         "reason": "Synthetic adjustment change", "by": row.last_edited_by,
                                         "at": m.now().isoformat()}]
    with transaction(h.app.state.engine) as db:
        assert fingerprint(db, db.get(m.Workspace, workspace["id"])) != approved_fingerprint

    calls = []
    original = planning.evaluate

    def counted(data, **kwargs):
        calls.append(True)
        result = original(data, **kwargs)
        shift = next(item for item in data["shifts"] if item["id"] in data["focus_ids"])
        finding = {"code": "SYNTHETIC_MANDATORY", "severity": "error", "message": "Synthetic mandatory finding",
                   "shift_id": shift["id"], "person_id": (shift["participants"][0]["person_id"]
                                                               if shift["participants"] else None),
                   "rule_id": None, "start": shift["start"], "end": shift["end"]}
        return {**result, "valid": False, "error_count": result["error_count"] + 1,
                "findings": [*result["findings"], finding]}

    monkeypatch.setattr(planning, "evaluate", counted)
    # A fresh mandatory finding takes precedence over stale review evidence.
    refused = h.lifecycle(workspace, "publish", status=422)
    assert refused["error"]["code"] == "VALIDATION_FAILED"
    assert refused["error"]["details"]["stale_approval"]["code"] == "STALE_APPROVAL"
    assert calls == [True]
    assert not h.get("coordinator", "/api/publications")["publications"]


def test_publish_does_not_reuse_evidence_without_manifest_currency(h, monkeypatch):
    workspace = h.shift(h.workspace("Missing currency evidence"))["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace["id"])
        row.validation = {key: value for key, value in row.validation.items() if key != "input_manifest"}
    calls = []
    original = planning.evaluate
    monkeypatch.setattr(planning, "evaluate", lambda data, **kwargs: (calls.append(True) or original(data, **kwargs)))
    # The lifecycle path uses the same reuse guard while submitting the draft.
    submitted = h.lifecycle(workspace, "submit")
    assert submitted["workspace"]["status"] == "submitted"
    assert calls == [True]
