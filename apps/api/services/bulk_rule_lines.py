"""Build complete governed proposals for selected edits and additive people actions."""
from copy import deepcopy
from datetime import date
from uuid import UUID, uuid5

from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import snapshot_hash
from packages.contracts import schemas as s
from packages.domain.conditions import scope_applies, tag_scope
from packages.domain.rule_lines import _by_number, _pairs, _spans, has_value, named_people, scope_of, tier, unconditional_people, when
from . import independence, rule_catalogue
from .common import scoped
from .policies import people_data
from .scoring_defaults import seeded_rows


def proposal_payload(policy, lines, reason, overrides=None, *, definition=None):
    """Both bulk entry points preserve the current rule's complete authored configuration."""
    definition = definition or policy.definition
    rule = definition["rule"]
    return {"catalogue_id": rule["id"], "catalogue_version": rule.get("catalogue_version", 1),
            "expected_policy_id": policy.id, "expected_revision": policy.revision,
            "on": policy.active and rule.get("on", True), "balance_between_people": definition.get("balance_between_people", True),
            "expected_line_ids": [line["id"] for line in rule.get("lines", [])],
            "settings": deepcopy(rule.get("settings", {})), "lines": lines,
            **(overrides or {}), "reason": reason}


def _additions(db, actor, item, payload, reason):
    value = s.BulkRuleLinesInput.model_validate({k: v for k, v in payload.items() if k != "reason"})
    policy = scoped(db, m.Policy, item.get("target_id"), actor)
    require(policy.kind == "catalogue" and policy.status == "active", "STALE_REVISION",
            "This rule changed since you opened it; reload and try again", 409)
    require(item.get("expected_revision") == policy.revision, "STALE_REVISION",
            "This rule changed since you opened it; reload and try again", 409)
    definition = seeded_rows(db, [record(policy)])[0]["definition"]
    previous = deepcopy(definition["rule"].get("lines", []))
    added = []
    for index, line in enumerate(value.add_lines):
        raw = line.model_dump(mode="json", exclude_none=True)
        raw["numbers"] = {key: number.model_dump(mode="json") for key, number in line.numbers.items()}
        raw["id"] = raw.get("id") or str(uuid5(UUID(policy.id), f"{index}:{snapshot_hash(raw)}"))
        added.append(s.RuleLine.model_validate(raw).model_dump(mode="json", exclude_unset=True))
    require(not ({line["id"] for line in previous} & {line["id"] for line in added})
            and len({line["id"] for line in added}) == len(added), "DUPLICATE_LINES",
            "Two lines are the same line; remove one or change its dates or days")
    proposal = proposal_payload(policy, previous + added, reason, definition=definition)
    # The editor validates the full rule, including settings, numeric slots and dated scope semantics.
    found = rule_catalogue.check(db, actor, proposal["catalogue_id"], proposal)
    pairs = _personal_overlaps(db, actor, policy, proposal, previous, added) if not found["issues"] else []
    return policy, proposal, previous, added, pairs


def _personal_overlaps(db, actor, policy, proposal, previous, added):
    """Use the editor's numeric/date groups and the shared dated condition evaluator."""
    definition = s.PolicyDefinition.model_validate({"rule": {"kind": "catalogue", "id": proposal["catalogue_id"],
        "catalogue_version": proposal["catalogue_version"], "settings": proposal["settings"],
        "on": proposal["on"], "lines": proposal["lines"]},
        "balance_between_people": proposal["balance_between_people"]}).model_dump(mode="json")
    rule, old_ids, new_ids = definition["rule"], {x["id"] for x in previous}, {x["id"] for x in added}
    lines = rule["lines"]
    spans = _spans(rule, lines, {"starts_on": policy.starts_on.isoformat(), "ends_on": None})
    candidates = {pair for group in _by_number(rule).values() for pair in _pairs(group, spans)}
    days, overlaps = {}, set()
    for left, right in candidates:
        old, new = lines[left], lines[right]
        if old["id"] not in old_ids or new["id"] not in new_ids or tier(old) != 3:
            continue
        start = max(spans[left][0], spans[right][0])
        today = independence.today(db, actor.department_id)
        day = date.fromisoformat(start) if start else today
        if day < today and min(spans[left][1], spans[right][1]) > today.isoformat():
            day = today
        if day not in days:
            days[day] = people_data(db, actor, day)
        data, a, b = days[day]
        scope = tag_scope(data, scope_of(new))
        if any(person in data["people"] and scope_applies(data, scope, person, None, a, b)
               for person in named_people(old)):
            overlaps.add((old["id"], new["id"]))
    return sorted(overlaps)


def _resolve(db, actor, policy, proposal, previous, added, pairs, choice):
    old = {line["id"]: line for line in previous}
    new = {line["id"]: line for line in added}
    if choice == "end_previous_lines":
        rule = {**policy.definition["rule"], "lines": proposal["lines"]}
        for old_id, new_id in pairs:
            # Group membership can change later; only the same explicit person preserves coverage.
            require(len(named_people(old[old_id])) == 1 and unconditional_people(new[new_id])
                    and named_people(new[new_id]) == named_people(old[old_id]), "BULK_RESOLUTION_STALE",
                    "That suggested fix is no longer available; preview again", 409)
            previous_numbers, added_numbers = old[old_id]["numbers"], new[new_id]["numbers"]
            # Ending a line removes every slot, including numbers the replacement leaves untouched.
            require(previous_numbers.keys() <= added_numbers.keys() and all(
                has_value(old[old_id], key) == has_value(new[new_id], key)
                and number.get("level", "mandatory") == added_numbers[key].get("level", "mandatory")
                for key, number in previous_numbers.items()), "BULK_RESOLUTION_STALE",
                "That suggested fix is no longer available; preview again", 409)
            first_day = when(rule, new[new_id])[0] or independence.today(db, actor.department_id).isoformat()
            # Dates are exclusive: this leaves the previous line effective through the day before.
            require(first_day > (old[old_id].get("starts_on") or policy.starts_on.isoformat()),
                    "BULK_RESOLUTION_STALE", "That suggested fix is no longer available; preview again", 409)
            old[old_id]["ends_on"] = min(old[old_id].get("ends_on") or first_day, first_day)
            new[new_id]["starts_on"] = first_day
    elif choice == "keep_previous_lines":
        # Same-person additions cannot coexist as duplicate lines; group additions keep personal overrides.
        omit = {new_id for _, new_id in pairs if tier(new[new_id]) == 3}
        new = {ident: line for ident, line in new.items() if ident not in omit}
    else:
        require(not pairs, "OVERLAPS_UNACKNOWLEDGED",
                "Some lines give way to more specific ones; review and acknowledge them", 409)
    proposal["lines"] = [*old.values(), *new.values()]
    if choice:
        found = rule_catalogue.check(db, actor, proposal["catalogue_id"], proposal)
        proposal["acknowledged_overlaps"] = [x["line_id"] for x in found["overlaps"]]
    return proposal


def expand_add_lines(db, actor, item, payload, reason):
    if "add_lines" not in payload:
        return payload
    policy, proposal, previous, added, pairs = _additions(db, actor, item, payload, reason)
    return _resolve(db, actor, policy, proposal, previous, added, pairs, item.get("rule_line_resolution"))


def resolution_options(db, actor, item, value, issue):
    payload = {**value.get("common", {}), **item.get("payload", {})}
    if "add_lines" not in payload or issue["code"] != "OVERLAPS_UNACKNOWLEDGED":
        return None
    policy, proposal, previous, added, pairs = _additions(db, actor, item, payload, value["reason"])
    if not pairs:
        return None
    options = []
    try:
        ending = _resolve(db, actor, policy, deepcopy(proposal), deepcopy(previous), deepcopy(added), pairs, "end_previous_lines")
        checked = rule_catalogue.check(db, actor, ending["catalogue_id"], ending)
        require(not checked["issues"] and not checked["duplicates"], "BULK_RESOLUTION_STALE",
                "That suggested fix is no longer available; preview again", 409)
    except Exception as error:
        from .bulk import _problem
        _problem(error)
        pass  # An invalid boundary or a loss of shared coverage is never offered as a fix.
    else:
        options.append({"id": "end_previous_lines", "label": "End them the day before", "best": True,
                        "operations": [{"patch_item": {"rule_line_resolution": "end_previous_lines"}}]})
    options.append({"id": "keep_previous_lines", "label": "Keep them",
                    "operations": [{"patch_item": {"rule_line_resolution": "keep_previous_lines"}}]})
    return options + issue["resolutions"]
