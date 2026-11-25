"""People's rule-number actions add lines through the normal proposal and review flow."""
from copy import deepcopy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import RULE_CATALOGUE_V2
from .test_bulk import REASON, preview
from .test_rule_catalogue_api import approve, line, person, propose


def _active(h, lines=()):
    return approve(h, propose(h, "duties_per_week", list(lines))["record"])


def _body(policy, lines):
    return {"item_kind": "rule.propose", "reason": REASON,
            "items": [{"target_id": policy["id"], "expected_revision": policy["revision"],
                       "payload": {"add_lines": lines}}]}


def _pending(h):
    with transaction(h.app.state.engine) as db:
        return [p.definition["rule"]["lines"] for p in db.scalars(select(m.Policy).where(m.Policy.status == "proposed"))]


@pytest.mark.parametrize("existing", [False, True])
def test_workload_rule_first_version_and_additions_preview_waiting_and_apply_as_proposals(h, existing):
    entry = next(e for e in RULE_CATALOGUE_V2 if e["id"] == "workload_limit")
    added = {**deepcopy(entry["default_line"]), "id": str(uuid.uuid4()),
             "who": {"match": "all", "conditions": [{"field": "tags", "op": "any", "values": ["student"]}]},
             "numbers": {"max": {"value": 3}}}
    if existing:
        active = approve(h, propose(h, "workload_limit", [], catalogue_version=2)["record"])
        body = _body(active, [added])
    else:
        body = {"item_kind": "rule.propose", "items": [{"payload": {
            "catalogue_id": "workload_limit", "catalogue_version": 2, "lines": [added]}}],
            "reason": "Students: at most 3"}
    checked = preview(h, "coordinator", body)
    assert checked["items"][0]["result"] == "waiting", checked
    assert _pending(h) == []
    result = h.command("coordinator", "bulk.apply", body)
    assert result["items"][0]["result"] == "waiting"
    proposed = result["items"][0]["results"][0]["record"]
    assert proposed["status"] == "proposed"
    assert proposed["definition"]["rule"]["lines"][0]["numbers"]["max"]["value"] == 3
    h.command("coordinator", "policy.activate", {"reason": REASON}, proposed["id"], proposed["revision"], status=403)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, proposed["id"]).status == "proposed"


@pytest.mark.parametrize("invalid", ["duplicate", "schema"])
def test_first_rule_proposal_errors_keep_the_handlers_message_on_the_row(h, invalid):
    lines = [line(numbers={"max": {"value": 3}})]
    if invalid == "duplicate":
        lines.append({**lines[0], "id": str(uuid.uuid4())})
    else:
        lines[0]["ends_on"] = "2026-01-01"
        lines[0]["starts_on"] = "2026-02-01"
    payload = {"catalogue_id": "workload_limit", "catalogue_version": 2, "lines": lines}
    single = h.command("coordinator", "rule.propose", {**payload, "reason": REASON}, status=422)["error"]
    body = {"item_kind": "rule.propose", "items": [{"payload": payload}], "reason": REASON}
    check = preview(h, "coordinator", body)
    row = check["items"][0]
    assert row["result"] == "problem"
    assert row["issues"][0]["code"] == single["code"]
    message = single["message"] if invalid == "duplicate" else "; ".join(
        f'{detail["field"]}: {detail["message"].removeprefix("Value error, ")}' for detail in single["details"])
    assert row["issues"][0]["message"] == message
    refused = h.command("coordinator", "bulk.apply", body, status=422)["error"]
    assert refused["code"] == single["code"] and refused["message"] == message
    assert refused["details"]["failed_index"] == 0 and refused["details"]["completed"] == 0
    assert _pending(h) == []


def test_bulk_schema_messages_name_the_field_without_echoing_values(h):
    submitted = "synthetic_invalid_date"
    ready = {"catalogue_id": "workload_limit", "catalogue_version": 2,
             "lines": [line(numbers={"max": {"value": 3}})]}
    invalid = {**ready, "lines": [line(numbers={"max": {"value": 3}}, who={
        "conditions": [{"field": "first_day", "op": "is", "value": submitted}]})]}
    body = {"item_kind": "rule.propose", "reason": REASON,
            "items": [{"payload": ready}, {"payload": invalid}]}

    checked = preview(h, "coordinator", body)
    assert checked["items"][0]["result"] == "waiting"
    row = checked["items"][1]
    assert row["result"] == "problem"
    issue = row["issues"][0]
    assert issue["code"] == "SCHEMA_INVALID"
    assert issue["message"].startswith("lines.0.who.conditions.0.")
    assert "A value is not valid for this request" in issue["message"]
    assert submitted not in str(checked)

    refused = h.command("coordinator", "bulk.apply", body, status=422)["error"]
    assert (refused["code"], refused["message"]) == (issue["code"], issue["message"])
    assert refused["details"]["failed_index"] == 1 and refused["details"]["completed"] == 0
    assert submitted not in str(refused)
    assert _pending(h) == []


def test_first_rule_unexpected_handler_error_is_a_row_issue_and_rolls_back(h, monkeypatch):
    from apps.api.services import rule_catalogue

    def broken(*args):
        raise RuntimeError("The rule could not be checked")

    monkeypatch.setattr(rule_catalogue, "propose", broken)
    check = preview(h, "coordinator", {"item_kind": "rule.propose", "reason": REASON,
        "items": [{"payload": {"catalogue_id": "workload_limit", "catalogue_version": 2, "lines": []}}]})
    row = check["items"][0]
    assert row["result"] == "problem"
    # Unexpected handler text stays out of row issues; the row gets the fixed message instead.
    assert row["issues"][0]["message"] == "A value is not valid for this request"
    assert _pending(h) == []


@pytest.mark.parametrize("mode", ["group", "each"])
def test_add_rule_lines_supports_group_tree_and_one_line_per_person_without_ids(h, mode):
    policy = _active(h)
    people = h.seed["people"][:2]
    group = {"match": "all", "conditions": [{"match": "any", "conditions": [
        {"field": "person_ids", "values": [p]} for p in people]}]}
    whos = [group] if mode == "group" else [person(p) for p in people]
    added = [{"who": who, "numbers": {"max": {"value": 3}}, "starts_on": "2026-11-01"} for who in whos]
    body = _body(policy, added)
    before = deepcopy(body)
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["waiting"] == 1
    assert _pending(h) == []
    result = h.command("coordinator", "bulk.apply", body)
    proposed = result["items"][0]["results"][0]["record"]
    assert proposed["status"] == "proposed"
    stored = proposed["definition"]["rule"]["lines"]
    assert len(stored) == len(whos) and all(uuid.UUID(x["id"]) for x in stored)
    assert [x["who"] for x in stored] == [
        # The condition validator supplies the group's default match value.
        s.Conditions.model_validate(w).model_dump(mode="json")
        for w in whos]
    assert body == before
    h.command("coordinator", "policy.activate", {"reason": REASON}, proposed["id"], proposed["revision"], status=403)
    h.command("coordinator", "bulk.apply", result["undo"]["payload"])
    assert _pending(h) == []


@pytest.mark.parametrize("mode,resolution", [("each", "end_previous_lines"),
    ("each", "keep_previous_lines"), ("group", None)])
def test_overlapping_personal_lines_have_explicit_end_or_keep_resolutions(h, mode, resolution):
    who = h.seed["people"][0]
    existing = line(who=person(who), numbers={"max": {"value": 5}}, starts_on="2026-10-01")
    policy = _active(h, [existing])
    if mode == "group":
        tag = h.configure("tags", {"name": "Bulk students", "labels": ["people"]})["record"]
        h.command("coordinator", "tag.members", {"add": [{"kind": "people", "id": who}], "reason": REASON},
                  tag["id"], tag["revision"])
        condition = {"match": "all", "conditions": [{"field": "tags", "values": ["bulk students"]}]}
    else:
        condition = person(who)
    added = {"who": condition, "numbers": {"max": {"value": 3}}, "starts_on": "2026-11-01"}
    body = _body(policy, [added])
    checked = preview(h, "coordinator", body)
    if mode == "each":
        assert checked["counts"]["problem"] == 1
        issue = checked["items"][0]["issues"][0]
        assert issue["code"] == "OVERLAPS_UNACKNOWLEDGED"
        options = {x["id"]: x for x in issue["resolutions"]}
        assert options["end_previous_lines"]["label"] == "End them the day before"
        assert options["end_previous_lines"]["best"]
        assert options["keep_previous_lines"]["label"] == "Keep them"
        body["resolutions"] = {issue["group_key"]: resolution}
        assert preview(h, "coordinator", body)["counts"]["waiting"] == 1
    else:
        assert checked["items"][0]["issues"] == []
        assert checked["counts"]["waiting"] == 1
    assert _pending(h) == []
    result = h.command("coordinator", "bulk.apply", body)
    proposed = result["items"][0]["results"][0]["record"]
    lines = proposed["definition"]["rule"]["lines"]
    old = next(x for x in lines if x["id"] == existing["id"])
    assert old.get("ends_on") == ("2026-11-01" if resolution == "end_previous_lines" else None)
    assert len(lines) == (1 if mode == "each" and resolution == "keep_previous_lines" else 2)
    with transaction(h.app.state.engine) as db:
        original = db.get(m.Policy, policy["id"])
        assert original.status == "active" and original.definition["rule"]["lines"][0].get("ends_on") is None


def test_additions_reject_proposal_overrides_and_stale_policy(h):
    policy = _active(h)
    body = _body(policy, [{"numbers": {"max": {"value": 3}}}])
    body["items"][0]["payload"]["on"] = False
    check = preview(h, "coordinator", body)
    assert check["counts"]["problem"] == 1 and check["items"][0]["issues"][0]["code"] == "SCHEMA_INVALID"
    del body["items"][0]["payload"]["on"]
    body["items"][0]["expected_revision"] += 1
    assert preview(h, "coordinator", body)["items"][0]["issues"][0]["code"] == "STALE_REVISION"


@pytest.mark.parametrize("additions", [None, 3])
def test_invalid_additions_are_row_validation_issues_instead_of_footprint_errors(h, additions):
    body = _body(_active(h), additions)
    row = preview(h, "coordinator", body)["items"][0]
    assert row["result"] == "problem" and row["issues"][0]["code"] == "SCHEMA_INVALID"
    assert "valid list" in row["issues"][0]["message"]


def test_additions_do_not_offer_an_invalid_earlier_end_date(h):
    who = person(h.seed["people"][0])
    existing = line(who=who, numbers={"max": {"value": 5}}, starts_on="2026-11-01")
    policy = _active(h, [existing])
    check = preview(h, "coordinator", _body(policy, [{"who": who, "starts_on": "2026-10-01",
                                                      "numbers": {"max": {"value": 3}}}]))
    assert [x["id"] for x in check["items"][0]["issues"][0]["resolutions"]] == ["keep_previous_lines", "skip"]


@pytest.mark.parametrize("difference", ["where", "number", "dates"])
def test_additions_only_resolve_personal_lines_with_the_same_numeric_and_dated_scope(h, difference):
    who = person(h.seed["people"][0])
    old = line(who=who, numbers={"max": {"value": 5}}, starts_on="2026-10-01")
    new = {"who": who, "numbers": {"max": {"value": 3}}, "starts_on": "2026-11-01"}
    if difference == "where":
        new["where"] = {"conditions": [{"field": "service_ids", "values": [h.seed["services"][0]]}]}
    elif difference == "number":
        new["numbers"] = {"min": {"value": 2}}
    else:
        old["ends_on"] = new["starts_on"]
    checked = preview(h, "coordinator", _body(_active(h, [old]), [new]))
    assert checked["counts"]["waiting"] == 1


def test_personal_quota_resolution_uses_the_effective_block_start(h):
    who = person(h.seed["people"][0])
    old = line(who=who, numbers={"max": {"value": 20}},
               settings={"block_quota": {"starts_on": "2026-10-01", "ends_on": "2027-10-01"}})
    policy = approve(h, propose(h, "duties_per_month", [old])["record"])
    new = {"who": who, "numbers": {"max": {"value": 3}},
           "settings": {"block_quota": {"starts_on": "2026-11-01", "ends_on": "2027-11-01"}}}
    body = _body(policy, [new])
    checked = preview(h, "coordinator", body)
    issue = checked["items"][0]["issues"][0]
    body["resolutions"] = {issue["group_key"]: "end_previous_lines"}
    result = h.command("coordinator", "bulk.apply", body)
    proposed = result["items"][0]["results"][0]["record"]
    previous = next(x for x in proposed["definition"]["rule"]["lines"] if x["id"] == old["id"])
    assert previous["ends_on"] == "2026-11-01"
