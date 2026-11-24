"""Staff account onboarding through the authenticated API and audited invite command."""
import json
import logging
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_accounts import PASSWORD, anonymous, users_file, with_users_file


PREVIEW = "/api/account-onboarding/preview"
APPLY = "/api/account-onboarding/apply"


def people(h, count):
    """Give each synthetic person an effective membership in the active department."""
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        category_id = db.scalar(select(m.Category.id).where(m.Category.department_id == department.id).limit(1))
        result = []
        for index in range(count):
            label = f"Staff {index + 1}"
            person = m.Person(organisation_id=department.organisation_id, display_name=label)
            db.add(person)
            db.flush()
            db.add(m.Membership(department_id=department.id, category_id=category_id, person_id=person.id,
                                starts_on=date(2020, 1, 1)))
            result.append({"person_id": person.id, "username": f"staff{index + 1}", "email": f"staff{index + 1}@example.test"})
        return result


def post(h, route, value, role="admin", status=200):
    response = h.client(role).post(route, json=value)
    assert response.status_code == status, response.json().get("error", {}).get("code")
    return response


def payload(rows):
    return {"rows": rows, "reason": "Invite selected department staff"}


def reviewed(h, value):
    review = post(h, PREVIEW, value).json()
    return {**value, "expected_fingerprint": review["expected_fingerprint"]}, review


def test_batch_preview_invite_resume_and_reissue(h, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    h = with_users_file(h, tmp_path)
    rows = people(h, 2)
    first = rows[0]["person_id"]
    value, preview = reviewed(h, payload(rows))
    assert preview["counts"]["ready"] == 2 and preview["people"][first] == "Staff 1"
    assert users_file(h) == {}
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.AccountLink))) == []

    response = post(h, APPLY, value)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"] == 'attachment; filename="staff-invitations.json"'
    result = response.json()
    assert result["counts"]["created"] == 2
    links = {item["person_id"]: item for item in result["invitations"]}
    assert links[first]["person_label"] == "Staff 1"
    identity = result["ledger"][first]["identity_id"]
    token = links[first]["one_time_link"].rsplit("/", 1)[1]

    replay = post(h, APPLY, value).json()
    assert replay["counts"]["already_linked"] == 2 and replay["invitations"] == []
    assert replay["ledger"][first]["account_id"] == result["ledger"][first]["account_id"]

    replacement = h.command("admin", "account.resend", {"reason": "Replace an undelivered invitation"}, identity)
    replacement_token = replacement["one_time_link"].rsplit("/", 1)[1]
    public = anonymous(h)
    assert public.get(f"/api/account-links/{token}").status_code == 404
    assert public.post(f"/api/account-links/{replacement_token}", json={"password": PASSWORD}).status_code == 200
    assert post(h, APPLY, value).json()["counts"]["already_linked"] == 2
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(("account.invite", "account.resend")))))
        receipts = list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind.in_(
            ("account.invite", "account.resend")))))
        assert len(events) == len(receipts) == 3
        assert {event.target_id for event in events if event.action == "account.invite"} == {
            row["identity_id"] for row in result["ledger"].values()}
        for row in rows:
            entry = result["ledger"][row["person_id"]]
            assert db.get(m.Account, entry["account_id"]).identity_id == entry["identity_id"]
            assert list(db.scalars(select(m.RoleBinding.role).where(m.RoleBinding.identity_id == entry["identity_id"]))) == ["staff"]
        retained = json.dumps([(event.reason, event.detail) for event in events] + [receipt.result for receipt in receipts])
    messages = caplog.text + str([record.__dict__ for record in caplog.records])
    tokens = (token, replacement_token, links[rows[1]["person_id"]]["one_time_link"].rsplit("/", 1)[1])
    assert all(secret not in retained and secret not in messages for secret in tokens)


def test_batch_duplicate_conflict_and_preview_binding(h, tmp_path):
    h = with_users_file(h, tmp_path)
    rows = people(h, 2)
    first, second = (row["person_id"] for row in rows)
    dup, preview = reviewed(h, payload([rows[0], {**rows[1], "email": rows[0]["email"].upper()}]))
    assert {item["code"] for item in preview["ledger"].values()} == {"DUPLICATE_SELECTION"}
    assert post(h, APPLY, dup).json()["counts"]["conflict"] == 2

    dup, preview = reviewed(h, payload([rows[0], {**rows[0], "username": "alternate"}]))
    assert preview["ledger"][first]["code"] == "DUPLICATE_SELECTION"
    assert post(h, APPLY, dup).json()["invitations"] == []

    value, _ = reviewed(h, payload(rows))
    post(h, APPLY, {**value, "rows": [rows[0]]}, status=409)
    h.command("admin", "account.invite", {"username": rows[1]["username"], "email": rows[1]["email"],
                                         "display_name": "Staff 2", "reason": "Existing sign-in"})
    result = post(h, APPLY, value, status=409).json()["error"]
    assert result["code"] == "STALE_PREVIEW"
    assert result["details"]["ledger"] == {
        first: {"status": "ready"}, second: {"status": "conflict", "code": "SIGN_IN_TAKEN"}}
    assert set(users_file(h)) == {rows[1]["username"]}


def test_batch_refuses_other_department_and_non_admin(h, tmp_path):
    h = with_users_file(h, tmp_path)
    same, other = people(h, 2)
    with transaction(h.app.state.engine, write=True) as db:
        current = db.get(m.Department, h.seed["department_id"])
        another = m.Department(organisation_id=current.organisation_id, name="Other department")
        db.add(another)
        db.flush()
        db.scalar(select(m.Membership).where(m.Membership.person_id == other["person_id"])).department_id = another.id
    value = payload([same, other])
    for route, body in ((PREVIEW, value), (APPLY, {**value, "expected_fingerprint": "0" * 64})):
        post(h, route, body, role="coordinator", status=403)
        assert anonymous(h).post(route, json=body).status_code == 401
    applied, preview = reviewed(h, value)
    assert other["person_id"] not in preview["people"]
    assert preview["ledger"][other["person_id"]]["code"] == "PERSON_NOT_IN_DEPARTMENT"
    assert post(h, APPLY, applied).json()["invitations"] == []
    assert users_file(h) == {}


@pytest.mark.postgres
def test_batch_recovers_between_file_write_and_command_commit(pg, tmp_path, monkeypatch):
    from apps.api.identity_store import AutheliaUsersFile, IdentityStoreError

    h = with_users_file(pg, tmp_path)
    rows = people(h, 3)
    value, _ = reviewed(h, payload(rows))
    create = AutheliaUsersFile.create

    def interrupt(self, username, display_name, email, **options):
        create(self, username, display_name, email, **options)
        if username == rows[1]["username"]:
            raise IdentityStoreError("Interrupted after writing the sign-in placeholder")

    with monkeypatch.context() as local:
        local.setattr(AutheliaUsersFile, "create", interrupt)
        partial = post(h, APPLY, value).json()
    assert partial["counts"] == {"created": 1, "already_linked": 0, "ready": 1, "conflict": 0, "stopped": 1}
    assert len(partial["invitations"]) == 1
    assert set(users_file(h)) == {rows[0]["username"], rows[1]["username"]}
    with transaction(h.app.state.engine) as db:
        assert [account.username for account in db.scalars(select(m.Account))] == [rows[0]["username"]]
        assert len(list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind == "account.invite")))) == 1

    # The uncommitted placeholder cannot be claimed through the regular invite path.
    h.command("admin", "account.invite", {**rows[1], "display_name": "Staff 2", "reason": "Different command"}, status=503)
    resumed = post(h, APPLY, value).json()
    assert resumed["counts"]["created"] == 2 and resumed["counts"]["already_linked"] == 1
    assert {item["person_id"] for item in resumed["invitations"]} == {row["person_id"] for row in rows[1:]}
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Account)))) == 3
        assert len(list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind == "account.invite")))) == 3
