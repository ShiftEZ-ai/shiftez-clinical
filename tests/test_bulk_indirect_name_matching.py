"""Search and pasted names follow the same visible identity/person links as rows."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_bulk_linked_people import people_and_accounts, post
from .test_rule_catalogue_api import line, propose
from .test_staff_swaps import source_pair


@pytest.mark.parametrize("mode", ["search", "paste"])
def test_account_role_names_match_the_joined_person_and_all_of_their_sign_ins(h, mode):
    people, _, accounts = people_and_accounts(h)
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, people[0])
        person.display_name, person.staff_number, person.short_code = "Jos\u00e9 Alex", "HR-42", "JAX"
        db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Synthetic alias",
            source_key="alias:Dr Jos\u00e9", target_kind="people", target_id=person.id))
        expected = set(db.scalars(select(m.RoleBinding.id).where(m.RoleBinding.identity_id.in_(accounts[:2]))))
    tokens = ["Alex", "HR-42", "JAX"] if mode == "search" else ["JOSE ALEX", "hr-42", "jax", "DR JOSE"]
    for token in tokens:
        selection = {"collection": "roles", **({"where": {"q": token}} if mode == "search" else {"names": [token]})}
        selection["parent"] = {"collection": "identities", "selection": {"collection": "identities", "include_ids": accounts}}
        resolved = post(h, "resolve", selection)
        assert {row["id"] for row in resolved["sample"]} == expected
        if mode == "paste":
            assert resolved["matches"][0]["line"] == token and set(resolved["matches"][0]["ids"]) == expected
            assert not resolved["unmatched"] and not resolved["ambiguous"]
        pages = [post(h, "rows", {"selection": selection, "columns": ["identity_id", "person_id"],
            "sort": [{"field": "identity_id"}], "offset": offset, "limit": 1}) for offset in (0, 1, 0)]
        assert pages[0] == pages[2] and {pages[i]["rows"][0]["id"] for i in (0, 1)} == expected


@pytest.mark.parametrize("collection", ["roles", "identities"])
@pytest.mark.parametrize("mode", ["search", "paste"])
def test_account_role_names_support_unlinked_identity_names_without_cross_department_matches(h, mode, collection):
    account = identity(h, "unlinked-searchable", ["staff", "programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, account).display_name = "Unlinked Jos\u00e9"
        expected = set(db.scalars(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == account)))
        if collection == "identities":
            expected = {account}
        other = m.Department(organisation_id=h.seed["organisation_id"], name="Other matched department")
        db.add(other)
        db.flush()
        hidden = m.Identity(issuer="local-development", subject="outside-search",
            display_name="Unlinked Jos\u00e9" if collection == "roles" else "Outside account")
        db.add(hidden)
        db.flush()
        db.add(m.RoleBinding(identity_id=hidden.id, department_id=other.id, role="staff", starts_on=date(2020, 1, 1)))
    selection = {"collection": collection, **({"where": {"q": "Unlinked"}} if mode == "search" else {"names": ["UNLINKED JOSE"]})}
    result = post(h, "resolve", selection)
    assert {r["id"] for r in result["sample"]} == expected
    assert not result["unmatched"] and not result["ambiguous"]
    assert post(h, "rows", {"selection": selection})["count"] == len(expected)


@pytest.mark.parametrize("collection", ["inbox", "feedback"])
@pytest.mark.parametrize("mode", ["search", "paste"])
def test_indirect_person_name_matching_preserves_private_row_visibility(h, collection, mode):
    people, _, accounts = people_and_accounts(h)
    if collection == "inbox":
        with transaction(h.app.state.engine, write=True) as db:
            event = m.Outbox(department_id=h.seed["department_id"], event_key="synthetic-indirect-names", kind="synthetic", payload={})
            db.add(event)
            db.flush()
            records = [m.Notification(outbox_id=event.id, identity_id=account, title="Synthetic notice", message="Synthetic message")
                for account in (accounts[0], accounts[2])]
            db.add_all(records)
            db.flush()
            wanted, hidden = [row.id for row in records]
    else:
        wanted, hidden = [h.command(role, "feedback.create", {"kind": "problem", "route": "#home", "message": "Synthetic name matching"})["id"]
            for role in ("linked-alex-staff", "linked-blair-staff")]
    for token, expected in (("Synthetic Alex", {wanted}), ("Synthetic Blair", set())):
        selection = {"collection": collection, **({"where": {"q": token}} if mode == "search" else {"names": [token]})}
        resolved = post(h, "resolve", selection, "linked-alex-staff")
        assert {r["id"] for r in resolved["sample"]} == expected
        assert hidden not in {r["id"] for r in resolved["sample"]}
        if mode == "paste" and not expected:
            assert resolved["matches"] == [{"line": token, "ids": []}] and not resolved["ambiguous"]
        assert post(h, "rows", {"selection": selection}, "linked-alex-staff")["count"] == len(expected)


@pytest.mark.parametrize("collection", ["sessions", "recurring_series", "policy_lines", "issues"])
@pytest.mark.parametrize("mode", ["search", "paste"])
def test_multi_person_and_finding_name_matching_uses_the_projected_people(h, collection, mode):
    person = h.seed["people"][0]
    if collection in {"sessions", "issues"}:
        saved = h.shift(h.workspace(), people=[person])
        workspace = saved["workspace"]
        shift = h.detail(workspace)["shifts"][0]
        if collection == "sessions":
            record_id = shift["id"]
            filters = {"workspace_id": [workspace["id"]]}
        else:
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Workspace, workspace["id"]).validation = {"findings": [{"code": "SYNTHETIC_LINKED_PERSON",
                    "severity": "warning", "person_id": person, "message": "Synthetic issue", "shift_id": shift["id"]}]}
            visible = post(h, "resolve", {"collection": "issues"})["sample"]
            record_id = next(row["id"] for row in visible if row["workspace_id"] == workspace["id"] and row["code"] == "SYNTHETIC_LINKED_PERSON")
            filters = {"id": [record_id]}
    elif collection == "recurring_series":
        record_id = h.configure("recurring_series", {"name": "Synthetic indirect name series", "starts_on": "2026-10-01",
            "ends_on": "2027-01-01", "location_id": h.seed["locations"][0], "assignment_type_id": h.seed["type"],
            "definition": {"anchor": "2026-10-04", "days": [{"weekday": 6, "start_time": "08:00", "end_time": "12:00"}],
                "default_person_ids": [person], "service_ids": [h.seed["services"][0]]}})["record"]["id"]
        filters = {"id": [record_id]}
    else:
        authored = line(numbers={"max": {"value": 3}})
        authored["who"] = {"conditions": [{"field": "person_ids", "values": [person]}]}
        policy = propose(h, "duties_per_week", [authored])["record"]
        record_id = authored["id"]
        filters = {"policy_id": [policy["id"]], "id": [record_id]}
    selection = {"collection": collection, "where": {"filters": filters}}
    if mode == "search":
        selection["where"]["q"] = "Alex"
    else:
        selection["names"] = ["STAFF 01(test)"]
    resolved = post(h, "resolve", selection)
    assert {row["id"] for row in resolved["sample"]} == {record_id}
    assert not resolved["unmatched"] and not resolved["ambiguous"]
    if mode == "paste":
        assert resolved["matches"] == [{"line": "STAFF 01(test)", "ids": [record_id]}]
    assert post(h, "rows", {"selection": selection})["count"] == 1


@pytest.mark.parametrize("mode", ["search", "paste"])
def test_swap_search_and_paste_follow_both_people_and_keep_participant_scope(h, mode):
    publication, first, _, _ = source_pair(h)
    swap = h.command("staff", "swap.offer", {"publication_id": publication, "assignment_id": first,
        "recipient_person_id": h.seed["people"][1], "expires_at": (m.now() + timedelta(hours=1)).isoformat(),
        "reason": "Synthetic participant-name match"})["swap"]
    with transaction(h.app.state.engine) as db:
        names = [db.get(m.Person, person).display_name for person in h.seed["people"][:2]]
    for name in names:
        selection = {"collection": "swaps", "where": {"filters": {"id": [swap["id"]]}}}
        if mode == "search":
            selection["where"]["q"] = name
        else:
            selection["names"] = [name]
        resolved = post(h, "resolve", selection, "staff")
        assert [row["id"] for row in resolved["sample"]] == [swap["id"]]
        assert post(h, "rows", {"selection": selection}, "staff")["count"] == 1
        hidden = post(h, "resolve", selection, "staff_three")
        assert hidden["count"] == 0 and not hidden["sample"] and not hidden["ambiguous"]
        if mode == "paste":
            assert hidden["matches"] == [{"line": name, "ids": []}]
