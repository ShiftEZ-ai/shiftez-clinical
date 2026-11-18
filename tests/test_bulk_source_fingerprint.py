"""Authorized bulk work retains its dependency guards, including creation without a target."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import preview
from .test_bulk_source_authorization import REASON, source_harness


@pytest.mark.parametrize("counter", ["input_revision", "availability_revision"])
@pytest.mark.parametrize("mode", ["source", "create", "mixed"])
def test_authorized_bulk_work_keeps_dependency_fingerprint_and_apply_guard(source_harness, counter, mode):
    h = source_harness
    payload = {"person_id": h.seed["people"][0], "first_day": "2026-11-10", "last_day": "2026-11-10",
               "kind": "prefer_off", "points": 1}
    change = {"item_kind": "wish.create", "reason": REASON, "items": [{"payload": payload}]}
    if mode != "create":
        own = h.command("staff", "wish.create", payload)["wish"]
        change.update(item_kind="wish.withdraw", items=[{
            "target_id": own["id"], "expected_revision": own["revision"]}])
    if mode == "mixed":
        hidden = h.command("coordinator", "wish.create", {**payload, "person_id": h.seed["people"][1]})["wish"]
        change["items"].append({"target_id": hidden["id"], "expected_revision": hidden["revision"]})
    before = preview(h, "staff", change)
    assert before["counts"]["ready"] == 1, before
    assert before["counts"]["problem"] == (1 if mode == "mixed" else 0), before
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed["organisation_id"])
        setattr(org, counter, getattr(org, counter) + 1)
    after = preview(h, "staff", change)
    assert after["counts"] == before["counts"]
    assert after["source_fingerprint"] != before["source_fingerprint"]
    refused = h.command("staff", "bulk.apply", change, expected_fingerprint=before["source_fingerprint"], status=409)
    assert refused["error"]["code"] == "STALE_PREVIEW"
