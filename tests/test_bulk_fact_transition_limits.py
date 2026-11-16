"""Replacement transitions count and revision-bind both approved fact records."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_bulk_fact_replacement_undo import chain, state
from .test_bulk_nested_limits import limits
from .test_bulk_reverse_transitions import FACT_FAMILIES, fact_payload


@pytest.mark.parametrize("family", FACT_FAMILIES)
def test_fact_replacement_activation_undo_and_redo_count_both_records(h, family):
    original = h.command("coordinator", family + ".propose", fact_payload(h, family))["fact"]
    original = h.command("approver", family + ".activate", {"reason": REASON},
                         original["id"], original["revision"])["fact"]
    payload = {key: deepcopy(value) for key, value in original["data"].items()
               if key in s.SchedulingFactInput.model_fields}
    payload.update(kind=original["kind"], person_id=original["person_id"], on=original["on"],
                   replaces_id=original["id"], expected_revision=original["revision"], reason=REASON)
    replacement = h.command("coordinator", family + ".propose", payload)["fact"]
    body = {"items": [{"item_kind": family + ".activate", "target_id": replacement["id"],
                      "expected_revision": replacement["revision"]}], "reason": REASON}
    for _ in range(3):
        before = state(h)
        limits(h, 1)
        checked = preview(h, "approver", body)
        assert checked["counts"]["problem"] == 1
        assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
        refused = h.command("approver", "bulk.apply", body, status=422)
        assert refused["error"]["code"] == "BULK_ITEM_LIMIT"
        assert state(h) == before
        limits(h, 2)
        assert preview(h, "approver", body)["counts"]["problem"] == 0
        applied = h.command("approver", "bulk.apply", body)
        body = applied["undo"]["payload"]


def test_bulk_fact_chain_inverse_binds_predecessor_revision_after_an_earlier_inverse(h):
    oldest, middle, newest = chain(h, 3)
    with transaction(h.app.state.engine) as db:
        middle = record(db.get(m.SchedulingFact, middle["id"]))
    body = {"items": [{"item_kind": "person.workload.undo", "target_id": row["id"],
                       "expected_revision": row["revision"]} for row in (newest, middle)], "reason": REASON}
    applied = h.command("approver", "bulk.apply", body)
    assert state(h)[oldest["id"]][0] == "active"
    for _ in range(3):
        checked = preview(h, "approver", applied["undo"]["payload"])
        assert checked["counts"]["problem"] == 0, checked
        applied = h.command("approver", **applied["undo"])
    assert {key: status for key, (status, _) in state(h).items()} == {
        oldest["id"]: "retired", middle["id"]: "retired", newest["id"]: "active"}
