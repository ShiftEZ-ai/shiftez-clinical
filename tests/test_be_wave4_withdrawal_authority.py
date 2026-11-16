"""Withdrawal and receipt authority follows current request ownership after identity unlinking."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.governance_support import identity


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def authority_store(request):
    return request.getfixturevalue(request.param)

def request(h, *, creator="coordinator"):
    return h.command(creator, "request.create", {"person_id": h.seed["people"][0], "kind": "meeting",
        "start": "2026-10-04T09:00:00+03:00", "end": "2026-10-04T10:00:00+03:00",
        "private_reason": "Synthetic request ownership explanation"})["record"]


def operation(h, row, kind, *, bulk=False, key=None, role="subject", status=200):
    payload = {"reason": "Synthetic withdrawal authority test"}
    if kind == "request.decide":
        payload["decision"] = "withdraw"
    args = ("bulk.apply", {"items": [{"item_kind": kind, "target_id": row["id"],
        "expected_revision": row["revision"], "payload": payload}], "reason": payload["reason"]}) if bulk else (
        kind, payload, row["id"], row["revision"])
    return h.command(role, *args, key=key, status=status)


def record(result, bulk):
    return result["items"][0]["results"][0]["record"] if bulk else result["record"]


def unlink(h, *, role="subject", person=None):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"][role]).person_id = person


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("relinked", [False, True])
def test_unlinked_subject_cannot_read_replay_or_undo_withdrawal(authority_store, bulk, relinked):
    h = authority_store
    identity(h, "subject", ["staff"], person=h.seed["people"][0])
    row, key = request(h), str(uuid.uuid4())
    result = operation(h, row, "request.decide", bulk=bulk, key=key)
    withdrawn = record(result, bulk)
    assert h.get("subject", "/api/commands/" + key)["result"] == result
    unlink(h, person=h.seed["people"][1] if relinked else None)
    h.get("subject", "/api/commands/" + key, status=403)
    operation(h, row, "request.decide", bulk=bulk, key=key, status=403)
    for undo_bulk in (False, True):
        operation(h, withdrawn, "request.undo", bulk=undo_bulk, status=403)
    with transaction(h.app.state.engine) as db:
        saved = db.get(m.RequestRecord, row["id"])
        assert saved.status == "withdrawn" and saved.revision == withdrawn["revision"]
        assert saved.details["withdrawn_by"] == h.seed["identities"]["subject"]


@pytest.mark.parametrize("bulk", [False, True])
def test_unlinked_subject_cannot_recover_existing_undo_receipt(authority_store, bulk):
    h = authority_store
    identity(h, "subject", ["staff"], person=h.seed["people"][0])
    row, withdrawal_key, undo_key = request(h), str(uuid.uuid4()), str(uuid.uuid4())
    withdrawal = operation(h, row, "request.decide", bulk=bulk, key=withdrawal_key)
    withdrawn = record(withdrawal, bulk)
    result = operation(h, withdrawn, "request.undo", bulk=bulk, key=undo_key)
    assert h.get("subject", "/api/commands/" + undo_key)["result"] == result
    unlink(h)
    for key in (withdrawal_key, undo_key):
        h.get("subject", "/api/commands/" + key, status=403)
    operation(h, row, "request.decide", bulk=bulk, key=withdrawal_key, status=403)
    operation(h, withdrawn, "request.undo", bulk=bulk, key=undo_key, status=403)


@pytest.mark.parametrize("bulk", [False, True])
def test_current_decision_authority_survives_unlink_but_not_role_revocation(authority_store, bulk):
    h = authority_store
    subject = identity(h, "subject", ["staff", "approver"], person=h.seed["people"][0])
    row, withdrawal_key, undo_key = request(h), str(uuid.uuid4()), str(uuid.uuid4())
    withdrawal = operation(h, row, "request.decide", bulk=bulk, key=withdrawal_key)
    withdrawn = record(withdrawal, bulk)
    unlink(h)
    assert h.get("subject", "/api/commands/" + withdrawal_key)["result"] == withdrawal
    assert operation(h, row, "request.decide", bulk=bulk, key=withdrawal_key) == withdrawal
    result = operation(h, withdrawn, "request.undo", bulk=bulk, key=undo_key)
    assert record(result, bulk)["status"] == "submitted"
    assert h.get("subject", "/api/commands/" + undo_key)["result"] == result
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == subject,
                                             m.RoleBinding.role == "approver")).revoked = True
    for key in (withdrawal_key, undo_key):
        h.get("subject", "/api/commands/" + key, status=403)
    operation(h, row, "request.decide", bulk=bulk, key=withdrawal_key, status=403)
    operation(h, withdrawn, "request.undo", bulk=bulk, key=undo_key, status=403)


@pytest.mark.parametrize("bulk", [False, True])
def test_current_submitter_ownership_keeps_withdrawal_undo_after_unlink(authority_store, bulk):
    h = authority_store
    identity(h, "subject", ["staff"], person=h.seed["people"][0])
    row, withdrawal_key, undo_key = request(h, creator="subject"), str(uuid.uuid4()), str(uuid.uuid4())
    withdrawn = record(operation(h, row, "request.decide", bulk=bulk, key=withdrawal_key), bulk)
    unlink(h)
    assert h.get("subject", "/api/commands/" + withdrawal_key)["status"] == "committed"
    result = operation(h, withdrawn, "request.undo", bulk=bulk, key=undo_key)
    assert record(result, bulk)["status"] == "submitted"
    assert operation(h, withdrawn, "request.undo", bulk=bulk, key=undo_key) == result
    assert h.get("subject", "/api/commands/" + undo_key)["result"] == result


def test_current_decider_and_same_subject_cannot_undo_another_identity_withdrawal(authority_store):
    h = authority_store
    identity(h, "subject", ["staff"], person=h.seed["people"][0])
    identity(h, "other_decider", ["staff", "approver"], person=h.seed["people"][0])
    withdrawn = operation(h, request(h), "request.decide")["record"]
    operation(h, withdrawn, "request.undo", role="other_decider", status=403)


def test_receipt_exemption_reads_current_request_subject(authority_store):
    h = authority_store
    identity(h, "subject", ["staff"], person=h.seed["people"][0])
    row, key = request(h), str(uuid.uuid4())
    operation(h, row, "request.decide", key=key)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RequestRecord, row["id"]).person_id = h.seed["people"][1]
    h.get("subject", "/api/commands/" + key, status=403)
    operation(h, row, "request.decide", key=key, status=403)
