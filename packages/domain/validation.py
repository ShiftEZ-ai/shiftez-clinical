"""The single scheduling/coverage evaluator: pure functions, no database writes.

Independent tests provide expected outcomes. Names/labels never select behaviour.
A supported shared duty is one shift with multiple services, not an overlap waiver.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from math import ceil
import json
import pickle
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .participation import presence_intervals
from . import leadership
from .time import configured_duration_matches, aware, duration_hours, local_midnight, local_stamp, overlaps, union_hours
from packages.contracts.schemas import (
    INTERNAL_CHECK_KEYS, TypeDefinition, WORKLOAD_COUNT_KINDS, requirement_key, safety_rule, validate_attributes,
)
from .conditions import (PERSON_SCOPE_KEYS, SESSION_FIELDS, active_rules, change_points, conditions_match,
                         has_dated_person_lines, has_session_lines, necessary_person_values, scope_tree, selector_sources,
                         counting_key, counting_views, scope_applies, scope_matches_throughout)
from .patterns import SHAPE_KINDS
from .recurrence import inactive, series_definition, usual_assignee_ids, with_series_state
from .overlap import evaluate_overlaps
from .supervision_levelling import levelled_finding, session_spread_policies, spread_gap, spread_policy_key
from .generation_scope import pooled_anchor, selected_session_ids

# 47: bind rotation leave policies and actual-absence conflicts to validation results.
# 48: invalidate stored checks for actionable, grouped session overlap findings.
# 49: bind finding causes and working-day quota amounts to validation results.
# 50: bind conflict-aware supervision and its trial-cache dependencies to validation results.
ENGINE_VERSION = "manual-core-52"
OVER_MAXIMUM = {"MAXIMUM_PARTICIPANTS", "SCOPED_PARTICIPANT_LIMIT"}



# Rule kinds the per-person rule loop of `evaluate` leaves to other checks (restrictions, quotas, cover, shapes...).
NOT_PERSON_RULES = frozenset({"restriction", "block_quota", "minimum_cover", "maximum_participants", "supervision_limit", "spread_evenly",
                              "allocation_preference", "allocation_overtime", "balance_goal", "team_lead_requirement"}) | SHAPE_KINDS
PERSON_ROWS = ("memberships", "grants", "eligibilities", "availability", "rotations", "programme_memberships", "mentorships")
# Quotas and balance own their periods; minimum rest already selects every neighbouring commitment.
VALIDITY_FOCAL_ONLY_KINDS = WORKLOAD_COUNT_KINDS - {'block_quota', 'balance_goal', 'min_rest'}


class PersonRowsIndex(dict):
    """Indexed person rows with a cheap length guard for mutable trial copies."""
    def __init__(self, values, source_lengths):
        super().__init__(values)
        self.source_lengths = source_lengths


def finding_observations(validation):
    """Every checked constraint behind the visible issues, with the parent's finding context."""
    findings = validation.get('findings', [])
    if validation.get('_plain_findings'):
        return iter(findings)

    def unpack(finding):
        detail = finding.get('detail') or {}
        children = detail.get('derived_findings', [])
        yield {**finding, 'detail': {k: v for k, v in detail.items() if k != 'derived_findings'} or None} if children else finding
        for child in children:
            yield from unpack({**finding, **child})

    return (observation for finding in findings for observation in unpack(finding))


def policy_group_tiers(policies):
    """Immutable rule-line priorities shared by assignment-only validation trials."""
    tiers, source_tiers = {}, {}
    for policy in policies:
        line = policy.get('template_line') or {}
        sources = policy.get('_catalogue_sources')
        if sources is not None and id(sources) not in source_tiers:
            source_tiers[id(sources)] = {source['line']: source['tier'] for source in sources.values()}
        tier = source_tiers.get(id(sources), {}).get(line.get('line'))
        if tier is None:
            scope = policy['definition']['scope']
            tier = 3 if named_people(scope) else 1 if person_free(scope) else 2
        source_id = (policy.get('_source_policy_id', policy['id'])
                     if policy.get('_live_zero') or policy.get('_live_mandatory_recheck') else policy['id'])
        tiers[source_id, line.get('line'), line.get('slot')] = tier
    return tiers


def grouped_rule_findings(findings, policies, *, tiers=None):
    """One visible rule/person/window issue, retaining every independently checked line."""
    codes = {'QUOTA_BELOW_MINIMUM', 'QUOTA_ABOVE_MAXIMUM', 'QUOTA_BELOW_TARGET', 'QUOTA_ABOVE_TARGET',
             'SCOPED_COVERAGE_GAP', 'SCOPED_PARTICIPANT_LIMIT', 'CAPACITY_OVERFLOW', 'BALANCE_GOAL', 'BALANCE_PAIR'}
    tiers = policy_group_tiers(policies) if tiers is None else tiers

    def priority(finding):
        detail = finding.get('detail') or {}
        deficit = max(detail.get('required', 0) - detail.get('available', 0),
                      detail.get('scheduled', 0) - detail.get('maximum', 0), abs(detail.get('difference', 0)))
        return (finding['severity'] == 'error', deficit if finding['severity'] == 'error' else 0,
                tiers.get((finding['rule_id'], detail.get('line'), detail.get('slot')), detail.get('rule_tier', 1)),
                ('low', 'medium', 'high', 'critical').index(detail.get('rule_strength') or 'medium'), deficit)

    groups, result = {}, []
    for finding in (observation for finding in findings for observation in
                    (finding_observations({'findings': [finding]}) if finding['code'] in codes else [finding])):
        if finding['code'] not in codes or not finding.get('rule_id'):
            result.append(finding)
            continue
        if policies:
            detail = dict(finding.get('detail') or {})
            detail['rule_tier'] = tiers.get((finding['rule_id'], detail.get('line'), detail.get('slot')), 1)
            finding = {**finding, 'detail': detail}
        # Match the stored/selected issue identity; independently clipped ends remain in the observations.
        key = tuple(finding.get(k) for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start'))
        if key not in groups:
            groups[key] = []
            result.append(groups[key])
        groups[key].append(finding)
    for n, group in enumerate(result):
        if not isinstance(group, list):
            continue
        primary = max(group, key=priority)
        secondary = [finding for finding in group if finding is not primary]
        result[n] = {**primary, 'detail': {**(primary.get('detail') or {}),
            'derived_findings': [*(primary.get('detail') or {}).get('derived_findings', []), *secondary]}} if secondary else primary
    return result


def index_person_rows(data):
    """Per-person views of the dated person records, built once per immutable evaluation."""
    index = {name: defaultdict(list) for name in PERSON_ROWS}
    for name in PERSON_ROWS:
        for row in data.get(name, []):
            index[name][row["person_id"]].append(row)
    return PersonRowsIndex(index, {name: len(data.get(name, ())) for name in PERSON_ROWS})


def prepare_assignment_trials(data):
    """Request-local immutable facts for trials changing participants only; rebuild after any fact edit."""
    from .rule_lines import read_quality_for
    read_quality = read_quality_for(data)
    snapshot_type = type(data) if getattr(data, 'read_quality', None) is not None else None
    data = dict(with_series_state(data, cache=True))
    policies = data.get('_active_policies')
    if policies is None:
        policies = active_rules(data['policies'], context=data, read_quality=read_quality)
    prepared = {**data, 'policies': policies, '_active_policies': policies,
            '_counted_policies': data['_counted_policies'] if '_counted_policies' in data else counting_views(data, policies),
            '_policy_group_tiers': data['_policy_group_tiers'] if '_policy_group_tiers' in data else policy_group_tiers(policies),
            '_person_rows': data.get('_person_rows') or index_person_rows(data),
            **{key: data.get(key, {}) for key in (
                '_scope_interval_cache', '_line_index_cache', '_bucket_keys_cache', '_person_dates_cache',
                '_restriction_index_cache', '_workload_matches_cache',
                '_workload_normalization_cache', '_distribution_cache', '_allocation_match_cache', '_policy_applies_cache')},
            '_policy_applies_people': True}
    if '_resolver_clock_cache' in prepared:
        # Resolver trials share clocks only; each evaluation owns its membership intersections.
        prepared.pop('_scope_interval_cache', None)
    if snapshot_type is not None:
        prepared = snapshot_type(prepared)
        prepared.read_quality = read_quality
    return prepared


def person_rows(data, name, person_id):
    """One person's rows, in source order. Uses the evaluation's index when it opted in."""
    index = data.get("_person_rows")
    if index is None:
        return [r for r in data.get(name, []) if r["person_id"] == person_id]
    rows = data.get(name, ())
    source_lengths = getattr(index, 'source_lengths', None)
    if source_lengths is not None and source_lengths.get(name) != len(rows):
        # Trial callers may append a source row after taking an immutable snapshot;
        # do not let the old index hide that edit.
        return [r for r in rows if r["person_id"] == person_id]
    return index[name].get(person_id, ())


def display_only_change(snapshot, current, *, live_staffing=False):
    """Ignore colour and, for drafts that enforce them live, hard staffing values."""
    if not snapshot:
        return False
    def core(r):
        definition = {k: v for k, v in (r.get("definition") or {}).items() if k != "colour"}
        if live_staffing:
            definition["requirements"] = [(row.get("from_minute", 0), row.get("to_minute"), row["target"])
                                          for row in definition.get("requirements") or () if row.get("target") is not None]
            definition.pop("supervision", None)
        return {**{k: v for k, v in r.items() if k not in ("revision", "draft_safety")}, "definition": definition}
    return core(snapshot) == core(current)


@lru_cache(maxsize=16384)
def _midnight(day: str, zone: str) -> datetime:
    return local_midnight(date.fromisoformat(day), zone)


def valid_dates(item: dict, a: datetime, b: datetime, zone: str) -> bool:
    return (item.get("active", True)
            and (not item.get("starts_on") or _midnight(item["starts_on"], zone) <= a)
            and (not item.get("ends_on") or b <= _midnight(item["ends_on"], zone)))


def within_dates(item: dict, a: datetime, b: datetime, zone: str) -> bool:
    return (item.get("active", True)
            and (not item.get("starts_on") or _midnight(item["starts_on"], zone) < b)
            and (not item.get("ends_on") or a < _midnight(item["ends_on"], zone)))


def categories_for(data, person_id, a, b):
    cache = data.get('_categories_cache')
    key = person_id, a, b
    if cache is not None and key in cache:
        return cache[key]
    zone = data["organisation"]["timezone"]
    result = {r["category_id"] for r in person_rows(data, "memberships", person_id)
            if valid_dates(r, a, b, zone) and r["category_id"] in data["categories"]
            and valid_dates(data["categories"][r["category_id"]], a, b, zone)}
    if cache is not None:
        cache[key] = result
    return result


def capabilities_for(data, person_id, shift, a, b):
    cache = data.get('_capabilities_cache')
    department = data['types'][shift['assignment_type_id']]['department_id']
    key = person_id, department, tuple(shift['service_ids']), a, b
    if cache is not None and key in cache:
        return cache[key]
    zone = data["organisation"]["timezone"]
    grants = [r for r in person_rows(data, "grants", person_id) if not r.get("revoked")
              and r["department_id"] == data["types"][shift["assignment_type_id"]]["department_id"]
              and valid_dates(r, a, b, zone) and r["capability_id"] in data["capabilities"]
              and valid_dates(data["capabilities"][r["capability_id"]], a, b, zone)]
    services = shift["service_ids"] or [None]
    sets = [{r["capability_id"] for r in grants if r["service_id"] is None or r["service_id"] == service} for service in services]
    result = set.intersection(*sets) if sets else set()
    if cache is not None:
        cache[key] = result
    return result


def attached_to(data, person_id, shift, a, b):
    """The consultants this trainee is attached to for this session throughout [a, b): {consultant id: 'required' |
    'preferred'}. An attachment (stored as a mentorship) holds for its dates, its duty types and clinics (empty: all) and,
    when it names a programme, while the trainee is in it."""
    zone, found = data['organisation']['timezone'], {}
    for r in person_rows(data, 'mentorships', person_id):
        if (not valid_dates(r, a, b, zone)
                or r.get('assignment_type_ids') and shift['assignment_type_id'] not in r['assignment_type_ids']
                or r.get('service_ids') and not set(r['service_ids']) & set(shift['service_ids'])
                or r.get('programme_id') and union_hours(scope_intervals(
                    data, {'programme_id': r['programme_id']}, person_id, a, b)) < duration_hours(a, b) - 1e-9):
            continue
        if found.get(r['mentor_id']) != 'required':
            found[r['mentor_id']] = r.get('strength') or 'preferred'
    return found


def authority_intervals(data, a, b):
    """Local-day intervals: dated ranks and clinical authority change at local midnight."""
    a, b = aware(a), aware(b)
    zone = data['organisation']['timezone']
    day = a.astimezone(ZoneInfo(zone)).date() + timedelta(days=1)
    bounds = [a]
    while local_midnight(day, zone) < b:
        bounds.append(local_midnight(day, zone))
        day += timedelta(days=1)
    bounds.append(b)
    return list(zip(bounds, bounds[1:]))


def is_trainee(data, supervision, participant, a, b):
    return participant['participation'] == 'trainee' or bool(
        set(supervision['trainee_category_ids']) & categories_for(data, participant['person_id'], a, b))


def _published_session(data, shift):
    workspace = data['workspace']
    return (workspace['status'] in ('published', 'superseded')
            or shift['workspace_id'] != workspace['id'])


def _supervision_scope(supervision):
    return supervision["capability_id"], frozenset(supervision["trainee_category_ids"])


def _draft_type_safety(data, shift):
    if shift.get("workspace_id") != data.get("workspace", {}).get("id"):
        return None, None
    typ = data.get("types", {}).get(shift.get("assignment_type_id"), {})
    overlay = typ.get("draft_safety")
    if overlay is None:
        return None, None
    live = overlay.get("live")
    if (live is not None and live != overlay.get("captured")
            and valid_dates(typ, aware(shift["start"]), aware(shift["end"]), data["organisation"]["timezone"])):
        return overlay, live
    return overlay, None


def effective_issued_definition(issued, current, *, draft=False):
    """Keep issued type fields frozen while tightening its safety requirements from today."""
    effective = deepcopy(issued or {})
    current = current or {}
    effective['requirements'] = list(effective.get('requirements') or [])
    issued_requirements = effective['requirements']
    current_requirements = current.get('requirements') or []
    if draft:
        current_requirements = [{**row, "target": None} for row in current_requirements
                                if row.get("minimum") or row.get("maximum") is not None or row.get("capabilities")]
    used = set()
    for row in issued_requirements:
        key = requirement_key(row)
        index = next((i for i, live in enumerate(current_requirements)
                      if i not in used and requirement_key(live) == key
                      and (not draft or not row.get("distinct_capability_seats")
                           or row.get("capabilities") == live.get("capabilities"))), None)
        if index is None:
            continue
        used.add(index)
        live = current_requirements[index]
        if isinstance(live.get('minimum'), (int, float)):
            row['minimum'] = max(row.get('minimum', 0), live['minimum'])
        if live.get('maximum') is not None:
            row['maximum'] = min(row['maximum'], live['maximum']) if row.get('maximum') is not None else live['maximum']
        live_caps = {str(clause.get('capability_id')): clause.get('minimum', 0)
                     for clause in live.get('capabilities') or []}
        for clause in row.get('capabilities') or []:
            capability_id = str(clause.get('capability_id'))
            if capability_id in live_caps:
                clause['minimum'] = max(clause.get('minimum', 0), live_caps[capability_id])
    issued_requirements.extend(deepcopy(row) for i, row in enumerate(current_requirements) if i not in used)
    issued_supervision = effective.get('supervision')
    current_supervision = current.get('supervision')
    if issued_supervision is None and current_supervision is not None:
        effective['supervision'] = deepcopy(current_supervision)
    elif (issued_supervision is not None and current_supervision is not None
          and (not draft or _supervision_scope(issued_supervision) == _supervision_scope(current_supervision))):
        issued_supervision['trainees_per_supervisor'] = min(
            issued_supervision.get('trainees_per_supervisor', 20),
            current_supervision.get('trainees_per_supervisor', 20))
        issued_supervision['require_pairing'] = bool(
            issued_supervision.get('require_pairing') or current_supervision.get('require_pairing'))
    return effective


def effective_type_definition(data, shift):
    """Resolve draft and operational safety while keeping each caller's definition detached."""
    issued = shift['type_snapshot']['definition']
    operational = bool(data.get('operational'))
    current = data['types'][shift['assignment_type_id']]['definition'] if operational else None
    published = _published_session(data, shift) if operational else False
    draft_safety, live_safety = _draft_type_safety(data, shift)
    cache = data.get('_line_index_cache')
    if cache is not None:
        definitions = cache.setdefault('effective_type_definitions', {})
        key = id(issued), id(current), id(draft_safety), live_safety is not None, operational, published
        entry = definitions.get(key)
        if entry is not None and entry[0] is issued and entry[1] is current and entry[3] is draft_safety:
            return pickle.loads(entry[2])
    if operational and published:
        effective = effective_issued_definition(issued, current)
    elif draft_safety is not None:
        authored = current if operational else issued
        effective = authored
        for source in (issued if operational else None, draft_safety.get("captured"), live_safety):
            if source is not None and source is not effective:
                effective = effective_issued_definition(effective, source, draft=True)
        if effective is authored:
            effective = effective_issued_definition(authored, None)
    else:
        definition = (current if operational else issued) or {}
        effective = dict(definition) if cache is not None else deepcopy(definition)
        effective['requirements'] = list(effective.get('requirements') or [])
    if cache is not None:
        # Retain the sources so their identities cannot be reused during this run.
        encoded = pickle.dumps(effective, protocol=pickle.HIGHEST_PROTOCOL)
        definitions[key] = issued, current, encoded, draft_safety
        return pickle.loads(encoded)
    return effective


def trainee_intervals(data, shift, participant):
    supervision = effective_type_definition(data, shift).get('supervision')
    if not supervision:
        return []
    return [(a, b) for start, end in presence_intervals(participant) for a, b in authority_intervals(data, start, end)
            if is_trainee(data, supervision, participant, a, b)]


def supervision_commitments(data):
    """Reserved envelopes, including breaks: an independent duty still occupies the person."""
    entries = defaultdict(list)
    for shift in data['shifts']:
        if not inactive(shift):
            for participant in shift['participants']:
                entries[participant['person_id']].append((shift['id'], aware(participant['start']), aware(participant['end'])))
    return entries


def qualified_supervisor(data, shift, participant, a, b, *, definition=None):
    """Clinical pairing needs grants and freedom from independent commitments."""
    commitments = data.get('_supervision_commitments')
    if commitments is None:
        commitments = supervision_commitments(data)
    if any(sid != shift['id'] and overlaps(a, b, lo, hi)
           for sid, lo, hi in commitments.get(participant['person_id'], ())):
        return False
    definition = effective_type_definition(data, shift) if definition is None else definition
    rule = definition.get('supervision')
    if not rule:
        return participant['participation'] == 'supervisor'
    if is_trainee(data, rule, participant, a, b):
        return False
    return all(rule['capability_id'] in capabilities_for(data, participant['person_id'], shift, lo, hi)
               for lo, hi in authority_intervals(data, a, b))



def supervision_limit_policies(data, shift, person_id, a, b, *, policies=None):
    """The counted supervisor-specific limits applicable to this participation interval."""
    if policies is None:
        policies = data.get('_counted_policies')
        if policies is None:
            policies = counting_views(data, data['policies'])
    return [policy for policy in policies if policy['definition']['rule']['kind'] == 'supervision_limit'
            and policy_applies(data, policy, person_id, shift, a, b)]


def pairing_obligations(data, shift, trainee, supervisor_ids, a, b, counted_policies):
    """Named-pair and supervisor-specific attribution errors for one present trainee."""
    pair = trainee.get('supervisor_person_id')
    supervision = effective_type_definition(data, shift).get('supervision') or {}
    if (pair and pair not in supervisor_ids) or (supervision.get('require_pairing') and not pair):
        yield 'The trainee needs a named, qualified supervisor present throughout this interval', None
    if not pair:
        for supervisor_id in supervisor_ids:
            for policy in supervision_limit_policies(data, shift, supervisor_id, a, b, policies=counted_policies):
                yield 'Name a supervisor to verify supervisor-specific limits', policy


def capabilities_during(data, person_id, shift, a, b):
    """Capabilities held during any part of a duty; interval checks remain required."""
    return set().union(*(capabilities_for(data, person_id, shift, lo, hi)
                         for lo, hi in authority_intervals(data, a, b)))


def participation_for(data, person_id, shift, a, b):
    cache = data.get('_participation_cache')
    key = (person_id, shift['id'], a, b)
    if cache is not None and key in cache:
        return cache[key]
    supervision = effective_type_definition(data, shift).get('supervision')
    if supervision:
        person = {'person_id': person_id, 'participation': 'staff'}
        # An explicit trainee role applies for the whole duty, including non-trainee rank intervals.
        if all(is_trainee(data, supervision, person, lo, hi) for lo, hi in authority_intervals(data, a, b)):
            result = 'trainee'
        elif supervision['capability_id'] in capabilities_during(data, person_id, shift, a, b):
            result = 'supervisor'
        else:
            result = 'staff'
    else:
        result = 'staff'
    if cache is not None:
        cache[key] = result
    return result


def lead_qualified(data, person_id, shift, lead, a, b):
    """Who may lead: any one listed skill and any one listed rank (an empty list allows anyone), and the conditions."""
    from .conditions import conditions_match
    if '_requirements' in lead:
        return leadership.qualified(data, person_id, shift, a, b)
    return ((not lead['capability_ids'] or bool(set(lead['capability_ids']) & capabilities_for(data, person_id, shift, a, b)))
            and (not lead['category_ids'] or bool(set(lead['category_ids']) & categories_for(data, person_id, a, b)))
            and conditions_match(data, lead.get('conditions'), person_id, shift, a, b))


def department_memberships(data, person_id, department_id, a, b):
    """Department membership with valid dated rank authority throughout this interval."""
    return [m for m in person_rows(data, "memberships", person_id) if m["department_id"] == department_id
            and valid_dates(m, a, b, data["organisation"]["timezone"])
            and m["category_id"] in categories_for(data, person_id, a, b)]


def person_active_on(active, inactive_from, day):
    """The same exclusive cutover for date values and database date expressions."""
    return active if inactive_from is None else active | (inactive_from > day)


def person_active_end(person, end, zone):
    """Clip a window's exclusive end to the person's local cutover, or return None if never active."""
    if not person:
        return None
    end = aware(end)
    active = person.get('active', True)
    inactive_from = None if active else person.get('inactive_from')
    if isinstance(inactive_from, datetime):
        inactive_from = inactive_from.date()
    elif isinstance(inactive_from, str):
        inactive_from = date.fromisoformat(inactive_from)
    cutoff = local_midnight(inactive_from, zone) if inactive_from is not None else None
    return end if person_active_on(active, cutoff, end) else cutoff


def person_active_for_interval(data, person_id, a, b):
    """Admit duties ending before the person's local inactive date begins."""
    person = data.get('people', {}).get(person_id)
    if not person or person.get('active', True):
        return bool(person)
    return person_active_end(person, b, data['organisation']['timezone']) == aware(b)


def qualification_failures(data, person_id, shift, definition, a, b):
    """Canonical eligibility decisions, with references suitable for explanations.

    These are eligibility findings only, not a claim about availability or rest.
    No private person or absence fields are included.
    """
    if not person_active_for_interval(data, person_id, a, b):
        return [{"code": "PERSON_INACTIVE", "person_id": person_id}]
    typ = data.get("types", {}).get(shift.get("assignment_type_id"))
    if typ is None:
        # A reduced/stale snapshot cannot prove qualification. Keep the candidate
        # out of the eligible set instead of turning the missing lookup into a 500.
        return [{"code": "ELIGIBILITY_MISSING", "person_id": person_id,
                 "service_id": None, "existing_record_ids": []}]
    dept = typ["department_id"]
    memberships = department_memberships(data, person_id, dept, a, b)
    findings = []
    if not memberships:
        findings.append({"code": "MEMBERSHIP_MISSING", "person_id": person_id,
                         "department_id": dept})
    allowed = definition.get("allowed_category_ids", [])
    if allowed and not any(m["category_id"] in allowed for m in memberships):
        findings.append({"code": "RANK_NOT_ALLOWED", "person_id": person_id,
                         "allowed_category_ids": list(allowed)})
    for service_id in shift["service_ids"]:
        if not any(e["service_id"] == service_id
                   and valid_dates(e, a, b, data["organisation"]["timezone"]) for e in person_rows(data, "eligibilities", person_id)):
            findings.append({"code": "ELIGIBILITY_MISSING", "person_id": person_id,
                             "service_id": service_id,
                             "existing_record_ids": [e["id"] for e in person_rows(data, "eligibilities", person_id)
                                 if e["service_id"] == service_id]})
    return findings


def _qualification_key(data, person_id, shift, definition, a, b):
    typ = data.get('types', {}).get(shift.get('assignment_type_id'))
    return (person_id, typ.get('department_id') if typ else None,
            tuple(shift['service_ids']), tuple(definition.get('allowed_category_ids', [])), a, b)


def qualified(data, person_id, shift, definition, a, b):
    cache = data.get('_qualification_cache')
    key = _qualification_key(data, person_id, shift, definition, a, b)
    failures_cache = data.get('_qualification_failures_cache')
    if failures_cache is not None:
        failures = failures_cache.get(key)
        if failures is None:
            failures = tuple(qualification_failures(data, person_id, shift, definition, a, b))
            failures_cache[key] = failures
        return not failures
    if cache is None:
        return not qualification_failures(data, person_id, shift, definition, a, b)
    if key not in cache:
        cache[key] = not qualification_failures(data, person_id, shift, definition, a, b)
    return cache[key]


def ancestor_ids(data, location_id):
    ids, current = [], location_id
    while current and current not in ids:
        ids.append(current)
        current = data["locations"].get(current, {}).get("parent_id")
    return ids


def named_people(scope):
    """The only people a scope can ever match (its person field, or a "Person is any of" line every match needs),
    or None when it can match anyone. A cheap first test: department-sized rota checks ask about every rule x person
    x session, and most rules name one person. ALL and one-condition ANY groups preserve necessary predicates."""
    named = person_scope_ids(scope)
    found = set(named) if named else None
    for values in necessary_person_values(scope.get('conditions')):
        found = set(values) if found is None else found & set(values)
    return found


def person_free(scope):
    """Nothing in the scope looks at the person: whether it matches depends only on the session and the time."""
    from .conditions import has_person_lines
    return not any(scope.get(k) for k in PERSON_SCOPE_KEYS) and not has_person_lines(scope_tree(scope))


def _policy_scope_key(policy):
    return (policy['status'], policy.get('active', True), policy['department_id'],
            policy.get('starts_on'), policy.get('ends_on'),
            json.dumps(policy['definition']['scope'], sort_keys=True, default=str))


def policy_applies(data, policy, person_id, shift, a, b):
    cache = data.get('_policy_applies_cache')
    if cache is None:
        return _policy_applies(data, policy, person_id, shift, a, b)
    # Only immutable solver preparation opts in: a rule that does not look at the person has one answer per
    # session and time for everybody. The entry holds the policy itself, so its id cannot be reused meanwhile.
    entry = cache.get(id(policy))
    if entry is None or entry[0] is not policy:
        scope = policy['definition']['scope']
        # Matching ignores names, strength and rule numbers. Equal immutable matching inputs share one answer
        # while every policy retains its own identity entry and the canonical predicate below remains authoritative.
        signature = ('policy_scope', *_policy_scope_key(policy))
        entry = cache[id(policy)] = (policy, person_free(scope), cache.setdefault(signature, {}))
    # A scheduling run (generation.py) also keeps answers per person: it asks about the same people on the same sessions
    # at every trial. The solver asks about everyone everywhere once, where keeping them would only cost memory.
    if not entry[1] and not data.get('_policy_applies_people'):
        return _policy_applies(data, policy, person_id, shift, a, b)
    key = (shift['id'], a, b, person_id is None) if entry[1] else (shift['id'], a, b, person_id)
    if key not in entry[2]:
        entry[2][key] = _policy_applies(data, policy, person_id, shift, a, b)
    return entry[2][key]


def _policy_applies(data, policy, person_id, shift, a, b):
    if policy["status"] != "active":
        return False
    if person_id is not None:
        named = named_people(policy["definition"]["scope"])
        if named is not None and person_id not in named:
            return False
    if not within_dates(policy, a, b, data["organisation"]["timezone"]):
        return False
    if policy["department_id"] != data["types"][shift["assignment_type_id"]]["department_id"]:
        return False
    scope = policy["definition"]["scope"]
    a = max(a, _midnight(policy['starts_on'], data['organisation']['timezone'])) if policy.get('starts_on') else a
    b = min(b, _midnight(policy['ends_on'], data['organisation']['timezone'])) if policy.get('ends_on') else b
    return scope_applies(data, scope, person_id, shift, a, b)


def service_scope_matches(scope, shift):
    selected = scope.get('service_ids') or ([scope['service_id']] if scope.get('service_id') else [])
    return not selected or bool(set(selected).intersection(shift['service_ids']))


def person_scope_ids(scope):
    """The people a rule names: one person, a list of people, or nobody (everyone)."""
    return scope.get('person_ids') or ([scope['person_id']] if scope.get('person_id') else [])


def category_scope_ids(scope):
    return scope.get('category_ids') or ([scope['category_id']] if scope.get('category_id') else [])


def session_scope(scope):
    """The part of a scope that selects sessions: people fields dropped, person condition lines left to count as met."""
    return {k: v for k, v in scope.items() if k not in PERSON_SCOPE_KEYS}


def session_matches(data, scope, shift):
    """Clinic, place, duty type and the session lines of the conditions (no people, no clock)."""
    return (service_scope_matches(scope, shift)
            and (not scope.get("location_id") or scope["location_id"] in ancestor_ids(data, shift["location_id"]))
            and (not scope.get("assignment_type_id") or scope["assignment_type_id"] == shift["assignment_type_id"])
            and (not has_session_lines(scope_tree(scope)) or conditions_match(data, scope_tree(scope), None, shift, None, None)))


def scope_matches(data, scope, person_id, shift, a, b):
    """Compatibility entry point for strict interval membership; obligation callers use scope_applies."""
    return scope_matches_throughout(data, scope, person_id, shift, a, b)


def person_in_scope(data, scope, person_id, a, b):
    """Compatibility entry point for strict person membership; reach callers use scope_applies."""
    return scope_matches_throughout(data, scope, person_id, None, a, b)


ALL_DAYS = frozenset(range(7))


def every_day_all_day(scope):
    """Every day, all day, no programme or rotation: any non-empty interval matches."""
    return (not scope.get('programme_id') and not scope.get('rotation_type_id')
            and len(scope.get('weekdays', ALL_DAYS)) == 7 and set(scope.get('weekdays', ALL_DAYS)) == ALL_DAYS
            and scope.get('start_time', '00:00') == '00:00' and scope.get('end_time', '24:00') == '24:00')


def scope_intervals(data, scope, person_id, a, b):
    """Intersect selected local time windows with dated programme membership."""
    cache = data.get('_scope_interval_cache')
    if cache is None:
        return _scope_intervals(data, scope, person_id, a, b)
    index = data.get('_scope_membership_index')
    if index is not None and any(scope.get(field) and not index.get((field, person_id, scope[field]))
                                 for field in ('programme_id', 'rotation_type_id')):
        return []
    # Only immutable solver preparation opts in. The cache belongs to that
    # snapshot, never to a person, request, database session or later edit.
    key = (tuple(scope.get('weekdays', range(7))), scope.get('start_time', '00:00'),
           scope.get('end_time', '24:00'), scope.get('programme_id'), scope.get('rotation_type_id'),
           person_id if scope.get('programme_id') or scope.get('rotation_type_id') else None, a, b)
    if key not in cache:
        result = _scope_intervals(data, scope, person_id, a, b)
        if len(cache) >= 65_536:
            return result
        cache[key] = tuple(result)
    return list(cache[key])


def _scope_intervals(data, scope, person_id, a, b):
    rule = {
        "weekdays": scope.get("weekdays", list(range(7))),
        "start_time": scope.get("start_time", "00:00"), "end_time": scope.get("end_time", "24:00")}
    # Different programme/person scopes share the same clocks before their membership intersections.
    cache = data.get('_resolver_clock_cache', data.get('_scope_interval_cache'))
    zone = data['organisation']['timezone']
    # Resolver trials share only clock preparation, never membership intersections or rule answers.
    key = ('clocks', zone, tuple(rule['weekdays']), rule['start_time'], rule['end_time'],
           a.astimezone(timezone.utc), b.astimezone(timezone.utc))
    windows = cache.get(key) if cache is not None else None
    if windows is None:
        windows = restricted_intervals(data, {'definition': {'rule': rule}}, a, b)
        if cache is not None and len(cache) < 65_536:
            cache[key] = tuple(windows)
    windows = list(windows)
    for key, definitions, memberships in (
            ('programme_id', 'programmes', 'programme_memberships'),
            ('rotation_type_id', 'rotation_types', 'rotations')):
        selected = scope.get(key)
        if not selected:
            continue
        definition = data.get(definitions, {}).get(selected)
        if not definition or not definition.get('active', True):
            return []
        result = []
        index = data.get('_scope_membership_index')
        for member in index.get((key, person_id, selected), []) if index is not None else person_rows(data, memberships, person_id):
            if member['person_id'] != person_id or member.get(key) != selected:
                continue
            parents = [definition]
            if key == 'rotation_type_id':
                cohort = data.get('cohorts', {}).get(member.get('cohort_id'))
                if cohort is None:
                    continue
                parents.append(cohort)
                if cohort.get('programme_id'):
                    parents.append(data.get('programmes', {}).get(cohort['programme_id'], {'active': False}))
            if any(not parent.get('active', True) for parent in parents):
                continue
            start = max([local_midnight(date.fromisoformat(member['starts_on']), zone)] +
                        [local_midnight(date.fromisoformat(parent['starts_on']), zone)
                         for parent in parents if parent.get('starts_on')])
            end = local_midnight(date.fromisoformat(member['ends_on']), zone)
            for parent in parents:
                if parent.get('ends_on'):
                    end = min(end, local_midnight(date.fromisoformat(parent['ends_on']), zone))
            result.extend((max(lo, start), min(hi, end)) for lo, hi in windows if overlaps(lo, hi, start, end))
        windows = result
    return windows


def rotation_block_rows(data, pid, a, b):
    return tuple(r for r in person_rows(data, "rotations", pid)
                 if not r.get("department_available", True)
                 and within_dates(r, a, b, data["organisation"]["timezone"]))


def rotation_blocks(data, pid, a, b):
    return bool(rotation_block_rows(data, pid, a, b))


def available_for_coverage(data, pid, a, b):
    cache = data.get('_coverage_availability_cache')
    key = pid, a, b
    if cache is not None and key in cache:
        return not cache[key]
    facts = []
    for row in rotation_block_rows(data, pid, a, b):
        facts.append((('code', 'EXTERNAL_ROTATION'), ('record_id', row.get('id')),
                      ('starts_on', row['starts_on']), ('ends_on', row.get('ends_on'))))
    for row in person_rows(data, 'availability', pid):
        if overlaps(a, b, aware(row['start']), aware(row['end'])):
            facts.append((('code', 'UNAVAILABLE'), ('record_id', row['id']),
                          ('start', row['start']), ('end', row['end'])))
    facts = tuple(facts)
    if cache is not None:
        cache[key] = facts
    return not facts


ROTATION_LEAVE_KINDS = frozenset({
    'leave', 'recorded_leave', 'approved_leave', 'actual_absence', 'external_commitment', 'off_duty',
})


def rotation_leave_findings(data):
    """The same dated rotation/leave rule for timeline previews, writes and rota checks."""
    zone = data['organisation']['timezone']
    away = defaultdict(list)
    for row in data.get('rotation_leave_availability', data.get('availability', [])):
        if row.get('retracted') or row.get('kind') not in ROTATION_LEAVE_KINDS:
            continue
        away[row['person_id']].append(row)
    workspace = data.get('workspace')
    window = ([date.fromisoformat(str(workspace[key])) for key in ('starts_on', 'ends_on')]
              if workspace else None)
    findings = []
    for rotation in data.get('rotations', []):
        absences = away.get(rotation['person_id'], [])
        if not absences:
            continue
        typ = data.get('rotation_types', {}).get(rotation.get('rotation_type_id'))
        if typ is None:
            continue
        policy = typ.get('leave_policy', 'not_allowed')
        if policy == 'allowed':
            continue
        first, last = (date.fromisoformat(str(rotation[key])) for key in ('starts_on', 'ends_on'))
        if window is not None:
            first, last = max(first, window[0]), min(last, window[1])
        if last <= first:
            continue
        a, b = local_midnight(first, zone), local_midnight(last, zone)
        for absence in absences:
            start, end = max(a, aware(absence['start'])), min(b, aware(absence['end']))
            if end <= start:
                continue
            approval = policy == 'needs_approval'
            detail = {'rotation_id': rotation.get('id'), 'rotation_type_id': rotation.get('rotation_type_id'),
                      'leave_policy': policy,
                      'request_id' if absence['kind'] == 'leave' else 'availability_id': absence.get('id')}
            findings.append({'code': 'ROTATION_LEAVE_NEEDS_APPROVAL' if approval else 'ROTATION_LEAVE_NOT_ALLOWED',
                'message': ('Leave or time away during this rotation requires approval.' if approval
                            else 'Leave or time away is not allowed during this rotation.'),
                'severity': 'warning' if approval else 'error', 'shift_id': None,
                'person_id': rotation['person_id'], 'start': start.isoformat(), 'end': end.isoformat(),
                'rule_id': None, 'detail': detail, 'outside_rota': False})
    return findings


def exception_for(data, policy, person_id, a, b):
    if safety_rule(policy['definition']) or not policy["definition"].get("exception_allowed"):
        return None
    for exc in data["exceptions"]:
        if (exc["status"] == "approved" and exc["policy_id"] == policy["id"]
                and exc["policy_revision"] == policy["revision"] and exc["person_id"] == person_id
                and valid_dates(exc, a, b, data["organisation"]["timezone"])):
            return exc
    return None


def restricted_intervals(data, policy, start, end, person_id=None):
    # Derived event facts retain exact instants (including overnight/DST), through the ordinary restriction path.
    if interval := policy.get('_protected_interval'):
        a, b = max(aware(interval['start']), start), min(aware(interval['end']), end)
        return [(a, b)] if a < b else []
    zone = data["organisation"]["timezone"]
    rule = policy["definition"]["rule"]
    buffer_days = rule.get('leave_buffer_days')
    # Rule clocks are shared by every candidate; leave buffers still depend on that person's records.
    cache = data.get('_resolver_clock_cache', data.get('_scope_interval_cache')) if not buffer_days else None
    if cache is not None:
        key = ('restriction', zone, tuple(rule['weekdays']), rule['start_time'], rule['end_time'],
               policy.get('starts_on'), policy.get('ends_on'),
               start.astimezone(timezone.utc), end.astimezone(timezone.utc))
        if key in cache:
            return list(cache[key])
    first = start.astimezone(ZoneInfo(zone)).date() - timedelta(days=1)
    last = end.astimezone(ZoneInfo(zone)).date()
    leave_dates = [(aware(r['start']).astimezone(ZoneInfo(zone)).date(),
                   (aware(r['end']) - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date())
                  for r in person_rows(data, 'availability', person_id)
                  if r['kind'] in {'approved_leave', 'recorded_leave'}] if buffer_days else []
    found = []
    for n in range((last - first).days + 1):
        day = first + timedelta(days=n)
        if day.weekday() not in rule["weekdays"]:
            continue
        if buffer_days and not any(first_leave - timedelta(days=buffer_days) <= day <= last_leave + timedelta(days=buffer_days) for first_leave, last_leave in leave_dates):
            continue
        a = local_stamp(day, rule["start_time"], zone)
        end_day = day + timedelta(days=1) if rule["end_time"] != "24:00" and rule["end_time"] <= rule["start_time"] else day
        b = local_stamp(end_day, rule["end_time"], zone)
        if policy.get("starts_on"):
            a = max(a, local_midnight(date.fromisoformat(policy["starts_on"]), zone))
        if policy.get("ends_on"):
            b = min(b, local_midnight(date.fromisoformat(policy["ends_on"]), zone))
        if a < b and overlaps(a, b, start, end):
            found.append((max(a, start), min(b, end)))
    if cache is not None and len(cache) < 65_536:
        cache[key] = tuple(found)
    return found


def applicable_restriction_intervals(data, policy, pid, shift, a, b):
    """Restricted time where the complete scope holds, split at dated and clock boundaries."""
    interval_cache = data.get('_restriction_interval_cache')
    cache_key = (id(policy), pid, id(shift), a, b)
    if interval_cache is not None:
        cached = interval_cache.get(cache_key)
        if cached is not None:
            yield from cached[2]
            return
    scope = policy['definition']['scope']
    named = named_people(scope)
    if named is not None and pid not in named:
        return
    tree = scope_tree(scope)
    dated = has_dated_person_lines(tree)
    points = set(change_points(data, pid, a, b)) if (
        dated or category_scope_ids(scope)
        or scope.get('programme_id') or scope.get('rotation_type_id')) else set()
    zone = data['organisation']['timezone']
    points.update(_midnight(source[field], zone) for source in [policy, *(selector_sources(data['policies'], tree, data.get('_line_index_cache')) if tree else [])]
                  for field in ('starts_on', 'ends_on') if source.get(field))
    if (tree and not dated and not any(a < t < b for t in points)
            and not conditions_match(data, tree, pid, shift, a, b)):
        if interval_cache is not None:
            interval_cache[cache_key] = (policy, shift, ())
        return  # A selector can change at its source's effective-date boundary.
    found = []
    for lo, hi in restricted_intervals(data, policy, a, b, pid):
        intervals = []
        for x, y in scope_intervals(data, scope, pid, lo, hi):
            cuts = sorted({x, y} | {t for t in points if x < t < y})
            intervals.extend((c, d) for c, d in zip(cuts, cuts[1:])
                             if policy_applies(data, policy, pid, shift, c, d))
        # Unrelated dated facts and overlapping membership rows must not multiply one restriction breach.
        merged = []
        for x, y in sorted(intervals):
            if merged and x <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], y))
            else:
                merged.append((x, y))
        found.extend(merged)
    if interval_cache is not None and len(interval_cache) < 65_536:
        interval_cache[cache_key] = (policy, shift, tuple(found))
    yield from found


def blocking_restrictions(data, pid, shift, a, b):
    policies = data['policies']
    cache = data.get('_restriction_index_cache')
    if cache is None and data.get('_line_index_cache') is not None:
        cache = data['_line_index_cache'].setdefault('blocking_restrictions', {})
    if cache is not None:
        entry = cache.get(id(policies))
        if entry is None or entry[0] is not policies or entry[1] != len(policies):
            everyone, people, restrictions = [], defaultdict(list), []
            for n, policy in enumerate(policies):
                if (policy['status'] != 'active' or not policy.get('active', True)
                        or policy['definition']['rule']['kind'] != 'restriction'
                        or policy['definition']['severity'] != 'mandatory'):
                    continue
                restrictions.append(policy)
                named = named_people(policy['definition']['scope'])
                if named is None:
                    everyone.append((n, policy))
                else:
                    for person in named:
                        people[person].append((n, policy))
            entry = cache[id(policies)] = (policies, len(policies), everyone, people, {}, restrictions, {})
        if pid not in entry[4]:
            entry[4][pid] = [policy for _, policy in sorted(entry[2] + entry[3].get(pid, []))]
        policies = entry[4][pid]
        if shift is not None and not shift.get('_date_projection'):
            key = (id(shift), a, b)
            selected = entry[6].get(key)
            if selected is None or selected[0] is not shift:
                department = data['types'][shift['assignment_type_id']]['department_id']
                zone = data['organisation']['timezone']
                selected = (shift, {id(policy) for policy in entry[5]
                                   if policy['department_id'] == department and within_dates(policy, a, b, zone)
                                   and session_matches(data, policy['definition']['scope'], shift)})
                if len(entry[6]) < 65_536:
                    entry[6][key] = selected
            policies = [policy for policy in policies if id(policy) in selected[1]]
    for policy in policies:
        if (policy["definition"]["rule"]["kind"] == "restriction"
                and policy["definition"]["severity"] == "mandatory"
                and any(not exception_for(data, policy, pid, x, y)
                        for x, y in applicable_restriction_intervals(data, policy, pid, shift, a, b))):
            yield policy


def restriction_blocks(data, pid, shift, a, b):
    return next(blocking_restrictions(data, pid, shift, a, b), None) is not None


def matching_size(seats: list[str], capabilities: dict[str, set[str]]) -> int:
    """Bipartite matching: one distinct person per requested capability seat."""
    matched: dict[str, int] = {}

    def assign(seat_index, seen):
        for person, caps in capabilities.items():
            if seats[seat_index] not in caps or person in seen:
                continue
            seen.add(person)
            if person not in matched or assign(matched[person], seen):
                matched[person] = seat_index
                return True
        return False
    return sum(assign(i, set()) for i in range(len(seats)))


def counting_policy(policy):
    """Validity selects focal duties, never the work in a run or an independent counting window."""
    if policy['definition'].get('rule', {}).get('kind') in VALIDITY_FOCAL_ONLY_KINDS:
        return {**policy, 'starts_on': None, 'ends_on': None}
    return policy  # A block_quota's dates still define its counting block.


def workload_matches(data, policy, person_id, shift, a, b):
    """Known person scope selects institutional work without filtering out other departments."""
    # Greedy trials change participants, while these dated scope facts stay fixed for the run.
    cache = data.get('_workload_matches_cache')
    scope = policy['definition']['scope']
    if cache is not None:
        entry = cache.get(id(policy))
        if entry is None or entry[0] is not policy:
            entry = cache[id(policy)] = (policy, person_free(scope), {})
        key = (None if entry[1] else person_id, id(shift), a, b)
        cached = entry[2].get(key)
        if cached is not None and cached[0] is shift:
            return cached[1]
    counted = counting_policy(policy)
    zone = data['organisation']['timezone']
    lo = max(a, _midnight(counted['starts_on'], zone)) if counted.get('starts_on') else a
    hi = min(b, _midnight(counted['ends_on'], zone)) if counted.get('ends_on') else b
    matched = within_dates(counted, a, b, zone) and lo < hi and scope_applies(data, scope, person_id, shift, lo, hi)
    if cache is not None:
        entry[2][key] = (shift, matched)
    return matched


def workload_windows(a, b, rule, zone, week_start):
    """Canonical limit windows contributed by one reserved duty."""
    return _workload_windows(aware(a), aware(b), rule['window'], rule.get('window_days'), zone, week_start)


def _focal_workload_windows(data, rule, selected, focal, zone):
    windows = {window for _, _, a, b in selected
               for window in workload_windows(a, b, rule, zone, data['organisation']['week_start'])}
    return ((a, b) for a, b in sorted(windows) if any(overlaps(a, b, c, d) for _, _, c, d in focal))


@lru_cache(maxsize=8192)
def _workload_windows(a, b, window, window_days, zone, week_start):
    if window == 'rolling':
        width = timedelta(days=window_days)
        return frozenset({(a, a + width), (b - width, b)})
    windows = set()
    day = a.astimezone(ZoneInfo(zone)).date()
    last_day = (b - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
    while day <= last_day:
        first = day if window == 'day' else day - timedelta(days=(day.weekday() - week_start) % 7)
        width_days = 1 if window == 'day' else 7
        windows.add((local_midnight(first, zone), local_midnight(first + timedelta(days=width_days), zone)))
        day += timedelta(days=1)
    return frozenset(windows)


def resource_sessions(data, shifts, resource_id, a, b):
    """Sessions held at a location or under it that overlap a..b. Indexed by start once per check: a rota check asks
    for every session, and scanning the whole rota each time made department-sized checks quadratic."""
    shifts = data.get('_resource_shifts', shifts)
    cached = data.get('_resource_sessions')
    if cached is None or cached[0] is not shifts:
        index = defaultdict(list)
        for t in shifts:
            for rid in ancestor_ids(data, t['location_id']):
                index[rid].append((aware(t['start']), aware(t['end']), t))
        for rows in index.values():
            rows.sort(key=lambda row: row[0])
        longest = {rid: max(e - s for s, e, _ in rows) for rid, rows in index.items()}
        cached = data['_resource_sessions'] = (shifts, index, {rid: [row[0] for row in rows] for rid, rows in index.items()}, longest)
    _, index, starts, longest = cached
    if resource_id not in index:
        return []
    rows = index[resource_id]
    lo = bisect_right(starts[resource_id], a - longest[resource_id])
    hi = bisect_left(starts[resource_id], b)
    return [t for c, d, t in rows[lo:hi] if a < d]


def location_type_allowed(definition, location):
    allowed = definition.get("allowed_location_type_ids") or []
    return not allowed or location["location_type_id"] in allowed


def location_validity_findings(data, location_id, start, end):
    """Dated location type and ancestor checks shared with overtime eligibility."""
    zone = data["organisation"]["timezone"]
    loc = data["locations"][location_id]
    if not valid_dates(data["location_types"][loc["location_type_id"]], start, end, zone):
        yield "LOCATION_TYPE_INACTIVE", "Location type is not active for the session interval"
    for resource_id in ancestor_ids(data, location_id):
        if not valid_dates(data["locations"][resource_id], start, end, zone):
            yield "LOCATION_INACTIVE", "A location or parent resource is inactive"


def validate_session_setup(data, shift, shifts, definition, add):
    """Assignment-independent checks shared by validation and solver preflight."""
    zone = data['organisation']['timezone']
    focus = set(data['focus_ids'])
    sid, start, end = shift['id'], aware(shift['start']), aware(shift['end'])
    typ = data["types"][shift["assignment_type_id"]]
    published = data.get('operational') and _published_session(data, shift)
    draft_safety, live_safety = _draft_type_safety(data, shift)
    if draft_safety is not None:
        authored = typ["definition"] if data.get("operational") else shift["type_snapshot"]["definition"]
    else:
        authored = definition
    if not valid_dates(shift['type_snapshot'] if published else typ, start, end, zone):
        add("TYPE_INACTIVE", "Assignment type is inactive or outside its effective interval", sid)
    # Different supervision scopes cannot be represented by one duty-type slot.
    if draft_safety and len({_supervision_scope(source["supervision"]) for source in
                             (shift["type_snapshot"]["definition"], draft_safety.get("captured"), live_safety)
                             if source and source.get("supervision")}) > 1:
        add("TYPE_VERSION_CHANGED", "Supervision scope changed; review and refresh the draft duty inputs", sid)
    elif (sid not in data.get("_issued_type_versions", ()) and shift["type_revision"] != typ["revision"]
            and not display_only_change(shift.get("type_snapshot"), typ, live_staffing=draft_safety is not None)
            and not published):
        add("TYPE_VERSION_CHANGED", "Type changed; explicitly refresh the draft definition and review its impact", sid,
            severity="error")
    if sid in focus and not configured_duration_matches(definition, start, end, zone):
        add('CONFIGURED_DURATION_REQUIRED', 'Book the complete configured duration; configure a shorter duty type instead', sid)
    if sid in focus and definition.get('session_unit_basis', 'fixed') != 'fixed':
        add('FIXED_SESSION_UNITS_REQUIRED', 'Update this duty type to count session units per complete assignment', sid)
    if duration_hours(start, end) * 60 > definition.get(
            "maximum_duration_minutes", TypeDefinition.model_fields["maximum_duration_minutes"].default):
        add("DURATION_LIMIT", "Session exceeds the assignment type's maximum duration", sid)
    try:
        # Independently valid limits can conflict across versions. Validate
        # attributes against the authored schema.
        validate_attributes(TypeDefinition.model_validate(authored), shift["attributes"])
    except ValueError as exc:
        add("CUSTOM_FIELD_INVALID", str(exc), sid)
    if definition["purpose"] == "staffed" and not shift["service_ids"]:
        add("SERVICE_REQUIRED", "Staffed duties require a service", sid)
    if len(shift["service_ids"]) > 1 and not definition["allow_shared_services"]:
        add("SHARED_DUTY_UNAPPROVED", "This type does not support a reviewed common duty", sid)
    for service_id in shift["service_ids"]:
        if service_id not in data["services"] or not valid_dates(data["services"][service_id], start, end, zone):
            add("SERVICE_INACTIVE", "A supported service is inactive during this session", sid)
    loc = data["locations"][shift["location_id"]]
    if not location_type_allowed(definition, loc):
        add("LOCATION_TYPE", "Location type is not permitted for this assignment type", sid)
    for code, message in location_validity_findings(data, loc["id"], start, end):
        add(code, message, sid)
    for resource_id in ancestor_ids(data, loc["id"]):
        resource = data["locations"][resource_id]
        for closure in data["closures"]:
            if closure["location_id"] == resource_id and overlaps(start, end, aware(closure["start"]), aware(closure["end"])):
                add("LOCATION_CLOSED", "A location or parent resource is closed", sid,
                    a=max(start, aware(closure["start"])), b=min(end, aware(closure["end"])))
        consuming = resource_sessions(data, shifts, resource_id, start, end)
        if len(consuming) <= resource["capacity"]:
            continue
        resource_bounds = sorted({start, end} | {max(start, aware(t["start"])) for t in consuming}
                                 | {min(end, aware(t["end"])) for t in consuming})
        for a, b in zip(resource_bounds, resource_bounds[1:]):
            count = sum(overlaps(a, b, aware(t["start"]), aware(t["end"])) for t in consuming)
            if count > resource["capacity"]:
                add("RESOURCE_CAPACITY", "Concurrent sessions exceed the location or shared parent capacity", sid,
                    a=a, b=b, extra={"scheduled_sessions": count, "capacity": resource["capacity"],
                                     "resource_id": resource_id,
                                     "published_sessions": sum(t["id"] not in focus and overlaps(a, b, aware(t["start"]), aware(t["end"])) for t in consuming)})



def coverage_boundary_inputs(data):
    """Snapshot-wide clocks and dated authority, prepared once for coverage consumers."""
    zone = data['organisation']['timezone']
    # Coverage boundaries depend on clocks, not the employee/service selectors.
    # Many scoped rules share the same clocks; split each distinct window once.
    # Stored catalogue/fact envelopes can omit scope; their resolved lines own the selectors.
    scopes = (p['definition'].get('scope') or {} for p in data['policies'])
    boundary_clocks = {(tuple(scope.get('weekdays', range(7))), scope.get('start_time', '00:00'),
                        scope.get('end_time', '24:00')) for scope in scopes}
    # These dates are snapshot-wide, not shift-specific. Convert each distinct
    # boundary once while retaining every membership/authority transition.
    dated_rows = (data["memberships"], data.get("programme_memberships", []), list(data.get("programmes", {}).values()), list(data.get("cohorts", {}).values()), list(data.get("rotation_types", {}).values()), data["policies"], data.get("rotations", []), data["eligibilities"], data["grants"], list(data["categories"].values()), list(data["capabilities"].values()))
    authority_bounds = {local_midnight(date.fromisoformat(value), zone)
        for value in {r[field] for rows in dated_rows for r in rows
                      for field in ("starts_on", "ends_on") if r.get(field)}}
    return authority_bounds, boundary_clocks


def coverage_boundaries(data, shift, a, b, *, presence=None, inputs=None):
    """Shared cuts for factual presence, availability, qualification and leadership counts."""
    a, b = aware(a), aware(b)
    authority, clocks = coverage_boundary_inputs(data) if inputs is None else inputs
    points = {a, b, *(t for t in authority if a < t < b)}
    start = aware(shift['start'])
    for req in effective_type_definition(data, shift)['requirements']:
        points.update(t for t in (start + timedelta(minutes=req['from_minute']),
            start + timedelta(minutes=req['to_minute']) if req.get('to_minute') is not None else aware(shift['end'])) if a < t < b)
    for participant in shift['participants']:
        pid = participant['person_id']
        points.update(t for t in (aware(participant['start']), aware(participant['end'])) if a < t < b)
        if presence is None:
            try:
                intervals = presence_intervals(participant)
            except ValueError:
                intervals = []
        else:
            intervals = presence[(shift['id'], pid)]
        points.update(t for pair in intervals for t in pair if a < t < b)
        for absence in person_rows(data, 'availability', pid):
            points.update(t for t in (aware(absence['start']), aware(absence['end'])) if a < t < b)
        for policy in blocking_restrictions(data, pid, shift, a, b):
            points.update(t for pair in applicable_restriction_intervals(data, policy, pid, shift, a, b)
                          for t in pair if a < t < b)
    for weekdays, start_time, end_time in clocks:
        for lo, hi in scope_intervals(data, {'weekdays': weekdays, 'start_time': start_time, 'end_time': end_time}, None, a, b):
            points.update((lo, hi))
    points.update(t for lo, hi, _ in leadership.segments(data, shift, a, b) for t in (lo, hi))
    return points


def rule_coverage_people(data, shift, a, b, *, presence=None, definition=None):
    """Present, eligible and available people, shared by staffing validation and rule reach."""
    definition = effective_type_definition(data, shift) if definition is None else definition
    def intervals(p):
        if presence is not None:
            return presence[(shift['id'], p['person_id'])]
        try:
            return presence_intervals(p)
        except ValueError:
            return []
    return [p for p in shift['participants']
        if any(lo <= a and b <= hi for lo, hi in intervals(p))
        and available_for_coverage(data, p['person_id'], a, b)
        and qualified(data, p['person_id'], shift, definition, a, b)
        and not restriction_blocks(data, p['person_id'], shift, a, b)]


def staffing_peers(data, policy, a, b, *, shifts=None):
    """Overlapping sessions selected by a staffing line, shared by validation and reach."""
    selector = session_scope(policy['definition']['scope'])
    return [s for s in (data['shifts'] if shifts is None else shifts) if not inactive(s)
        and data['types'][s['assignment_type_id']]['department_id'] == policy['department_id']
        and aware(s['start']) < b and a < aware(s['end']) and scope_matches_throughout(data, selector, None, s, a, b)]


def staffing_credit(data, policy, shift, a, b, choices, *, conditional_credit=None):
    """Qualified contributions on one duty; conditional people need independently qualifying companions.

    The compiler supplies only the Boolean translation of conditional credit; population selection and the
    same-duty, non-circular counting semantics stay here for validation, greedy and CP-SAT.
    """
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    triggers = set(rule.get('when_person_ids', []))
    eligible = {pid: choice for pid, choice in choices.items() if pid not in triggers
                and scope_matches_throughout(data, scope, pid, shift, a, b)}
    if rule.get('selection_order'):
        from .staffing_selection import selected_tier
        tier = selected_tier(data, policy, shift, a, b)
        if tier < 0:
            return {}
        conditions = rule['selection_order'][tier]
        eligible = {pid: choice for pid, choice in eligible.items()
                    if scope_matches_throughout(data, {'conditions': conditions}, pid, shift, a, b)}
    conditions, minimum = rule.get('credit_conditions'), rule.get('credit_minimum_others', 0)
    if not conditions or not minimum:
        return eligible
    conditional = {pid: choice for pid, choice in eligible.items()
                   if scope_matches_throughout(data, {'conditions': conditions}, pid, shift, a, b)}
    independent = {pid: choice for pid, choice in eligible.items() if pid not in conditional}
    if not conditional or len(independent) < minimum:
        return independent
    others = sum(independent.values())
    if conditional_credit is not None:
        return independent | conditional_credit(conditional, others, minimum)
    return eligible if others >= minimum else independent


def staffing_counts(data, policy, shift, a, b, *, peers=None, presence=None, available=None):
    """One line's distinct-person staffing observations, including triggers and permitted overflow."""
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    pooled = rule.get('aggregation', 'assignment') != 'assignment'
    peers = (staffing_peers(data, policy, a, b) if peers is None else peers) if pooled else [shift]

    def present(peer):
        return [p for p in peer['participants'] if any(lo <= a and b <= hi for lo, hi in (
            presence[(peer['id'], p['person_id'])] if presence is not None else presence_intervals(p)))]

    if rule['kind'] == 'minimum_cover':
        triggers = set(rule.get('when_person_ids', []))
        if triggers and not triggers.intersection(p['person_id'] for peer in peers for p in present(peer)):
            return []
        people = {pid for peer in peers for pid in staffing_credit(data, policy, peer, a, b,
            {p['person_id']: 1 for p in (available if available is not None and peer['id'] == shift['id'] else
                rule_coverage_people(data, peer, a, b, presence=presence))})}
        focus = set(data['focus_ids']) if pooled else set()
        return [{'required': rule['minimum'], 'available': len(people),
                 **({'affected_shift_ids': sorted(s['id'] for s in peers if s['id'] in focus)} if pooled else {})}]

    groups = [(None, peers)]
    if rule.get('aggregation') == 'service':
        selected = scope.get('service_ids') or ([scope['service_id']] if scope.get('service_id') else shift['service_ids'])
        groups = [(service_id, [peer for peer in peers if service_id in peer['service_ids']])
                  for service_id in set(shift['service_ids']) & set(selected)]
    observations = []
    for service_id, group in groups:
        people = {p['person_id'] for peer in group for p in present(peer)
                  if scope_matches_throughout(data, scope, p['person_id'], peer, a, b)}
        detail = {'maximum': rule['maximum'], 'scheduled': len(people), **({'service_id': service_id} if service_id else {})}
        if rule.get('overflow_maximum', 0):
            ranks = set(rule['overflow_category_ids'])
            extra = min(rule['overflow_maximum'], sum(bool(ranks & categories_for(data, pid, a, b)) for pid in people))
            detail.update(permitted_extra=extra, permitted_maximum=rule['maximum'] + extra)
        observations.append(detail)
    return observations


def workload_actual(rule, selected, a, b, zone):
    """Reserved hours or distinct duties in a workload window, including the existing grouping modes."""
    if rule['kind'] == 'max_hours':
        return [(None, union_hours([(max(a, c), min(b, d)) for _, _, c, d in selected if overlaps(a, b, c, d)]))]
    grouped = defaultdict(set)
    for shift, p, c, _ in selected:
        if a <= c < b:
            mode = rule.get('group_by', 'all')
            key = c.astimezone(ZoneInfo(zone)).weekday() if mode == 'weekday' else p.get('supervisor_person_id') if mode == 'supervisor' else None
            if mode != 'supervisor' or key:
                grouped[key].add(shift['id'])
    return [(key, len(duties)) for key, duties in grouped.items()] or [(None, 0)]


def workload_day_groups(selected, zone):
    """Reserved duties on each touched local day, shared by concrete and symbolic run counting."""
    days = defaultdict(list)
    for row in selected:
        _, _, a, b, *_ = row
        day = a.astimezone(ZoneInfo(zone)).date()
        last = (b - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
        while day <= last:
            days[day].append(row)
            day += timedelta(days=1)
    return days


def consecutive_day_count(rule, total, ordinary, *, max_of=max):
    """Overtime-only days may extend a run, but never reset it or excuse extra ordinary days."""
    extension = rule.get('overtime_extension_days', 0)
    return max_of(ordinary, total - extension) if extension else total


def workload_day_observations(selected, zone):
    """Every consecutive-day prefix and its ordinary-day count (a mixed day is ordinary)."""
    days = workload_day_groups(selected, zone)
    run = []
    ordinary = 0
    for day in sorted(days):
        if not run or (day - run[-1]).days != 1:
            run, ordinary = [], 0
        run.append(day)
        ordinary += any(not row[1].get('overtime', False) for row in days[day])
        yield tuple(run), ordinary


def workload_day_runs(selected, zone):
    """Every consecutive-day prefix, in the same order as workload validation."""
    yield from (run for run, _ in workload_day_observations(selected, zone))


def workload_weekend_groups(data, policy, pid, selected):
    """One bucket per actual worked weekend, shared by validation and CP-SAT."""
    rule, scope, zone = policy['definition']['rule'], policy['definition']['scope'], data['organisation']['timezone']
    weekends = defaultdict(list)
    for row in selected:
        _, _, a, b, *_ = row
        for lo, hi in scope_intervals(data, scope, pid, a, b):
            day = lo.astimezone(ZoneInfo(zone)).date()
            last = (hi - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
            while day <= last:
                offset = (day.weekday() - rule['weekend_start']) % 7
                if offset < rule['weekend_days']:
                    first = day - timedelta(days=offset)
                    weekends[first].append(row)
                day += timedelta(days=1)
    return weekends


def workload_weekend_runs(data, policy, pid, selected, focus):
    """Every weekly prefix as (weekend date, overtime, focused) observations."""
    weekends = workload_weekend_groups(data, policy, pid, selected)
    run = []
    for first in sorted(weekends):
        rows = weekends[first]
        observation = (first, any(row[1].get('overtime', False) for row in rows), any(row[0]['id'] in focus for row in rows))
        run = run + [observation] if run and (first - run[-1][0]).days == 7 else [observation]
        yield tuple(run)


def workload_weekend_violations(data, policy, pid, selected, focus):
    """Each excess weekend window, with the same overtime and focus gates as validation."""
    rule, zone = policy['definition']['rule'], data['organisation']['timezone']
    for run in workload_weekend_runs(data, policy, pid, selected, focus):
        window = run[-(rule['maximum'] + 1):]
        if (len(window) > rule['maximum'] and any(focused for _, _, focused in window)
                and (not rule['overtime_only'] or any(overtime for _, overtime, _ in window))):
            yield (local_midnight(window[0][0], zone),
                   local_midnight(window[-1][0] + timedelta(days=rule['weekend_days']), zone), len(window))


def workload_predecessors(selected):
    """Start-ordered work paired with the latest occupied end; keep its source row."""
    previous = None
    for current in selected:
        if previous is not None:
            yield previous, current
        if previous is None or current[3] >= previous[3]:
            previous = current


def workload_gap_blockers(selected, index):
    """Optional rows that replace this source in the latest-end sweep (equal ends use the later row)."""
    end = selected[index][3]
    return [(i, row[4]) for i, row in enumerate(selected)
            if row[3] > end or i > index and row[3] == end]


def workload_gaps(data, rule, selected, targets):
    """Latest-end rest/travel gaps with the target, ancestor and unknown-place gates."""
    kind = rule['kind']
    required = rule['hours'] if kind == 'min_rest' else rule['minutes'] / 60
    for previous, current in workload_predecessors(selected):
        s, _, _, b = previous
        t, _, c, _ = current
        if s['id'] not in targets and t['id'] not in targets:
            continue
        unknown = kind == 'travel_buffer' and (not s['location_id'] or not t['location_id'])
        if kind == 'travel_buffer' and not unknown and not (
                rule['from_location_id'] in ancestor_ids(data, s['location_id'])
                and rule['to_location_id'] in ancestor_ids(data, t['location_id'])):
            continue
        yield {'previous': s, 'current': t, 'start': min(b, c), 'end': max(b, c),
               'actual_hours': duration_hours(b, c), 'required_hours': required, 'unknown_location': bool(unknown)}


def supervision_capacity(data, rule, paired, a, b, *, supervisor_id):
    """Named trainee load and permitted capacity, including explicitly configured rank-limited overflow."""
    selected = set(rule['trainee_category_ids'])
    counted = [p for p in paired if not selected or selected.intersection(categories_for(data, p['person_id'], a, b))]
    overflow_ranks = set(rule.get('overflow_category_ids', []))
    extra = min(rule.get('overflow_maximum', 0), sum(bool(overflow_ranks.intersection(
        categories_for(data, p['person_id'], a, b))) for p in counted))
    detail = {'maximum': rule['maximum'], 'scheduled': len(counted)}
    if rule.get('overflow_maximum', 0):
        detail.update(supervisor_person_id=supervisor_id, permitted_extra=extra, permitted_maximum=rule['maximum'] + extra)
    return detail


def supervision_counts(data, policy, shift, a, b, *, available=None):
    """Capacity observations for qualified supervisors reached by this rule on an interval."""
    definition = effective_type_definition(data, shift)
    rule, supervision = policy['definition']['rule'], definition.get('supervision')
    if not supervision or rule['mode'] != 'capacity':
        return []
    available = rule_coverage_people(data, shift, a, b, definition=definition) if available is None else available
    trainees = [p for p in available if is_trainee(data, supervision, p, a, b)]
    rows = []
    for supervisor in available:
        pid = supervisor['person_id']
        if qualified_supervisor(data, shift, supervisor, a, b, definition=definition) and policy_applies(data, policy, pid, shift, a, b):
            detail = supervision_capacity(data, rule, [p for p in trainees if p.get('supervisor_person_id') == pid], a, b,
                                          supervisor_id=pid)
            rows.append((pid, detail))
    return rows


def workload_selected(data, policy, pid, entries):
    """The counted institutional duties and external duties lacking the classification this rule needs."""
    rule, scope = policy['definition']['rule'], policy['definition']['scope']
    kind = rule['kind']
    selected = list(entries) if kind == 'min_rest' else [(s, p, a, b) for s, p, a, b in entries
        if workload_matches(data, policy, pid, s, a, b)
        and (rule.get('include_overtime', True) or not p.get('overtime', False))]
    unknown = []
    for r in person_rows(data, 'availability', pid):
        if r['kind'] != 'external_commitment':
            continue
        a, b = aware(r['start']), aware(r['end'])
        if kind != 'min_rest' and not workload_matches(data, policy, pid, None, a, b):
            continue
        if kind != 'min_rest' and (rule.get('overtime_only') or rule.get('group_by') == 'supervisor'
                or scope.get('service_id') or scope.get('service_ids') or scope.get('location_id')
                or scope.get('assignment_type_id')
                or scope.get('weekdays', list(range(7))) != list(range(7))
                or scope.get('start_time', '00:00') != '00:00' or scope.get('end_time', '24:00') != '24:00'
                or not rule.get('include_overtime', True) or has_session_lines(scope_tree(scope))):
            unknown.append(r)
        else:
            selected.append(({'id': 'external:' + r['id'], 'location_id': None, 'service_ids': []},
                             {'person_id': pid, 'overtime': False}, a, b))
    return sorted(selected, key=lambda row: (row[2], row[3])), unknown


def participation_start_day(participant, zone):
    """Next-day combinations use the person's participation start, in organisation time."""
    return aware(participant["start"]).astimezone(ZoneInfo(zone)).date()


def is_open_extra_cover(definition):
    """Only the duty type's headcount minimum may remain advisory."""
    return definition.get('extra_cover', False) and definition.get('extra_cover_unfilled', 'open') == 'open'


def check_contracts():
    """Contracts binding complete stored checks and their read projections."""
    from .quotas import METRIC_VERSION
    from .coverage import COVERAGE_CONTRACT
    return {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1', 'cover_places': COVERAGE_CONTRACT}


class TrialPresence(dict):
    """Load presence only for the checked duties and the peers their checks read."""

    def __init__(self, shifts):
        super().__init__()
        self.shifts = shifts

    def load(self, shift):
        invalid = []
        for person in shift['participants']:
            pid = person['person_id']
            try:
                intervals = presence_intervals(person)
            except ValueError:
                intervals = []
                invalid.append(pid)
            self[shift['id'], pid] = intervals
        return invalid

    def __missing__(self, key):
        self.load(self.shifts[key[0]])
        return dict.__getitem__(self, key)


def incremental_edit_scope(data, before, previous, rule_sources):
    """Reuse only a complete check of the exact pre-edit schedule and current non-edit inputs."""
    from .finding_causes import DIAGNOSTIC_VERSION
    from .input_manifest import FIELDS, manifest, project, shift_input
    from .validation_scope import assignment_scope

    workspace = data.get('workspace')
    prior = before.get('workspace')
    stamp = previous.get('input_manifest') or {}
    current = data.get('input_manifest') or {}
    prior_components = stamp.get('components') or {}
    prior_sources = (previous.get('fix_list_context') or {}).get('sources')
    if (not workspace or not prior or data.get('operational') or previous.get('scope')
            or previous.get('engine_version') != ENGINE_VERSION or previous.get('contracts') != check_contracts()
            or previous.get('diagnostic_version') != DIAGNOSTIC_VERSION or not previous.get('evaluated_at')
            or not isinstance(previous.get('_session_parts'), list) or not isinstance(previous.get('_person_parts'), dict)
            or project(prior, FIELDS['workspace']) != project(workspace, FIELDS['workspace'])
            or stamp.get('format') != current.get('format')
            or not prior_components or not current.get('components')
            or any(value != prior_components.get(key)
                   for key, value in current.get('components', {}).items() if key != 'content')
            or previous.get('fingerprint') != stamp.get('token')
            or prior_sources is None or list(prior_sources.items()) != list(rule_sources.items())):
        return None

    focus = set(data['focus_ids'])
    prior_ids = {shift['id'] for shift in before['shifts']}
    # A stored check without its coverage rows (older stored shape) cannot be merged; check in full.
    coverage = previous.get('coverage')
    if not isinstance(coverage, list) or {row['shift_id'] for row in coverage} != prior_ids:
        return None
    own = [shift for shift in data['shifts'] if shift['id'] in focus]
    others = [shift for shift in data['shifts'] if shift['id'] not in focus]
    old = with_series_state({**data, 'shifts': before['shifts'] + others,
                             'workspace': prior, 'focus_ids': prior_ids})
    if any(shift['assignment_type_id'] not in data['types'] for shift in old['shifts']):
        return None
    old_stamp = manifest(old)
    if any(stamp.get(key) != old_stamp[key] for key in ('token', 'components', 'dirty_hint')):
        return None

    was = {row['id']: shift_input(row) for row in old['shifts'] if row['id'] in prior_ids}
    now = {row['id']: shift_input(row) for row in own}
    touched = {sid for sid in was.keys() | now.keys() if was.get(sid) != now.get(sid)}
    changed_before = [row for row in old['shifts'] if row['id'] in touched]
    changed_after = [row for row in own if row['id'] in touched]
    scope = assignment_scope(data, changed_before, changed_after)
    scope['shifts'].update(touched)
    people = set()
    for sid in touched:
        old_input, new_input = was.get(sid, {}), now.get(sid, {})
        old_people = {row['id']: row for row in old_input.get('participants', ())}
        new_people = {row['id']: row for row in new_input.get('participants', ())}
        fields = (old_input.keys() | new_input.keys()) - {'revision', 'participants'}
        setup_changed = any(old_input.get(key) != new_input.get(key) for key in fields)
        for assignment_id in old_people.keys() | new_people.keys():
            before_row, after_row = old_people.get(assignment_id), new_people.get(assignment_id)
            if setup_changed or before_row != after_row:
                people.update(row['person_id'] for row in (before_row, after_row) if row is not None)
    # Each edited person keeps their whole duty horizon for window and adjacent-duties rules.
    scope['people'] = people
    # A leave finding names all of the person's duties during leave, even on other dates.
    leave_people = {row['person_id'] for row in data.get('availability', ())
                    if row['kind'] == 'approved_leave'} & scope['people']
    if leave_people:
        scope['shifts'].update(row['id'] for row in own
                               if any(p['person_id'] in leave_people for p in row['participants']))
    return {**scope, 'previous': previous, 'complete': True}


def _merge_session_checks(partial, shifts, findings, coverage, session_start):
    previous = partial['previous']
    affected = partial['shifts']
    findings.extend(previous['findings'][index] for index in previous['_session_parts']
                    if previous['findings'][index]['shift_id'] not in affected)
    order = {shift['id']: index for index, shift in enumerate(shifts)}
    findings[session_start:] = sorted(findings[session_start:], key=lambda row: order[row['shift_id']])
    coverage.extend(row for row in previous['coverage'] if row['shift_id'] not in affected)
    # Whole checks append inactive rows in input order after all active coverage.
    coverage.sort(key=lambda row: (row['status'] in {'cancelled', 'closed'}, order[row['shift_id']]))


def evaluate(data: dict, *, quota_only=False, session_cache=None, explain_findings=True, session_parts=False, trial_cache=None) -> dict:
    """`session_cache`: private to assignment-copy trials on one otherwise immutable snapshot.
    Reuse unchanged sessions' checks; department reports remain fresh.
    Internal decision/score trials may omit cause/hint decoration; all constraint checks still run.
    `session_parts` records private session indices and person-rule findings for assignment-only edits.
    Complete partials retain other people's findings and refresh every cross-person report.
    Greedy's private person-duty lookup belongs to an assignment-only trial. It needs only
    mandatory personal rules, because advisory workload observations do not guide placements.
    `trial_cache` reuses personal rule checks and quota attribution for unchanged duties. Rebuild it after any fact,
    rule or session setup edit; participant edits and focus changes are checked by their complete signatures."""
    from .rule_lines import read_quality_for
    read_quality = read_quality_for(data)
    # Private to participant-only trials on one otherwise immutable snapshot. Keep only each person's latest state.
    person_cache = trial_cache.setdefault('people', {}) if trial_cache is not None else None
    contribution_cache = trial_cache.setdefault('quota_contributions', {}) if trial_cache is not None else None
    overlap_witness = data.get('_overlap_witness')
    if overlap_witness is not None:
        overlap_witness.clear()
    check = data.get('_check_budget', lambda: None)
    check()
    # Proposals must not split coverage or consume evaluation work. Keep the
    # source snapshot intact for fingerprints, review previews and evidence.
    # Time windows depend on scopes, dates and memberships only, never on who is on a session: shared within this
    # check, or across the checks of one scheduling run that passes its own cache.
    # Immutable swap trials change participants only; their snapshot already projected recurring-session state.
    data = dict(data if '_active_policies' in data else with_series_state(data))
    person_shifts = (data.get('_partial') or {}).get('person_shifts')
    policies = data.get('_active_policies')
    if policies is None:
        policies = active_rules(data['policies'], context=data, read_quality=read_quality)
    qualification_cache = {}
    policies_by_id = {policy['id']: policy for policy in policies}
    data = {**data, "policies": policies,
            "_person_rows": data["_person_rows"] if "_series_state_cache" in data or "_active_policies" in data else index_person_rows(data),
            "_policy_applies_cache": data.get("_policy_applies_cache", {}),
            "_policy_applies_people": True,
            "_allocation_match_cache": data.get("_allocation_match_cache", {}),
            "_scope_interval_cache": data.get("_scope_interval_cache", {}),
            "_bucket_keys_cache": data.get("_bucket_keys_cache", {}), "_person_dates_cache": data.get("_person_dates_cache", {}),
            "_staffing_selection_cache": data.get("_staffing_selection_cache", {}),
            "_restriction_index_cache": data.get("_restriction_index_cache", {}),
            # Restriction intervals can include leave-buffer availability; own them per evaluation.
            "_restriction_interval_cache": {},
            "_scope_matches_cache": data.get("_scope_matches_cache", {}),
            "_line_index_cache": data.get("_line_index_cache", {}),
            # Dated qualification is assignment-independent; never carry it across evaluations or edits.
            "_qualification_cache": qualification_cache,
            # Evidence may reuse successful qualification only inside this fresh evaluation.
            "_qualification_evidence_cache": qualification_cache,
            "_coverage_availability_cache": {},
            "_coverage_availability_owned": True,
            # Absence answers read dated rotations and availability; own them per evaluation.
            "_candidate_absence_cache": {},
            "_categories_cache": data.get("_categories_cache", {}),
            "_capabilities_cache": data.get("_capabilities_cache", {}),
            "_supervision_commitments": ({} if person_shifts is not None else data.get('_supervision_commitments')
                                          if data.get('_reuse_supervision_commitments')
                                          else supervision_commitments(data))}
    from .date_sets import prepare_date_sets
    data['_date_set_cache'] = prepare_date_sets(data)
    leadership.prepare(data)
    counted_policies = data['_counted_policies'] if '_counted_policies' in data else counting_views(data, data['policies'])
    if trial_cache is not None and '_quota_policies' not in data:
        from .quotas import applicable_policies
        # Expanded rolling/calendar windows need stable identities, bounded by this immutable batch's rules.
        if 'quota_policies' not in trial_cache:
            trial_cache['quota_policies'] = applicable_policies(data)
        data['_quota_policies'] = trial_cache['quota_policies']
    zone = data["organisation"]["timezone"]
    focus = set(data["focus_ids"])
    workspace = data.get("workspace")
    rota_start, rota_end = ((local_midnight(date.fromisoformat(str(workspace[k])), zone)
                             for k in ("starts_on", "ends_on")) if workspace else (None, None))
    shifts = [s for s in data["shifts"] if not inactive(s)]
    shifts_by_id = {s['id']: s for s in shifts}
    # A scheduling run's re-check (generation.py): only the session checks of these sessions and the person checks of
    # these people, each exactly as a whole check computes them (every session stays in view); department-wide reports
    # that are participant-independent or advisory only are left out. Swap previews also request resource checks.
    partial = data.get("_partial")
    if partial and partial.get("complete") and partial["previous"].get("contracts") != check_contracts():
        partial = None
    complete_partial = partial is not None and partial.get('complete', False)
    trial_people = partial['people'] if person_shifts is not None else None
    checked_sessions = shifts if partial is None else [s for s in shifts if s['id'] in partial['shifts']]
    setup_cache = partial.get('setup_cache') if partial is not None else None
    service_shifts = defaultdict(list)
    for shift in shifts:
        for service_id in shift['service_ids']:
            service_shifts[service_id].append(shift)
    service_caps = any(p['status'] == 'active' and p['definition']['rule'].get('aggregation') == 'service'
                       for p in data['policies'])
    pooled_cover = [p for p in data['policies'] if p['status'] == 'active'
                    and p['definition']['rule'].get('aggregation') == 'service_group']
    # These session checks read other sessions' participants; keep their full evaluation.
    if ((partial is not None and not complete_partial) or quota_only
            or (data.get('skip_coverage') and not data.get('_allow_session_cache_with_skip_coverage'))
            or service_caps or pooled_cover):
        session_cache = None
    supervision_qualification = {**data, '_supervision_commitments': {}}
    findings, coverage = [], []
    seen, added_keys = set(), []
    changed_types = {}
    tightened_types = set()
    from .finding_causes import FindingCauses, COVERAGE_CODES
    causes = FindingCauses(data) if explain_findings else None

    def tightening_caused(shift_id, code, detail, a, b):
        if shift_id not in shifts_by_id:
            return False
        shift = shifts_by_id[shift_id]
        type_id = shift['assignment_type_id']
        if type_id not in tightened_types:
            return False
        issued = shift['type_snapshot']['definition']
        effective = effective_issued_definition(issued, data['types'][type_id]['definition'])
        start = aware(shift['start'])
        requirements = [row for row in issued.get('requirements') or []
                        if start + timedelta(minutes=row.get('from_minute', 0)) <= a
                        and b <= (start + timedelta(minutes=row['to_minute'])
                                  if row.get('to_minute') is not None else aware(shift['end']))]
        current_requirements = [row for row in effective.get('requirements') or []
                                if start + timedelta(minutes=row.get('from_minute', 0)) <= a
                                and b <= (start + timedelta(minutes=row['to_minute'])
                                          if row.get('to_minute') is not None else aware(shift['end']))]
        if code in {'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP'}:
            available = detail.get('available', 0)
            if code == 'COVERAGE_GAP':
                old = max((row.get('minimum', 0) for row in requirements), default=0)
                new = max((row.get('minimum', 0) for row in current_requirements), default=0)
            elif code == 'CAPABILITY_GAP':
                capability_id = detail.get('capability_id')
                old = max((clause.get('minimum', 0) for row in requirements
                           for clause in row.get('capabilities') or []
                           if clause.get('capability_id') == capability_id), default=0)
                new = max((clause.get('minimum', 0) for row in current_requirements
                           for clause in row.get('capabilities') or []
                           if clause.get('capability_id') == capability_id), default=0)
            else:
                old = max((sum(clause.get('minimum', 0) for clause in row.get('capabilities') or [])
                           for row in requirements if row.get('distinct_capability_seats')), default=0)
                new = max((sum(clause.get('minimum', 0) for clause in row.get('capabilities') or [])
                           for row in current_requirements if row.get('distinct_capability_seats')), default=0)
            return new > old and available >= old
        if code == 'MAXIMUM_PARTICIPANTS':
            old = min((row['maximum'] for row in requirements if row.get('maximum') is not None), default=None)
            new = min((row['maximum'] for row in current_requirements if row.get('maximum') is not None), default=None)
            return new is not None and new < (old if old is not None else float('inf')) and detail.get('scheduled', 0) <= (old if old is not None else detail.get('scheduled', 0))
        issued_supervision = issued.get('supervision') or {}
        effective_supervision = effective.get('supervision') or {}
        if code in {'SUPERVISOR_CAPACITY', 'SUPERVISION_GAP'}:
            old = issued_supervision.get('trainees_per_supervisor', 20)
            new = effective_supervision.get('trainees_per_supervisor', old)
            if new >= old:
                return False
            if code == 'SUPERVISOR_CAPACITY':
                return detail.get('scheduled', 0) <= old
            return not (detail.get('trainees', 0) > detail.get('qualified_supervisors', 0) * old)
        if code == 'SUPERVISION_PAIRING':
            return not issued_supervision.get('require_pairing', False) and effective_supervision.get('require_pairing', False)
        return True

    def add(code, message, shift=None, person=None, a=None, b=None, severity="error", rule=None, extra=None,
            explanation=None):
        policy = rule if isinstance(rule, dict) else policies_by_id.get(rule)
        if isinstance(rule, dict):
            line = rule.get('template_line') or {}
            extra = {**(extra or {}), 'rule_severity': rule['definition']['severity'],
                     'rule_strength': rule['definition'].get('strength', 'medium'),
                     **{k: line[k] for k in ('line', 'slot') if k in line},
                     **({'count_bucket': rule['count_bucket']} if 'count_bucket' in rule else {})}
            rule = rule['id']
        if policy and (policy.get('_live_zero') or policy.get('_live_mandatory_recheck')):
            rule = policy.get('_source_policy_id', policy['id'])
        if policy and policy.get('_live_zero'):
            message += f' (current department rule {rule})'
            extra = {**(extra or {}), 'live_zero': True}
        leave_classification = (causes.leave_classification(person, shift)
                                if code == 'UNAVAILABLE' and causes and person else {})
        if leave_classification:
            # Keep the approved-leave duty fact on the finding itself so the
            # Fix list can group it with any related quota observations.
            extra = {**(extra or {}), **leave_classification}
        key = (code, shift, person, str(a), str(b), rule, str(extra))
        if key in seen:
            return
        seen.add(key)
        if (data.get('operational') and shift in shifts_by_id and code in {
                'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SUPERVISION_GAP',
                'SUPERVISION_PAIRING', 'SUPERVISOR_CAPACITY', 'MAXIMUM_PARTICIPANTS'}
                and tightening_caused(shift, code, extra or {}, a, b)):
            message = f"Today's requirement: {message}"
        if causes and code in COVERAGE_CODES and shift and a is not None and b is not None and 'maximum' not in (extra or {}):
            explanation = causes.coverage(code, shift, a, b, extra or {}, policy)
        if session_cache is not None:
            added_keys.append(key)
        outside = bool(rule and a is not None and b is not None and rota_start is not None
                       and (a < rota_start or rota_end < b))
        findings.append({"code": code, "message": message, "severity": severity,
                         "shift_id": shift, "person_id": person, "start": a.isoformat() if a else None,
                         "end": b.isoformat() if b else None, "rule_id": rule, "detail": extra,
                         "outside_rota": outside, **leave_classification, **(explanation or {}),
                         **({"window": {"start": a.isoformat(), "end": b.isoformat()}} if outside else {})})

    if data.get('operational'):
        for shift in shifts:
            typ = data['types'][shift['assignment_type_id']]
            if (_published_session(data, shift) and shift['id'] not in data.get('_issued_type_versions', ())
                    and shift['type_revision'] != typ['revision']
                    and not display_only_change(shift.get('type_snapshot'), typ)):
                type_id = shift['assignment_type_id']
                row = changed_types.setdefault(type_id,
                                              {'name': typ.get('name', type_id), 'count': 0})
                row['count'] += 1
                if effective_issued_definition(shift['type_snapshot']['definition'], typ['definition']) != shift['type_snapshot']['definition']:
                    tightened_types.add(type_id)
        for type_id, row in changed_types.items():
            add('TYPE_VERSION_CHANGED',
                f"{row['name']} changed since publication; {row['count']} issued sessions keep the issued definition",
                severity='info', extra={'type_id': type_id, 'type_name': row['name'],
                                        'issued_session_count': row['count']})

    for finding in read_quality.get('_fair_share_goal_findings', ()):
        add(finding['code'], finding['message'], severity='warning', rule=finding.get('rule_id'),
            extra=finding.get('detail'))

    for change in data.get('_live_validity_changes', ()):
        before, after = change['before'], change['after']
        retired = (not after.get('active', True)
                   or (after.get('starts_on') or '') > (before.get('starts_on') or '')
                   or (after.get('ends_on') or date.max.isoformat()) < (before.get('ends_on') or date.max.isoformat()))
        label = {'types': 'Duty type', 'services': 'Service', 'locations': 'Place',
                 'location_types': 'Place type'}.get(change['catalogue'], change['catalogue'].replace('_', ' ').capitalize())
        state = 'retired' if retired else 're-activated'
        add('CATALOGUE_VALIDITY_CHANGED', f"{label} {change['name']} {state} since this draft was made",
            severity='warning', extra=change)

    if quota_only:
        # Quota-only rule admission consumes the complete quota report, never a full-rota validity claim.
        from .quotas import evaluate_quotas
        quota_report = evaluate_quotas(data, add, ancestor_ids, exception_for, explain=causes.quota if causes else None, contribution_cache=contribution_cache)
        return {'scope': 'quotas', 'findings': grouped_rule_findings(findings, data['policies'], tiers=data.get('_policy_group_tiers')),
                'quota_report': quota_report}

    findings.extend(rotation_leave_findings(data))

    boundary_inputs = (data['_trial_boundary_inputs'] if '_trial_boundary_inputs' in data else
                       coverage_boundary_inputs(data)) if checked_sessions else None
    definitions = {}
    for shift in shifts if person_shifts is None else checked_sessions:
        definitions[shift["id"]] = effective_type_definition(data, shift)

    # Full envelopes remain the authority for conflicts, rest and reserved-hours
    # rules. Only qualified continuous coverage uses explicit presence segments.
    commitments = defaultdict(list)
    if person_shifts is not None:
        presence = TrialPresence(shifts_by_id)
        supervisors = set()
        for shift in checked_sessions:
            if shift['id'] in focus:
                for pid in presence.load(shift):
                    add("PARTICIPATION_INVALID", "Planned presence segments are invalid; no qualified cover is inferred",
                        shift["id"], pid)
            if definitions[shift['id']].get('supervision'):
                supervisors.update(p['person_id'] for p in shift['participants'])
        for pid in trial_people | supervisors:
            for shift in person_shifts(pid):
                if shift['id'] not in shifts_by_id:
                    continue
                for person in shift['participants']:
                    if person['person_id'] != pid:
                        continue
                    a, b = aware(person['start']), aware(person['end'])
                    if pid in trial_people:
                        commitments[pid].append((shift, person, a, b))
                    if pid in supervisors:
                        data['_supervision_commitments'].setdefault(pid, []).append((shift['id'], a, b))
    else:
        presence = {}
        # Swap trials change only person IDs: reservation times and planned presence stay immutable for the lookup.
        participation_cache = data.get('_participation_cache')
        for shift in shifts:
            for person in shift["participants"]:
                cached = participation_cache.get(person['id']) if participation_cache is not None else None
                if cached is None:
                    a, b = aware(person['start']), aware(person['end'])
                    try:
                        intervals = presence_intervals(person)
                    except ValueError:
                        intervals = None
                    cached = a, b, intervals
                    if participation_cache is not None:
                        participation_cache[person['id']] = cached
                a, b, intervals = cached
                pid = person['person_id']
                commitments[pid].append((shift, person, a, b))
                presence[shift["id"], pid] = intervals or []
                if intervals is None and shift["id"] in focus and (partial is None or complete_partial or shift["id"] in partial["shifts"]):
                    add("PARTICIPATION_INVALID", "Planned presence segments are invalid; no qualified cover is inferred",
                        shift["id"], pid)

    for pid, entries in commitments.items():
        entries.sort(key=lambda row: row[2])
    overlap_groups = defaultdict(list)
    overlap_conflicts = evaluate_overlaps(data, include_unavailability=False, commitments=commitments)
    if (overlap_witness is not None and (partial is None or complete_partial) and not data.get('skip_coverage')
            and data.get('person_rule_subjects') is None and data.get('quota_subjects') is None
            and not data.get('availability')
            and not any(not row.get('department_available', True) for row in data.get('rotations', []))
            and not any(series_definition(shift).get('usual_assignees_fixed')
                        and usual_assignee_ids(shift) - {p['person_id'] for p in shift['participants']} for shift in shifts)):
        # Only this complete check has the same reservations as the broader session-flag projection.
        overlap_witness['conflicts'] = overlap_conflicts
    for conflict in overlap_conflicts:
        if ("_published_session_checks" in data and conflict["shift_id"] not in data["_published_session_checks"]
                and conflict["person_id"] not in data["person_rule_subjects"]):
            continue
        overlap_groups[conflict['shift_id'], conflict['person_id'], conflict['start']].append(conflict)
    for conflicts in overlap_groups.values():
        conflict = conflicts[0]
        names = [conflict['session_name'], *(row['other_session_name'] for row in conflicts)]
        detail = {'overlap': conflict}
        if len(conflicts) > 1:
            # One selectable issue key, retaining every independent pair for protected safety rechecks.
            detail['derived_findings'] = [{'message': f"{row['session_name']} and {row['other_session_name']} overlap; cancel one occurrence",
                'start': row['start'], 'end': row['end'], 'detail': {'overlap': row}} for row in conflicts[1:]]
        add("TIME_OVERLAP", f"{' and '.join(names)} overlap; cancel one occurrence",
            conflict['shift_id'], conflict['person_id'], aware(conflict['start']), aware(conflict['end']),
            extra=detail)
    non_session_found = len(findings)

    def coverage_people(shift, a, b):
        return rule_coverage_people(data, shift, a, b, presence=presence, definition=definitions[shift['id']])

    # Reuse identical pooled-coverage lookups only within this immutable evaluation.
    # Every edit/allocation attempt calls evaluate again with a fresh cache.
    peer_cache = {}

    # Restrictions in rule order, each with the only people it can apply to (most name one person): asked per participant.
    restrictions = [(p, named_people(p["definition"]["scope"])) for p in data["policies"]
                    if p["definition"]["rule"]["kind"] == "restriction"]
    staffing = [p for p in counted_policies if p['definition']['rule']['kind'] in {'minimum_cover', 'maximum_participants'}]

    def cover_peers(policy, a, b):
        key = (policy['id'], (policy.get('template_line') or {}).get('line'), counting_key(policy), a, b)
        if key in peer_cache:
            return peer_cache[key]
        peer_cache[key] = staffing_peers(data, policy, a, b, shifts=shifts)
        return peer_cache[key]

    for shift in checked_sessions:
        if (shift["id"] not in focus
                or "_published_session_checks" in data and shift["id"] not in data["_published_session_checks"]):
            continue
        check()
        sid, start, end = shift["id"], aware(shift["start"]), aware(shift["end"])
        cached = session_cache.get(sid) if session_cache is not None else None
        session_signature, supervisor_candidates = None, ()
        own = (explain_findings, repr(shift['participants'])) if session_cache is not None else None
        if definitions[sid].get('supervision') and not data.get('skip_coverage'):
            if cached is not None and cached[0] is shift and cached[4][0] == own:
                supervisor_candidates = cached[5]
            else:
                # Qualification facts are immutable across these assignment-copy trials; availability is not.
                supervisor_candidates = tuple(p['person_id'] for p in shift['participants'] if any(
                    qualified_supervisor(supervision_qualification, shift, p, a, b, definition=definitions[sid])
                    for lo, hi in presence[(sid, p['person_id'])] for a, b in authority_intervals(data, lo, hi)))
        if session_cache is not None:
            dependencies = tuple((pid, tuple(sorted({(max(start, lo), min(end, hi))
                for other, lo, hi in data['_supervision_commitments'].get(pid, ())
                if other != sid and overlaps(start, end, lo, hi)}))) for pid in supervisor_candidates)
            session_signature = own, dependencies
        if cached is not None and cached[0] is shift and cached[4] == session_signature:
            findings.extend(cached[1])
            coverage.extend(cached[2])
            seen.update(cached[3])
            continue
        found_at, covered_at, keys_at = len(findings), len(coverage), len(added_keys)
        person_ids = [p["person_id"] for p in shift["participants"]]
        if len(person_ids) != len(set(person_ids)):
            add("DUPLICATE_PARTICIPATION", "A session cannot contain the same person twice", sid)
        typ = data["types"][shift["assignment_type_id"]]
        definition = definitions[sid]
        if setup_cache is None:
            validate_session_setup(data, shift, shifts, definition, add)
        else:
            if sid not in setup_cache:
                setup = []
                validate_session_setup(data, shift, shifts, definition,
                                       lambda *args, **kwargs: setup.append((args, kwargs)))
                setup_cache[sid] = setup
            for args, kwargs in setup_cache[sid]:
                add(*args, **kwargs)

        bounds = {start, end}
        if definition.get('supervision'):
            for pid in supervisor_candidates:
                for other, lo, hi in data['_supervision_commitments'].get(pid, ()):
                    if other != sid and overlaps(start, end, lo, hi):
                        bounds.update(t for t in (lo, hi) if start < t < end)
        # Candidate checks may skip session coverage: it yields only session-level findings, never person ones.
        skip_coverage = data.get("skip_coverage", False)
        for policy in ([] if skip_coverage else pooled_cover):
            peers = cover_peers(policy, start, end)
            if not any(p['id'] == sid for p in peers):
                continue
            for peer in peers:
                bounds.update(t for t in (aware(peer['start']), aware(peer['end'])) if start < t < end)
                for participant in peer['participants']:
                    pid = participant['person_id']
                    for lo, hi in presence[(peer['id'], pid)]:
                        bounds.update(t for t in (lo, hi) if start < t < end)
                    for absence in person_rows(data, 'availability', pid):
                        bounds.update(t for t in (aware(absence['start']), aware(absence['end'])) if start < t < end)
                    for restriction in blocking_restrictions(data, pid, peer, start, end):
                        for lo, hi in applicable_restriction_intervals(data, restriction, pid, peer, start, end):
                            bounds.update(t for t in (lo, hi) if start < t < end)
        if service_caps:
            # Peer boundaries prevent a short overlap from disappearing inside a long duty.
            for service_id in shift['service_ids']:
                for peer in service_shifts[service_id]:
                    for participant in peer['participants']:
                        for lo, hi in presence[(peer['id'], participant['person_id'])]:
                            bounds.update(t for t in (lo, hi) if start < t < end)
        for req in definition["requirements"]:
            a = start + timedelta(minutes=req["from_minute"])
            b = start + timedelta(minutes=req["to_minute"]) if req.get("to_minute") is not None else end
            if a >= end or b > end:
                add("REQUIREMENT_OUTSIDE_SHIFT", "A configured requirement interval extends outside this session", sid)
        for participant in shift["participants"]:
            pid, a, b = participant["person_id"], aware(participant["start"]), aware(participant["end"])
            if definition.get('extra_cover', False) and not participant['overtime']:
                add('EXTRA_COVER_NOT_OVERTIME', 'This extra-cover duty is intended for overtime; this person is assigned as regular work',
                    sid, pid, a, b, severity='warning')
            if sid in focus and (a != start or b != end):
                add('FULL_ASSIGNMENT_REQUIRED', 'Every person must book the complete assignment; configure a shorter duty instead', sid, pid, a, b)
            lead = leadership.summary(data, shift, a, b)
            if not lead and participant.get('team_lead'):
                add('LEAD_NOT_QUALIFIED', 'This duty has no team lead requirement; clear the lead', sid, pid, a, b, 'warning')
            if lead and participant.get('team_lead'):
                if not leadership.covers_required(data, participant, shift):
                    requirements = [r for r in leadership.requirements(data, shift, start, end)
                                    if r['continuity'] == 'whole_duty']
                    add('LEAD_NOT_QUALIFIED', 'This team lead must cover every required interval of the duty',
                        sid, pid, a, b, rule=requirements[0].get('policy'),
                        extra={'lead_sources': [leadership.source(r) for r in requirements]})
                for lo, hi, requirements in leadership.segments(data, shift, a, b, people=[pid]):
                    for requirement in requirements:
                        if not leadership.matches(data, pid, shift, requirement, lo, hi):
                            add('LEAD_NOT_QUALIFIED', 'This team lead does not meet who may lead this duty', sid, pid, lo, hi,
                                rule=requirement.get('policy'), extra={**requirement.get('legacy', {}), 'lead_sources': [leadership.source(requirement)]})
            if not qualified(data, pid, shift, definition, a, b):
                add("NOT_ELIGIBLE", "Active membership, allowed category and service eligibility must cover the full participation interval", sid, pid, a, b)
            if rotation_blocks(data, pid, a, b):
                add("EXTERNAL_ROTATION", "An away rotation makes this person unavailable to the department", sid, pid, a, b)
            for absence in person_rows(data, "availability", pid):
                if overlaps(a, b, aware(absence["start"]), aware(absence["end"])):
                    lo, hi = max(a, aware(absence["start"])), min(b, aware(absence["end"]))
                    add("UNAVAILABLE", "Known unavailability overlaps the scheduled participation; no private reason is disclosed", sid, pid, lo, hi)
            for policy, named in restrictions:
                if named is None or pid in named:
                    for x, y in applicable_restriction_intervals(data, policy, pid, shift, a, b):
                        exc = exception_for(data, policy, pid, x, y)
                        add("RULE_EXCEPTION" if exc else "RESTRICTION", "Approved time-bounded exception applies" if exc else policy["name"],
                            sid, pid, x, y, "warning" if exc or policy["definition"]["severity"] == "advisory" else "error", policy,
                            {"exception_id": exc["id"]} if exc else None)
        if skip_coverage:
            continue
        bounds.update(coverage_boundaries(data, shift, start, end, presence=presence, inputs=boundary_inputs))
        for group in leadership.concurrent_groups(data, shift, start, end):
            bounds.update((group['start'], group['end']))
        bounds = sorted(t for t in bounds if start <= t <= end)
        for a, b in zip(bounds, bounds[1:]):
            scheduled = [p for p in shift["participants"]
                         if any(lo <= a and b <= hi for lo, hi in presence[(sid, p["person_id"])])]
            available = coverage_people(shift, a, b)
            caps = {p["person_id"]: capabilities_for(data, p["person_id"], shift, a, b) for p in available}
            applicable = [r for r in definition["requirements"] if start + timedelta(minutes=r["from_minute"]) <= a
                          and b <= (start + timedelta(minutes=r["to_minute"]) if r.get("to_minute") is not None else end)]
            empty_open_extra = is_open_extra_cover(definition) and not shift['participants']
            headcount_gap = any(len(available) < r["minimum"] for r in applicable)
            derived_findings = []
            planning_demand = max([0, *[max(r['minimum'], r.get('target') or 0) for r in applicable]])
            place_requirements = []
            found_before = len(findings)  # findings only grow: this segment's are the ones after
            for policy in staffing:
                rule = policy["definition"]["rule"]
                scope = policy["definition"]["scope"]
                selector = session_scope(scope)
                if (policy["status"] != "active" or policy["department_id"] != typ["department_id"]
                        or not within_dates(policy, a, b, zone) or not scope_matches_throughout(data, selector, None, shift, a, b)):
                    continue
                if rule['kind'] == 'maximum_participants':
                    peers = [shift]
                    if rule.get('aggregation', 'assignment') == 'service':
                        selected = scope.get('service_ids') or ([scope['service_id']] if scope.get('service_id') else shift['service_ids'])
                        peers = [peer for service_id in set(shift['service_ids']) & set(selected)
                                 for peer in service_shifts[service_id]
                                 if data['types'][peer['assignment_type_id']]['department_id'] == policy['department_id']]
                    for detail in staffing_counts(data, policy, shift, a, b, peers=peers, presence=presence):
                        count, extra = detail['scheduled'], detail.get('permitted_extra', 0)
                        if policy.get('template_line'):
                            detail['line'] = policy['template_line']['line']
                        if count > rule['maximum'] + extra:
                            add('SCOPED_PARTICIPANT_LIMIT', policy['name'], sid, a=a, b=b,
                                severity='error' if policy['definition']['severity'] == 'mandatory' else 'warning',
                                rule=policy, extra=detail)
                        elif count > rule['maximum']:
                            add('CAPACITY_OVERFLOW', policy['name'] + ': configured overflow places used', sid, a=a, b=b,
                                severity='warning', rule=policy, extra=detail)
                    continue
                peers = cover_peers(policy, a, b) if rule.get('aggregation') == 'service_group' else [shift]
                coverage_focus = focus if partial is None else partial.get('coverage_focus', focus)
                coverage_selected = set(selected_session_ids(data)) if partial is None else set(partial.get('coverage_focus', coverage_focus))
                anchor = pooled_anchor([s['id'] for s in peers], coverage_selected, coverage_focus)
                if rule.get('aggregation') == 'service_group' and anchor and sid != anchor:
                    continue
                observations = staffing_counts(data, policy, shift, a, b, peers=peers, presence=presence, available=available)
                if not observations:
                    continue
                detail = observations[0]
                count, minimum = detail['available'], detail['required']
                if policy['definition']['severity'] == 'mandatory':
                    place_requirements.append(detail)
                planning_demand = max(planning_demand, minimum)
                if count < minimum:
                    add("SCOPED_COVERAGE_GAP", policy["name"], sid, a=a, b=b,
                        severity="error" if policy["definition"]["severity"] == "mandatory" else "warning",
                        rule=policy, extra={**detail,
                            **({'line': policy['template_line']['line']} if policy.get('template_line') else {})})
            for r in applicable:
                if len(available) < r["minimum"]:
                    open_extra = is_open_extra_cover(definition)
                    add('OPEN_EXTRA_COVER' if open_extra else 'COVERAGE_GAP',
                        'Extra-cover places remain open for overtime' if open_extra else 'Available eligible cover is below the required headcount',
                        sid, a=a, b=b, severity='warning' if open_extra else 'error',
                        extra={"required": r["minimum"], "available": len(available)})
                if r["target"] is not None and len(available) < r["target"]:
                    detail = {"target": r["target"], "available": len(available)}
                    if headcount_gap:
                        derived_findings.append({"code": "COVERAGE_TARGET", "severity": "warning", "detail": detail})
                    else:
                        add("COVERAGE_TARGET", "Available cover is below the advisory target", sid, a=a, b=b,
                            severity="warning", extra=detail)
                if r["maximum"] is not None and len(scheduled) > r["maximum"]:
                    add("MAXIMUM_PARTICIPANTS", "Scheduled participation exceeds the configured maximum", sid, a=a, b=b,
                        extra={'maximum': r['maximum'], 'scheduled': len(scheduled)})
                seats = []
                for clause in r["capabilities"]:
                    count = sum(clause["capability_id"] in cs for cs in caps.values())
                    seats += [clause["capability_id"]] * clause["minimum"]
                    if not empty_open_extra:
                        place_requirements.append({"required": clause["minimum"], "available": count})
                    if count < clause["minimum"]:
                        detail = {"capability_id": clause["capability_id"], "required": clause["minimum"], "available": count}
                        if empty_open_extra or headcount_gap and not is_open_extra_cover(definition):
                            derived_findings.append({"code": "CAPABILITY_GAP",
                                "severity": "warning" if empty_open_extra else "error", "detail": detail})
                        else:
                            add("CAPABILITY_GAP", "Continuous capability requirement is not met", sid, a=a, b=b, extra=detail)
                matched = matching_size(seats, caps) if r['distinct_capability_seats'] else 0
                if r['distinct_capability_seats'] and not empty_open_extra:
                    place_requirements.append({'required': len(seats), 'available': matched})
                if r["distinct_capability_seats"] and matched < len(seats):
                    detail = {'required': len(seats), 'available': matched}
                    if empty_open_extra:
                        derived_findings.append({'code': 'DISTINCT_PERSON_GAP', 'severity': 'warning', 'detail': detail})
                    else:
                        add("DISTINCT_PERSON_GAP", "Capability clauses require more distinct qualified people", sid, a=a, b=b,
                            extra=detail)
            supervision = definition.get("supervision")
            if supervision:
                trainees = [p for p in available if is_trainee(data, supervision, p, a, b)]
                supervisors = [p for p in available if qualified_supervisor(data, shift, p, a, b, definition=definition)]
                supervisor_ids = {p['person_id'] for p in supervisors}
                for trainee in trainees:
                    pair = trainee.get('supervisor_person_id')
                    for message, policy in pairing_obligations(data, shift, trainee, supervisor_ids, a, b, counted_policies):
                        add('SUPERVISION_PAIRING', message, sid, trainee['person_id'], a, b, rule=policy)
                    # A required attachment: with the consultant here and able to supervise, the trainee is theirs.
                    required = [q for q, strength in attached_to(data, trainee['person_id'], shift, a, b).items()
                                if strength == 'required' and q in supervisor_ids]
                    if required and pair not in required:
                        add('ATTACHMENT_PAIRING', 'The trainee is attached to a consultant on this session; pair them',
                            sid, trainee['person_id'], a, b, extra={'supervisor_person_id': required[0]})
                for supervisor_id in supervisor_ids:
                    paired = sum(p.get('supervisor_person_id') == supervisor_id for p in trainees)
                    if paired > supervision['trainees_per_supervisor']:
                        add('SUPERVISOR_CAPACITY', 'Named supervision exceeds this supervisor’s configured capacity',
                            sid, supervisor_id, a, b,
                            extra={'maximum': supervision['trainees_per_supervisor'], 'scheduled': paired})
                for supervisor_id in supervisor_ids:
                    limits = supervision_limit_policies(data, shift, supervisor_id, a, b, policies=counted_policies)
                    for policy in limits:
                        rule = policy['definition']['rule']
                        paired = [p for p in trainees if p.get('supervisor_person_id') == supervisor_id]
                        selected = set(rule['trainee_category_ids'])
                        if rule['mode'] == 'allowed_ranks':
                            for trainee in paired:
                                if not selected.intersection(categories_for(data, trainee['person_id'], a, b)):
                                    add('SUPERVISION_RANK', 'This supervisor is restricted from supervising the trainee rank',
                                        sid, trainee['person_id'], a, b, rule=policy, extra={'supervisor_person_id': supervisor_id})
                        else:
                            detail = supervision_capacity(data, rule, paired, a, b, supervisor_id=supervisor_id)
                            count, extra = detail['scheduled'], detail.get('permitted_extra', 0)
                            if count > rule['maximum'] + extra:
                                add('SUPERVISOR_SCOPED_CAPACITY', 'Named supervision exceeds the configured rank-specific capacity',
                                    sid, supervisor_id, a, b, rule=policy, extra=detail)
                            elif count > rule['maximum']:
                                add('CAPACITY_OVERFLOW', 'Named supervision uses explicitly permitted extra places',
                                    sid, supervisor_id, a, b, severity='warning', rule=policy, extra=detail)
                spread_lines = {}
                for trainee in trainees:
                    for spread in session_spread_policies(
                            counted_policies,
                            lambda candidate, member, duty, lo, hi: policy_applies(data, candidate, member, duty, lo, hi),
                            trainee['person_id'], shift, a, b):
                        spread_lines.setdefault(spread_policy_key(spread), spread)
                for spread in spread_lines.values():
                    members = [trainee for trainee in trainees if spread in session_spread_policies(
                        counted_policies,
                        lambda candidate, member, duty, lo, hi: policy_applies(data, candidate, member, duty, lo, hi),
                        trainee['person_id'], shift, a, b)]
                    if not members:
                        continue
                    eligible = {trainee['person_id']: set(supervisor_ids) for trainee in members}
                    capacities = dict.fromkeys(supervisor_ids, supervision['trainees_per_supervisor'])
                    for supervisor_id in supervisor_ids:
                        all_limits = [policy for policy in counted_policies
                                      if policy['definition']['rule']['kind'] == 'supervision_limit'
                                      and policy_applies(data, policy, supervisor_id, shift, a, b)]
                        applicable = [policy['definition']['rule'] for policy in all_limits
                                      if policy['definition']['rule'].get('mode') == 'capacity']
                        if applicable:
                            capacities[supervisor_id] = min(
                                [capacities[supervisor_id]] + [rule['maximum'] for rule in applicable
                                                               if rule.get('maximum') is not None
                                                               and (not rule.get('trainee_category_ids')
                                                                    or all(set(rule['trainee_category_ids']).intersection(
                                                                        categories_for(data, trainee['person_id'], a, b))
                                                                        for trainee in members))])
                        for trainee in members:
                            if any(rule['mode'] == 'allowed_ranks'
                                   and not set(rule['trainee_category_ids']).intersection(
                                       categories_for(data, trainee['person_id'], a, b))
                                   for policy in all_limits
                                   for rule in [policy['definition']['rule']]):
                                eligible[trainee['person_id']].discard(supervisor_id)
                    assignments = {trainee['person_id']: trainee.get('supervisor_person_id') for trainee in members}
                    detail = levelled_finding([p['person_id'] for p in members], sorted(supervisor_ids), assignments,
                                              eligible=eligible, capacities=capacities, maximum_gap=spread_gap(spread))
                    if detail:
                        requested_severity = 'error' if spread['definition']['severity'] == 'mandatory' else 'warning'
                        # A heterogeneous rank pool or cap can make a perfect level
                        # impossible. Keep that diagnosis advisory; the supervision
                        # safety limits still decide whether the rota is admissible.
                        severity = ('warning' if detail['reason'] in {'eligibility', 'capacity', 'no_eligible_supervisor', 'unavoidable_gap'}
                                    else requested_severity)
                        add('SUPERVISION_LEVEL', 'Selected members are not spread evenly across session supervisors',
                            sid, a=a, b=b, severity=severity, rule=spread, extra={**detail, 'maximum_gap': spread_gap(spread)})
                supervision_cover = {'required': ceil(len(trainees) / supervision['trainees_per_supervisor']),
                                     'available': len(supervisors)}
                place_requirements.append(supervision_cover)
                if len(trainees) > len(supervisors) * supervision["trainees_per_supervisor"]:
                    add("SUPERVISION_GAP", "Qualified available supervision capacity is insufficient", sid, a=a, b=b,
                        extra={"trainees": len(trainees), "qualified_supervisors": len(supervisors), **supervision_cover})
            elif any(p["participation"] == "trainee" or p.get('supervisor_person_id') for p in scheduled):
                add("SUPERVISION_UNCONFIGURED", "Trainees require an explicitly configured supervision rule", sid, a=a, b=b)
            lead = leadership.counts(data, shift, a, b, available=available, scheduled=scheduled)
            if lead:
                count = lead['available']
                for requirement in lead['_requirements']:
                    if requirement.get('count_scope', 'duty') == 'concurrent_duties':
                        groups = [g for g in lead.get('_groups', ())
                                  if g['key'] == leadership._requirement_key(requirement)
                                  and g['start'] < b and a < g['end']]
                        if not groups or not leadership.is_group_anchor(data, shift, requirement, a, b):
                            continue
                        group = next((g for g in groups if g['start'] <= a and b <= g['end']), groups[0])
                        count, scheduled_count = group['available'], group['scheduled']
                        extra = {'lead_sources': [leadership.source(requirement)],
                                 'affected_shift_ids': [duty['id'] for duty, _ in group['duties']]}
                    else:
                        scheduled_count = lead['scheduled']
                        extra = {'lead_sources': [leadership.source(requirement)]}
                    if count < requirement['minimum']:
                        detail = {'required': requirement['minimum'], 'available': count, **extra}
                        if empty_open_extra:
                            derived_findings.append({'code': 'LEAD_GAP', 'severity': 'warning', 'detail': detail})
                        else:
                            add('LEAD_GAP', 'This session needs a team lead' if requirement['minimum'] == 1 else
                                f"This session needs {requirement['minimum']} team leads", sid, a=a, b=b,
                                rule=requirement.get('policy'), extra=detail)
                    if requirement['maximum'] is not None and scheduled_count > requirement['maximum']:
                        add('LEAD_GAP', 'This session has more team leads than allowed', sid, a=a, b=b,
                            rule=requirement.get('policy'), extra={'maximum': requirement['maximum'], 'scheduled': scheduled_count, **extra})
            if derived_findings:
                if causes:
                    derived_findings = [{**f, **causes.coverage(f['code'], sid, a, b, f['detail'])}
                                        for f in derived_findings]
                # One visible issue, retaining each distinct shortfall for the greedy run's deficit comparison.
                gap = next((f for f in findings[found_before:] if f["code"] in {'COVERAGE_GAP', 'OPEN_EXTRA_COVER'}), None)
                if gap is None:  # An empty optional session can need skills or a lead without a headcount floor.
                    add('OPEN_EXTRA_COVER', 'Extra-cover places remain open for overtime', sid, a=a, b=b,
                        severity='warning', extra={'required': max((r['minimum'] for r in applicable), default=0),
                                                   'available': len(available)})
                    gap = findings[-1]
                gap["detail"]["derived_findings"] = list({(f["code"], str(f["detail"])): f for f in derived_findings}.values())
            maxima = [r['maximum'] for r in applicable if r.get('maximum') is not None]
            # Supervised trainees reserve additional training places, but only available people count as cover.
            supervision = definition.get('supervision') or {}
            available_trainees = sum(is_trainee(data, supervision, p, a, b) for p in available) if supervision else 0
            planning_places = min(maxima) if maxima else planning_demand + available_trainees
            cover_count = len(available)
            if planning_places > 0 and cover_count > planning_places and not maxima:
                add('SURPLUS_COVER', 'More people are scheduled than the configured cover places need', sid,
                    a=a, b=b, severity='warning', extra={'places': planning_places, 'scheduled': cover_count})
            # A segment is a gap when it is short of cover; too many people (a maximum) is an error, not an open place.
            segment_failed = any(f["severity"] == "error" and f["code"] not in OVER_MAXIMUM for f in findings[found_before:])
            from .coverage import coverage_places
            row = {"shift_id": sid, "start": a.isoformat(), "end": b.isoformat(),
                             "scheduled": len(scheduled), "available_eligible": len(available),
                             "required": max((r["minimum"] for r in applicable), default=0),
                             "status": "gap" if segment_failed else "covered" if applicable else "not_staffed",
                             "availability_basis": "known_records_not_attendance"}
            coverage.append({**row, **coverage_places(row, place_requirements)})
        if session_cache is not None:
            session_cache[sid] = (shift, findings[found_at:], coverage[covered_at:], added_keys[keys_at:],
                                  session_signature, supervisor_candidates)

    from .rota_summary import grouped_findings
    if partial is None or complete_partial:
        for shift in data["shifts"]:
            if (shift["id"] in focus and inactive(shift)
                    and (not complete_partial or shift['id'] in partial['shifts'])):
                coverage.append({"shift_id": shift["id"], "start": shift["start"], "end": shift["end"],
                                 "scheduled": len(shift["participants"]), "available_eligible": 0,
                                 "required": 0, "status": 'closed' if shift.get('closed') else 'cancelled',
                                 "availability_basis": "known_records_not_attendance"})
    if complete_partial:
        _merge_session_checks(partial, data['shifts'], findings, coverage, non_session_found)
    findings = grouped_findings(findings, coverage)
    session_keys = {tuple(f.get(key) for key in ('code', 'shift_id', 'person_id', 'rule_id', 'start'))
                    for f in findings[non_session_found:]} if session_parts else set()
    session_found = len(findings)
    personal_parts = {}
    retained_people = (partial['previous'].get('_person_parts') if complete_partial else None)
    signatures = {pid: (frozenset(focus), tuple((id(s), repr(p)) for s, p, _, _ in entries))
                  for pid, entries in commitments.items()} if person_cache is not None else {}
    maximum_hours = {}
    for policy_index, policy in enumerate(counted_policies):
        check()
        rule = policy["definition"]["rule"]
        kind = rule["kind"]
        if policy["status"] != "active" or kind in NOT_PERSON_RULES:
            continue  # shape rules: evaluate_shapes below
        severity = "error" if policy["definition"]["severity"] == "mandatory" else "warning"
        if severity == 'warning' and person_shifts is not None:
            continue
        only = data.get("person_rule_subjects") if partial is None or complete_partial else partial["people"]
        named = named_people(policy["definition"]["scope"])  # a rule naming people never applies to anyone else
        hours_by_person = None
        if kind == 'max_hours':
            # One evaluation has fixed duties and focus. Equal counting inputs share observations,
            # while each policy still applies its own threshold, exceptions and finding identity.
            counting = json.dumps({key: value for key, value in rule.items() if key != 'hours'}, sort_keys=True,
                                  default=str)
            hours_by_person = maximum_hours.setdefault((_policy_scope_key(policy), counting), {})

        def check_person(pid, entries, add):
            # Applicability is anchored in the policy's department, but a person's
            # unscoped total/rest limit counts all their institutional commitments.
            # Moving a duty to another department cannot erase its hours or rest.
            observed = hours_by_person.get(pid) if hours_by_person is not None else None
            focal = observed[0] if observed is not None else [
                (s, p, a, b) for s, p, a, b in entries if s["id"] in focus
                and policy_applies(data, policy, pid, s, a, b)
                and (rule.get("include_overtime", True) or not p["overtime"])]
            if not focal:
                if hours_by_person is not None and observed is None:
                    hours_by_person[pid] = (focal, (), ())
                return
            targets = {s['id'] for s, _, _, _ in focal}
            if kind == 'forbidden_transition':
                # Compare start dates, not elapsed rest or adjacent list entries.
                # An intervening duty cannot erase a prohibited next-day pair.
                following = defaultdict(list)
                for t, tp, c, d in entries:
                    if t['assignment_type_id'] in rule['to_type_ids']:
                        following[participation_start_day(tp, zone)].append((t, tp, c, d))
                for s, sp, a, b in entries:
                    if s['assignment_type_id'] not in rule['from_type_ids']:
                        continue
                    next_day = participation_start_day(sp, zone) + timedelta(days=1)
                    for t, tp, c, d in following[next_day]:
                        if s['id'] not in targets and t['id'] not in targets:
                            continue
                        if rule.get('overtime_only') and not (sp['overtime'] or tp['overtime']):
                            continue
                        exc = exception_for(data, policy, pid, a, d)
                        add('RULE_EXCEPTION' if exc else 'FORBIDDEN_TRANSITION',
                            'Approved exception applies' if exc else policy['name'],
                            t['id'] if t['id'] in targets else s['id'], pid, a, d,
                            'warning' if exc else severity, policy,
                            {'exception_id': exc['id']} if exc else {'from_shift_id': s['id'], 'to_shift_id': t['id']})
                for external in person_rows(data, 'availability', pid):
                    if external['kind'] != 'external_commitment':
                        continue
                    day = participation_start_day(external, zone)
                    for s, participant, a, b in focal:
                        target_day = participation_start_day(participant, zone)
                        if ((s['assignment_type_id'] in rule['to_type_ids'] and (target_day - day).days == 1)
                                or (s['assignment_type_id'] in rule['from_type_ids'] and (day - target_day).days == 1)):
                            add('EXTERNAL_CLASSIFICATION_UNKNOWN', 'External duty type is required to check the next-day combination',
                                s['id'], pid, a, b, severity, policy)
                return
            # Scope selects the duty that needs rest, not the work that can consume it.
            # The preceding/following duty can be a different type, day or programme,
            # or end immediately before this policy becomes effective.
            if observed is None:
                selected, unknown_external = workload_selected(data, policy, pid, entries)
                if hours_by_person is not None:
                    hour_windows = [(a, b, workload_actual(rule, selected, a, b, zone)[0][1])
                                    for a, b in _focal_workload_windows(data, rule, selected, focal, zone)]
                    hours_by_person[pid] = focal, unknown_external, hour_windows
            else:
                _, unknown_external, hour_windows = observed
            for r in unknown_external:
                # External commitments represent known work, not leave/attendance.
                # Their precise service/location/OT classification is not supplied.
                a, b = aware(r["start"]), aware(r["end"])
                near = any(a < d + timedelta(days=31) and c - timedelta(days=31) < b for _, _, c, d in focal)
                if near:
                    add("EXTERNAL_CLASSIFICATION_UNKNOWN", "A known external duty lacks classification needed by this scoped/OT-excluding rule; reconcile its details before claiming compliance",
                        focal[0][0]["id"], pid, a, b, severity, policy)

            def violation(code, message, a, b, chosen=None, detail=None):
                exc = exception_for(data, policy, pid, a, b)
                add("RULE_EXCEPTION" if exc else code, "Approved exception applies" if exc else message,
                    (chosen or focal[0][0])["id"], pid, a, b, "warning" if exc else severity, policy,
                    {"exception_id": exc["id"]} if exc else detail)

            if kind in {"min_rest", "travel_buffer"}:
                rest_targets = {s["id"] for s, _, _, _ in focal}
                for gap in workload_gaps(data, rule, selected, rest_targets if kind == 'min_rest' else focus):
                    s, t = gap['previous'], gap['current']
                    detail = {"required_hours": gap['required_hours'], "actual_hours": gap['actual_hours'],
                              "previous_shift_id": s['id'], "next_shift_id": t['id']}
                    if gap['unknown_location']:
                        if gap['actual_hours'] < gap['required_hours']:
                            violation("EXTERNAL_LOCATION_UNKNOWN", "External duty location is unknown; transition feasibility cannot be confirmed", gap['start'], gap['end'], t if t["id"] in focus else s, detail)
                        continue
                    if gap['actual_hours'] < gap['required_hours']:
                        violation("MIN_REST" if kind == "min_rest" else "TRAVEL_BUFFER", policy["name"], gap['start'], gap['end'],
                                  t if t["id"] in focus else s, detail)
            elif kind == "max_hours":
                limit = rule["hours"]
                for a, b, actual in hour_windows:
                    if actual > limit + 1e-9:
                        violation("MAX_HOURS", policy["name"], a, b,
                                  detail={"actual": round(actual, 4), "maximum": limit})
            elif kind == "max_duties":
                per_supervisor = rule.get('group_by') == 'supervisor'
                for a, b in _focal_workload_windows(data, rule, selected, focal, zone):
                    if rule.get('group_by') == 'weekday':
                        for weekday, actual in workload_actual(rule, selected, a, b, zone):
                            if actual > rule['maximum'] and any(s['id'] in targets and a <= c < b
                                    and c.astimezone(ZoneInfo(zone)).weekday() == weekday for s, _, c, _ in selected):
                                violation('MAX_DUTIES', policy['name'], a, b,
                                    detail={'actual': actual, 'maximum': rule['maximum'],
                                            'weekday': weekday, 'group_by': 'weekday'})
                        continue
                    if per_supervisor:
                        if any(a <= c < b and not p.get('supervisor_person_id') for _, p, c, _ in selected):
                            violation('SUPERVISOR_REQUIRED', 'Select a supervisor for matching duties to check this limit', a, b)
                        for supervisor, actual in workload_actual(rule, selected, a, b, zone):
                            if actual > rule['maximum'] and any(s['id'] in targets and a <= c < b
                                    and p.get('supervisor_person_id') == supervisor for s, p, c, _ in selected):
                                violation('MAX_DUTIES', policy['name'], a, b,
                                    detail={'actual': actual, 'maximum': rule['maximum'],
                                            'supervisor_person_id': supervisor, 'group_by': 'supervisor'})
                        continue
                    actual = workload_actual(rule, selected, a, b, zone)[0][1]
                    limit = rule["maximum"]
                    if actual > limit + 1e-9:
                        violation("MAX_DUTIES", policy["name"], a, b,
                                  detail={"actual": round(actual, 4), "maximum": limit})
            elif kind == 'max_consecutive_weekends':
                for a, b, count in workload_weekend_violations(data, policy, pid, selected, targets):
                    violation('CONSECUTIVE_WEEKENDS', policy['name'], a, b,
                              detail={'weekends': count, 'maximum': rule['maximum']})
            elif kind == "max_consecutive_days":
                for run, ordinary in workload_day_observations(selected, zone):
                    counted = consecutive_day_count(rule, len(run), ordinary)
                    if counted > rule["maximum"]:
                        a, b = local_midnight(run[0], zone), local_midnight(run[-1] + timedelta(days=1), zone)
                        if any(overlaps(a, b, c, d) for _, _, c, d in focal):
                            detail = {"days": counted, "maximum": rule["maximum"]}
                            if rule.get('overtime_extension_days'):
                                detail.update(total_days=len(run), ordinary_days=ordinary,
                                              overtime_extension_days=rule['overtime_extension_days'])
                            violation("CONSECUTIVE_DAYS", policy["name"], a, b, detail=detail)

        policy_parts = {}
        old_parts = (retained_people or {}).get(str(policy_index), {})
        for pid, entries in commitments.items():
            if only is not None and pid not in only or named is not None and pid not in named:
                continue
            if retained_people is not None and pid not in partial['people']:
                retained = old_parts.get(pid, [])
                findings.extend(retained)
                if retained:
                    policy_parts[pid] = retained
                continue
            found_at = len(findings)
            signature = signatures.get(pid)
            cached = person_cache.get((id(policy), pid)) if person_cache is not None else None
            if cached is not None and cached[0] is policy and cached[1] == signature:
                # Replay canonical additions, including duplicate suppression and current finding decoration.
                for args, kwargs in cached[3]:
                    add(*args, **kwargs)
                if session_parts and len(findings) > found_at:
                    policy_parts[pid] = findings[found_at:]
                continue
            additions = []
            def record(*args, **kwargs):
                additions.append((args, kwargs))
                add(*args, **kwargs)
            check_person(pid, entries, record if person_cache is not None else add)
            if session_parts and len(findings) > found_at:
                policy_parts[pid] = findings[found_at:]
            if person_cache is not None:
                # Retain the session objects so a discarded trial's identity cannot be recycled into a cache hit.
                person_cache[id(policy), pid] = (policy, signature, tuple(s for s, _, _, _ in entries), additions)

        if policy_parts:
            personal_parts[str(policy_index)] = policy_parts

    from .quotas import evaluate_quotas, METRIC_VERSION
    if partial is not None and not complete_partial:
        if partial.get("include_resources"):
            from .resources import evaluate_resources
            evaluate_resources(data, definitions, shifts, partial["shifts"], add, ancestor_ids, valid_dates)
        subjects = partial["people"] if data.get("quota_subjects") is None else partial["people"] & set(data["quota_subjects"])
        # Own-duty prechecks request no quotas; department reports cannot decide their overlap/rest/limit errors.
        quota_report = evaluate_quotas({**data, "quota_subjects": subjects}, add, ancestor_ids, exception_for,
                                       explain=causes.quota if causes else None, contribution_cache=contribution_cache,
                                       **({'person_shifts': person_shifts} if person_shifts is not None else {})) if subjects else None
        # Which check each finding came from: the sessions' own checks, or the people's (overlaps, limits, quotas).
        return {"session_findings": grouped_rule_findings([f for f in findings[:session_found] if f["code"] != "TIME_OVERLAP"], data['policies'], tiers=data.get('_policy_group_tiers')),
                "person_findings": grouped_rule_findings([f for f in findings[:session_found] if f["code"] == "TIME_OVERLAP"] + findings[session_found:], data['policies'], tiers=data.get('_policy_group_tiers')),
                'coverage': coverage,
                "quota_report": quota_report}

    from .patterns import evaluate_shapes
    evaluate_shapes({**data, 'policies': counted_policies}, commitments, focus, add)
    check()

    quota_report = (None if data.get('_skip_staffing_quota') else
                    evaluate_quotas(data, add, ancestor_ids, exception_for,
                                    explain=causes.quota if causes else None,
                                    contribution_cache=contribution_cache))
    check()

    from .fairness import evaluate_goals
    goals = (evaluate_goals(data, add, quota_report=quota_report)
             if data.get("workspace") and not data.get('_skip_staffing_quota') else [])
    check()

    from .resources import evaluate_resources
    evaluate_resources(data, definitions, shifts, focus, add, ancestor_ids, valid_dates)

    findings = grouped_rule_findings(findings, data['policies'], tiers=data.get('_policy_group_tiers'))
    check()
    if '_published_session_checks' in data:
        checked = data['_published_session_checks']
        people = data['person_rule_subjects']
        findings = [f for f in findings if f['shift_id'] in checked or f['person_id'] in people
                    or checked.intersection((f.get('detail') or {}).get('affected_shift_ids', []))]
    from .generation_scope import population_info
    population = population_info(data)
    return {"engine_version": ENGINE_VERSION, "contracts": check_contracts(), "fingerprint": data.get("fingerprint"),
            "valid": not any(f["severity"] == "error" for f in findings),
            "error_count": sum(f["severity"] == "error" for f in findings),
            "warning_count": sum(f["severity"] == "warning" for f in findings),
            "findings": findings, "coverage": coverage,
            **({'_person_parts': personal_parts} if session_parts else {}),
            **({'_session_parts': [index for index, f in enumerate(findings)
                if tuple(f.get(key) for key in ('code', 'shift_id', 'person_id', 'rule_id', 'start')) in session_keys]}
               if session_parts else {}),
            **({"quota_report": quota_report} if quota_report is not None else {}),
            **({"goals": goals} if goals else {}),
            **({"planning_population": population} if population is not None else {})}
