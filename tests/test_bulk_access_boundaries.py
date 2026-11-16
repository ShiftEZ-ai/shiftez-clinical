"""Bulk expansion must authorize intent before disclosing target values or revisions."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity


REASON = "Synthetic bulk access-boundary regression"


def preview(h, role, value):
    response = h.client(role).post("/api/commands/preview", json={"kind": "bulk.apply", "payload": value})
    assert response.status_code == 200, response.text
    return response.json()


def service_change(row, *, revision=None, changes=None):
    return {"item_kind": "catalogue.update", "common": {"catalogue": "services"}, "reason": REASON,
        "items": [{"target_id": row["id"], "expected_revision": row["revision"] if revision is None else revision,
                   "changes": changes or [{"field": "description", "value": "Synthetic changed description"}]}]}


def test_wrong_role_is_refused_before_catalogue_name_and_revision_are_disclosed(h):
    row = h.configure("services", {"name": "Synthetic hidden catalogue target", "starts_on": "2020-01-01"})["record"]
    value = service_change(row, revision=999)
    check = preview(h, "staff", value)
    issue = check["items"][0]["issues"][0]
    assert issue["code"] == "FORBIDDEN"
    assert row["name"] not in str(check) and "current_revision" not in str(check)
    assert not check["items"][0].get("record")


def test_noop_tag_action_still_checks_its_role_and_never_returns_the_target(h):
    row = h.configure("services", {"name": "Synthetic untagged hidden service", "starts_on": "2020-01-01"})["record"]
    value = service_change(row, changes=[{"action": "tag", "how": "set", "value": []}])
    check = preview(h, "staff", value)
    assert check["items"][0]["issues"][0]["code"] == "FORBIDDEN"
    assert not check["items"][0].get("record") and row["name"] not in str(check)
    result = h.command("staff", "bulk.apply", value, status=409)
    assert result["error"]["code"] == "FORBIDDEN"


def test_unprivileged_preview_fingerprint_does_not_encode_hidden_target_revisions(h):
    row = h.configure("services", {"name": "Synthetic revision-private service", "starts_on": "2020-01-01"})["record"]
    value = service_change(row, revision=999)
    before = preview(h, "staff", value)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Service, row["id"]).revision += 1
    after = preview(h, "staff", value)
    assert before["source_fingerprint"] == after["source_fingerprint"]


def test_identity_scope_is_checked_before_its_revision_or_fingerprint_is_disclosed(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic other department")
        db.add(department)
        db.flush()
        department_id = department.id
    target = identity(h, "other_department_identity", ["staff"], department=department_id)
    value = {"item_kind": "identity.disable", "reason": REASON,
             "items": [{"target_id": target, "expected_revision": 999}]}
    check = preview(h, "admin", value)
    assert check["items"][0]["issues"][0]["code"] == "OPERATOR_REQUIRED"
    assert "current_revision" not in str(check) and "Fictional other_department_identity" not in str(check)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, target).revision += 1
    again = preview(h, "admin", value)
    assert check["source_fingerprint"] == again["source_fingerprint"]
    h.command("admin", "identity.disable", {"reason": REASON}, target, 999, status=403)


def test_action_only_credentialing_does_not_require_the_unused_people_edit_role(h):
    person = h.get("admin", "/api/catalogues")["people"][0]
    service = h.configure("services", {"name": "Synthetic authorised child action", "starts_on": "2020-01-01"})["record"]
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "people"}, "reason": REASON,
        "items": [{"target_id": person["id"], "expected_revision": person["revision"], "changes": [
            {"action": "authorise", "value": {"service_id": service["id"], "starts_on": "2026-10-01"}}]}]}
    check = preview(h, "credentialer", value)
    assert check["counts"]["ready"] == 1, check
    result = h.command("credentialer", "bulk.apply", value)
    assert result["count"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Eligibility.id).where(m.Eligibility.person_id == person["id"],
            m.Eligibility.service_id == service["id"]))
