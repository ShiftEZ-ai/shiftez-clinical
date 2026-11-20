import time

from .workspace_access import shared_clause
from bisect import bisect_right
from datetime import date, timedelta
import re
from types import SimpleNamespace as Line

from sqlalchemy import func, select, tuple_
from packages.contracts import schemas as s
from packages.domain.conditions import (TAG_FIELDS, absolute_values, tag_scope, with_tags, check_definition_count_budget,
                                        scope_applies, scope_has_duty_witness)
from packages.domain.validation import evaluate, finding_observations
from packages.domain.feasibility import unchanged_input_issues
from packages.domain.solver_rules import definition_issues, COMPILER_VERSION
from packages.domain.rule_map import numeric_limit
from packages.domain.rule_serialization import stored_definition
from apps.api import models as m
from apps.api.auth import require_catalogue_read, require_permission, require_role, person_access, current_roles
from apps.api.errors import require
from apps.api.serialization import CHECK_SUMMARY, record, json_value
from apps.api.snapshots import (DomainSnapshot, domain_snapshot, fingerprint, feasibility_fingerprints,
                                snapshot_hash, snapshot_inputs, tag_data)
from .common import scoped, expected, bump_inputs, history, history_many, history_version
from .date_sets import check_policy_references
from .reviews import person_key
from . import independence

ACTIVATION_PHASES = (
    'receipts',
    'feasibility_fingerprints',
    'policy_impact',
    'compile',
    'validation',
    'serialized_writer',
)


def activation_phase_timings(db):
    """Return the ordered, monotonic activation timings carried by a command."""
    existing = db.info.get('_activation_phase_timings') or {}
    timings = {name: existing.get(name, 0.0) for name in ACTIVATION_PHASES}
    db.info['_activation_phase_timings'] = timings
    return timings


def _activation_phase(db, name, function, *args, **kwargs):
    if not db.info.get('_activation_timing'):
        return function(*args, **kwargs)
    started = time.monotonic()
    try:
        return function(*args, **kwargs)
    finally:
        activation_phase_timings(db)[name] += time.monotonic() - started


def policy_versions(db, actor, rows):
    """Approval labels on policy versions; absent historical evidence stays unknown."""
    if not rows:
        return []
    ids = {row['id'] for row in rows}
    identity_ids = {row[key] for row in rows for key in ('approved_by', 'created_by') if row.get(key)}
    mapped = set(db.scalars(select(m.SourceMapping.target_id).where(m.SourceMapping.department_id == actor.department_id,
        m.SourceMapping.target_kind == 'policies', m.SourceMapping.target_id.in_(ids))))
    markers = {row['id']: match[1] for row in rows if (match := re.search(r'; archive ([0-9a-f]{64})', row.get('source') or ''))}
    archives = set(db.scalars(select(m.ReferenceImport.archive_sha256).where(
        m.ReferenceImport.department_id == actor.department_id,
        m.ReferenceImport.archive_sha256.in_(set(markers.values()))))) if markers else set()
    evidence, revisions, reasons, previous, stamps = {}, {}, {}, {}, {}
    H = m.DefinitionHistory
    histories = list(db.execute(select(
            H.object_id, H.object_revision, H.actor_id, H.created_at, H.snapshot['approved_by'].as_string(),
            H.snapshot['status'].as_string(), H.reason, H.snapshot['approval_transition'].as_boolean()).where(
            H.department_id == actor.department_id, H.kind == 'policy', H.object_id.in_(ids))
            .order_by(H.object_revision, H.created_at, H.id)))
    # Older conversion destinations were created active; their reviewed conversion proves that exact initial version.
    converted = {row['id'] for row in rows if row.get('source') == 'Rule catalogue v2 department conversion'}
    first = {}
    for item in histories:
        first.setdefault(item[0], item)
    unproven = {ident for ident, (_, revision, by, _, saved_by, status, _, transition) in first.items()
                if ident in converted and revision == 1 and status == 'active' and by == saved_by and transition is not True}
    pins = {(pin['policy_id'], pin['revision']) for row in rows if row['id'] in unproven
            for line in row['definition']['rule'].get('lines', [])
            for pin in line.get('source_bindings', {}).values()}
    conversions = {}
    if pins:
        B, C = m.RuleSourceBinding, m.CatalogueConversion
        for destinations, by, at in db.execute(select(B.data['destinations'], C.approved_by, C.approved_at)
                .join(C, C.id == B.migration_proposal_id).where(B.department_id == actor.department_id,
                    C.department_id == actor.department_id, C.status.in_(('active', 'retired')),
                    C.approved_by.is_not(None), C.approved_at.is_not(None),
                    tuple_(B.source_policy_id, B.source_revision).in_(pins))):
            for destination in destinations:
                if destination['kind'] == 'policy' and destination['id'] in unproven:
                    conversions[(destination['id'], 1, by)] = json_value(at)
    # A v2 source row is a projection, so its copied approved_by belongs to the
    # retired v1 source. Resolve its own conversion or fact approval instead.
    v2_ids = {row['id'] for row in rows if row.get('_v2_source')}
    fact_approvals = {}
    if v2_ids:
        fact_rows = db.scalars(select(m.SchedulingFact).where(
            m.SchedulingFact.department_id == actor.department_id, m.SchedulingFact.id.in_(v2_ids)))
        fact_approvals.update({fact.id: (fact.approved_by, json_value(fact.approved_at)) for fact in fact_rows})
    conversion_approvals = {}
    if v2_ids:
        B, C = m.RuleSourceBinding, m.CatalogueConversion
        for source_id, by, at in db.execute(select(B.source_policy_id, C.approved_by, C.approved_at)
                .join(C, C.id == B.migration_proposal_id).where(
                    B.department_id == actor.department_id, C.department_id == actor.department_id,
                    C.status.in_(('active', 'retired')), B.source_policy_id.in_(v2_ids),
                    C.approved_by.is_not(None), C.approved_at.is_not(None))
                .order_by(C.approved_at.desc(), C.id.desc())):
            conversion_approvals.setdefault(source_id, (by, json_value(at)))
    identity_ids.update(by for by, _ in fact_approvals.values() if by)
    identity_ids.update(by for by, _ in conversion_approvals.values() if by)
    identities = {id_: (name, issuer) for id_, name, issuer in db.execute(select(
        m.Identity.id, m.Identity.display_name, m.Identity.issuer).where(m.Identity.id.in_(identity_ids)))}
    for policy_id, revision, by, at, saved_by, status, reason, transition in histories:
        reasons.setdefault(policy_id, reason)
        prior = previous.get(policy_id)
        preserved = transition is False or (transition is not True and reason in {
            'Source retained before catalogue conversion', 'Original definition retained before private metadata repair'})
        approved = (status == 'active' and saved_by and by == saved_by and not preserved
                    and (transition is True or prior == (revision - 1, 'proposed', None)))
        converted_at = conversions.get((policy_id, revision, saved_by))
        if approved or converted_at:
            stamps[policy_id] = converted_at or json_value(at)
        elif prior is None or prior[2] != saved_by:
            stamps[policy_id] = None
        previous[policy_id] = (revision, status, saved_by)
        revisions.setdefault(policy_id, []).append(revision)
        evidence.setdefault(policy_id, []).append((saved_by, stamps.get(policy_id)))
    result = []
    for row in rows:
        at = None
        approval_by = row.get('approved_by')
        index = bisect_right(revisions.get(row['id'], []), row['revision']) - 1
        if index >= 0 and row.get('approved_by'):
            saved_by, stamp = evidence[row['id']][index]
            at = stamp if saved_by == row['approved_by'] else None
        if row.get('_v2_source'):
            approval_by, at = fact_approvals.get(row['id'], conversion_approvals.get(row['id'], (None, None)))
        imported = (row['id'] in mapped or identities.get(row.get('created_by'), ('', ''))[1] == 'urn:fmuc:reference-import'
                    or markers.get(row['id']) in archives)
        reason = ('Grouped imported requirements for review.' if row['definition'].get('merged_ids') else
                  'Imported scheduling requirement for review.') if imported else reasons.get(row['id'], row.get('source'))
        result.append({**row, 'approved_by_name': identities.get(approval_by, (None, None))[0],
                       'approved_at': at, 'reason': reason})
    return result


def policies_with_status(db, actor, *statuses):
    """Read lifecycle state after earlier transitions, including unflushed batch writes."""
    rows = dict.fromkeys([*db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id,
                                                           m.Policy.status.in_(statuses))),
                          *(p for p in set(db.new) | set(db.dirty) if isinstance(p, m.Policy))])
    return [p for p in rows if p not in db.deleted and p.department_id == actor.department_id and p.status in statuses]


def advance_revision(db, policy):
    """Carry unchanged seeded lines into the stored definition before advancing their bound revision."""
    if policy.kind == 'catalogue':
        from .scoring_defaults import seeded_rows
        policy.definition = seeded_rows(db, [record(policy)])[0]['definition']
    policy.revision += 1


def effective_source(db, actor, policy):
    """Resolve a converted identity from approved destinations for existing rule-reference commands."""
    return effective_sources(db, actor, [policy])[policy.id]


def effective_sources(db, actor, policies):
    """Resolve a source collection with one scheduling snapshot and one binding expansion."""
    require_catalogue_read(actor, 'policies')
    from apps.api.snapshots import scheduling_inputs
    from packages.domain.rule_lines import bound_policies
    context = scheduling_inputs(db, [actor.department_id])
    bound = {b['source_policy_id'] for b in context['source_bindings']}
    if not any(p.id in bound for p in policies):
        from packages.domain.rule_lines import seeded_policies
        return {p['id']: p for p in seeded_policies([record(p) for p in policies], context['policy_seed_lines'])}
    rows = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id))]
    effective = {p['id']: p for p in bound_policies(rows, context)}
    return {p.id: effective.get(p.id, {**record(p), 'status': 'retired', 'active': False}) for p in policies}


def condition_reference_rows(db, tree, tags=True, *, reference_rows=None):
    """Resolve a union once; callers retain the rows and authorize references in their original order."""
    from .planning import chunks
    classes = {"person_ids": m.Person, "usual_assignee_ids": m.Person, "category_ids": m.Category,
               "capability_ids": m.Capability, "programme_ids": m.Programme,
               "rotation_type_ids": m.RotationType, "cohort_ids": m.Cohort, "service_ids": m.Service, "eligible_service_ids": m.Service,
               "location_ids": m.Location,
               "assignment_type_ids": m.AssignmentType, "date_set_ids": m.DateSet}
    lines = list(tree.conditions) if tree else []
    for line in lines:
        if isinstance(line, s.Conditions):
            lines.extend(line.conditions)
    references = [(line, absolute_values(line) or [], classes[line.field]) for line in lines
                  if not isinstance(line, s.Conditions) and line.field in classes]
    reference_rows = {} if reference_rows is None else reference_rows
    identifiers = {}
    for line, values, cls in references:
        identifiers.setdefault(cls, set()).update(values)
        if tags and line.field in TAG_FIELDS:
            identifiers.setdefault(m.Tag, set()).update(values)
    for cls, values in identifiers.items():
        rows = reference_rows.setdefault(cls, {})
        missing = set(values) - rows.keys()
        if missing:
            # Keep fetched objects alive across expanded lines; the ORM identity map holds weak references.
            rows.update(dict.fromkeys(missing))
            for group in chunks(sorted(missing)):
                rows.update((row.id, row) for row in db.scalars(select(cls).where(cls.id.in_(group))))
    return references, reference_rows


def condition_references(db, actor, tree, tags=True, *, reference_rows=None):
    """Every record a condition tree names belongs to this department (rule scopes, who may lead)."""
    referenced, reference_rows = condition_reference_rows(db, tree, tags, reference_rows=reference_rows)
    for line, values, cls in referenced:
        for rid in values:
            tag = reference_rows[m.Tag].get(rid) if tags and line.field in TAG_FIELDS else None
            if tag is not None and tag.department_id == actor.department_id:
                scoped(db, m.Tag, rid, actor)
                # A tag stands for its members of the line's kind (conditions.with_tags).
                require(TAG_FIELDS[line.field] in (tag.labels or []), "TAG_KIND",
                        f"The tag {tag.name} does not label {TAG_FIELDS[line.field].replace('_', ' ')}")
                continue
            scoped(db, cls, rid, actor)
        if line.field == 'date_set_ids':
            from .date_sets import references
            references(db, actor, values, line.entry_names, op=line.op)


def policy_references(db, actor, definition, *, reference_rows=None):
    reference_rows = {} if reference_rows is None else reference_rows
    scope = definition.scope
    if isinstance(definition.rule, s.AllocationPreference):
        for person_id in definition.rule.supervisor_ids:
            scoped(db, m.Person, person_id, actor)
    if isinstance(definition.rule, s.MinimumCover):
        for person_id in definition.rule.when_person_ids:
            scoped(db, m.Person, person_id, actor)
        condition_references(db, actor, definition.rule.credit_conditions, reference_rows=reference_rows)
        for tree in definition.rule.selection_order:
            condition_references(db, actor, tree, reference_rows=reference_rows)
    if isinstance(definition.rule, s.BalanceGoal):
        condition_references(db, actor, definition.rule.partners, reference_rows=reference_rows)
        condition_references(db, actor, definition.rule.within, reference_rows=reference_rows)
        for type_id in definition.rule.assignment_type_ids:
            scoped(db, m.AssignmentType, type_id, actor)
        for service_id in definition.rule.service_ids:
            scoped(db, m.Service, service_id, actor)
    if isinstance(definition.rule, s.AllocationOvertime):
        from packages.domain.conditions import active_rules
        for policy_id in definition.rule.required_policy_ids:
            source = record(scoped(db, m.Policy, policy_id, actor))
            # Admission checks the referenced limit's kind; its actual on/off state still gates overtime at evaluation.
            if source['definition']['rule']['kind'] == 'catalogue':
                source = {**source, 'definition': {**source['definition'], 'rule': {**source['definition']['rule'], 'on': True}}}
            limits = active_rules([{**source, 'status': 'active', 'active': True}], overrides=False)
            require(any(cap['id'] == policy_id and cap['definition']['severity'] == 'mandatory' and (
                        cap['definition']['rule']['kind'] == 'block_quota' and cap['definition']['rule']['comparison'] in ('maximum', 'range')
                        or cap['definition']['rule']['kind'] in {'forbidden_transition', 'max_consecutive_weekends'}) for cap in limits),
                    'OVERTIME_CAP_REQUIRED', 'Required policies must be mandatory maximum quotas, duty-transition or consecutive-weekend restrictions')
    if definition.workspace_id:
        workspace = scoped(db, m.Workspace, definition.workspace_id, actor)
        from .workspace_access import access_record
        require(access_record(db, workspace)['purpose'] != 'sandbox', 'SANDBOX_RULE_ADJUSTMENT_ONLY',
                'Use a rota-only adjustment to experiment in a sandbox', 409)
    condition_references(db, actor, scope.conditions, reference_rows=reference_rows)
    # The flat scope fields read as condition lines: the same records, and a department tag where a line may name one
    # (a catalogue line's who or where naming a #tag is written to a flat field).
    flat = [Line(field=f, values=getattr(scope, f)) for f in ("person_ids", "category_ids", "service_ids", "capability_ids")] + [
        Line(field=f + "s", values=[getattr(scope, f)]) for f in ("person_id", "category_id", "service_id", "location_id", "programme_id",
                                                                   "rotation_type_id", "assignment_type_id") if getattr(scope, f)]
    condition_references(db, actor, Line(conditions=flat), reference_rows=reference_rows)
    if isinstance(definition.rule, (s.MaximumParticipants, s.SupervisionLimit)):
        for category_id in definition.rule.overflow_category_ids:
            scoped(db, m.Category, category_id, actor)
    if isinstance(definition.rule, s.SupervisionLimit):
        for category_id in definition.rule.trainee_category_ids:
            scoped(db, m.Category, category_id, actor)
    if isinstance(definition.rule, s.BlockQuota):
        for type_id in definition.rule.assignment_type_ids:
            scoped(db, m.AssignmentType, type_id, actor)
    if isinstance(definition.rule, s.TravelBuffer):
        scoped(db, m.Location, definition.rule.from_location_id, actor)
        scoped(db, m.Location, definition.rule.to_location_id, actor)
    if isinstance(definition.rule, s.ForbiddenTransition):
        for type_id in definition.rule.from_type_ids + definition.rule.to_type_ids:
            scoped(db, m.AssignmentType, type_id, actor)
    # Keep the complete reference union alive while scoped() applies the usual
    # organisation/department checks in each line's original order.
    references = [Line(field=field, values=values) for row in definition.per_person
                  for field, values in [('person_ids', [row.person_id] if row.person_id else []),
                      ('programme_ids', [row.programme_id] if row.programme_id else []),
                      ('service_ids', row.service_ids), ('assignment_type_ids', row.assignment_type_ids)]]
    condition_reference_rows(db, Line(conditions=references), tags=False, reference_rows=reference_rows)
    for row in definition.per_person:
        for cls, ids in [(m.Person, [row.person_id] if row.person_id else []), (m.Programme, [row.programme_id] if row.programme_id else []),
                         (m.Service, row.service_ids), (m.AssignmentType, row.assignment_type_ids)]:
            for rid in ids:
                scoped(db, cls, rid, actor)
    if definition.calendar_entry_id:
        scoped(db, m.ProgrammeCalendarEntry, definition.calendar_entry_id, actor)
    for policy_id in definition.merged_ids + ([definition.replaces_id] if definition.replaces_id else []):
        scoped(db, m.Policy, policy_id, actor)
    override_checks(db, actor, definition)


MERGED_INTO = "Grouped into "
CHANGED_INTO = "Changed into "
KIND_NAMES = {'restriction': 'time restriction', 'min_rest': 'minimum rest', 'forbidden_transition': 'next-day combination',
              'supervision_limit': 'supervision', 'spread_evenly': 'spread evenly', 'minimum_cover': 'minimum cover', 'maximum_participants': 'maximum people'}


def override_checks(db, actor, definition):
    """A rule may replace another for the people it matches only when that can never weaken safety: both are advisory,
    or the other is a numeric limit and this is the same kind of limit for a strictly narrower group."""
    from packages.domain.conditions import implies
    for target_id in definition.overrides:
        other = scoped(db, m.Policy, target_id, actor)
        theirs = s.PolicyDefinition.model_validate(other.definition, context={'historical': True})
        kind = theirs.rule.kind
        require(other.status != "retired", "OVERRIDE_RETIRED", f"“{other.name}” is retired; choose a rule in use")
        require(not s.safety_rule(other.definition), "OVERRIDE_SAFETY",
                f"“{other.name}” is a {KIND_NAMES.get(kind, kind)} rule. Safety rules can never be replaced by another rule; "
                "propose a change to that rule instead")
        if definition.severity == "advisory" and theirs.severity == "advisory":
            continue
        numeric = numeric_limit(theirs.rule.model_dump())
        require(numeric, "OVERRIDE_NOT_ALLOWED",
                f"“{other.name}” cannot be replaced: only advisory rules (when this rule is advisory too) and numeric limits can be")
        require(definition.rule.kind == kind and (kind != "block_quota" or definition.rule.comparison == "maximum"), "OVERRIDE_KIND",
                f"A limit can only be replaced by the same kind of limit; “{other.name}” is a {kind.replace('_', ' ')} rule")
        require(theirs.severity == "advisory" or definition.severity == "mandatory", "OVERRIDE_SEVERITY",
                f"“{other.name}” is mandatory, so the rule replacing it must be mandatory too")
        mine, their = definition.scope.model_dump(mode="json"), theirs.scope.model_dump(mode="json")
        require(implies(mine, their) and not implies(their, mine), "OVERRIDE_NOT_SPECIFIC",
                f"This rule must be more specific than “{other.name}”: its conditions have to pick out a narrower group "
                "within the people, duties, days and hours that rule covers")


def people_data(db, actor, day):
    """What the people side of a rule reads (dated ranks, skills, programmes, rotations, cohorts, clinics they can
    work at) for people in the department on `day`, its retained duty contexts, and that day's local bounds."""
    from datetime import timedelta
    from packages.domain.time import local_midnight
    department = db.get(m.Department, actor.department_id)
    zone = db.get(m.Organisation, department.organisation_id).timezone
    rows = lambda cls: [record(r) for r in db.scalars(select(cls).where(cls.department_id == actor.department_id))]
    data = DomainSnapshot({"organisation": {"timezone": zone}, "memberships": rows(m.Membership), "grants": rows(m.CapabilityGrant),
            "programme_memberships": rows(m.ProgrammeMembership), "rotations": rows(m.Rotation), "eligibilities": rows(m.Eligibility)})
    for name, cls in {"categories": m.Category, "capabilities": m.Capability, "programmes": m.Programme,
                      "rotation_types": m.RotationType, "cohorts": m.Cohort, "types": m.AssignmentType,
                      "services": m.Service, "locations": m.Location}.items():
        data[name] = {r["id"]: r for r in rows(cls)}
    members = {r["person_id"] for r in data["memberships"] if r["starts_on"] <= day.isoformat() and (not r.get("ends_on") or day.isoformat() < r["ends_on"])}
    data["people"] = {p.id: record(p) for p in db.scalars(select(m.Person).where(m.Person.id.in_(list(members) or [""]), m.Person.active.is_(True)))}
    data["tags"] = tag_data(db, [actor.department_id])
    from apps.api.snapshots import scheduling_inputs
    data.update(scheduling_inputs(db, [actor.department_id]))
    return with_tags(data), local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)


def people_now(db, actor, scope, people=None):
    """How many people in the department today match the rule's people side (the same evaluator the checks use)."""

    data, a, b = people or people_data(db, actor, independence.today(db, actor.department_id))
    scope = tag_scope(data, scope)
    return sum(scope_applies(data, scope, pid, None, a, b) for pid in data["people"])


def people_period(db, actor, first, last):
    """Dated membership union, shared by saved and unsaved line previews."""
    from packages.domain.time import local_midnight
    require(first < last, 'REACH_PERIOD', 'The end date must be after the start date', 422)
    data, a, _ = people_data(db, actor, first)
    members = {r['person_id'] for r in data['memberships'] if r['starts_on'] < last.isoformat()
               and (not r.get('ends_on') or first.isoformat() < r['ends_on'])}
    data['people'] = {p.id: record(p) for p in db.scalars(select(m.Person).where(
        m.Person.id.in_(members or {''}), m.Person.active.is_(True)))}
    return data, a, local_midnight(last, data['organisation']['timezone'])


def rules_for_person(db, actor, person_id, day):
    """Which active department rule applies to a person on a day, with the number they get and why: their own line
    of a rule template, the rule's conditions, or another rule that replaces it for them."""
    from packages.domain.conditions import active_rules
    from packages.domain.quotas import MAX_SUBJECT_CONTEXTS
    from packages.domain.validation import index_person_rows, within_dates
    require_catalogue_read(actor, 'policies')
    person = scoped(db, m.Person, person_id, actor)
    data, a, b = people_data(db, actor, day)
    data["people"][person.id] = record(person)
    zone = data["organisation"]["timezone"]
    policies = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id))]
    data["policies"] = [p for p in policies if not p["definition"].get("workspace_id")]
    data.pop('_catalogue_v2_materialized', None)
    policies = [p for p in with_tags(data)["policies"] if p['status'] == 'active']
    data["policies"] = active_rules(policies, context=data)
    data['_person_rows'] = index_person_rows(data)
    data['_line_index_cache'], data['_person_dates_cache'] = {}, {}
    context_budget = data['_quota_subject_context_budget'] = [MAX_SUBJECT_CONTEXTS]
    plain = active_rules(policies, overrides=False, context=data)
    names = {p["id"]: p["name"] for p in policies}
    def matched(copy):
        if not within_dates(copy, a, b, zone):
            return False
        scope = copy['definition']['scope']
        return scope_has_duty_witness(data, scope, person.id, a, b, copy['department_id'], budget=context_budget)
    result = []
    for policy in sorted(policies, key=lambda p: p["name"].casefold()):
        if policy["definition"]["rule"]["kind"] == "catalogue":
            # Number by number: each copy that holds is one number they get, from the line that sets it (a number their
            # own line leaves blank comes from a broader line, whose copy then holds for them too).
            for c in (c for c in data["policies"] if c["id"] == policy["id"] and matched(c)):
                safety = s.safety_rule(c['definition'])
                line = c["template_line"]
                result.append({"policy_id": policy["id"], "name": policy["name"], "kind": c["kind"], "severity": c["definition"]["severity"],
                               "applies": True, "rule": c["definition"]["rule"], "own_line": line, "overridden_by": [], "overrides": [],
                               "why": f"Line {line['line'] + 1} of this rule: its {line['slot'].replace('+', ' and ')}"
                                      + (" (safety: every line that holds applies)" if safety else " (the most specific line that sets it)")})
            continue
        hit = next((c for c in data["policies"] if c["id"] == policy["id"] and matched(c)), None)
        base = hit or next((c for c in plain if c["id"] == policy["id"] and matched(c)), None)
        if base is None:
            continue
        winners = [o["id"] for o in policies if policy["id"] in (o["definition"].get("overrides") or [])
                   and any(c["id"] == o["id"] and matched(c) for c in data["policies"])]
        line = base.get("template_line")
        why = ("Replaced for this person by " + ", ".join(f"“{names[w]}”" for w in winners) if hit is None
               else f"Line {line['line'] + 1} of this rule (the most specific line that holds for them)" if line and line.get("id")
               else "Their own number in this rule’s per-person table" if line
               else "Matches this rule’s conditions" + (" (not listed in its per-person table)" if policy["definition"].get("per_person") else ""))
        result.append({"policy_id": policy["id"], "name": policy["name"], "kind": base["kind"], "severity": base["definition"]["severity"],
                       "applies": hit is not None, "rule": base["definition"]["rule"], "own_line": line,
                       "overridden_by": winners if hit is None else [], "overrides": policy["definition"].get("overrides") or [], "why": why})
    return {"person_id": person.id, "day": day.isoformat(), "rules": result}


def policy_reach(db, actor, day, workspace_id=None, starts_on=None, ends_on=None, details=False, policy_id=None, line_id=None, *, people=None):
    """How many people each active or proposed department rule reaches on a day (the evaluator every check uses)."""
    from packages.domain.rule_map import reach
    require_catalogue_read(actor, 'policies')
    require((starts_on is None) == (ends_on is None), 'REACH_PERIOD', 'Choose both start and end dates', 422)
    if policy_id and line_id and starts_on is None:
        from packages.domain.rule_lines import when
        from .scoring_defaults import seeded_rows
        policy = scoped(db, m.Policy, policy_id, actor)
        rule = seeded_rows(db, [record(policy)])[0]['definition']['rule']
        line = next((line for line in rule.get('lines') or [] if line['id'] == line_id), None)
        require(line is not None, 'NOT_FOUND', 'Rule line not found', 404)
        first, last = when(rule, line)
        starts_on = max(policy.starts_on, date.fromisoformat(str(first)) if first else policy.starts_on)
        ends = [date.fromisoformat(str(last)) if last else None, policy.ends_on]
        if workspace_id:
            workspace = scoped(db, m.Workspace, workspace_id, actor)
            starts_on = max(starts_on, workspace.starts_on)
            ends.append(workspace.ends_on)
        ends_on = min([end for end in ends if end is not None], default=starts_on + timedelta(days=1))
    details = details or workspace_id is not None or starts_on is not None or line_id is not None
    if details:
        require_role(actor, 'coordinator', 'approver', 'programme_lead', 'auditor')
    if workspace_id:
        workspace = scoped(db, m.Workspace, workspace_id, actor)
        data = domain_snapshot(db, workspace)
        from packages.domain.time import local_midnight
        first, last = starts_on or workspace.starts_on, ends_on or workspace.ends_on
        a, b = (local_midnight(d, data['organisation']['timezone']) for d in (first, last))
        require(workspace.starts_on <= first and last <= workspace.ends_on, 'REACH_PERIOD',
                'Choose a period inside this rota', 422)
        policies = [p for p in data['policies'] if p['department_id'] == actor.department_id and p['status'] in ('active', 'proposed')]
    else:
        data, a, b = people_period(db, actor, starts_on, ends_on) if starts_on else people or people_data(db, actor, day)
        from .scoring_defaults import seeded_rows
        policies = seeded_rows(db, [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id))])
        data['policies'] = [p for p in policies if not p['definition'].get('workspace_id')]
        policies = [p for p in with_tags(data)['policies'] if p['status'] in ('active', 'proposed')]
    require(a < b, 'REACH_PERIOD', 'The end date must be after the start date', 422)
    selected = [p for p in policies if policy_id is None or p['id'] == policy_id]
    validation_selected = selected
    if policy_id is not None and not selected:
        require(workspace_id is not None, 'NOT_FOUND', 'Rule not found', 404)
        from .scoring_defaults import seeded_rows
        validation_selected = seeded_rows(db, [record(scoped(db, m.Policy, policy_id, actor))])
        require(validation_selected[0]['status'] in ('active', 'proposed'), 'NOT_FOUND', 'Rule not found', 404)
        if not workspace_id:
            selected = validation_selected
    elif policy_id is not None:
        from .scoring_defaults import seeded_rows
        validation_selected = selected = seeded_rows(db, selected)
    if line_id is not None:
        require(any(line.get('id') == line_id for p in validation_selected for line in p['definition']['rule'].get('lines') or []),
                'NOT_FOUND', 'Rule line not found', 404)
    effective_policies = selected if policy_id is not None else policies
    if not details:
        return {'reach': reach(data, effective_policies, a, b, policy_id=policy_id)}
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(data['organisation']['timezone'])
    return {**reach(data, effective_policies, a, b, details=True, policy_id=policy_id, line_id=line_id),
            'starts_on': a.astimezone(zone).date().isoformat(), 'ends_on': b.astimezone(zone).date().isoformat(),
            'workspace_id': workspace_id}


def check_policy(db, actor, definition, *, preview=True):
    require_catalogue_read(actor, 'policies')
    require_role(actor, "coordinator", "admin", "approver")
    issues = _activation_phase(db, 'compile', definition_issues, definition)
    result = {"compatible": not issues, "issues": issues, "compiler_version": COMPILER_VERSION, "feasibility": "not_checked"}
    if not issues:
        def validate():
            value = s.PolicyDefinition.model_validate(definition)
            policy_references(db, actor, value)
            if not preview:
                # Activation needs the admission checks; reach details belong to the operator's preview.
                check_definition_count_budget({'tags': tag_data(db, [actor.department_id])}, value.model_dump(mode='json'))
                return result
            from packages.domain.rule_map import reach
            people = people_data(db, actor, independence.today(db, actor.department_id))
            data, a, b = people
            result["people_now"] = people_now(db, actor, value.scope.model_dump(mode="json"), people)
            preview_record = {'id': 'preview', 'department_id': actor.department_id, 'status': 'active', 'active': True,
                              'starts_on': None, 'ends_on': None, 'definition': value.model_dump(mode='json')}
            check_definition_count_budget(data, preview_record['definition'])
            data['policies'] = [preview_record]
            result['reach_details'] = reach(data, with_tags(data)['policies'], a, b, details=True)['details'].get('preview', {'lines': []})
            return result
        result = _activation_phase(db, 'validation', validate)
    return result


def require_policy_version(db, actor, *definitions, retained=()):
    if not definitions:
        return
    converted = db.scalar(select(m.CatalogueConversion.id).where(m.CatalogueConversion.department_id == actor.department_id,
        m.CatalogueConversion.status == 'active'))
    if not converted:
        return
    from .catalogue_version import department_version

    def admitted(definition):
        rule = definition['rule']
        if rule['kind'] == 'catalogue' and rule.get('catalogue_version', 1) == 2:
            return True
        if rule['kind'] not in {'balance_goal', 'block_quota'} or definition['severity'] != 'advisory' or definition.get('merged_ids'):
            return False
        # Existing approved defaults and their reviewed successors retain their authenticated provenance on Undo.
        old = next((p for p in retained if p.definition == definition and p.approved_by is not None), None)
        if old is not None:
            candidate = {**record(old), 'status': 'active'}
        elif previous := definition.get('replaces_id'):
            old = scoped(db, m.Policy, previous, actor)
            if not (old.status == 'active' and old.active and old.kind == rule['kind'] == old.definition['rule']['kind']
                    and old.definition['severity'] == 'advisory'):
                return False
            candidate = old
        else:
            return False
        return department_version(db, actor.department_id, [candidate]) == 2

    require(all(admitted(d) for d in definitions), 'USE_CATALOGUE_V2',
            'Propose v2 catalogue lines or person scheduling facts in a converted department', 409)


def create_policy(db, actor, command, catalogue=False):
    require_role(actor, "coordinator", "admin")
    value = s.PolicyInput.model_validate(command.payload)
    require(value.definition.rule.kind != 'team_lead_requirement' and value.definition.legacy_lead_conversion is None,
            'USE_RULE_CATALOGUE', 'Change team leads in Rules', 422)
    require(value.definition.seeded_line_ids is None, 'SOURCE_BINDING_IMMUTABLE',
            'The proposal seed baseline is captured by the server', 409)
    # Catalogue rules are proposed through the catalogue (one waiting version each, its checks): rule.propose.
    require(catalogue or value.definition.rule.kind != "catalogue", "USE_RULE_CATALOGUE", "Propose catalogue rules from the rule catalogue", 422)
    require_policy_version(db, actor, value.definition.model_dump(mode='json'))
    checked = check_policy(db, actor, value.definition)
    require(checked['compatible'], "SOLVER_RULE_INCOMPATIBLE",
            "Adjust the highlighted rule settings before saving", 422, checked['issues'])
    values = value.model_dump()
    values["definition"] = stored_definition(json_value(values["definition"]))
    if catalogue:
        from .scoring_defaults import seed_line_ids
        sources = value.definition.merged_ids + ([value.definition.replaces_id] if value.definition.replaces_id else [])
        values['definition']['seeded_line_ids'] = sorted(seed_line_ids(db, sources))
    row = m.Policy(department_id=actor.department_id, kind=value.definition.rule.kind,
                    status="proposed", created_by=actor.identity_id, **values)
    db.add(row)
    db.flush()
    history(db, actor, "policy", row, value.source)
    from .common import outbox
    outbox(db, actor, f"rule:{row.id}:{row.revision}", "rule", {"policy_id": row.id})
    return {"record": record(row), "message": "Proposed policy saved; it is not active until independently approved."}


def policy_impact(db, actor, policy, *, details=True, acknowledge_unresolved=False, workspaces=None, retired_ids=()):
    require_role(actor, "coordinator", "admin", "approver")
    policies = policy if isinstance(policy, list) else [policy]
    policy_ids = {p.id for p in policies}
    policy_ids.update(pin['policy_id'] for p in policies for line in p.definition['rule'].get('lines') or []
                      for pin in (line.get('source_bindings') or {}).values())
    impact = []
    # Only rotas still being planned and overlapping the rules' dates: published rotas keep the rules they were approved under.
    from .rule_effects import affected_workspaces
    if workspaces is None:
        workspaces = affected_workspaces(db, actor, policies)
    require(len(workspaces) <= 100, "IMPACT_LIMIT", "Impact preview exceeds the bounded interactive workload; operator review required")
    candidates = [{**record(p), "status": "active"} for p in policies]
    if retired_ids:
        retired = {p.id: {**record(p), 'status': 'retired', 'active': False}
                   for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id,
                                                            m.Policy.id.in_(set(retired_ids))))}
        candidates = [*retired.values(), *candidates]
    from apps.api.snapshots import activation_shared_inputs
    shared = (activation_shared_inputs(db, db.get(m.Organisation, actor.organisation_id))
              if workspaces else None)
    quota_only = not details and all(p.definition['rule']['kind'] == 'block_quota' for p in policies)
    receipt_fast_path = (not details and not retired_ids and len(policies) == 1 and
                         not policies[0].definition.get('replaces_id') and
                         not policies[0].definition.get('merged_ids'))
    receipts = db.info.get('_activation_receipts', {}) if receipt_fast_path else {}
    for workspace in workspaces:
        data = result = None
        try:
            receipt = receipts.get(workspace.id)
            proposed = (receipt or {}).get('checks', {}).get('proposed', {}) if receipt else {}
            validation = proposed.get('validation') if isinstance(proposed, dict) else None
            quota_complete = (not quota_only or
                              (validation or {}).get('quota_report', {}).get('complete', True) is True)
            if (isinstance(validation, dict) and isinstance(validation.get('findings'), list)
                    and quota_complete):
                findings = [finding for finding in validation['findings'] if finding.get('rule_id') in policy_ids]
                if findings:
                    impact.append({'workspace_id': workspace.id, 'workspace_name': workspace.name,
                                   'status': workspace.status, 'findings': findings,
                                   'evaluation': {key: validation[key] for key in CHECK_SUMMARY if key in validation}})
                continue
            # Scope intervals are keyed by clocks and person, but retaining a
            # department-wide cache across rotas makes a long activation grow
            # with every workspace.  Rebuild this bounded per-rota cache; the
            # shared source projection still avoids reloading department rows.
            data = domain_snapshot(db, workspace, operational=workspace.status == "published", policy_overrides=candidates,
                                   shared_inputs=shared, scope_interval_cache={}, include_fairness=not quota_only)
            # Matching facts stay immutable for this check; never share its answers across rotas or commands.
            data.update(_policy_applies_cache={}, _policy_applies_people=True, _workload_matches_cache={})
            result = evaluate(data, quota_only=True) if quota_only else evaluate(data)
            from packages.domain.conditions import active_rules
            if any(p['id'] in policy_ids and p['kind'] == 'block_quota' for p in active_rules(data)) and result.get("quota_report") is not None:
                require(result["quota_report"]["complete"] or acknowledge_unresolved, "QUOTA_IMPACT_UNRESOLVED",
                        "The complete quota impact could not be evaluated; narrow or reconcile the source before activation",
                        409, {"workspace_id": workspace.id,
                              "findings": [f for f in result["findings"] if f["code"].startswith("QUOTA_")]})
            findings = [f for f in result["findings"] if f["rule_id"] in policy_ids]
            if findings:
                summary = ({'scope': 'quotas', 'quota_complete': result['quota_report']['complete']} if quota_only
                           else {key: result[key] for key in CHECK_SUMMARY if key in result})
                impact.append({"workspace_id": workspace.id, "workspace_name": workspace.name,
                               "status": workspace.status, "findings": findings,
                               "evaluation": result if details else summary})
        finally:
            # Keep only the bounded impact rows. Evaluator caches and their
            # source snapshot must not survive into the next rota.
            if result is not None and not details:
                result.clear()
                del result
            if data is not None:
                data.clear()
                del data
    del shared
    return impact


RULE_CHECK_REQUIRED_MESSAGE = 'Run a fresh check of these rules for this rota before activation'


def context_check_blocker(result):
    if result.get('newly_introduced_contradiction'):
        return 'RULE_INTRODUCES_CONFLICT'
    status = result.get('checks', {}).get('proposed', {}).get('status')
    if status not in {'candidate_feasible', 'requirements_conflict', 'unknown_time_limit'} and not unchanged_input_issues(result.get('checks', {})):
        return 'RULE_CHECK_FAILED'
    return None


def require_context_receipts(db, actor, pending, decisions, *, source_records=None, checked_records=None,
                             compare_problems=False):
    context = (db.info.get('bulk') or {}).get('approval_context')
    if context:
        # Bulk verified the exact union before dispatch; each handler still checks its own unchanged candidate.
        bindings = context['bindings']
        require(context['comparison_checked' if compare_problems else 'feasibility_checked'] and all(p.id in bindings
            and bindings[p.id]['source_hash'] == snapshot_hash(source_records[p.id] if source_records is not None else record(p))
            and bindings[p.id]['checked_hash'] == snapshot_hash(checked_records[p.id] if checked_records is not None else {**record(p), 'status': 'active'})
            for p, _ in pending), 'RULE_CHECK_REQUIRED', RULE_CHECK_REQUIRED_MESSAGE, 409)
        return context['evidence']
    policies = [p for p, _ in pending]
    from .rule_effects import CHECK_RECEIPT, affected_workspaces, check_result, checked_effect, receipt_current
    source_hashes = {p.id: snapshot_hash(source_records[p.id] if source_records is not None else record(p)) for p in policies}
    checked_hashes = {p.id: snapshot_hash(checked_records[p.id] if checked_records is not None else {**record(p), 'status': 'active'})
                      for p in policies}
    jobs = [scoped(db, m.Job, ident, actor, options=CHECK_RECEIPT) for ident in {i for d in decisions for i in d.check_job_ids}]
    positions = dict(db.execute(select(m.Job.id, m.Job.seq).where(m.Job.id.in_([job.id for job in jobs]))).all()) if jobs else {}
    workspaces = affected_workspaces(db, actor, policies)
    require(len(workspaces) <= 100, 'IMPACT_LIMIT', 'Too many current rotas for an interactive review')
    evidence = []
    receipt_results = {}
    line_review = all(p.kind == 'catalogue' for p in policies)
    acknowledged = line_review and all(d.acknowledge_unresolved and d.reason and d.reason.strip() for d in decisions)
    reviews = None
    # A feasibility receipt already stores the compact currency hint and the
    # fingerprint it was built from. Prove unchanged inputs from those fields
    # before considering a full snapshot. This is the activation memory guard:
    # normal receipts do not need to materialise every rota and its history.
    candidate_jobs = {workspace.id: [job for job in jobs if job.workspace_id == workspace.id
                                     and job.kind == 'feasibility' and job.status == 'succeeded' and check_result(job)]
                      for workspace in workspaces}
    from apps.api.snapshots import activation_shared_inputs, receipt_currencies
    def build_currencies(db, rows):
        org = db.get(m.Organisation, actor.organisation_id)
        return _activation_phase(db, 'feasibility_fingerprints', feasibility_fingerprints,
            db, rows, shared_inputs=activation_shared_inputs(db, org))
    feasibility_currencies = receipt_currencies(db, workspaces, candidate_jobs, build_currencies)
    for workspace in workspaces:
        if not any(p.starts_on < workspace.ends_on and (p.ends_on is None or p.ends_on > workspace.starts_on)
                   and p.definition.get("workspace_id") in (None, workspace.id) for p in policies):
            continue
        candidates = candidate_jobs[workspace.id]
        valid = []
        for job in candidates:
            result = check_result(job)
            if receipt_current(job, result, feasibility_currencies.get(workspace.id), source_hashes, checked_hashes):
                valid.append(job)
        # Missing receipts are explicit unresolved evidence, never a successful or feasible check.
        if not valid and acknowledged and len(policies) == 1:
            require(not s.safety_rule(policies[0].definition), 'RULE_CHECK_UNRESOLVED',
                    'Safety rules require a complete effect check; uncertainty cannot be acknowledged', 409)
            from .rule_effects import review
            if reviews is None:
                reviews = [review(db, actor, p.id) for p in policies]
            require(all(r['can_decide'] for r in reviews),
                    'RULE_CHECK_UNRESOLVED', 'Wait for running checks and resolve proven conflicts before deciding', 409)
            effects = [e for r in reviews for e in r['effect'] if e['workspace_id'] == workspace.id]
            evidence.append({'workspace_id': workspace.id, 'revision': workspace.revision,
                'check_job_id': effects[0]['job_id'] if len(effects) == 1 else None,
                'comparison_complete': False, 'no_new_problems': False,
                'effect': {'status': effects[0]['status'] if len(effects) == 1 else 'unresolved'},
                'review_effects': effects, 'acknowledged_unresolved': True,
                'summary': 'Effect not established; approver accepted the uncertainty with a recorded reason.'})
            continue
        require(valid, 'RULE_CHECK_REQUIRED', RULE_CHECK_REQUIRED_MESSAGE,
                409, {'workspace_id': workspace.id, 'workspace_name': workspace.name})
        job = max(valid, key=lambda j: (j.created_at, positions[j.id]))
        result = check_result(job)
        # policy_impact can reuse this compact proposed validation for the
        # exact receipt/input pair instead of evaluating the same rota again.
        receipt_results[workspace.id] = result
        blocker = context_check_blocker(result)
        require(blocker != 'RULE_INTRODUCES_CONFLICT', 'RULE_INTRODUCES_CONFLICT',
                'This change makes a previously feasible rota impossible; adjust the rule before activation',
                409, {'workspace_id': workspace.id})
        proposed_status = result.get('checks', {}).get('proposed', {}).get('status')
        require(blocker != 'RULE_CHECK_FAILED',
                'RULE_CHECK_FAILED', 'Resolve the configuration or check failure before activation', 409)
        require(not (compare_problems and result.get('comparison_complete') is not True
                     and any(s.safety_rule(p.definition) for p in policies)), 'RULE_CHECK_UNRESOLVED',
                'Safety rules require a complete effect check; uncertainty cannot be acknowledged', 409)
        no_new = (result.get('comparison_complete') is True and result.get('no_new_problems') is True
                  and result.get('new_or_worse_findings') == [])
        resolved = no_new if compare_problems else result.get('full_feasibility_established')
        findings = result.get('new_or_worse_findings') or []
        observed = list(finding_observations({'findings': findings}))
        advisory = (result.get('comparison_complete') is True and proposed_status == 'candidate_feasible'
            and result.get('checks', {}).get('baseline', {}).get('status') == 'candidate_feasible'
            and bool(observed) and all(f.get('severity') == 'warning'
                and (f.get('detail') or {}).get('rule_severity') == 'advisory'
                and not f.get('implementation_error') and f.get('proven') is not False
                and all(r.get('kind') == 'policy' and (r.get('definition') or {}).get('severity') == 'advisory'
                        for r in f.get('requirements', [])) for f in observed))
        references = [{k: f[k] for k in ('code', 'key', 'rule_id', 'shift_id', 'person_id', 'severity') if k in f}
                      for f in observed]
        unresolved = {'workspace_id': workspace.id, 'check_job_id': job.id, 'findings': references,
                      **({'severity': 'advisory'} if advisory else {})}
        effect = checked_effect(result)
        known_effect = effect['status'] == 'ready'
        require((acknowledged or (known_effect and resolved) if line_review else resolved or all(d.acknowledge_unresolved for d in decisions)),
                'RULE_CHECK_UNRESOLVED', ('Wait for a complete effect check before approving this line' if line_review else
                                         'Review and acknowledge the unresolved rota findings before activation'),
                409, unresolved)
        if compare_problems:
            count = result.get('unchanged_existing_count', 0)
            added = len(result.get('new_or_worse_findings', []))
            evidence.append({'workspace_id': workspace.id, 'check_job_id': job.id,
                **({'effect': effect} if known_effect else {}),
                **({'effect': effect, 'acknowledged_unresolved': True}
                   if line_review and not known_effect and acknowledged else {}),
                'no_new_problems': no_new, 'comparison_complete': result.get('comparison_complete', False),
                'unchanged_existing_count': count, 'new_or_worse_count': added,
                **({'unresolved_findings': references, 'severity': 'advisory'} if not resolved and advisory else {}),
                'summary': effect.get('summary', 'Effect not established') if line_review else f'no new problems; {count} existing unchanged' if no_new else
                           f'adds {added} problems' if added and result.get('comparison_complete') is True else 'comparison unresolved'})
        else:
            evidence.append({'workspace_id': workspace.id, 'check_job_id': job.id,
                             'full_feasibility_established': bool(result.get('full_feasibility_established'))})
    db.info['_activation_receipts'] = receipt_results
    return evidence


def _activation_commands(command):
    """Expand the public single/batch command into one canonical activation list."""
    if command.kind != "policy.activate_many":
        return [command]
    value = s.PolicyManyActivation.model_validate(command.payload)
    shared = value.model_dump(exclude={"items", "replaces_ids"})
    return [s.Command(kind="policy.activate", target_id=item.policy_id, expected_revision=item.expected_revision,
                      payload={**shared, "replaces_ids": value.replaces_ids if index == 0 else []})
            for index, item in enumerate(value.items)]


def _activation_inputs(db, actor, commands, *, enforce_independence=True):
    """Read and validate activation candidates without changing lifecycle state."""
    pending, decisions = [], []
    require_role(actor, "approver")
    require(0 < len(commands) <= 1000, "POLICY_BATCH_LIMIT", "Select between 1 and 1000 policies")
    require(len({c.target_id for c in commands}) == len(commands), "DUPLICATE_POLICY", "Select each policy once")
    for command in commands:
        require(command.kind == "policy.activate", "WORKFLOW_STATE", "Only policy activation can be batched")
        value = s.PolicyActivationDecision.model_validate(command.payload)
        decisions.append(value)
        policy = scoped(db, m.Policy, command.target_id, actor)
        expected(policy, command.expected_revision)
        require(policy.status == "proposed", "WORKFLOW_STATE", "Only a proposed policy can be activated", 409)
        if enforce_independence:
            independence.ensure(db, actor, "rule.activate", person_key(db, policy.created_by) != person_key(db, actor.identity_id),
                                "Another authorised person must approve this policy", value.reason, policy.id)
        if previous := policy.definition.get("replaces_id"):
            old = scoped(db, m.Policy, previous, actor)
            require(old.status == "active", "WORKFLOW_STATE", "Only an active rule can be replaced", 409)
            require(old.kind == policy.kind, "REPLACEMENT_KIND",
                    "A rule can only replace active rules of the same type", 422)
        if policy.kind == 'catalogue':
            from .scoring_defaults import seed_line_ids
            source_ids = [*(policy.definition.get('merged_ids') or []), *value.replaces_ids]
            if policy.definition.get('replaces_id'):
                source_ids.append(policy.definition['replaces_id'])
            sources = [scoped(db, m.Policy, ident, actor).id for ident in set(source_ids)]
            require(seed_line_ids(db, sources) <= set(policy.definition.get('seeded_line_ids') or []),
                    'STALE_REVISION', 'This rule changed since it was proposed; reload and propose it again', 409)
        checked = check_policy(db, actor, policy.definition, preview=False)
        require(checked['compatible'], "SOLVER_RULE_INCOMPATIBLE",
                "Adjust the rule settings before activation", 422, checked['issues'])
        pending.append((policy, value.reason))
    require_policy_version(db, actor, *(policy.definition for policy, _ in pending),
                           retained=[policy for policy, _ in pending])
    entries, predecessors = set(), set()
    for (policy, _), value in zip(pending, decisions):
        entry = policy.definition.get('calendar_entry_id')
        replaces = set(value.replaces_ids) | set(policy.definition.get('merged_ids') or [])
        if previous := policy.definition.get('replaces_id'):
            replaces.add(previous)
        require(not (entry and entry in entries) and not predecessors.intersection(replaces),
                "WORKFLOW_STATE", "Activate successive versions as separate changes", 409)
        if entry:
            entries.add(entry)
        predecessors.update(replaces)
    return pending, decisions


def _activation_stamp(db, actor):
    """Compact relational revisions, including new drafts and check receipts, bind the read to the write."""
    W, P, J = m.Workspace, m.Policy, m.Job
    org = db.get(m.Organisation, actor.organisation_id)
    return snapshot_hash(json_value({
        'actor': (actor.identity_id, actor.department_id),
        'inputs': (org.input_revision, org.availability_revision),
        'department': record(db.get(m.Department, actor.department_id)),
        'policies': [tuple(row) for row in db.execute(select(P.id, P.revision, P.status, P.active).where(
            P.department_id == actor.department_id).order_by(P.id))],
        'workspaces': [tuple(row) for row in db.execute(select(W.id, W.revision, W.content_revision,
            W.status, W.archived_at).join(m.Department).where(
            m.Department.organisation_id == actor.organisation_id).order_by(W.id))],
        'checks': [tuple(row) for row in db.execute(select(J.id, J.revision, J.status).where(
            J.department_id == actor.department_id, J.kind == 'feasibility').order_by(J.id))],
    }))


def prepare_activation(db, actor, command, require_checks=False):
    """Compute activation evidence before the serialized writer takes the global lock."""
    db.info['_activation_read'] = True
    db.info['_activation_timing'] = True
    activation_phase_timings(db)
    stamp = _activation_stamp(db, actor)
    commands = _activation_commands(command)
    pending, decisions = _activation_inputs(db, actor, commands, enforce_independence=False)
    retired_ids = {old_id for (policy, _), decision in zip(pending, decisions)
                   for old_id in (set(decision.replaces_ids) | set(policy.definition.get('merged_ids') or []) |
                                  ({policy.definition['replaces_id']} if policy.definition.get('replaces_id') else set()))}
    entries = {policy.definition['calendar_entry_id'] for policy, _ in pending if policy.definition.get('calendar_entry_id')}
    if entries:
        retired_ids.update(p.id for p in policies_with_status(db, actor, 'active')
                           if p.definition.get('calendar_entry_id') in entries)
    # Reject protected personal targets before impact preparation can evaluate a stale converted source.
    from .scheduling_facts import guard_goal_change
    for old_id in retired_ids:
        guard_goal_change(db, actor, [old_id])
    from .rule_effects import affected_workspaces
    workspaces = affected_workspaces(db, actor, [policy for policy, _ in pending])
    evidence = (_activation_phase(db, 'receipts', require_context_receipts, db, actor, pending, decisions,
                                  compare_problems=True) if require_checks else [])
    # CP-SAT activation already proved each affected rota against the exact
    # proposed rule in ``evidence``. Rebuilding every rota a second time here
    # was the 563-second successor path. Quotas retain their extra completeness
    # guard because their impact report has a separate boundedness contract.
    impact = ([] if require_checks and all(policy.definition['rule']['kind'] != 'block_quota' for policy, _ in pending) else
              _activation_phase(db, 'policy_impact', policy_impact, db, actor, [policy for policy, _ in pending],
                                details=False, workspaces=workspaces, retired_ids=retired_ids,
                                acknowledge_unresolved=all(policy.kind == 'catalogue' and decision.acknowledge_unresolved
                                                           and decision.reason and decision.reason.strip()
                                                           for (policy, _), decision in zip(pending, decisions))))
    return {'stamp': stamp,
            'policy_ids': [policy.id for policy, _ in pending],
            'workspace_ids': [workspace.id for workspace in workspaces],
            'evidence': evidence, 'impact': impact,
            'phase_timings': dict(activation_phase_timings(db))}


def activate_policies(db, actor, commands, require_checks=False):
    """Import reconciliation shares the single-policy gates and combined impact."""
    from .scheduling_facts import guard_goal_change
    db.info['_activation_timing'] = True
    activation_phase_timings(db)
    prepared = db.info.get('_activation_preparation')
    if prepared is not None and prepared.get('phase_timings'):
        activation_phase_timings(db).update(prepared['phase_timings'])
    if prepared is None:
        pending, decisions = _activation_inputs(db, actor, commands)
    else:
        require(_activation_stamp(db, actor) == prepared['stamp'], 'STALE_INPUTS',
                'The policy impact inputs changed; prepare the activation again', 409)
        pending, decisions = [], []
        for command in commands:
            value = s.PolicyActivationDecision.model_validate(command.payload)
            policy = scoped(db, m.Policy, command.target_id, actor)
            expected(policy, command.expected_revision)
            require(policy.status == 'proposed', 'WORKFLOW_STATE', 'Only a proposed policy can be activated', 409)
            independence.ensure(db, actor, 'rule.activate', person_key(db, policy.created_by) != person_key(db, actor.identity_id),
                                'Another authorised person must approve this policy', value.reason, policy.id)
            pending.append((policy, value.reason))
            decisions.append(value)
    if prepared is not None:
        require([p.id for p, _ in pending] == prepared['policy_ids'], 'STALE_INPUTS',
                'The policy proposal changed; prepare the activation again', 409)
        evidence, impact_workspaces = prepared['evidence'], None
    else:
        evidence = (_activation_phase(db, 'receipts', require_context_receipts, db, actor, pending, decisions,
                                      compare_problems=True) if require_checks else [])
        from .rule_effects import affected_workspaces
        impact_workspaces = affected_workspaces(db, actor, [p for p, _ in pending])
    # Retire the copies being replaced first, so the impact check never counts a rule twice. `replaced`: the rules
    # retired here that retiring the new rule does not bring back by itself (Undo restores them; grouped rules come
    # back with the retire).
    replaced, retired_history = [], []
    for (policy, reason), value in zip(pending, decisions):
        for old_id in dict.fromkeys(value.replaces_ids):
            old = scoped(db, m.Policy, old_id, actor)
            require(old.status == "active", "WORKFLOW_STATE", "Only an active rule can be replaced", 409)
            guard_goal_change(db, actor, [old.id])
            require(old.definition["rule"]["kind"] == policy.definition["rule"]["kind"], "REPLACEMENT_KIND",
                    "A rule can only replace active rules of the same type", 422)
            old.status, old.active = "retired", False
            advance_revision(db, old)
            retired_history.append(("policy", old, f"Replaced by {policy.name}: {reason}"[:500]))
            replaced.append(old)
        # A template retires the near-duplicate rules it was grouped from (those still active).
        merged = policy.definition.get("merged_ids") or []
        for old_id in merged:
            old = scoped(db, m.Policy, old_id, actor)
            if old.status == "active":
                guard_goal_change(db, actor, [old.id])
                old.status, old.active = "retired", False
                advance_revision(db, old)
                retired_history.append(("policy", old, f"{MERGED_INTO if len(merged) > 1 else CHANGED_INTO}{policy.name}: {reason}"[:500]))
        # A new version replaces the rule it changes; one protected-time rule per academic calendar entry.
        entry, previous = policy.definition.get("calendar_entry_id"), policy.definition.get("replaces_id")
        if entry or previous:
            for old in policies_with_status(db, actor, "active"):
                if old.id == policy.id:
                    continue
                if old.id == previous and old.kind == policy.kind or entry and old.definition.get("calendar_entry_id") == entry:
                    guard_goal_change(db, actor, [old.id])
                    old.status, old.active = "retired", False
                    advance_revision(db, old)
                    retired_history.append(("policy", old, f"Replaced by {policy.name}: {reason}"[:500]))
                    replaced.append(old)
    history_many(db, actor, retired_history)
    if retired_history and db.info.get("bulk"):
        # The active catalogue uniqueness constraint needs retirements written before replacement activations.
        db.flush()
    from .leadership import activate_conversion
    for policy, _ in pending:
        activate_conversion(db, actor, policy)
    impact = (prepared['impact'] if prepared is not None else
              ([] if require_checks and all(p.definition['rule']['kind'] != 'block_quota' for p, _ in pending) else
               _activation_phase(db, 'policy_impact', policy_impact, db, actor, [p for p, _ in pending],
                                 details=False, workspaces=impact_workspaces,
                                 acknowledge_unresolved=all(p.kind == 'catalogue' and d.acknowledge_unresolved and d.reason and d.reason.strip()
                                                            for (p, _), d in zip(pending, decisions)))))
    for policy, reason in pending:
        policy.status, policy.approved_by = "active", actor.identity_id
        advance_revision(db, policy)
    history_many(db, actor, [("policy", policy, reason) for policy, reason in pending],
                 snapshot_extra={'approval_transition': True})
    from .common import outbox
    for policy, _ in pending:
        outbox(db, actor, f"rule:{policy.id}:{policy.revision}", "rule", {"policy_id": policy.id})
    bump_inputs(db, actor)
    return {"records": [record(p) for p, _ in pending], "impact": impact, "replaced": [record(p) for p in replaced],
            "context_checks": sorted({ident for d in decisions for ident in d.check_job_ids}) if require_checks else [],
            "context_check_evidence": evidence,
            "unresolved_acknowledged": require_checks and all(d.acknowledge_unresolved for d in decisions)}


def decline_policy(db, actor, command):
    """A proposed rule that should never apply is declined (kept in history, shown with retired rules)."""
    require_role(actor, "approver", "coordinator", "admin")
    value = s.Decision.model_validate(command.payload)
    policy = scoped(db, m.Policy, command.target_id, actor)
    expected(policy, command.expected_revision)
    require(policy.status == "proposed", "WORKFLOW_STATE", "Only a proposed rule can be declined", 409)
    policy.status, policy.active = "retired", False
    advance_revision(db, policy)
    history(db, actor, "policy", policy, value.reason)
    from .common import outbox
    outbox(db, actor, f"rule:{policy.id}:{policy.revision}", "rule", {"policy_id": policy.id})
    return {"record": record(policy)}


def restore_proposal(db, actor, command):
    """Undo a decline as a waiting proposal, preserving independent activation."""
    require_role(actor, "approver", "coordinator", "admin")
    value = s.Decision.model_validate(command.payload)
    policy = scoped(db, m.Policy, command.target_id, actor)
    expected(policy, command.expected_revision)
    require(policy.status == "retired" and policy.approved_by is None, "WORKFLOW_STATE",
            "Only a declined proposal can be put back for approval", 409)
    previous = history_version(db, actor, "policy", policy.id, policy.revision - 1)
    require(previous is not None and previous.snapshot.get("status") == "proposed", "WORKFLOW_STATE",
            "The previous proposal state is unavailable; propose the rule again", 409)
    require_policy_version(db, actor, policy.definition, retained=[policy])
    if policy.kind == "catalogue":
        from .rule_catalogue import catalogue_version, waiting_proposal
        require(waiting_proposal(db, actor, policy.definition["rule"]["id"]) is None, "PROPOSAL_WAITING",
                "A change to this rule is already waiting for approval; approve or decline it first", 409)
        current = catalogue_version(db, actor, policy.definition["rule"]["id"], "active")
        require(current is None or current.id in (policy.definition.get("merged_ids") or []), "STALE_REVISION",
                "This rule changed since you opened it; reload and try again", 409)
    policy.status, policy.active = "proposed", previous.snapshot["active"]
    advance_revision(db, policy)
    history(db, actor, "policy", policy, value.reason)
    from .common import outbox
    outbox(db, actor, f"rule:{policy.id}:{policy.revision}", "rule", {"policy_id": policy.id})
    return {"record": record(policy)}


def activate_many(db, actor, command, require_checks=False):
    return activate_policies(db, actor, _activation_commands(command), require_checks)


def transition_approval_state(db, actor, policy, *, excluded=()):
    """Replay revision-bound votes once; withdrawals cancel only their signing account's vote."""
    proposers = {independence.person_key(db, policy.created_by), *excluded}
    kind = "policy_transition_approval"
    H = m.DefinitionHistory
    scope = (H.department_id == actor.department_id, H.kind == kind, H.object_id == policy.id)
    last_revision = db.scalar(select(func.max(H.object_revision)).where(*scope)) or 0
    approvals = list(db.scalars(select(H).where(*scope, H.snapshot["policy_revision"].as_integer() == policy.revision)
        .order_by(H.object_revision)))
    pending = [row for row in db.new if isinstance(row, H)
        and row.department_id == actor.department_id and row.kind == kind and row.object_id == policy.id]
    approvals.extend(pending)
    approved = {}
    for approval in approvals:
        value = approval.snapshot
        if value.get("policy_revision") != policy.revision:
            continue
        identity = db.get(m.Identity, approval.actor_id)
        person = independence.person_key(db, approval.actor_id)
        key = (value["action"], person)
        if value.get("withdrawn"):
            if (key in approved and approved[key].actor_id == approval.actor_id
                    and approved[key].snapshot["decision_hash"] == value["decision_hash"]):
                approved.pop(key)
        elif identity is not None and not identity.disabled and person not in proposers and "approver" in current_roles(
                db, approval.actor_id, actor.department_id):
            # A newer effects decision supersedes the person's old vote, even if effects later change back.
            approved[key] = approval
    return max([last_revision, *(row.object_revision for row in pending)]), approved


def transition_approval(db, actor, policy, action, reason, effects, *, excluded=()):
    required = db.get(m.Department, actor.department_id).policy_retire_approvals_required
    proposers = {independence.person_key(db, policy.created_by), *excluded}
    reviewer = independence.person_key(db, actor.identity_id)
    safety = s.safety_rule(policy.definition)
    independence.ensure(db, actor, "rule.activate", reviewer not in proposers,
        "The rule proposer cannot approve its retirement or restoration", reason, policy.id,
        allow_self_approval=required == 1 and (
            not safety or independence.permission_for(db, actor, "rule.activate") is not None))
    if approval := db.info.get("bulk", {}).get("policy_undo_approval"):
        return {**approval, "policy_revision": policy.revision, "action": action}
    if required == 1:
        return None
    decision_hash = snapshot_hash(effects)
    last_revision, votes = transition_approval_state(db, actor, policy, excluded=excluded)
    approved = {key[1]: row.actor_id for key, row in votes.items()
        if key[0] == action and row.snapshot["decision_hash"] == decision_hash}
    require(reviewer not in approved, "DUPLICATE_REVIEWER", "Each approval must represent a different person", 409)
    db.add(m.DefinitionHistory(department_id=actor.department_id, kind="policy_transition_approval", object_id=policy.id,
        object_revision=last_revision + 1,
        snapshot={"policy_revision": policy.revision, "action": action, "decision_hash": decision_hash,
                  "approved_by": actor.identity_id}, actor_id=actor.identity_id, reason=reason,
        batch_id=db.info.get("bulk", {}).get("batch_id")))
    approved[reviewer] = actor.identity_id
    return {"approvals_required": required, "approvals_received": len(approved), "complete": len(approved) >= required,
            "policy_revision": policy.revision, "action": action, "approved_by": sorted(approved.values())}


def withdraw_transition_approval(db, actor, command):
    """Undo of a retire or restore approval that is still waiting for other approvers: nothing was applied, so the
    signed-in approver's own approval is withdrawn (recorded, never deleted). Refused once the change was applied."""
    require_role(actor, "approver")
    value = s.Decision.model_validate(command.payload)
    policy = scoped(db, m.Policy, command.target_id, actor)
    expected(policy, command.expected_revision)
    last_revision, votes = transition_approval_state(db, actor, policy)
    action = {"active": "retire", "retired": "restore"}.get(policy.status)
    mine = next((row for key, row in votes.items() if key[0] == action and row.actor_id == actor.identity_id), None)
    require(mine is not None, "WORKFLOW_STATE", "There is no approval of yours waiting to withdraw", 409)
    # The shared history sequence is the optimistic token: a racing vote rolls back on its unique constraint.
    db.add(m.DefinitionHistory(department_id=actor.department_id, kind=mine.kind, object_id=policy.id,
        object_revision=last_revision + 1, snapshot={**mine.snapshot, "withdrawn": True},
        actor_id=actor.identity_id, reason=value.reason, batch_id=db.info.get("bulk", {}).get("batch_id")))
    return {"record": record(policy), "withdrawn": action}


def policy_transition(db, actor, command, require_checks=False):
    require_role(actor, "approver")
    if command.kind == "policy.activate":
        result = activate_policies(db, actor, [command], require_checks)
        return {**result, "record": result["records"][0]}
    retirement_reason = None
    if command.kind == "policy.retire" and (db.info.get("bulk") or {}).get("undo_source"):
        class RetirementUndo(s.Decision):
            retirement_reason: s.Reason | None = None

        value = RetirementUndo.model_validate(command.payload)
        retirement_reason = value.retirement_reason
    else:
        value = s.Decision.model_validate(command.payload)
    policy = scoped(db, m.Policy, command.target_id, actor)
    expected(policy, command.expected_revision)
    if command.kind == "policy.restore":
        return restore_policy(db, actor, policy, value.reason)
    require(policy.status == "active", "WORKFLOW_STATE", "Only an active policy can be retired", 409)
    from .scheduling_facts import guard_goal_change
    guard_goal_change(db, actor, [policy.id])
    # Undo a grouping: the rules this template retired apply again, as they were approved (not when it is retired
    # because it has ended: the rules it replaced ended with it).
    restoring = []
    old_ids = [] if retirement_reason is not None or value.reason.startswith(ENDED_REASON) else policy.definition.get("merged_ids") or []
    for old_id in dict.fromkeys(old_ids):
        old = scoped(db, m.Policy, old_id, actor)
        last = history_version(db, actor, "policy", old.id)
        # Retired when a grouped rule was activated (this one, or an earlier version of it under another name).
        if old.status == "retired" and last is not None and last.reason.startswith((MERGED_INTO, CHANGED_INTO)):
            restoring.append((old, last))
    require_policy_version(db, actor, *(old.definition for old, _ in restoring), retained=[old for old, _ in restoring])
    approval = transition_approval(db, actor, policy, "retire", value.reason,
        {"restoring": [(old.id, old.revision) for old, _ in restoring], "retirement_reason": retirement_reason})
    if approval and not approval["complete"]:
        return {"record": record(policy), "impact": [], "restored_ids": [], "restored": [], "approval": approval}
    policy.status, policy.active = "retired", False
    impact = []
    advance_revision(db, policy)
    history(db, actor, "policy", policy, retirement_reason or value.reason)
    restored = []
    for old, last in restoring:
        check_policy_references(db, actor, old.definition)
        if db.info.get("bulk"):
            db.flush()
        old.status, old.active = "active", approved_active_before_retirement(db, actor, old)
        advance_revision(db, old)
        history(db, actor, "policy", old, f"Restored because {policy.name} was retired: {value.reason}"[:500])
        restored.append({**record(old), "retirement_reason": last.reason})
    bump_inputs(db, actor)
    return {"record": record(policy), "impact": impact, "restored_ids": [row["id"] for row in restored],
            "restored": restored, **({"approval": approval} if approval else {})}


def approved_active_before_retirement(db, actor, policy):
    previous = history_version(db, actor, "policy", policy.id, policy.revision - 1)
    bulk_undo = (db.info.get("bulk") or {}).get("undo_source")
    require(previous is not None and previous.snapshot.get("status") == "active"
            and isinstance(previous.snapshot.get("active"), bool),
            "BULK_UNDO_UNAVAILABLE" if bulk_undo else "WORKFLOW_STATE",
            f'The previous approved rule state is unavailable for "{policy.name}"; ' + (
                "review the rule individually" if bulk_undo else "review its history and propose it again"), 409,
            {"policy_id": policy.id, "policy_name": policy.name})
    return previous.snapshot["active"]


def restore_policy(db, actor, policy, reason):
    """Undo a retirement: a rule that was approved and then retired (an ended rule, or retired by mistake) applies
    again as it was approved. Refused for a declined proposal, and for a rule another rule in force replaced
    (retire that rule instead: its undo brings this one back)."""
    require(db.scalar(select(m.RuleSourceBinding.id).join(m.CatalogueConversion,
            m.RuleSourceBinding.migration_proposal_id == m.CatalogueConversion.id).where(
                m.RuleSourceBinding.source_policy_id == policy.id, m.CatalogueConversion.status == 'active')) is None,
            'WORKFLOW_STATE', 'This source is managed by an active catalogue conversion; undo the conversion instead', 409)
    require(policy.status == "retired" and policy.approved_by is not None, "WORKFLOW_STATE",
            "Only a rule that was in force can be restored", 409)
    require_policy_version(db, actor, policy.definition, retained=[policy])
    if db.info.get("bulk"):
        db.flush()
    for other in policies_with_status(db, actor, "active", "proposed"):
        require(other.status != "active" or other.id not in (policy.definition.get("merged_ids") or []),
                "WORKFLOW_STATE", f"{other.name} is already in force; retire it before restoring this rule", 409)
        require(policy.id not in (other.definition.get("merged_ids") or []) and other.definition.get("replaces_id") != policy.id
                and not (policy.definition.get("calendar_entry_id") and other.definition.get("calendar_entry_id") == policy.definition["calendar_entry_id"]),
                "WORKFLOW_STATE", f"{other.name} replaces this rule; retire or decline it instead", 409)
    check_policy_references(db, actor, policy.definition)
    active = approved_active_before_retirement(db, actor, policy)
    last = history_version(db, actor, "policy", policy.id)
    approval = transition_approval(db, actor, policy, "restore", reason, {"active": active})
    if approval and not approval["complete"]:
        return {"record": record(policy), "impact": [], "restored_ids": [], "approval": approval}
    policy.status, policy.active = "active", active
    advance_revision(db, policy)
    history(db, actor, "policy", policy, f"Restored: {reason}"[:500])
    bump_inputs(db, actor)
    return {"record": record(policy), "impact": [], "restored_ids": [policy.id],
            "retirement_reason": last.reason if last is not None else reason,
            **({"approval": approval} if approval else {})}


def create_exception(db, actor, value):
    """A proposed exception for one person to one rule (approved separately); its record."""
    require_role(actor, "coordinator")
    policy = effective_source(db, actor, scoped(db, m.Policy, value.policy_id, actor))
    person_access(db, actor, value.person_id)
    require(not s.safety_rule(policy['definition']), 'SAFETY_RULE', 'Safety rules cannot have person exceptions', 409)
    require(policy['status'] == "active" and policy['definition']["exception_allowed"],
            "EXCEPTION_NOT_PERMITTED", "This active rule does not permit exceptions", 403)
    require(policy['starts_on'] <= value.starts_on.isoformat() and (policy['ends_on'] is None or value.ends_on.isoformat() <= policy['ends_on']),
            "EXCEPTION_SCOPE", "Exception must be inside the policy's effective dates")
    rule = policy['definition']["rule"]
    if rule["kind"] == "block_quota":
        require(value.starts_on.isoformat() == rule["starts_on"] and value.ends_on.isoformat() == rule["ends_on"],
                "QUOTA_EXCEPTION_WINDOW", "A quota exception must explicitly cover exactly the entire block; partial days cannot waive a block commitment")
    row = m.PolicyException(department_id=actor.department_id, policy_revision=policy['revision'],
                            created_by=actor.identity_id, **value.model_dump())
    db.add(row)
    db.flush()
    history(db, actor, "exception", row, value.reason)
    return record(row)


def exception_approval(db, actor, exception_id):
    """Canonical current load and planning alternatives, never combined across drafts."""
    from packages.domain.rule_map import reach
    from packages.domain.time import local_midnight
    from .schedule import current_publications, in_force_shifts, until

    require_role(actor, 'admin', 'coordinator', 'approver', 'auditor')
    row = scoped(db, m.PolicyException, exception_id, actor)
    person = person_access(db, actor, row.person_id)
    policy = effective_source(db, actor, scoped(db, m.Policy, row.policy_id, actor))
    org = db.get(m.Organisation, actor.organisation_id)
    query = select(m.Workspace).where(shared_clause(), m.Workspace.department_id == actor.department_id,
        m.Workspace.archived_at.is_(None), m.Workspace.status.in_(('draft', 'submitted', 'approved')),
        m.Workspace.starts_on < row.ends_on, m.Workspace.ends_on > row.starts_on)
    if policy['definition'].get('workspace_id'):
        query = query.where(m.Workspace.id == policy['definition']['workspace_id'])
    contexts = [(workspace, None) for workspace in db.scalars(
        query.order_by(m.Workspace.starts_on, m.Workspace.id).limit(101))]
    publications = current_publications(db, [actor.department_id], row.starts_on, row.ends_on, light=True,
        workspace_ids=[policy['definition']['workspace_id']] if policy['definition'].get('workspace_id') else None)
    require(len(contexts) + len(publications) <= 100, 'IMPACT_LIMIT', 'Too many overlapping rotas; review a narrower period', 409)
    published_workspaces = {workspace.id: workspace for workspace in db.scalars(select(m.Workspace).where(shared_clause(),
        m.Workspace.id.in_({publication.workspace_id for publication in publications})))} if publications else {}
    contexts += [(published_workspaces[publication.workspace_id], publication) for publication in publications]
    shared = snapshot_inputs(db, org) if contexts else None
    loads = []
    for workspace, publication in contexts:
        first = max(row.starts_on, publication.starts_on if publication else workspace.starts_on)
        last = min(row.ends_on, until(publication) if publication else workspace.ends_on)
        context = {'workspace_id': workspace.id, 'workspace_name': workspace.name,
                   'status': 'published' if publication else workspace.status,
                   'publication_id': publication.id if publication else None,
                   'starts_on': first.isoformat(), 'ends_on': last.isoformat()}
        if publication and 'shifts' not in publication.snapshot:
            loads.append({**context, 'lines': [], 'complete': False})
            continue
        own = in_force_shifts(publication, publication.snapshot['shifts'], org.timezone) if publication else None
        data = domain_snapshot(db, workspace, shared_inputs=shared, include_fairness=False,
                               current_scheduling=True, own_shifts=own)
        if publication:
            data['workspace'].update(starts_on=row.starts_on.isoformat(), ends_on=row.ends_on.isoformat())
        data['quota_subjects'] = {person.id}
        a, b = local_midnight(first, org.timezone), local_midnight(last, org.timezone)
        found = reach(data, data['policies'], a, b, details=True, policy_id=row.policy_id)['details']
        lines = []
        for line in found.get(row.policy_id, {}).get('lines', []):
            selected = next((p for p in line['people'] if p['person_id'] == person.id), None)
            if selected is not None:
                lines.append({key: line[key] for key in ('line_id', 'line', 'slot', 'kind', 'number')}
                             | {'counts': selected['counts']})
        complete = bool(lines) and all(line['counts'] and all(
            count.get('actual') is not None and count.get('status') != 'unresolved' for count in line['counts']) for line in lines)
        loads.append({**context, 'lines': lines, 'complete': complete})
    return {'exception': record(row), 'policy': policy_versions(db, actor, [policy])[0],
            'person': {'id': person.id, 'display_name': person.display_name},
            'starts_on': row.starts_on.isoformat(), 'ends_on': row.ends_on.isoformat(), 'timezone': org.timezone,
            'current_load': loads, 'complete': bool(loads) and all(load['complete'] for load in loads),
            'basis': 'In-force published duties and each planning rota are counted separately with current rules and required history. '
                     'Count windows retain the rule’s exact dates. Missing counts are unknown; published rotas are unchanged.'}


def policy_exception(db, actor, command):
    if command.kind == "exception.create":
        return {"record": create_exception(db, actor, s.ExceptionInput.model_validate(command.payload))}
    value = s.Decision.model_validate(command.payload)
    row = scoped(db, m.PolicyException, command.target_id, actor)
    expected(row, command.expected_revision)
    if command.kind == "exception.end":
        require(row.status in {"proposed", "approved"}, "WORKFLOW_STATE", "This exception has already ended", 409)
        if row.status == "proposed":
            require_role(actor, "coordinator", "approver")
            require(actor.permits("approver") or person_key(db, row.created_by) == person_key(db, actor.identity_id), "FORBIDDEN",
                    "Only the proposer or an approver can withdraw this exception", 403)
            status = "withdrawn"
        else:
            require_role(actor, "approver")
            status = "ended"
        # Existing exceptions may predate definition history; retain the approved/requested state before ending it.
        if history_version(db, actor, "exception", row.id, row.revision) is None:
            history(db, actor, "exception", row, "Exception retained before ending")
        row.status = status
    elif command.kind == "exception.undo" and row.status == "withdrawn":
        require_role(actor, "coordinator", "approver")
        require(actor.permits("approver") or person_key(db, row.created_by) == person_key(db, actor.identity_id), "FORBIDDEN",
                "Only the proposer or an approver can restore a withdrawn exception request", 403)
        row.status = "proposed"
    else:
        require_role(actor, "approver")
        restoring = command.kind == "exception.undo"
        require(row.status == ("ended" if restoring else "proposed"), "WORKFLOW_STATE",
                "Only an ended exception can be restored" if restoring else "Only a proposed exception can be approved", 409)
        require(not restoring or row.approved_by is not None, "WORKFLOW_STATE",
                "Only a previously approved exception can be restored", 409)
        policy = effective_source(db, actor, scoped(db, m.Policy, row.policy_id, actor))
        require(not s.safety_rule(policy['definition']), 'SAFETY_RULE', 'Safety rules cannot have person exceptions', 409)
        require(policy['status'] == "active" and policy['revision'] == row.policy_revision and policy['definition']["exception_allowed"],
                "STALE_POLICY", "Exception no longer matches an active exception-permitting rule", 409)
        independence.ensure_subject(db, actor, "exception.approve", (row.person_id,), row.created_by,
                            "Exception needs an independent approver", value.reason, row.id)
        row.status = "approved"
        if not restoring:
            row.approved_by = actor.identity_id
    row.revision += 1
    history(db, actor, "exception", row, value.reason)
    bump_inputs(db, actor)
    return {"record": record(row)}




# Similar rules become one rule (packages/domain/rule_groups.py) ----------------------

# Restrictions named like this are suggested as protected time on the academic calendar (a suggestion the person confirms).
from packages.domain.rule_groups import PROTECTED_TIME  # noqa: E402


def grouping_inputs(db, actor):
    """The department's active and proposed rules, the names group names leave out, and the department's first weekday."""
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    rows = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id,
                                                                  m.Policy.status.in_(["active", "proposed"])))]
    # Catalogue rules are already one rule per idea: grouping reads the older rules only (and leaves alone those a
    # waiting catalogue rule takes over).
    taken = {i for p in rows if p["kind"] == "catalogue" for i in p["definition"].get("merged_ids") or []}
    rows = [p for p in rows if p["kind"] != "catalogue" and p["id"] not in taken]
    named = lambda cls, attr="name": {r.id: getattr(r, attr) for r in db.scalars(select(cls).where(cls.department_id == actor.department_id))}
    labels = {"people": {p.id: p.display_name for p in db.scalars(select(m.Person).where(m.Person.organisation_id == organisation.id))},
              "programmes": named(m.Programme), "services": named(m.Service)}
    return rows, labels, organisation.week_start


def _calendar_for(payload, entries):
    """Where a protected-time rule sits on the academic calendar: its programme's entry for exactly these dates, or a
    new entry to add (named after the rule). None without one programme or an end date."""
    d = payload["definition"]
    programmes = {d["scope"].get("programme_id")} | {r.get("programme_id") for r in d.get("per_person") or []}
    programmes.discard(None)
    if len(programmes) != 1 or not payload.get("ends_on") or d["rule"]["kind"] != "restriction":
        return None
    programme = programmes.pop()
    same = next((e for e in entries if e.programme_id == programme and e.starts_on.isoformat() == str(payload["starts_on"])
                 and e.ends_on.isoformat() == str(payload["ends_on"])), None)
    from packages.domain.rule_groups import rule_stem
    return {"entry_id": same.id if same else None, "name": same.name if same else rule_stem(payload["name"])[:160], "programme_id": programme,
            "starts_on": str(payload["starts_on"]), "ends_on": str(payload["ends_on"])}


def duplicate_groups(db, actor):
    """Similar rules (the same rule for different people, programmes, sessions or dates) as proposed single rules, and
    protected academic days as protected time on the academic calendar. A preview: nothing is saved here."""
    import re
    from packages.domain.rule_groups import plan, normal, _groupable, rule_stem
    require_role(actor, "coordinator", "admin", "approver")
    rows, labels, week_start = grouping_inputs(db, actor)
    # A rule with a person exception keeps its own id, which the exception names.
    held = set(db.scalars(select(m.PolicyException.policy_id).where(m.PolicyException.department_id == actor.department_id)))
    # Exact copies of another rule are retired (by an approver) rather than grouped: held out of the groups meanwhile.
    from packages.domain.rule_groups import duplicates
    copies = duplicates(rows, held, week_start)
    held |= {c["rule_ids"][0] for c in copies}
    groups = plan(rows, labels, week_start, held)
    entries = list(db.scalars(select(m.ProgrammeCalendarEntry).where(m.ProgrammeCalendarEntry.department_id == actor.department_id)))
    protected = re.compile(PROTECTED_TIME, re.I)
    for g in groups:
        if "payload" in g and protected.search(g["name"]):
            g["calendar"] = _calendar_for(g["payload"], entries)
    grouped = {i for g in groups if "payload" in g for i in g["rule_ids"]}
    waiting = {i for p in rows if p["status"] == "proposed" for i in p["definition"].get("merged_ids") or []}
    for p in rows:
        if p["id"] in grouped or p["kind"] != "restriction" or not protected.search(p["name"]) or not _groupable(p, waiting):
            continue
        payload = {"name": rule_stem(p["name"])[:160], "owner": p["owner"], "source": "Protected time on the academic calendar", "active": True,
                   "starts_on": p["starts_on"], "ends_on": p["ends_on"], "definition": {**normal(p["definition"]), "merged_ids": [p["id"]]}}
        calendar = _calendar_for(payload, entries)
        if calendar:
            groups.append({"shape": "protect", "rule_ids": [p["id"]], "kind": p["kind"], "name": p["name"], "lines": 0, "people": 0,
                           "amounts": None, "default_amount": None, "starts_on": payload["starts_on"], "ends_on": payload["ends_on"],
                           "switched_off": False, "payload": payload, "calendar": calendar})
    active = sum(p["status"] == "active" for p in rows)
    merged = sum(len(g["rule_ids"]) for g in groups if "payload" in g and g["shape"] != "protect")
    made = sum(1 for g in groups if "payload" in g and g["shape"] != "protect")
    # Changes that are not identical, each its own choice with what changes: balances as Balance goals, and overtime
    # offers switched off by retired limits pointed at the limits in force.
    from packages.domain.rule_groups import goal_conversions, overtime_switch_on
    services = {c.id: {"mode": c.mode} for c in db.scalars(select(m.Service).where(m.Service.department_id == actor.department_id))}
    needed = {i for p in rows if p["kind"] == "allocation_overtime" for i in p["definition"]["rule"].get("required_policy_ids") or []}
    retired = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id, m.Policy.status == "retired",
                                                                  m.Policy.id.in_(list(needed) or [""])))]
    changes = [c for c in goal_conversions(rows, services) + overtime_switch_on(rows + retired, week_start)
               if not set(c["rule_ids"]) & grouped and not (c["shape"] == "overtime" and c["missing"])]
    if not grouped:
        # Phase 3, once phases 1 and 2 are in force: tables whose lines have their own settings, and ended rules.
        from packages.domain.rule_groups import simplify
        rotas = [(w.starts_on, w.ends_on) for w in db.scalars(select(m.Workspace).where(shared_clause(),
            m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None)))]
        more, also = simplify(rows, labels, week_start, held, today=independence.today(db, actor.department_id), rotas=rotas, services=services)
        taken = {i for c in more + also for i in c["rule_ids"]}
        changes = [c for c in changes if not set(c["rule_ids"]) & taken] + also
        groups += more
        merged += sum(len(g["rule_ids"]) for g in more)
        made += len(more)
    retiring = {c["rule_ids"][0] for c in copies}
    changes = [c for c in changes if not set(c["rule_ids"]) & retiring] + copies
    return {"groups": groups, "changes": changes, "rules": merged, "active": active, "after": active - merged + made}


def grouping_proof(db, actor, groups, model=False, score_may_move=False):
    """What the proposed groups would change, per rota being planned: validator findings and quota rows (rule ids
    mapped to the new rules), the schedule score, the rules the scheduler reads and, with `model`, the CP-SAT model
    itself. Empty differences prove the grouping changes nothing."""
    from packages.domain.rule_groups import canonical_model, expanded, same_checks, same_model
    from packages.domain.score import schedule_score
    department = db.get(m.Department, actor.department_id)
    week_start = db.get(m.Organisation, department.organisation_id).week_start
    # A change may add a second rule (`extra`: a Balance goal) or only retire (`retire`: an ended rule).
    groups = [g for g in groups if "payload" in g or g.get("retire")]
    id_map = {i: f"00000000-0000-4000-8000-{n:012d}" for n, g in enumerate(groups) for i in g["rule_ids"]}
    made = [{**payload, "id": f"00000000-0000-4000-{k:04d}-{n:012d}", "status": "active", "active": True, "revision": 1,
             "kind": payload["definition"]["rule"]["kind"], "department_id": actor.department_id}
            for n, g in enumerate(groups) for k, payload in enumerate(([g["payload"]] if "payload" in g else []) + g.get("extra", []), 8000)]
    retire = [{**record(p), "status": "retired", "active": False}
              for p in db.scalars(select(m.Policy).where(m.Policy.id.in_(list(id_map) or [""])))]
    workspaces = list(db.scalars(select(m.Workspace).where(shared_clause(), m.Workspace.department_id == actor.department_id,
                                 m.Workspace.status.in_(["draft", "submitted", "approved"]), m.Workspace.archived_at.is_(None))))
    require(len(workspaces) <= 100, "IMPACT_LIMIT", "Too many current rotas for an interactive proof")
    rules = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id, m.Policy.status == "active"))]
    after = [p for p in rules if p["id"] not in id_map] + made
    result = {"rules_before": len(rules), "rules_after": len(after),
              "solver_inputs_equal": expanded(rules, id_map, week_start) == expanded(after, None, week_start), "rotas": []}
    for w in workspaces:
        before_data = domain_snapshot(db, w, operational=w.status == "published", mandatory_rules=False)
        after_data = domain_snapshot(db, w, operational=w.status == "published", policy_overrides=retire + made,
                                     mandatory_rules=False)
        a, b = evaluate(before_data), evaluate(after_data)
        diff = same_checks(a, b, id_map)
        score = lambda data, validation: schedule_score(data, validation, department.generation_defaults)["score"]
        result["rotas"].append({"workspace_id": w.id, "name": w.name, "status": w.status, **diff,
                                "findings": len(a["findings"]), "findings_after": len(b["findings"]), "quota_rows": len((a.get("quota_report") or {}).get("rows", [])),
                                "solver_inputs_equal": expanded(before_data["policies"], id_map, week_start) == expanded(after_data["policies"], None, week_start),
                                "score_before": score(before_data, a), "score_after": score(after_data, b)})
        if model:
            result["rotas"][-1]["model_equal"] = same_model(canonical_model(before_data), canonical_model(after_data), id_map)
    result["identical"] = result["solver_inputs_equal"] and all(
        not r["findings_only_before"] and not r["findings_only_after"] and r["quota_equal"] and r["solver_inputs_equal"]
        and (score_may_move or r["score_before"] == r["score_after"]) and r.get("model_equal", True) for r in result["rotas"])
    return result


# Retirement reasons that an undo recognises: an ended rule, and a clinic permission opened (until changed).
ENDED_REASON = s.ENDED_REASON
OPENED_REASON = "Until changed (was until {}):"
CHANGE_SOURCES = {"goal": "Converted to a Balance goal", "overtime": "Overtime switched back on with the limits in force",
                  "companions": "Joined; an implied line dropped", "order": "Joined into the allocation order; balancing as a Balance goal",
                  "ceiling": "Joined into the weekly ceiling"}


def group_rules(db, actor, command):
    """Propose one rule per previewed group (and protected time for academic days). The old rules keep applying until
    an independent approver activates the new rule (which retires them); retiring it later brings them back."""
    from types import SimpleNamespace
    from .catalogue import write_catalogue
    require_role(actor, "coordinator", "admin")
    value = s.PolicyGroupInput.model_validate(command.payload)
    preview = duplicate_groups(db, actor)
    found = {frozenset(g["rule_ids"]): g for g in preview["groups"] if "payload" in g}
    created, calendar = [], 0
    for ids in value.changes:
        change = next((c for c in preview["changes"] if frozenset(c["rule_ids"]) == frozenset(ids)), None)
        require(change is not None, "GROUP_CHANGED", "This change no longer applies the same way; preview again", 409)
        # An ended rule is retired by an approver (Retire, or the script's --activate --changes), not proposed.
        require(not change.get("retire"), "WORKFLOW_STATE", "An ended or duplicate rule is retired by an approver, not proposed", 422)
        what = CHANGE_SOURCES.get(change["shape"], "Rule simplification")
        for payload in [change["payload"], *change.get("extra", [])]:
            payload = {**payload, "source": f"{what}: {value.reason}"[:500]}
            created.append(create_policy(db, actor, SimpleNamespace(payload=payload))["record"])
    for ids in value.groups:
        group = found.get(frozenset(ids))
        require(group is not None, "GROUP_CHANGED", "These rules no longer group the same way; preview the groups again", 409)
        rename = next((value.names[i] for i in ids if i in value.names), None)
        if rename:
            group = {**group, "payload": {**group["payload"], "name": rename}}
        link = value.link_calendar and group.get("calendar")
        require(group["shape"] != "protect" or link, "GROUP_NOTHING", "This rule is only linked to the academic calendar; keep that option on")
        what = "Linked to the academic calendar" if group["shape"] == "protect" else f"Grouped from {len(ids)} similar rules"
        payload = {**group["payload"], "source": f"{what}: {value.reason}"[:500]}
        if link:
            entry = link["entry_id"]
            if not entry:
                entry = write_catalogue(db, actor, SimpleNamespace(target_id=None, expected_revision=None, payload={
                    "catalogue": "programme_calendar_entries", "reason": value.reason,
                    "value": {"programme_id": link["programme_id"], "name": link["name"], "kind": "academic_block",
                              "description": "Protected time (added when grouping rules)", "starts_on": link["starts_on"], "ends_on": link["ends_on"]}}))["record"]["id"]
                calendar += 1
            payload["definition"] = {**payload["definition"], "calendar_entry_id": entry}
        created.append(create_policy(db, actor, SimpleNamespace(payload=payload))["record"])
    rules = sum(len(ids) for ids in value.groups + value.changes)
    return {"records": created, "calendar_entries": calendar,
            "message": f"{len(created)} rule{'s' if len(created) != 1 else ''} proposed in place of {rules}. "
                       "The old rules keep applying until an approver activates each new one; retiring it brings them back."}
