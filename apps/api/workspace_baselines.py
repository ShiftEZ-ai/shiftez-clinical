"""Immutable department-input baselines and auditable three-way refreshes."""
from copy import deepcopy
import json
import uuid

from sqlalchemy import func, select

from . import models as m
from .errors import require
from .serialization import json_value, record, records_many
from .snapshots import snapshot_hash, snapshot_inputs, snapshot_input_specs, snapshot_window
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


def _strictest_policy(frozen, live, kind):
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
    """Read only the live safety and qualification sources needed by a draft overlay."""
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    department_ids = select(m.Department.id).where(m.Department.organisation_id == organisation.id)
    person_ids = select(m.Person.id).where(m.Person.organisation_id == organisation.id)
    policy_ids = select(m.Policy.id).where(m.Policy.department_id.in_(department_ids))
    lo, hi = snapshot_window(workspace, organisation.timezone)
    sources = {
        "people": (m.Person, m.Person.organisation_id == organisation.id),
        "departments": (m.Department, m.Department.organisation_id == organisation.id),
        **{key: (cls, cls.department_id.in_(department_ids)) for key, cls in LIVE_VALIDITY_SOURCES.items()},
        **{key: (cls, cls.department_id.in_(department_ids)) for key, cls in {
            "programmes": m.Programme, "rotation_types": m.RotationType, "cohorts": m.Cohort,
            "tags": m.Tag, "tag_members": m.TagMember, "capabilities": m.Capability, "categories": m.Category,
        }.items()},
        "memberships": (m.Membership, m.Membership.department_id.in_(department_ids)),
        "eligibilities": (m.Eligibility, m.Eligibility.department_id.in_(department_ids)),
        "grants": (m.CapabilityGrant, m.CapabilityGrant.department_id.in_(department_ids)),
        "rotations": (m.Rotation, m.Rotation.department_id.in_(department_ids)),
        "programme_memberships": (m.ProgrammeMembership, m.ProgrammeMembership.department_id.in_(department_ids)),
        "mentorships": (m.Mentorship, m.Mentorship.department_id.in_(department_ids)),
        "resource_pools": (m.ResourcePool, m.ResourcePool.department_id.in_(department_ids)),
        "operating_calendars": (m.OperatingCalendar, m.OperatingCalendar.department_id.in_(department_ids)),
        "closures": (m.LocationClosure, m.LocationClosure.department_id.in_(department_ids)),
        "date_sets": (m.DateSet, m.DateSet.department_id.in_(department_ids)),
        "policies": (m.Policy, m.Policy.department_id.in_(department_ids)),
        "exceptions": (m.PolicyException, m.PolicyException.policy_id.in_(policy_ids)),
        "scheduling_facts": (m.SchedulingFact, m.SchedulingFact.department_id.in_(department_ids)),
        # Keep retracted rows in the currency so both adding and removing a leave
        # invalidates a bound check; the evaluator still receives only active rows.
        "type_lead_ownership": (m.TypeLeadOwnership, m.TypeLeadOwnership.department_id.in_(department_ids)),
    }
    cache = db.info.setdefault("_live_safety_sources", {})
    cache_key = (organisation.id, organisation.input_revision, organisation.availability_revision)
    acquired = cache.get(cache_key)
    if acquired is None:
        acquired = records_many(db, sources, exclude={"people": ("pager",)})
        cache[cache_key] = acquired
    availability = records_many(db, {"availability": (m.Availability, m.Availability.person_id.in_(person_ids)
                         & (m.Availability.start < hi) & (m.Availability.end > lo))},
                               exclude={"availability": ("private_reason",)})["availability"]
    acquired = {**acquired, "availability": availability}
    from .snapshots import tag_data
    scheduling_facts = [row for row in acquired["scheduling_facts"]
                        if row.get("status") in ("active", "retired") and row.get("approved_by")]
    return {"organisation": record(organisation),
            "people": {row["id"]: row for row in acquired["people"]},
             **{key: {row["id"]: row for row in acquired[key]} for key in
                ("resource_pools", "operating_calendars", "date_sets", *LIVE_VALIDITY_SOURCES)},
             "closures": acquired["closures"],
             "tags": tag_data(db, department_ids, acquired={"tags": acquired["tags"],
                                                               "tag_members": acquired["tag_members"]}),
            **{key: acquired[key] for key in (*LIVE_QUALIFICATION_KEYS, *LIVE_QUALIFICATION_DEFINITIONS,
                                               "policies", "exceptions", "availability", "type_lead_ownership",
                                               "departments") if key != "tags"},
            "scheduling_facts": scheduling_facts}


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
    live_policies = [policy for policy in active_current_policies
                     if (s.safety_rule(policy.get("definition") or {}) or include_mandatory
                         and (policy.get("definition") or {}).get("severity") == "mandatory") and
                     (policy.get("id"), policy.get("revision"), policy.get("status"), policy.get("active", True)) not in frozen_ids]
    if include_mandatory:
        replaced = {policy.get("id") for policy in live_policies}
        frozen_policies = [policy for policy in frozen_policies
                           if policy.get("id") not in replaced or s.safety_rule(policy.get("definition") or {})]
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
                grouped_rows = _strictest_policy(base, policy, kind)
            else:
                grouped_rows.append(policy)
        merged.extend(grouped_rows)
    result["policies"] = merged
    if "team_lead_requirement" in kinds:
        result["type_lead_ownership"] = current.get("type_lead_ownership", [])
    for key in LIVE_QUALIFICATION_KEYS:
        result[key] = current.get(key, result.get(key, []))
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
    verify(saved)
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
    input_manifest = bind_generation_admission(snapshot.get("input_manifest") or {}, snapshot)
    result = result or {}
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


def attach(db, workspace, value):
    row = latest(db, workspace.id)
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
