"""Bulk composition exercises the public command boundary and original handlers."""
import uuid
import pytest
from sqlalchemy import func, select
from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_single_commands import relationship


REASON = "Department bulk change test"


def preview(h, role, payload):
    response = h.client(role).post("/api/commands/preview", json={"kind": "bulk.apply", "payload": payload})
    assert response.status_code == 200, response.text
    return response.json()


def people_change(h, **extra):
    catalogues = h.get("coordinator", "/api/catalogues")
    people = catalogues["people"][:2]
    return {"item_kind": "catalogue.update", "common": {"catalogue": "people"},
            "items": [{"target_id": p["id"], "expected_revision": p["revision"],
                       "changes": [{"field": "title", "value": "Bulk title"},
                                   {"field": "directory_section", "value": "Bulk section"}]} for p in people],
            "reason": REASON, **extra}


def test_bulk_preview_rollback_apply_idempotency_and_undo(h):
    payload = people_change(h)
    before = h.get("coordinator", "/api/catalogues")["people"]
    check = preview(h, "coordinator", payload)
    assert check["counts"]["ready"] == 2
    assert {row["result"] for row in check["items"]} <= {"ready", "unchanged", "problem", "waiting"}
    assert all(p["after"]["title"] == "Bulk title" for p in check["items"])
    assert h.get("coordinator", "/api/catalogues")["people"] == before
    key = str(uuid.uuid4())
    reply = h.command("coordinator", "bulk.apply", payload, key=key)
    assert reply["count"] == 2
    assert reply["counts"]["changed"] == 2 and "fixed" not in reply["counts"]
    assert {row["result"] for row in reply["items"]} <= {"changed", "skipped", "refused", "waiting", "unchanged", "staged"}
    assert h.command("coordinator", "bulk.apply", payload, key=key)["receipt_id"] == reply["receipt_id"]
    assert reply["undo"]["kind"] == "bulk.apply"
    h.command("coordinator", **{"kind": reply["undo"]["kind"], "payload": reply["undo"]["payload"]})
    after = h.get("coordinator", "/api/catalogues")["people"]
    keys = {p["target_id"] for p in payload["items"]}
    assert [(p["id"], p["title"], p["directory_section"]) for p in after if p["id"] in keys] == [
        (p["id"], p["title"], p["directory_section"]) for p in before if p["id"] in keys]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == "bulk.apply")) == 2
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.batch_id == reply["batch_id"])))
        assert len(history) == 2


def test_bulk_failure_is_atomic_and_has_index(h):
    payload = people_change(h)
    payload["items"][1]["expected_revision"] = 999
    before = h.get("coordinator", "/api/catalogues")["people"]
    result = h.command("coordinator", "bulk.apply", payload, status=409)
    assert result["error"]["details"]["failed_index"] == 1
    assert h.get("coordinator", "/api/catalogues")["people"] == before


def test_bulk_stale_fingerprint_and_undo_refuse_changed_record(h):
    payload = people_change(h)
    check = preview(h, "coordinator", payload)
    reply = h.command("coordinator", "bulk.apply", payload)
    response = h.client("coordinator").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
        json={"kind": "bulk.apply", "payload": payload, "expected_fingerprint": check["fingerprint"]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_PREVIEW"
    person = h.get("coordinator", "/api/catalogues")["people"][0]
    second = {"item_kind": "catalogue.update", "common": {"catalogue": "people"},
              "items": [{"target_id": person["id"], "expected_revision": person["revision"],
                         "changes": [{"field": "title", "value": "Changed later"}]}], "reason": REASON}
    h.command("coordinator", "bulk.apply", second)
    refused = h.command("coordinator", "bulk.apply", reply["undo"]["payload"], status=409)
    assert person["id"] in refused["error"]["message"]


def test_bulk_roles_and_only_row_revalidation(h):
    payload = people_change(h)
    check = preview(h, "credentialer", payload)
    assert check["counts"]["problem"] == 2
    assert len(check["groups"]) == 1
    assert all(r["issues"][0]["code"] == "FORBIDDEN" for r in check["items"])
    payload["only"] = [1]
    assert [r["index"] for r in preview(h, "coordinator", payload)["items"]] == [1]


@pytest.mark.parametrize("kind", ["workspace.publish", "staff_import.apply", "bulk.apply", "integration.github.test"])
def test_bulk_disallows_unsafe_kinds(h, kind):
    h.command("admin", "bulk.apply", {"item_kind": kind, "items": [{"payload": {}}], "reason": REASON}, status=422)


def test_bulk_relationship_creation_uses_unchanged_single_command_payload(h):
    row, payload, role = relationship(h, "eligibility")
    payload = {**payload, "starts_on": "2027-02-01", "ends_on": "2027-03-01"}
    reply = h.command(role, "bulk.apply", {"item_kind": "eligibility.create", "items": [{"payload": payload}], "reason": REASON})
    assert reply["items"][0]["record"]["starts_on"] == payload["starts_on"]
    h.command(role, "bulk.apply", reply["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, reply["items"][0]["record"]["id"]) is None
        assert db.get(m.Eligibility, row["id"]) is not None


@pytest.mark.parametrize("choice", ["stop_before_later", "shift_later_start", "skip"])
def test_bulk_overlap_resolution_is_rederived_and_atomic(h, choice):
    row, payload, role = relationship(h, "eligibility")
    later = h.command(role, "eligibility.create", {**payload, "starts_on": "2026-12-01", "ends_on": "2027-03-01"})["record"]
    value = {"item_kind": "eligibility.extend", "common": {"ends_on": "2027-02-01"},
             "items": [{"target_id": row["id"], "expected_revision": row["revision"]}], "reason": REASON}
    check = preview(h, role, value)
    issue = check["items"][0]["issues"][0]
    assert issue["message"] == "A later record already covers part of this period"
    assert {r["id"] for r in issue["resolutions"]} == {"stop_before_later", "shift_later_start", "skip"}
    value["resolutions"] = {issue["group_key"]: choice}
    fixed = preview(h, role, value)
    assert fixed["items"][0]["result"] == ("unchanged" if choice == "skip" else "ready")
    reply = h.command(role, "bulk.apply", value)
    assert reply["items"][0]["result"] == ("skipped" if choice == "skip" else "changed")
    with transaction(h.app.state.engine) as db:
        first, second = db.get(m.Eligibility, row["id"]), db.get(m.Eligibility, later["id"])
        assert first.ends_on.isoformat() == {"stop_before_later": "2026-12-01", "shift_later_start": "2027-02-01", "skip": "2026-11-01"}[choice]
        assert second.starts_on.isoformat() == ("2027-02-01" if choice == "shift_later_start" else "2026-12-01")
    if choice != "skip":
        h.command(role, "bulk.apply", reply["undo"]["payload"])
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Eligibility, row["id"]).ends_on.isoformat() == "2026-11-01"
            assert db.get(m.Eligibility, later["id"]).starts_on.isoformat() == "2026-12-01"


@pytest.mark.parametrize("choice", ["keep", "shorten_anyway"])
def test_bulk_shortening_requires_explicit_resolution(h, choice):
    row, _, role = relationship(h, "eligibility")
    value = {"item_kind": "eligibility.update_dates", "common": {"ends_on": "2026-10-15"},
             "items": [{"target_id": row["id"], "expected_revision": row["revision"]}], "reason": REASON}
    check = preview(h, role, value)
    assert check["items"][0]["issues"][0]["code"] == "WOULD_SHORTEN"
    value["resolutions"] = {"0": choice}
    reply = h.command(role, "bulk.apply", value)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, row["id"]).ends_on.isoformat() == ("2026-10-15" if choice == "shorten_anyway" else "2026-11-01")
    if choice == "shorten_anyway":
        h.command(role, "bulk.apply", reply["undo"]["payload"])


def test_bulk_two_changes_same_record_and_one_undo(h):
    value = people_change(h)
    value["items"] = value["items"][:1]
    # Use two top-level rows with the same observed revision, as a sheet can edit a person twice.
    value["items"][0]["changes"] = [{"field": "title", "value": "First"}]
    second = {**value["items"][0], "changes": [{"field": "directory_section", "value": "Second"}]}
    value["items"].append(second)
    reply = h.command("coordinator", "bulk.apply", value)
    assert len(reply["undo"]["payload"]["items"]) == 2
    h.command("coordinator", "bulk.apply", reply["undo"]["payload"])


def test_bulk_clear_text_and_ready_only_counts(h):
    value = people_change(h)
    value["items"][0]["changes"] = [{"field": "title", "how": "clear"}]
    value["items"][1]["expected_revision"] = 999
    value["only"] = [0]
    reply = h.command("coordinator", "bulk.apply", value)
    assert reply["staged_count"] == 1
    assert reply["items"][1]["result"] == "staged" and reply["counts"]["staged"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, value["items"][0]["target_id"]).title == ""


def test_problem_group_ignores_row_specific_dates():
    from apps.api.errors import Problem
    from apps.api.services.bulk import _group_issue, _issue
    first = _group_issue(_issue(Problem(409, "WOULD_SHORTEN", "Shortens 2026-01-01 to 2026-02-01")))
    second = _group_issue(_issue(Problem(409, "WOULD_SHORTEN", "Shortens 2027-03-01 to 2027-04-01")))
    assert first["group_key"] == second["group_key"]
    assert first["message"] != second["message"]


def test_bulk_planner_changes_share_initial_snapshot_and_one_undo(h):
    workspace = h.workspace()
    one = h.shift(workspace)
    two = h.shift(one["workspace"], start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")
    workspace = two["workspace"]
    before = h.detail(workspace)["shifts"]
    with transaction(h.app.state.engine) as db:
        count = db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace["id"]))
    value = {"item_kind": "shift.action", "reason": REASON,
        "items": [{"target_id": workspace["id"], "expected_revision": workspace["revision"],
                   "payload": {"workspace_id": workspace["id"], "shift_ids": [row["id"]], "action": "lock"}}
                  for row in before]}
    reply = h.command("coordinator", "bulk.apply", value)
    assert len(reply["undo"]["payload"]["items"]) == 1
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace["id"])) == count + 1
    h.command("coordinator", "bulk.apply", reply["undo"]["payload"])
    assert h.detail(workspace)["shifts"] == before


def test_bulk_aggregate_footprint_cannot_bypass_department_limit(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).bulk_settings = {"bulk_max_items": 1, "bulk_direct_max": 1}
    value = {"item_kind": "membership.bulk_create", "reason": REASON,
             "items": [{"payload": {"person_ids": h.seed["people"][:2], "category_id": h.seed["category"], "starts_on": "2026-01-01"}}]}
    refused = h.command("coordinator", "bulk.apply", value, status=422)
    assert refused["error"]["code"] == "BULK_ITEM_LIMIT"


def test_bulk_fields_without_command_are_refused_instead_of_silent_success(h):
    value = people_change(h)
    value.pop("item_kind")
    check = preview(h, "coordinator", value)
    assert check["counts"]["problem"] == 2
    assert check["items"][0]["issues"][0]["code"] == "BULK_KIND_REQUIRED"
    h.command("coordinator", "bulk.apply", value, status=422)


def test_universal_form_combines_person_fields_and_related_rank_action(h):
    rank = h.configure("categories", {"name": "Synthetic action rank", "starts_on": "2020-01-01"})["record"]
    value = people_change(h)
    value["items"] = value["items"][:1]
    value["items"][0]["changes"].append({"action": "rank", "value": {
        "category_id": rank["id"], "starts_on": "2026-10-01"}})
    reply = h.command("coordinator", "bulk.apply", value)
    assert reply["items"][0]["after"]["title"] == "Bulk title"
    assert reply["items"][0]["after"]["membership.create"]["category_id"] == rank["id"]
    h.command("coordinator", "bulk.apply", reply["undo"]["payload"])


def test_universal_action_only_omits_empty_primary_edit(h):
    rank = h.configure("categories", {"name": "Synthetic action only rank", "starts_on": "2020-01-01"})["record"]
    value = people_change(h)
    value["items"] = value["items"][:1]
    value["items"][0]["changes"] = [{"action": "rank", "value": {
        "category_id": rank["id"], "starts_on": "2026-10-01"}}]
    reply = h.command("coordinator", "bulk.apply", value)
    assert len(reply["items"][0]["results"]) == 1
    assert reply["items"][0]["results"][0]["record"]["category_id"] == rank["id"]


def test_bulk_undo_is_the_receipt_owned_complete_reverse(h):
    reply = h.command("coordinator", "bulk.apply", people_change(h))
    reverse = reply["undo"]["payload"]
    assert reverse["undo_of"] == reply["receipt_id"]
    changed = h.command("coordinator", "bulk.apply", {**reverse, "reason": "Substitute another operation"}, status=409)
    assert changed["error"]["code"] == "BULK_UNDO_CHANGED"
    h.command("admin", "bulk.apply", reverse, status=403)
    h.command("coordinator", "bulk.apply", reverse)


def test_rule_line_selection_groups_changes_into_governed_rule_proposal(h):
    lines = [{"id": str(uuid.uuid4()), "starts_on": start, "ends_on": end, "numbers": {"max": {"value": 5}}}
             for start, end in (("2026-10-01", "2026-11-01"), ("2026-12-01", "2027-01-01"))]
    proposal = h.command("coordinator", "rule.propose", {"catalogue_id": "duties_per_week", "lines": lines, "reason": REASON})["record"]
    active = h.command("approver", "policy.activate", {"reason": REASON}, proposal["id"], proposal["revision"])["record"]
    payload = {"item_kind": "rule.propose", "selection": {"collection": "policy_lines",
        "where": {"filters": {"status": ["active"], "policy_id": [active["id"]]}}}, "changes": [{"field": "numbers", "value": {"max": {"value": 7}}}], "reason": REASON}
    check = preview(h, "coordinator", payload)
    assert check["count"] == 1 and check["counts"]["waiting"] == 1
    response = h.client("coordinator").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
        json={"kind": "bulk.apply", "payload": payload, "expected_fingerprint": check["source_fingerprint"]})
    assert response.status_code == 200, response.text
    result = response.json()
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active["id"]).status == "active"
        pending = db.scalar(select(m.Policy).where(m.Policy.id != active["id"], m.Policy.status == "proposed"))
        assert pending is not None
        assert [line["numbers"]["max"]["value"] for line in pending.definition["rule"]["lines"]] == [7, 7]
    h.command("coordinator", "bulk.apply", result["undo"]["payload"])


@pytest.mark.parametrize("severity", ["advisory", "mandatory"])
def test_bulk_operational_impact_uses_real_validator_messages_and_safe_resolutions(h, severity):
    h.configure("tags", {"name": "BulkAffected", "labels": ["people"]})
    h.policy({"kind": "restriction"}, {"tags": ["BulkAffected"]}, severity=severity)
    workspace = h.workspace()
    h.shift(workspace)
    person = next(p for p in h.get("coordinator", "/api/catalogues")["people"] if p["id"] == h.seed["people"][0])
    payload = {"item_kind": "catalogue.update", "common": {"catalogue": "people"},
        "items": [{"target_id": person["id"], "expected_revision": person["revision"],
                   "changes": [{"field": "tags", "how": "add", "value": ["BulkAffected"]}]}], "reason": REASON}
    check = preview(h, "coordinator", payload)
    assert check["warnings"] and check["counts"]["problem"] == 1
    issue = check["items"][0]["issues"][0]
    assert issue["message"] in {finding["message"] for finding in check["warnings"]}
    choices = {choice["id"] for choice in issue["resolutions"]}
    assert ("apply_anyway" in choices) == (severity == "advisory")
    refused = h.command("coordinator", "bulk.apply", payload, status=409)
    assert refused["error"]["code"] == issue["code"]
    choice = "apply_anyway" if severity == "advisory" else "skip"
    result = h.command("coordinator", "bulk.apply", {**payload, "resolutions": {"0": choice}})
    with transaction(h.app.state.engine) as db:
        assert ("BulkAffected" in db.get(m.Person, person["id"]).tags) == (severity == "advisory")
        if severity == "advisory":
            audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "catalogue.update",
                m.AuditEvent.detail["batch_id"].as_string() == result["batch_id"]))
            assert audit.detail["resolved_finding"]["code"] == issue["code"]


@pytest.mark.parametrize("rule_timing", ["captured", "late"])
def test_bulk_tag_change_refuses_mandatory_weekly_duty_cap(h, rule_timing):
    tag = h.configure("tags", {"name": "Synthetic duty cap tag", "labels": ["people"]})["record"]
    person_id = h.seed["people"][0]
    draft = h.shift(h.workspace(), people=[person_id])["workspace"] if rule_timing == "late" else None
    cap = h.policy({"kind": "max_duties", "maximum": 0, "window": "week"}, {"tags": [tag["name"]]})
    if draft is None:
        draft = h.shift(h.workspace(), people=[person_id])["workspace"]
    person = next(row for row in h.get("admin", "/api/catalogues")["people"] if row["id"] == person_id)
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "people"}, "reason": REASON,
             "items": [{"target_id": person_id, "expected_revision": person["revision"],
                        "changes": [{"field": "tags", "how": "add", "value": [tag["name"]]}]}]}

    check = preview(h, "admin", value)
    assert check["counts"]["problem"] == 1
    finding, = [finding for finding in check["warnings"] if finding["rule_id"] == cap["id"]]
    assert (finding["code"], finding["severity"]) == ("MAX_DUTIES", "error")
    assert (finding["workspace_id"], finding["person_id"]) == (draft["id"], person_id)
    issue, = [issue for issue in check["items"][0]["issues"] if issue["code"] == "MAX_DUTIES"]
    assert [option["id"] for option in issue["resolutions"]] == ["skip"]
    reply = h.command("admin", "bulk.apply", value, expected_fingerprint=check["source_fingerprint"], status=409)
    assert reply["error"]["code"] == "MAX_DUTIES"
    with transaction(h.app.state.engine) as db:
        assert tag["name"] not in db.get(m.Person, person_id).tags


@pytest.mark.parametrize("collection", ["resource_pools", "operating_calendars"])
def test_bulk_mandatory_impact_is_attributed_to_indirect_configuration(h, collection):
    from .test_resources_calendars import pool, typ, calendar, attach
    if collection == "resource_pools":
        row = pool(h, capacity=2)
        duty = typ(h, row, resources=[{"resource_pool_id": row["id"], "units": 2}])
        h.shift(h.workspace(), typ=duty["id"])
        change = {"field": "capacity", "value": 1}
        code = "POOLED_RESOURCE_CAPACITY"
    else:
        row = calendar(h)
        attach(h, row)
        h.shift(h.workspace())
        change = {"field": "definition", "value": {"timezone": "Asia/Riyadh", "week": []}}
        code = "CALENDAR_CLOSED"
    value = {"item_kind": "catalogue.update", "common": {"catalogue": collection},
        "items": [{"target_id": row["id"], "expected_revision": row["revision"], "changes": [change]}], "reason": REASON}
    check = preview(h, "admin", value)
    assert code in {finding["code"] for finding in check["warnings"]}
    assert check["counts"]["problem"] == 1
    issue = next(issue for issue in check["items"][0]["issues"] if issue["code"] == code)
    assert {resolution["id"] for resolution in issue["resolutions"]} == {"skip"}
    refused = h.command("admin", "bulk.apply", value, status=409)
    assert refused["error"]["code"] == code


def test_single_record_and_bulk_pool_capacity_previews_match(h):
    from .test_resources_calendars import pool, typ

    resource = pool(h, capacity=2)
    duty = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    h.shift(h.workspace("Preview parity"), typ=duty["id"])
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "resource_pools"},
             "items": [{"target_id": resource["id"], "expected_revision": resource["revision"],
                         "changes": [{"field": "capacity", "value": 1}]}], "reason": REASON}
    single = h.client("admin").post("/api/catalogue/preview", json={
        "kind": "catalogue.update", "target_id": resource["id"], "expected_revision": resource["revision"],
        "payload": {"catalogue": "resource_pools",
                    "value": {"name": resource["name"], "starts_on": resource["starts_on"], "capacity": 1},
                    "reason": REASON}})
    assert single.status_code == 200, single.text
    single_finding = next(f for f in single.json()["impacts"][0]["introduced"] if f["code"] == "POOLED_RESOURCE_CAPACITY")
    bulk = preview(h, "admin", value)
    bulk_finding = next(f for f in bulk["warnings"] if f["code"] == "POOLED_RESOURCE_CAPACITY")
    assert single_finding["code"] == bulk_finding["code"] == "POOLED_RESOURCE_CAPACITY"
    issue = next(issue for issue in bulk["items"][0]["issues"] if issue["code"] == "POOLED_RESOURCE_CAPACITY")
    assert {choice["id"] for choice in issue["resolutions"]} == {"skip"}


@pytest.mark.parametrize("many", [False, True])
def test_bulk_activation_undo_restores_every_replaced_policy(h, many):
    old = h.policy({"kind": "max_duties", "maximum": 10, "window": "week"})
    proposed = h.command("coordinator", "policy.create", {"name": "Replacement synthetic rule", "owner": "Synthetic owner",
        "source": REASON, "starts_on": "2020-01-01", "definition": {
            "rule": {"kind": "max_duties", "maximum": 9, "window": "week"}}})["record"]
    kind = "policy.activate_many" if many else "policy.activate"
    payload = {"replaces_ids": [old["id"]]}
    item = {"payload": payload}
    if many:
        payload["items"] = [{"policy_id": proposed["id"], "expected_revision": proposed["revision"]}]
    else:
        item.update(target_id=proposed["id"], expected_revision=proposed["revision"])
    result = h.command("approver", "bulk.apply", {"item_kind": kind, "items": [item], "reason": REASON})
    inverse = result["undo"]["payload"]["items"]
    assert [(i["item_kind"], i["target_id"]) for i in inverse] == [("policy.retire", proposed["id"]), ("policy.restore", old["id"])]
    h.command("approver", "bulk.apply", result["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, old["id"]).status == "active"
        assert db.get(m.Policy, proposed["id"]).status == "retired"


@pytest.mark.parametrize("unchanged", [False, True])
def test_noop_relationship_updates_add_no_false_reverse(h, unchanged):
    row, _, role = relationship(h, "eligibility")
    end = row["ends_on"] if unchanged else "2027-01-01"
    item = {"target_id": row["id"], "expected_revision": row["revision"], "payload": {"ends_on": end}}
    result = h.command(role, "bulk.apply", {"item_kind": "eligibility.update_dates", "items": [item, item], "reason": REASON})
    if unchanged:
        assert result["undo"] is None
    else:
        assert len(result["undo"]["payload"]["items"]) == 1
        h.command(role, "bulk.apply", result["undo"]["payload"])
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Eligibility, row["id"]).ends_on.isoformat() == row["ends_on"]


def test_applying_only_one_row_rechecks_that_exact_union(h):
    from .test_resources_calendars import pool, typ
    resource = pool(h, capacity=2)
    duty = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    h.shift(h.workspace(), typ=duty["id"])
    items = [{"target_id": resource["id"], "expected_revision": resource["revision"],
              "changes": [{"field": "capacity", "value": capacity}]} for capacity in (1, 2)]
    value = {"item_kind": "catalogue.update", "common": {"catalogue": "resource_pools"}, "items": items, "reason": REASON}
    assert preview(h, "admin", value)["counts"]["problem"] == 0
    refused = h.command("admin", "bulk.apply", {**value, "only": [0]}, status=409)
    assert refused["error"]["code"] == "POOLED_RESOURCE_CAPACITY"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ResourcePool, resource["id"]).capacity == 2


@pytest.mark.parametrize("collection", ["people", "services"])
def test_universal_tag_action_reuses_tag_members_and_combines_other_changes(h, collection):
    tag = h.configure("tags", {"name": "Universal synthetic tag", "labels": [collection]})["record"]
    source = h.get("admin", "/api/catalogues")[collection][0]
    field = "title" if collection == "people" else "name"
    changes = [{"field": field, "value": "Universal changed value"}, {"action": "tag", "how": "add", "value": [tag["id"]]}]
    rank = None
    if collection == "people":
        rank = h.configure("categories", {"name": "Universal action rank", "starts_on": "2020-01-01"})["record"]
        changes.append({"action": "rank", "value": {"category_id": rank["id"], "starts_on": "2026-10-01"}})
    value = {"item_kind": "catalogue.update", "common": {"catalogue": collection},
             "items": [{"target_id": source["id"], "expected_revision": source["revision"], "changes": changes}], "reason": REASON}
    result = h.command("admin", "bulk.apply", value)
    assert {"catalogue.update", "tag.members"} <= set(result["item_kinds"])
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == tag["id"], m.TagMember.member_id == source["id"]))
    h.command("admin", "bulk.apply", result["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == tag["id"], m.TagMember.member_id == source["id"]))
        cls = m.Person if collection == "people" else m.Service
        assert getattr(db.get(cls, source["id"]), field) == source[field]
        if rank:
            assert not db.scalar(select(m.Membership.id).where(m.Membership.person_id == source["id"], m.Membership.category_id == rank["id"]))


@pytest.mark.parametrize("action", ["tag", "rank"])
def test_action_only_source_revision_is_checked_before_related_writes(h, action):
    person = h.get("admin", "/api/catalogues")["people"][0]
    tag = h.configure("tags", {"name": "Stale source tag", "labels": ["people"]})["record"]
    rank = h.configure("categories", {"name": "Stale source rank", "starts_on": "2020-01-01"})["record"]
    change = {"action": "tag", "how": "add", "value": [tag["id"]]} if action == "tag" else {
        "action": "rank", "value": {"category_id": rank["id"], "starts_on": "2026-10-01"}}
    refused = h.command("admin", "bulk.apply", {"item_kind": "catalogue.update", "common": {"catalogue": "people"},
        "items": [{"target_id": person["id"], "expected_revision": 999, "changes": [change]}], "reason": REASON}, status=409)
    assert refused["error"]["code"] == "STALE_REVISION"
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.TagMember.id).where(m.TagMember.tag_id == tag["id"], m.TagMember.member_id == person["id"]))
        assert not db.scalar(select(m.Membership.id).where(m.Membership.category_id == rank["id"], m.Membership.person_id == person["id"]))
