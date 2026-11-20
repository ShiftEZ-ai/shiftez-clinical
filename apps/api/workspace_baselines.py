"""Immutable department-input baselines and auditable three-way refreshes."""
from collections import OrderedDict
from copy import deepcopy
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from . import models as m
from .errors import require
from .serialization import json_value, record
from .settings import Settings
from .snapshots import snapshot_hash, snapshot_inputs, snapshot_input_specs
from packages.contracts import schemas as s
from packages.domain.generation_scope import selected_session_ids


BASELINE_SCHEMA_VERSION = 1
SOURCE_NAMES = ("people", "duties", "calendars", "rules", "score", "auto", "receipts", "display")
GENERATION_MANIFEST_VERSION = 1
SOURCE_DEPENDENCIES = {
    "people": (),
    "duties": (),
    "calendars": (),
    "rules": ("people", "duties", "calendars"),
    "score": ("rules", "people"),
    "auto": ("rules", "score", "people", "duties", "calendars"),
    "receipts": (),
    "display": (),
}
SOURCE_KEYS = {
    "people": {"people", "memberships", "eligibilities", "grants", "rotations", "programme_memberships", "mentorships"},
    "duties": {"types", "services", "locations", "location_types", "categories", "capabilities", "resource_pools", "recurring_series"},
    "calendars": {"operating_calendars", "closures", "date_sets", "programmes", "rotation_types", "cohorts", "tags", "tag_members"},
    "rules": {"policies", "exceptions", "scheduling_facts", "type_lead_ownership", "policy_seed_lines", "source_bindings", "catalogue_conversions"},
    "score": {"departments"},
    "auto": {"departments"},
    "receipts": set(),
    "display": {"departments"},
}
# S4 owns these settings; every other Department model column follows live.
DEPARTMENT_SETTING_FIELDS = {
    "score": ["generation_defaults"],
    "auto": ["generation_defaults"],
    "display": ["display_settings"],
}
ALWAYS_KEYS = {"organisation", "departments", "department_ids"}
LIVE_SAFETY_KINDS = frozenset(s.NEVER_OVERRIDDEN)
ZERO_LIMIT_KINDS = frozenset({"max_duties", "max_hours", "max_consecutive_days", "max_consecutive_weekends", "block_quota"})
LIVE_QUALIFICATION_KEYS = ("memberships", "eligibilities", "grants", "rotations", "programme_memberships", "mentorships")
LIVE_QUALIFICATION_DEFINITIONS = ("programmes", "rotation_types", "cohorts", "tags", "tag_members", "capabilities", "categories")
LIVE_OPERATIONAL_KEYS = ("operating_calendars", "closures", "date_sets", "resource_pools")
LIVE_OVERLAY_KEYS = LIVE_QUALIFICATION_KEYS + LIVE_QUALIFICATION_DEFINITIONS + LIVE_OPERATIONAL_KEYS
# Governed/listed rules stay captured; keyed Effective catalogues follow live validity.
LIVE_VALIDITY_FIELDS = tuple(m.Effective.__annotations__)
LIVE_VALIDITY_SOURCES = {key: cls for key, cls, _category, _scope, shape, _excluded in snapshot_input_specs()
                         if shape == "keyed" and issubclass(cls, m.Effective) and key not in LIVE_OVERLAY_KEYS}


def _rule_shape(policy, kind):
    """Return an obligation shape with numeric values removed for comparison."""
    definition = deepcopy(policy.get("definition") or {})
    rule = definition.get("rule") or {}
    if kind == "catalogue":
        lines = []
        for line in rule.get("lines") or []:
            line = deepcopy(line)
            line["numbers"] = {key: {k: value for k, value in number.items() if k != "value"}
                                for key, number in (line.get("numbers") or {}).items()}
            lines.append(line)
        rule["lines"] = sorted(lines, key=lambda line: line.get("id", ""))
    else:
        for field in s.SAFETY_STRICTNESS.get(kind, {}):
            rule.pop(field, None)
    definition["rule"] = rule
    return definition


def _distinct_live_policy(policy):
    """Give a differently scoped live obligation its own transient evaluation id."""
    payload = json.dumps(policy.get("definition") or {}, sort_keys=True, default=str, separators=(",", ":"))
    ident = str(uuid.uuid5(uuid.NAMESPACE_URL,
                           f"fmuc:live-safety:{policy.get('id')}:{policy.get('revision')}:{payload}"))
    return {**deepcopy(policy), "id": ident, "_source_policy_id": policy.get("id")}


def _ceiling(rule):
    kind = rule.get("kind")
    if kind == "block_quota":
        return {"maximum": rule.get("amount"), "range": rule.get("maximum_amount")}.get(rule.get("comparison"))
    if kind in ZERO_LIMIT_KINDS:
        return rule.get("hours" if kind == "max_hours" else "maximum")
    return None


def _with_catalogue_numbers(line, numbers):
    projected = {**deepcopy(line), "numbers": deepcopy(numbers)}
    if "source_bindings" in projected:
        projected["source_bindings"] = {key: value for key, value in projected["source_bindings"].items()
                                         if key in numbers}
    return projected


def _indexed_catalogue_lines(lines):
    return [line if source_index == index else {**line, "_source_index": line.get("_source_index", source_index)}
            for index, (source_index, line) in enumerate(lines)]


def _legacy_projection_scope(projected, original, indexes):
    """Keep the original identities and selectors when part of a legacy template is projected."""
    definition = projected["definition"]
    if definition["rule"]["kind"] == "block_quota":
        projected["_quota_line_indices"] = indexes
        if len(indexes) < len(original):
            projected["_quota_template_people"] = sorted({line["person_id"] for line in original if line.get("person_id")})
    elif not definition.get("listed_only") and len(indexes) < len(original):
        kept = set(indexes)
        projected["_legacy_shadow_rows"] = [
            *projected.get("_legacy_shadow_rows", []),
            *[{key: line[key] for key in ("person_id", "starts_on", "ends_on") if key in line}
              for index, line in enumerate(original) if index not in kept and line.get("person_id")],
        ]
    return projected


def _zero_limit_policy(policy):
    """Return the mandatory at-most-zero part of a policy, if it has one."""
    definition = policy.get("definition") or {}
    if definition.get("overrides"):
        definition = {**definition, "overrides": []}
    rule = definition.get("rule") or {}
    if rule.get("kind") != "catalogue":
        if definition.get("severity") != "mandatory" or rule.get("kind") not in ZERO_LIMIT_KINDS:
            return None
        default_zero = _ceiling(rule) == 0
        lines = definition.get("per_person") or []
        if not lines:
            return deepcopy({**policy, "definition": definition}) if default_zero and not definition.get("listed_only") else None
        field = s.line_field(definition)
        zeros = [(index, line) for index, line in enumerate(lines)
                 if _ceiling(s.line_rule(rule, line, field)) == 0]
        listed = definition.get("listed_only") or not default_zero
        if not zeros and listed:
            return None
        projected = {**deepcopy(policy), "definition": {
            **deepcopy(definition), "per_person": [deepcopy(line) for _, line in zeros], "listed_only": listed}}
        return _legacy_projection_scope(projected, lines, [index for index, _ in zeros])

    from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number
    entry = catalogue_entry(rule.get("id"), rule.get("catalogue_version", 1))
    if not entry or "max" not in entry["numbers"] or not rule.get("on", True):
        return None
    source = rule.get("lines") or []
    delegated = set(policy.get("_live_zero_line_ids", ()))
    zeros = {}
    for index, line in enumerate(source):
        numbers = line.get("numbers") or {}
        maximum = numbers.get("max") or {}
        level = maximum.get("level", "mandatory")
        if line["id"] in delegated or maximum.get("value") != 0 or level not in ("mandatory", None):
            continue
        slot = slot_for_number(entry, "max", rule, line, numbers)
        if slot["kind"] not in ZERO_LIMIT_KINDS or slot["advisory"]:
            continue
        zeros[index] = _project_maximum(entry, rule, line)
    if not zeros:
        return None
    # Resolve precedence after tag expansion, with positive maxima and level donors still available.
    for index, line in enumerate(source):
        if index not in zeros and "max" in (line.get("numbers") or {}):
            zeros[index] = {**_project_maximum(entry, rule, line), "_source_default_only": True}
    return {**deepcopy(policy), "_zero_max_projection": True, "definition": {**deepcopy(definition),
        "rule": {**deepcopy(rule), "lines": _indexed_catalogue_lines(sorted(zeros.items()))}}}


def _project_maximum(entry, rule, line):
    from packages.contracts.rule_catalogue import slot_for_number

    kind = slot_for_number(entry, "max", rule, line, line["numbers"])["kind"]
    projected = _with_catalogue_numbers(line, {"max": line["numbers"]["max"]})
    # A removed minimum or target can otherwise change which measure the maximum counts.
    if slot_for_number(entry, "max", rule, projected, projected["numbers"])["kind"] != kind:
        projected.setdefault("settings", {}).setdefault(kind, {})
    return projected


def _without_zero_limit(policy, covered_by):
    """Remove only frozen zeros whose complete scope is also zero live."""
    definition = policy.get("definition") or {}
    rule = definition.get("rule") or {}

    def scope(row, *, omit_person_lines=False):
        value = {key: value for key, value in (row.get("definition") or {}).items()
                 if key not in {"merged_ids", "replaces_id"}}
        if omit_person_lines:
            value.pop("per_person", None)
        return {**{key: row.get(key) for key in ("department_id", "starts_on", "ends_on")},
                "definition": {**value, "rule": {key: value for key, value in
                    (value.get("rule") or {}).items() if key != "lines"}}}

    # Equality proves coverage without guessing containment of condition trees.
    captured_default = (rule.get("kind") != "catalogue" and _ceiling(rule) == 0
                        and not definition.get("per_person") and not definition.get("listed_only"))
    if scope(policy, omit_person_lines=captured_default) != scope(covered_by, omit_person_lines=captured_default):
        return deepcopy(policy)
    if rule.get("kind") != "catalogue":
        field = s.line_field(definition)
        original = definition.get("per_person") or []
        lines = [(index, line) for index, line in enumerate(original)
                 if _ceiling(s.line_rule(rule, line, field)) != 0]
        listed = definition.get("listed_only", False) or _ceiling(rule) == 0
        if listed and not lines:
            return None
        retained = {**policy, "definition": {
            **definition, "per_person": [line for _, line in lines], "listed_only": listed}}
        return (_legacy_projection_scope(retained, original, [index for index, _ in lines])
                if len(lines) < len(original) else retained)
    live_lines = {line.get("id"): line for line in
                  ((covered_by.get("definition") or {}).get("rule") or {}).get("lines") or []}
    lines = []
    for index, line in enumerate(rule.get("lines") or []):
        live = live_lines.get(line.get("id")) or {}
        covered = ({key: value for key, value in line.items() if key != "numbers"} ==
                   {key: value for key, value in live.items() if key != "numbers"}
                   and (live.get("numbers") or {}).get("max", {}).get("value") == 0
                   and (live.get("numbers") or {}).get("max", {}).get("level", "mandatory") == "mandatory")
        numbers = {key: deepcopy(number) for key, number in (line.get("numbers") or {}).items()
                   if not (covered and key == "max" and number.get("value") == 0 and
                           number.get("level", "mandatory") == "mandatory")}
        if numbers:
            lines.append((index, _with_catalogue_numbers(line, numbers)))
    if not lines:
        return None
    return {**deepcopy(policy), "definition": {**deepcopy(definition),
        "rule": {**deepcopy(rule), "lines": _indexed_catalogue_lines(lines)}}}


def _without_projected_catalogue_zeros(policy, zero):
    # Keep line definitions for specificity and inherited strengths. Only the zero projection emits these caps.
    zero_lines = {line["id"] for line in zero["definition"]["rule"]["lines"] if not line.get("_source_default_only")}
    if all(line["id"] in zero_lines and set(line["numbers"]) == {"max"}
           and line["numbers"]["max"].get("level", "mandatory") == "mandatory"
           for line in policy["definition"]["rule"]["lines"]):
        return None
    return {**policy, "_live_zero_line_ids": sorted(zero_lines)}


def _mandatory_recheck_policy(policy):
    zero = _zero_limit_policy(policy)
    if zero is None:
        return policy
    projected = (_without_projected_catalogue_zeros(policy, zero) if policy["definition"]["rule"]["kind"] == "catalogue"
                 else _without_zero_limit(policy, policy))
    # A partial live rule is additive. The captured definitions own any replacement.
    if projected is not None and projected["definition"].get("overrides"):
        projected = {**projected, "definition": {**projected["definition"], "overrides": []}}
    return projected


def _overlay_zero_limits(frozen, current, safety_ids=()):
    """Overlay live zero ceilings without relaxing frozen draft obligations."""
    # shortcut: a captured zero is suppressed only when its projection or legacy scope equals live, else it binds twice (stricter, never looser); fold into one overlay after v0.1.0 (#1006)
    def newest(rows):
        result = {}
        for row in sorted(rows, key=lambda item: (item.get("revision", 0),
                                                   str(item.get("approved_at") or ""),
                                                   str(item.get("created_at") or ""),
                                                   str(item.get("id") or ""))):
            result[row.get("id")] = row
        return result

    def obligation(row):
        definition = {key: value for key, value in row["definition"].items()
                      if key not in {"merged_ids", "replaces_id"}}
        rule = definition["rule"]
        if rule.get("kind") == "catalogue":
            lines = []
            for line in rule.get("lines", ()):
                line = {key: value for key, value in line.items() if key != "_source_index"}
                if line.get("_source_default_only"):
                    line["numbers"] = {
                        name: {key: value for key, value in number.items() if key != "value"}
                        for name, number in line["numbers"].items()
                    }
                lines.append(line)
            definition = {**definition, "rule": {**rule, "lines": lines}}
        return (row.get("department_id"), row.get("starts_on"), row.get("ends_on"), definition)

    current_by_id = newest(current)
    live_zeros = {key: zero for key, policy in current_by_id.items()
                  if policy.get("status") == "active" and policy.get("active", True)
                  and (zero := _zero_limit_policy(policy)) is not None}

    def predecessors(row):
        definition = row["definition"]
        return [*(definition.get("merged_ids") or ()), definition.get("replaces_id")]

    successors = {}
    for ident in live_zeros:
        row = current_by_id[ident]
        rule = row["definition"]["rule"]
        if rule.get("kind") != "catalogue" or not row.get("approved_by"):
            continue
        # Approved catalogue edits create new IDs. Only follow retired versions of the same entry.
        seen, pending = {ident}, predecessors(row)
        while pending:
            previous = pending.pop()
            if previous in seen:
                continue
            seen.add(previous)
            old = current_by_id.get(previous)
            if not old or old.get("status") != "retired" or old.get("department_id") != row.get("department_id"):
                continue
            old_rule = old["definition"]["rule"]
            if old_rule.get("kind") == "catalogue" and old_rule.get("id") == rule.get("id"):
                successors.setdefault(previous, set()).add(ident)
                pending.extend(predecessors(old))

    out = []
    for policy in frozen:
        source_id = policy.get("_source_policy_id", policy.get("id"))
        candidates = successors.get(source_id, ())
        live_id = next(iter(candidates)) if source_id not in live_zeros and len(candidates) == 1 else source_id
        live_zero = live_zeros.get(live_id)
        if live_zero is None or (saved_zero := _zero_limit_policy(policy)) is None:
            out.append(policy)
            continue
        zero_lines = saved_zero["definition"]["rule"].get("lines", ())
        inherits_level = any(line["numbers"]["max"].get("level", "mandatory") is None for line in zero_lines)
        if inherits_level or policy.get("id") in safety_ids or s.safety_rule(policy.get("definition") or {}):
            # Inherited and mixed-safety lines require equivalent zero projections before suppressing a copy.
            same_zero = obligation(saved_zero) == obligation(live_zero)
            retained = _without_projected_catalogue_zeros(policy, saved_zero) if same_zero else policy
        else:
            retained = _without_zero_limit(policy, current_by_id[live_id])
        if retained is not None:
            out.append(retained)
    retained_ids = {policy.get("id") for policy in out}
    for source_id, zero in live_zeros.items():
        zero.update(_live_zero=True, _source_policy_id=source_id)
        out.append(_distinct_live_policy(zero) if source_id in retained_ids else zero)
    return out


def _strict_value(kind, field, left, right):
    if not isinstance(left, (int, float)):
        return right
    if not isinstance(right, (int, float)):
        return left
    direction = s.safety_merge_policy(kind, field)
    if direction == "max":
        return max(left, right)
    if direction == "min":
        return min(left, right)
    raise ValueError(f"Cannot numerically merge safety slot {kind}.{field}")


def _strictest_policy(frozen, live, kind, *, include_mandatory=False):
    """Resolve one live safety revision without losing a differently scoped obligation."""
    frozen_active = frozen.get("status") == "active" and frozen.get("active", True)
    live_active = live.get("status") == "active" and live.get("active", True)
    # A proposed/retired baseline row contributes no obligation.  Once the
    # same live revision is active it must be the row downstream consumers see.
    if live_active and not frozen_active:
        return [deepcopy(live)]
    if frozen_active and not live_active:
        return [deepcopy(frozen)]
    if kind in s.SAFETY_KEEP_BOTH:
        return [frozen, _distinct_live_policy(live)]
    if kind == "team_lead_requirement":
        merged = deepcopy(live)
        merged["id"] = frozen.get("id", merged.get("id"))
        frozen_lines = ((frozen.get("definition") or {}).get("rule") or {}).get("lines") or []
        lines = ((merged.get("definition") or {}).get("rule") or {}).get("lines") or []

        def place_key(line):
            # Leadership qualification subjects are deliberately excluded: the
            # live line supplies WHO, while this key identifies the required place.
            return line.get("where"), line.get("settings") or {}

        for line in lines:
            match = next((old for old in frozen_lines if place_key(old) == place_key(line)), None)
            if not match:
                continue
            for key in ("min", "max"):
                old_number = (match.get("numbers") or {}).get(key) or {}
                new_number = (line.get("numbers") or {}).get(key) or {}
                if "value" in old_number and "value" in new_number:
                    line.setdefault("numbers", {}).setdefault(key, {})["value"] = _strict_value(
                        "team_lead_requirement", "minimum" if key == "min" else "maximum",
                        old_number.get("value"), new_number.get("value"))
            old_settings = (match.get("settings") or {}).get("team_lead_requirement") or {}
            new_settings = line.setdefault("settings", {}).setdefault("team_lead_requirement", {})
            if old_settings.get("continuity") == "continuous" or new_settings.get("continuity") == "continuous":
                new_settings["continuity"] = "continuous"
            if old_settings.get("count_scope") == "concurrent_duties" or new_settings.get("count_scope") == "concurrent_duties":
                new_settings["count_scope"] = "concurrent_duties"
            numbers = line.get("numbers") or {}
            minimum = (numbers.get("min") or {}).get("value")
            maximum = (numbers.get("max") or {}).get("value")
            if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and maximum < minimum:
                numbers.setdefault("max", {})["value"] = minimum
        return [merged]

    if _rule_shape(frozen, kind) != _rule_shape(live, kind):
        return [frozen, _distinct_live_policy(live)]

    if kind == "catalogue":
        from packages.contracts.rule_catalogue import catalogue_entry, safety_slot_policy

        merged = deepcopy(frozen)
        frozen_rule = (merged.get("definition") or {}).get("rule") or {}
        live_rule = (live.get("definition") or {}).get("rule") or {}
        entry = catalogue_entry(live_rule.get("id"), live_rule.get("catalogue_version", 1)) or {}
        if include_mandatory and entry:
            prior = {line["id"]: line.get("numbers") or {} for line in frozen_rule.get("lines") or []}
            changed = {(line.get("_source_identity", line["id"]), key) for line in live_rule.get("lines") or []
                       for key, number in (line.get("numbers") or {}).items()
                       if prior[line["id"]][key].get("value") != number.get("value")}
            if changed:
                from packages.domain.rule_lines import expand

                for copy in expand(live, outrank=False):
                    definition, template = copy["definition"], copy["template_line"]
                    if (definition["severity"] == "mandatory" and definition["rule"]["kind"] not in LIVE_SAFETY_KINDS
                            and any((template["id"], key) in changed for key in template["slot"].split("+"))):
                        return [frozen, _distinct_live_policy({**live, "_live_mandatory_recheck": True})]

        def line_key(line):
            return (line.get("where"), line.get("who"), tuple(line.get("weekdays") or ()),
                    line.get("start_time"), line.get("end_time"), line.get("settings") or {},
                    line.get("starts_on"), line.get("ends_on"), line.get("calendar_entry_id"))

        def safety_number(rule, line, key):
            try:
                return safety_slot_policy(entry, key, rule, line)
            except (KeyError, TypeError):
                return False

        frozen_lines = frozen_rule.get("lines") or []
        for live_line in live_rule.get("lines") or []:
            match = next((line for line in frozen_lines if line_key(line) == line_key(live_line)), None)
            if match is None:
                continue
            for key, number in (live_line.get("numbers") or {}).items():
                policy = safety_number(live_rule, live_line, key)
                if not policy or policy[0] not in LIVE_SAFETY_KINDS:
                    continue
                slot_kind, slot_field, direction = policy
                if direction == "keep_both":
                    return [frozen, _distinct_live_policy(live)]
                old = (match.get("numbers") or {}).get(key)
                if old is None:
                    match.setdefault("numbers", {})[key] = deepcopy(number)
                elif "value" in old and "value" in number:
                    old["value"] = _strict_value(slot_kind, slot_field, old.get("value"), number.get("value"))
        return [merged]

    rule = (frozen.get("definition") or {}).get("rule") or {}
    live_rule = (live.get("definition") or {}).get("rule") or {}
    merged = deepcopy(frozen)
    merged_rule = merged.setdefault("definition", {}).setdefault("rule", {})
    for field in s.SAFETY_STRICTNESS.get(kind, {}):
        if field in live_rule and field in merged_rule:
            merged_rule[field] = _strict_value(kind, field, merged_rule[field], live_rule[field])
    return [merged]


def _catalogue_destination_at_least_as_strict(source, destination):
    """Return whether a catalogue destination preserves every source safety obligation."""
    from packages.domain.conditions import active_rules

    def shape(policy, kind):
        definition = deepcopy(policy.get("definition") or {})
        definition.pop("merged_ids", None)
        definition.pop("replaces_id", None)
        rule = definition.get("rule") or {}
        for field in s.SAFETY_STRICTNESS.get(kind, {}):
            rule.pop(field, None)
        definition["rule"] = rule
        return definition

    source_rules = [rule for rule in active_rules([source]) if rule.get("kind") in LIVE_SAFETY_KINDS]
    destination_rules = [rule for rule in active_rules([destination]) if rule.get("kind") in LIVE_SAFETY_KINDS]
    for source_rule in source_rules:
        kind = source_rule["kind"]
        target = next((rule for rule in destination_rules
                       if rule.get("kind") == kind and shape(rule, kind) == shape(source_rule, kind)), None)
        if target is None:
            return False
        source_body = (source_rule.get("definition") or {}).get("rule") or {}
        destination_body = (target.get("definition") or {}).get("rule") or {}
        for field in s.SAFETY_STRICTNESS.get(kind, {}):
            direction = s.safety_merge_policy(kind, field)
            if field not in source_body:
                continue
            if field not in destination_body:
                return False
            if direction == "max" and destination_body[field] < source_body[field]:
                return False
            if direction == "min" and destination_body[field] > source_body[field]:
                return False
        if not s.SAFETY_STRICTNESS.get(kind):
            # Non-numeric safety kinds use the contract's explicit keep-both policy;
            # the shape match above is the complete obligation comparison.
            if s.safety_merge_policy(kind, None) != "keep_both":
                return False
    return True


def _live_safety_snapshot(db, workspace):
    """Reacquire the same shared inputs used by a fresh draft snapshot."""
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    cache = db.info.setdefault("_live_safety_sources", {})
    cache_key = (organisation.id, organisation.input_revision, organisation.availability_revision)
    if cache_key not in cache:
        cache[cache_key] = snapshot_inputs(db, organisation)
    return cache[cache_key]


def _scope(value):
    """Validate a bounded scope and return the transitive dependency closure."""
    try:
        value = s.WorkspaceBaselineScope.model_validate(value or {})
    except Exception as error:
        require(False, "BASELINE_SCOPE_INVALID", "Review the baseline scope fields", 422,
                [{"field": ".", "message": str(error)}])
    mode = value.mode
    requested = list(SOURCE_NAMES if mode == "department" else value.sources)
    require(requested and len(requested) == len(set(requested)), "BASELINE_SCOPE_INVALID",
            "Custom scope must include at least one source once", 422)
    included = set(requested)
    closure = {name: [] for name in requested}
    changed = True
    while changed:
        changed = False
        for name in sorted(tuple(included)):
            for dependency in SOURCE_DEPENDENCIES[name]:
                closure.setdefault(name, [])
                if dependency not in closure[name]:
                    closure[name].append(dependency)
                if dependency not in included:
                    included.add(dependency)
                    changed = True
    for name in SOURCE_NAMES:
        closure.setdefault(name, [])
    included.update({"receipts"})
    return mode, requested, [name for name in SOURCE_NAMES if name in included], [name for name in SOURCE_NAMES if name not in included], closure


def _empty_like(value):
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    return value


def scoped_payload(payload, included):
    """Keep the shared-input shape while omitting unselected source groups."""
    payload = json_value(payload)
    keys = set().union(*(SOURCE_KEYS[name] for name in included)) | ALWAYS_KEYS
    return {key: value if key in keys else _empty_like(value) for key, value in payload.items()}


def group_payload(payload, source):
    keys = SOURCE_KEYS[source]
    return {key: payload.get(key) for key in sorted(keys) if key in payload}


def group_hash(payload, source):
    return snapshot_hash(group_payload(payload, source))


def frozen_department_settings(payload, department_id, sources=DEPARTMENT_SETTING_FIELDS):
    department = (payload.get("departments") or {}).get(department_id) or {}
    return {field: deepcopy(department[field]) for source in sources
            for field in DEPARTMENT_SETTING_FIELDS.get(source, ()) if field in department}


def live_department_settings(department):
    frozen = {field for fields in DEPARTMENT_SETTING_FIELDS.values() for field in fields}
    return {field: department[field] for field in m.Department.__mapper__.column_attrs.keys()
            if field not in frozen and field in department}


LIVE_DRIFT_KEYS = frozenset({*LIVE_OVERLAY_KEYS, "people", "exceptions", "scheduling_facts", "_baseline_availability"})


def _drift_group_hash(payload, source):
    """Hash only refreshable baseline inputs; live person data is never a refresh conflict."""
    value = {key: item for key, item in group_payload(payload, source).items() if key not in LIVE_DRIFT_KEYS}
    for key in LIVE_VALIDITY_SOURCES:
        if key in value:
            value[key] = {ident: {field: item for field, item in row.items()
                                 if field not in (*LIVE_VALIDITY_FIELDS, "revision")}
                          for ident, row in (value[key] or {}).items()}
    if source in DEPARTMENT_SETTING_FIELDS:
        value["departments"] = {ident: frozen_department_settings(payload, ident, [source])
                                for ident in payload.get("departments", {})}
    return snapshot_hash(value)


def _source_revisions(payload, included):
    revisions = {}
    for source in included:
        rows = []
        for key in SOURCE_KEYS[source]:
            value = payload.get(key)
            rows.extend(value.values() if isinstance(value, dict) else value or [])
        revisions[source] = max((int(row.get("revision", 1)) for row in rows if isinstance(row, dict)), default=1)
    return revisions


def _provenance(db, workspace, actor_id, payload, reason=None):
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    from packages.domain.fairness import VERSION as fairness_version
    from packages.domain.score import VERSION as score_version
    from packages.domain.solver_rules import COMPILER_VERSION
    from packages.domain.validation import ENGINE_VERSION
    from packages.domain.generation import ALGORITHM_VERSION
    return {
        "captured_at": m.now().isoformat(),
        "actor_id": actor_id,
        "workspace_id": workspace.id,
        "workspace_revision": workspace.revision,
        "organisation_revision": organisation.revision,
        "organisation_input_revision": organisation.input_revision,
        "organisation_availability_revision": organisation.availability_revision,
        "department_revision": department.revision,
        "department_catalogue_revision": department.catalogue_revision,
        "versions": {"validator": ENGINE_VERSION, "compiler": COMPILER_VERSION, "scheduler": ALGORITHM_VERSION,
                      "score": score_version, "fairness": fairness_version},
        "payload_keys": sorted(payload),
        **({"capture_reason": reason[:500]} if reason else {}),
    }


def _canonical(payload, included):
    return scoped_payload(payload, included)


def live_safety_inputs(db, workspace, frozen, *, kinds=LIVE_SAFETY_KINDS, current=None, include_mandatory=False,
                       validity_baseline=None):
    """Overlay live facts; rows can share nested values, so callers must copy before writing."""
    def rows(value):
        return value.values() if isinstance(value, dict) else value or []

    def sorted_rows(value):
        return sorted(rows(value), key=lambda row: row.get("id", ""))

    kinds = set(kinds)
    if not kinds:
        return frozen
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    if (current is None or current.get("organisation", {}).get("input_revision") != organisation.input_revision
            or current.get("organisation", {}).get("availability_revision") != organisation.availability_revision
            or not {"policies", "exceptions"} <= set(current)):
        current = _live_safety_snapshot(db, workspace)
    result = dict(frozen)
    for key in LIVE_OPERATIONAL_KEYS:
        if current.get(key) is not None:
            result[key] = current[key]
    if workspace.status not in {"draft", "submitted", "approved"}:
        result["_live_safety_staleness"] = {
            key: sorted_rows(current.get(key, []))
            for key in ("policies", "exceptions", "grants", "availability", "scheduling_facts")
        }
        result["_live_safety_departments"] = sorted_rows(current.get("departments", []))
        return result

    frozen_policies = frozen.get("policies", [])
    current_policies = current.get("policies", [])
    frozen_ids = {(policy.get("id"), policy.get("revision"), policy.get("status"), policy.get("active", True))
                  for policy in frozen_policies}
    active_current_policies = [policy for policy in current_policies
                               if policy.get("status") == "active" and policy.get("active", True)]
    current_safety_ids = {policy["id"] for policy in active_current_policies
                          if s.safety_rule(policy.get("definition") or {})}
    live_policies = [policy for policy in active_current_policies
                     if (policy["id"] in current_safety_ids or include_mandatory
                         and (policy.get("definition") or {}).get("severity") == "mandatory") and
                     (policy.get("id"), policy.get("revision"), policy.get("status"), policy.get("active", True)) not in frozen_ids]
    if include_mandatory:
        filtered = [(policy, policy if policy["id"] in current_safety_ids else _mandatory_recheck_policy(policy))
                    for policy in live_policies]
        # A partly filtered policy still needs the captured numbers for the excluded slots.
        replaced = {policy["id"] for policy, projected in filtered if projected is policy}
        live_policies = [projected if projected is policy else
                         _distinct_live_policy({**projected, "_live_mandatory_recheck": True})
                         for policy, projected in filtered if projected is not None]
        retained = [policy for policy in frozen_policies
                    if (policy.get("id") not in replaced or s.safety_rule(policy.get("definition") or {})
                        or _zero_limit_policy(policy) is not None)]
        result["_mandatory_overlay_changed"] = len(retained) != len(frozen_policies) or any(
            projected is not None and policy["id"] not in current_safety_ids for policy, projected in filtered)
        frozen_policies = retained
    # A reviewed catalogue conversion replaces its source only when the destination
    # is itself present and at least as strict in the same overlay.
    frozen_by_id = {policy.get("id"): policy for policy in frozen_policies}
    overlay_keys = {(policy.get("id"), policy.get("revision"), policy.get("status"), policy.get("active", True))
                    for policy in frozen_policies + live_policies}
    converted_source_ids = {source_id
                            for policy in active_current_policies
                            if ((policy.get("definition") or {}).get("rule") or {}).get("kind") == "catalogue"
                            and s.safety_rule(policy.get("definition") or {})
                            and (policy.get("id"), policy.get("revision"), policy.get("status"), policy.get("active", True)) in overlay_keys
                            for source_id in (policy.get("definition") or {}).get("merged_ids") or ()
                            if source_id in frozen_by_id and s.safety_rule(
                                frozen_by_id[source_id].get("definition") or {})
                            and _catalogue_destination_at_least_as_strict(frozen_by_id[source_id], policy)}
    frozen_policies = [policy for policy in frozen_policies if policy.get("id") not in converted_source_ids]
    # Time settings are live scheduling facts, including inside an existing draft.
    live_organisation = current.get("organisation") or {}
    result["organisation"] = {
        **(result.get("organisation") or {}),
        "input_revision": live_organisation.get("input_revision", organisation.input_revision),
        "availability_revision": live_organisation.get("availability_revision", organisation.availability_revision),
        "timezone": live_organisation.get("timezone", organisation.timezone),
        "week_start": live_organisation.get("week_start", organisation.week_start),
    }
    live_departments = {row["id"]: row for row in rows(current.get("departments"))}
    result["departments"] = {ident: {**row, **live_department_settings(live_departments.get(ident, row))}
                             for ident, row in (result.get("departments") or {}).items()}
    validity_changes = []
    for key in LIVE_VALIDITY_SOURCES:
        if key in current:
            live = current[key]
            captured = (validity_baseline if validity_baseline is not None else frozen).get(key) or {}
            overlaid = {}
            for ident, row in (result.get(key) or {}).items():
                after = ({**row, **{field: live[ident].get(field, row.get(field)) for field in LIVE_VALIDITY_FIELDS}}
                         if ident in live else {**row, "active": False})
                overlaid[ident] = after
                before = captured.get(ident)
                if (before and before.get("department_id") == workspace.department_id
                        and any(before.get(field) != after.get(field) for field in LIVE_VALIDITY_FIELDS)):
                    validity_changes.append({"catalogue": key, "record_id": ident, "name": before.get("name", ident),
                        "before": {field: before.get(field) for field in LIVE_VALIDITY_FIELDS},
                        "after": {field: after.get(field) for field in LIVE_VALIDITY_FIELDS}})
            result[key] = overlaid
    result["_live_validity_changes"] = validity_changes
    # Leadership qualification is a live fact. If a replacement is active, it
    # supersedes the frozen qualification; a retirement without replacement
    # retains the frozen safety obligation.
    live_leadership = [policy for policy in live_policies
                       if ((policy.get("definition") or {}).get("rule") or {}).get("kind") == "catalogue"
                           and ((policy.get("definition") or {}).get("rule") or {}).get("id") == "team_leads"]
    # Some historical rows remain active while a catalogue replacement is being
    # materialised. Qualification is a live fact, so use the newest active row
    # deterministically; the frozen row still supplies the numeric baseline.
    latest_leadership = {}
    for policy in sorted(live_leadership, key=lambda row: (row.get("revision", 0), row.get("id", ""))):
        latest_leadership[((policy.get("definition") or {}).get("rule") or {}).get("id")] = policy
    live_leadership = list(latest_leadership.values())
    result["policies"] = list(frozen_policies)
    # Apply can carry a positive replacement alongside a stricter captured zero.
    if include_mandatory:
        retained_ids = {policy["id"] for policy in frozen_policies}
        live_policies = [_distinct_live_policy({**policy, "_live_mandatory_recheck": True}) if policy["id"] in retained_ids
                         and policy["id"] not in current_safety_ids else policy for policy in live_policies]
    result["policies"] += live_leadership
    result["policies"] += [policy for policy in live_policies if policy not in live_leadership]
    # A revised safety row keeps one policy id for every downstream consumer;
    # choose the strict numeric revision instead of allowing a live relaxation
    # to replace the frozen row in id-keyed solver/evidence maps.
    grouped = {}
    for policy in result["policies"]:
        rule = (policy.get("definition") or {}).get("rule") or {}
        kind = "team_lead_requirement" if rule.get("kind") == "catalogue" and rule.get("id") == "team_leads" else rule.get("kind")
        ident = "team_leads" if kind == "team_lead_requirement" else policy.get("id")
        grouped.setdefault((ident, kind), []).append(policy)
    merged = []
    for (_ident, kind), policies in grouped.items():
        grouped_rows = [policies[0]]
        for policy in policies[1:]:
            safety = (kind in LIVE_SAFETY_KINDS or kind == "team_lead_requirement"
                      or kind == "catalogue" and (s.safety_rule(policy.get("definition") or {})
                          or include_mandatory and s.safety_rule(grouped_rows[0].get("definition") or {})))
            if safety:
                base = grouped_rows.pop(0)
                grouped_rows = _strictest_policy(base, policy, kind, include_mandatory=include_mandatory)
            else:
                grouped_rows.append(policy)
        merged.extend(grouped_rows)
    if include_mandatory and any(policy.get("_live_mandatory_recheck") for policy in merged):
        result["_mandatory_overlay_changed"] = True
    from packages.domain.rule_lines import seeded_policies
    # Make migration defaults available to the zero projection without changing safety merging.
    saved_seeds = result.get("policy_seed_lines") or []
    live_seeds = current.get("policy_seed_lines") or []
    result["policies"] = _overlay_zero_limits(seeded_policies(merged, saved_seeds),
        seeded_policies(current_policies, live_seeds), current_safety_ids)
    if saved_seeds:
        result["policy_seed_lines"] = []
    if "team_lead_requirement" in kinds:
        result["type_lead_ownership"] = current.get("type_lead_ownership", [])
    for key in LIVE_QUALIFICATION_KEYS:
        result[key] = sorted_rows(current.get(key, result.get(key, [])))
    for key in LIVE_QUALIFICATION_DEFINITIONS:
        value = current.get(key)
        if value is not None:
            values = value.values() if isinstance(value, dict) else value
            result[key] = ({row.get("id"): row for row in values}
                           if key in {"programmes", "rotation_types", "cohorts", "tags", "capabilities", "categories"}
                           else value)
    # Person exceptions and scheduling facts are live data. A safety policy's
    # frozen exception is retained so an approved exception can never waive a
    # NEVER_OVERRIDDEN rule inside an existing draft.
    safety_ids = {row.get("id") for row in result["policies"] if s.safety_rule(row.get("definition") or {})}
    frozen_exceptions = result.get("exceptions") or []
    live_exceptions = current.get("exceptions") or []
    result["exceptions"] = [row for row in frozen_exceptions if row.get("policy_id") in safety_ids]
    result["exceptions"] += [row for row in live_exceptions if row.get("policy_id") not in safety_ids]
    aliases = {}
    for policy in result["policies"]:
        if policy.get("_live_zero") or policy.get("_live_mandatory_recheck"):
            source_id = policy["_source_policy_id"]
            if source_id != policy["id"]:
                aliases.setdefault((source_id, policy["revision"]), []).append(policy["id"])
    result["exceptions"] += [{**row, "policy_id": alias} for row in live_exceptions
                             for alias in aliases.get((row.get("policy_id"), row.get("policy_revision")), ())]
    result["scheduling_facts"] = current.get("scheduling_facts", result.get("scheduling_facts", []))
    result["people"] = {ident: dict(row) for ident, row in (current.get("people") or {}).items()}
    result["_live_safety_staleness"] = {
        key: sorted_rows(current.get(key, []))
        for key in ("policies", "exceptions", "grants", "availability", "scheduling_facts")
    }
    result["_live_safety_departments"] = sorted_rows(current.get("departments", []))
    return result


def capture(db, workspace, actor_id, scope=None, *, payload=None, workspace_revision=None, reason=None):
    mode, requested, included, omitted, closure = _scope(scope)
    payload = _canonical(payload if payload is not None else snapshot_inputs(
        db, db.get(m.Organisation, db.get(m.Department, workspace.department_id).organisation_id)), included)
    row = m.WorkspaceBaseline(
        department_id=workspace.department_id,
        workspace_id=workspace.id,
        workspace_revision=workspace.revision if workspace_revision is None else workspace_revision,
        schema_version=BASELINE_SCHEMA_VERSION,
        scope_mode=mode,
        requested_sources=requested,
        included_sources=included,
        omitted_sources=omitted,
        dependency_closure=closure,
        source_revisions=_source_revisions(payload, included),
        provenance=_provenance(db, workspace, actor_id, payload, reason),
        canonical_payload=payload,
        payload_hash=snapshot_hash(payload),
        actor_id=actor_id,
    )
    db.add(row)
    db.flush()
    rows = db.info.get('_baseline_rows')
    if rows:
        for key in list(rows):
            if key[0] == workspace.id:
                rows.pop(key, None)
    return row


def capture_draft_baseline(db, workspace, actor_id, scope=None, *, payload=None,
                           workspace_revision=None, reason="Captured draft baseline"):
    """Capture one immutable draft baseline and fail closed unless it is readable immediately."""
    row = capture(db, workspace, actor_id, scope, payload=payload,
                  workspace_revision=workspace_revision, reason=reason)
    saved = latest(db, workspace.id)
    require(saved is not None and saved.id == row.id, "BASELINE_CAPTURE_FAILED",
            "The draft baseline could not be saved; scheduling is unavailable", 409)
    verify_in_transaction(db, saved)
    baseline_rows(db)[(workspace.id, workspace.revision)] = saved
    return saved


def latest(db, workspace_id):
    return db.scalar(select(m.WorkspaceBaseline).where(m.WorkspaceBaseline.workspace_id == workspace_id)
                    .order_by(m.WorkspaceBaseline.workspace_revision.desc(), m.WorkspaceBaseline.created_at.desc(),
                              m.WorkspaceBaseline.id.desc()).limit(1))


def verify(row):
    if row is None:
        return None
    payload = json_value(row.canonical_payload)
    require(row.schema_version == BASELINE_SCHEMA_VERSION and isinstance(payload, dict), "BASELINE_INVALID",
            "The saved draft baseline is incomplete; recreate the draft", 409)
    require(snapshot_hash(payload) == row.payload_hash, "BASELINE_INVALID",
            "The saved draft baseline failed its integrity check; recreate the draft", 409)
    require(set(row.included_sources) <= set(SOURCE_NAMES), "BASELINE_INVALID",
            "The saved draft baseline contains an unknown source", 409)
    return payload


def baseline_rows(db):
    transaction = db.get_transaction()
    if db.info.get('_verified_baseline_transaction') is not transaction:
        db.info['_verified_baseline_transaction'] = transaction
        db.info.pop('_baseline_rows', None)
        db.info.pop('_verified_baselines', None)
        db.info.pop('_verified_baseline_keys', None)
    return db.info.setdefault('_baseline_rows', {})


def verify_in_transaction(db, row):
    """Return a verified payload for read-only use by this transaction."""
    if row is None:
        return None
    if object_session(row) is not db or not db.in_transaction() or row in db.dirty:
        return verify(row)
    baseline_rows(db)
    cache = db.info.setdefault('_verified_baselines', OrderedDict())
    keys = db.info.setdefault('_verified_baseline_keys', set())
    key = row.id, row.payload_hash
    if key not in cache:
        cache[key] = json_value(row.canonical_payload) if key in keys else verify(row)
        keys.add(key)
    cache.move_to_end(key)
    settings = db.get_bind().get_execution_options().get('rota_write_settings') or Settings()
    while len(cache) > settings.verified_baseline_cache_entries:
        cache.popitem(last=False)
    return cache[key]


def verify_generation_snapshot(db, workspace, snapshot):
    """Verify the immutable baseline reference carried by a generation snapshot."""
    reference = snapshot.get("baseline") or {}
    baseline_id = reference.get("id")
    require(baseline_id, "BASELINE_MISSING", "The generation snapshot has no immutable baseline reference", 409)
    row = db.get(m.WorkspaceBaseline, baseline_id)
    require(row is not None and row.workspace_id == workspace.id,
            "BASELINE_INVALID", "The generation baseline is missing or belongs to another draft", 409)
    expected = baseline_reference(row)
    require(reference.get("hash") == expected["hash"] and
            reference.get("snapshot_hash", reference.get("hash")) == expected["snapshot_hash"],
            "BASELINE_INVALID", "The generation baseline hash no longer matches its reference", 409)
    require(reference.get("schema_version") in (None, expected["schema_version"]),
            "BASELINE_INVALID", "The generation baseline schema no longer matches its reference", 409)
    verify(row)
    return row


def generation_manifest(row, snapshot, *, job_id=None, requester_id=None, job=None, result=None, status=None):
    """Compact, indexed provenance for a generation job; the full input stays in the snapshot."""
    payload = verify(row)
    info = projection(row)
    source_hashes = {source: group_hash(payload, source) for source in info["included_sources"]}
    from packages.domain.input_manifest import bind_generation_admission
    from .serialization import job_result_with_reconciliation
    input_manifest = bind_generation_admission(snapshot.get("input_manifest") or {}, snapshot)
    result = job_result_with_reconciliation(job, result) or {}
    result_copy = {key: value for key, value in result.items() if key != "generation_manifest"}
    result_hash = snapshot_hash(result_copy) if result_copy else None
    candidate_hash = result.get("candidate_hash") or (snapshot_hash({key: result.get(key) for key in ("additions", "removals")})
                                                        if result.get("additions") is not None or result.get("removals") is not None else None)
    created_at = getattr(job, "created_at", None) or snapshot.get("created_at") or m.now()
    started_at = getattr(job, "started_at", None)
    completed_at = getattr(job, "completed_at", None)
    status = status or getattr(job, "status", None) or result.get("status") or "queued"
    return {
        "format": GENERATION_MANIFEST_VERSION,
        "workspace": {"id": snapshot.get("workspace", {}).get("id", row.workspace_id),
                      "revision": snapshot.get("workspace", {}).get("revision", row.workspace_revision)},
        "baseline": baseline_reference(row),
        "sources": {"requested": info["requested_sources"], "included": info["included_sources"],
                    "omitted": info["omitted_sources"], "dependency_closure": info["dependency_closure"],
                    "revisions": info["source_revisions"], "hashes": source_hashes},
        "frozen": {source: {"revision": info["source_revisions"].get(source, 1), "hash": source_hashes[source]}
                   for source in info["included_sources"]},
        "provenance": info.get("provenance") or {},
        "generation": {"defaults": snapshot.get("generation_defaults"), "options": snapshot.get("generation_options"),
                       "engine": snapshot.get("generation_engine"), "compiler": snapshot.get("compiler_version"),
                       "toolchain": input_manifest.get("components", {}).get("toolchain"),
                       "source_digest": snapshot.get("generation_scope_source"),
                       "build_digest": input_manifest.get("token")},
        "scope": {"session_ids": selected_session_ids(snapshot),
                  "population": snapshot.get("planning_population"), "generation_scope": snapshot.get("generation_scope"),
                  "source_identity": snapshot.get("generation_scope_source")},
        "currency": snapshot.get("currency_hint"), "input_fingerprint": snapshot.get("fingerprint"),
        "audit_watermark": snapshot.get("audit_watermark"),
        "refresh": snapshot.get("baseline_refresh"),
        "job": {"id": job_id or getattr(job, "id", None), "run_id": job_id or getattr(job, "id", None),
                "requester": requester_id or getattr(job, "requested_by", None), "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
                "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
                "completed_at": completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at,
                "status": status},
        "result": {"status": result.get("status"), "hash": result_hash, "candidate_hash": candidate_hash,
                   "rollback": result.get("reconciliation") or result.get("rollback")},
    }


def projection(row):
    if row is None:
        return None
    return {
        **baseline_reference(row),
        "scope": row.scope_mode,
        "requested_sources": row.requested_sources or [],
        "included_sources": row.included_sources or [],
        "omitted_sources": row.omitted_sources or [],
        "dependency_closure": row.dependency_closure or {},
        "source_revisions": row.source_revisions or {},
        "snapshot_hash": row.payload_hash,
        "baseline_hash": row.payload_hash,
        "workspace_revision": row.workspace_revision,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "provenance": row.provenance or {},
    }


def baseline_reference(row):
    """Canonical baseline identity shared by projections, jobs and worker validation."""
    if row is None:
        return None
    return {
        "id": row.id,
        "hash": row.payload_hash,
        "snapshot_hash": row.payload_hash,
        "schema_version": row.schema_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "included_sources": row.included_sources or [],
    }


def attach(db, workspace, value, *, baseline=None):
    row = latest(db, workspace.id) if baseline is None else baseline
    if row is None:
        return value
    info = projection(row)
    return {**value, "baseline": info, "snapshot_hash": row.payload_hash, "baseline_hash": row.payload_hash,
            "snapshot_provenance": info["provenance"]}


def _draft_payload(row, workspace):
    payload = json_value(row.canonical_payload)
    adjustments = workspace.rule_adjustments or []
    if adjustments:
        from .services.planning import adjusted_rules
        policies = []
        for policy in payload.get("policies", []):
            adjusted = adjusted_rules(policy, adjustments)
            if adjusted is not None:
                policies.append(adjusted)
        payload["policies"] = policies
    return payload


def _diff(row, workspace, current):
    base = verify(row)
    draft = _draft_payload(row, workspace)
    changes = []
    for source in row.included_sources or []:
        before, department, local = (_drift_group_hash(base, source), _drift_group_hash(current, source),
                                     _drift_group_hash(draft, source))
        dept_changed, draft_changed = department != before, local != before
        if not dept_changed and not draft_changed:
            status = "unchanged"
        elif dept_changed and draft_changed and department == local:
            status = "same"
        elif dept_changed and draft_changed:
            status = "conflict"
        elif dept_changed:
            status = "department"
        else:
            status = "draft"
        if status != "unchanged":
            changes.append({"source": source, "status": status, "snapshot_hash": before,
                            "department_hash": department, "draft_hash": local,
                            "safety": source == "rules" and bool(base.get("policies") or current.get("policies"))})
    return base, draft, changes


def refresh_preview(db, actor, command):
    workspace = db.get(m.Workspace, command.target_id)
    require(workspace is not None and workspace.department_id == actor.department_id, "NOT_FOUND", "Record not found", 404)
    require(workspace.status == "draft" and workspace.archived_at is None, "WORKFLOW_STATE",
            "Only an active draft can refresh its baseline", 409)
    row = latest(db, workspace.id)
    captured_initial = False
    if row is None:
        row = capture_draft_baseline(db, workspace, actor.identity_id,
                                     reason="Captured missing draft baseline during refresh")
        captured_initial = True
    organisation = db.get(m.Organisation, actor.organisation_id)
    current = scoped_payload(snapshot_inputs(db, organisation), row.included_sources)
    base, draft, changes = _diff(row, workspace, current)
    return {"workspace_id": workspace.id, "revision": workspace.revision,
            "baseline": projection(row), "current_hash": snapshot_hash(current),
            "changes": changes, "conflicts": [item["source"] for item in changes if item["status"] == "conflict"],
            "ready": not any(item["status"] == "conflict" for item in changes),
            "captured_initial": captured_initial,
            "local_adjustments": deepcopy(workspace.rule_adjustments or []),
            "target_overrides": deepcopy(workspace.target_overrides or [])}


def refresh(db, actor, command):
    preview = refresh_preview(db, actor, command)
    if command.kind.endswith(".preview"):
        return preview
    payload = dict(command.payload)
    if "snapshot_hash" in payload and "expected_baseline_hash" not in payload:
        payload["expected_baseline_hash"] = payload.pop("snapshot_hash")
    try:
        value = s.WorkspaceBaselineRefreshInput.model_validate(payload)
    except Exception as error:
        require(False, "REFRESH_INPUT_INVALID", "Review the refresh decision fields", 422,
                [{"field": ".", "message": str(error)}])
    require(value.reason is not None, "REASON_REQUIRED", "A reason is required to apply a baseline refresh", 422)
    if preview.get("captured_initial") and not preview["changes"]:
        workspace = db.get(m.Workspace, command.target_id)
        return {**preview, "applied": True, "decisions": {},
                "workspace": attach(db, workspace, record(workspace, exclude=("validation",))),
                "snapshot_hash": preview["baseline"]["snapshot_hash"]}
    decisions = value.decisions
    expected_hash = value.expected_baseline_hash
    current_hash = preview["baseline"]["snapshot_hash"]
    require(expected_hash in (None, current_hash), "STALE_BASELINE", "Refresh this draft before applying the diff", 409,
            {"current_baseline_hash": current_hash})
    conflicts = set(preview["conflicts"])
    selected = {str(key): str(choice) for key, choice in decisions.items()}
    aliases = {"accept": "use_department", "use_department": "use_department", "keep": "keep_draft", "keep_draft": "keep_draft"}
    selected = {key: aliases.get(value, value) for key, value in selected.items()}
    require(conflicts <= set(selected), "REFRESH_DECISION_REQUIRED", "Choose Use department or Keep this draft for each conflict", 409,
            {"conflicts": sorted(conflicts)})
    require(all(value in {"use_department", "keep_draft"} for value in selected.values()), "REFRESH_DECISION_INVALID",
            "Refresh decisions must use use_department or keep_draft", 422)
    require(not any(selected.get(source) == "keep_draft" and next(item for item in preview["changes"] if item["source"] == source)["safety"]
                    for source in conflicts), "SAFETY_RULE", "A mandatory safety rule cannot be kept below the department baseline", 409)
    row = latest(db, command.target_id)
    base = verify(row)
    current = scoped_payload(snapshot_inputs(db, db.get(m.Organisation, actor.organisation_id)), row.included_sources)
    draft = _draft_payload(row, db.get(m.Workspace, command.target_id))
    merged = deepcopy(current)
    for source in row.included_sources or []:
        if selected.get(source) == "keep_draft":
            if source in DEPARTMENT_SETTING_FIELDS:
                for ident in merged.get("departments", {}):
                    merged["departments"][ident].update(frozen_department_settings(draft, ident, [source]))
                continue
            for key in SOURCE_KEYS[source]:
                if key in draft:
                    merged[key] = deepcopy(draft[key])
    workspace = db.get(m.Workspace, command.target_id)
    workspace.revision += 1
    # Refreshing immutable department inputs does not edit the rota content;
    # preserve the latest Undo/Redo change-set's content revision.
    workspace.last_edited_by = actor.identity_id
    workspace.validation = None
    new_row = capture_draft_baseline(db, workspace, actor.identity_id,
                                     {"mode": row.scope_mode, "sources": row.requested_sources},
                                     payload=merged, workspace_revision=workspace.revision,
                                     reason="Refreshed draft baseline after reviewed source decisions")
    db.flush()
    return {**preview, "applied": True, "decisions": selected, "workspace": attach(db, workspace, record(workspace, exclude=("validation",))),
            "baseline": projection(new_row), "snapshot_hash": new_row.payload_hash,
            "current_hash": new_row.payload_hash, "changes": [], "conflicts": [], "ready": True}


def capture_missing(db, actor, command):
    """Explicitly capture the first baseline for a legacy draft, idempotently."""
    workspace = db.get(m.Workspace, command.target_id)
    require(workspace is not None and workspace.department_id == actor.department_id, "NOT_FOUND", "Record not found", 404)
    require(workspace.status == "draft" and workspace.archived_at is None, "WORKFLOW_STATE",
            "Only an active draft can capture its baseline", 409)
    from .services.common import expected
    expected(workspace, command.expected_revision)
    row = latest(db, workspace.id)
    if row is None:
        row = capture_draft_baseline(db, workspace, actor.identity_id,
                                     reason="Captured draft baseline before scheduling")
    else:
        verify(row)
    return {"workspace": attach(db, workspace, record(workspace, exclude=("validation",))),
            "baseline": projection(row), "snapshot_hash": row.payload_hash}
