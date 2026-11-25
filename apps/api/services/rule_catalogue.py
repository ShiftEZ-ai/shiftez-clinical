"""The rule catalogue: rules in force moved into catalogue rules, with the proof."""
from .workspace_access import shared_clause, shared_scope
from packages.domain.conditions import effective_severity, scope_applies, scope_has_duty_witness
import json
import copy
import hashlib
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from datetime import date, timedelta
from threading import Event, RLock
from types import SimpleNamespace as Line
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import case, func, literal, select, tuple_
from sqlalchemy.sql.elements import BindParameter
from sqlalchemy.sql.visitors import replacement_traverse

from packages.contracts import schemas as s
from packages.domain import rule_catalogue_migration as migration
from packages.domain.rule_serialization import stored_definition
from packages.domain.input_manifest import digest
from apps.api import models as m
from apps.api.auth import can_read_rules, require_catalogue_read, require_permission, require_role
from apps.api.errors import Problem, require
from apps.api.serialization import record
from .policies import create_policy, grouping_proof, policies_with_status
from .catalogue_version import department_version
from apps.api.settings import Settings
from . import independence


_catalogue_cache = OrderedDict()
_catalogue_building = {}
_catalogue_cache_lock = RLock()
_baseline_catalogue_cache = OrderedDict()

MIGRATED = "Rule catalogue: rules as lines"


class RulesCapture(NamedTuple):
    workspace_id: str
    baseline_id: str
    captured_at: str | None
    policies: list[dict]
    inputs: dict


def migration_inputs(db, actor):
    """The department's active and proposed rules, the rules that must stay as they are, and the first weekday."""
    department = db.get(m.Department, actor.department_id)
    week_start = db.get(m.Organisation, department.organisation_id).week_start
    rows = [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id,
                                                                  m.Policy.status.in_(["active", "proposed"])))]
    from .scoring_defaults import seeded_rows
    rows = seeded_rows(db, rows)
    exceptions = set(db.scalars(select(m.PolicyException.policy_id).where(
        m.PolicyException.department_id == actor.department_id,
        m.PolicyException.status.in_(['proposed', 'approved']),
        m.PolicyException.ends_on > independence.today(db, actor.department_id))))
    rotas = list(db.scalars(select(m.Workspace).where(shared_clause(), m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None))))
    adjusted = {r["policy_id"] for w in rotas for r in w.rule_adjustments or []}
    targets = {r["policy_id"] for w in rotas for r in w.target_overrides or []}
    return rows, migration.held_reasons(rows, exceptions, adjusted, targets), week_start


def migration_plan(db, actor):
    """What moving the rules in force into the catalogue proposes, and what stays as it is (a preview)."""
    require_role(actor, "coordinator", "admin", "approver", "auditor")
    rows, held, week_start = migration_inputs(db, actor)
    proposals, report = migration.plan(rows, held, week_start, actor.department_id)
    active = sum(p["status"] == "active" and p["kind"] != "catalogue" for p in rows)
    return {"proposals": [{**x, "lines": len(x["payload"]["definition"]["rule"]["lines"])} for x in proposals], "report": report,
            "active": active, "moving": sum(len(x["rule_ids"]) for x in proposals), "kept": len(report)}


def migration_proof(db, actor, plan, model=True, solve=False):
    """The grouping proof over every rota being planned (findings, quota rows, scheduler inputs and, with `model`, the
    CP-SAT model); with `solve`, a deterministic CP-SAT solve and greedy generation give the same assignments. The
    migration must keep the findings, quota, score and model identical."""
    groups = [{"rule_ids": x["rule_ids"] + ([x["replaces_id"]] if x["replaces_id"] else []), "payload": x["payload"]} for x in plan["proposals"]]
    proof = grouping_proof(db, actor, groups, model=model, score_may_move=False)
    if solve and groups:
        _solve_proof(db, actor, groups, proof)
    return proof


def _solve_proof(db, actor, groups, proof):
    import copy
    from packages.domain import cp_sat, generation
    from apps.api.snapshots import domain_snapshot
    made = [{**g["payload"], "id": f"00000000-0000-4000-8000-{n:012d}", "status": "active", "active": True, "revision": 1,
             "kind": g["payload"]["definition"]["rule"]["kind"], "department_id": actor.department_id} for n, g in enumerate(groups)]
    old = {i for g in groups for i in g["rule_ids"]}
    retire = [{**record(p), "status": "retired", "active": False} for p in db.scalars(select(m.Policy).where(m.Policy.id.in_(list(old) or [""])))]
    for r in proof["rotas"]:
        w = db.get(m.Workspace, r["workspace_id"])
        before = domain_snapshot(db, w, mandatory_rules=False)
        after = domain_snapshot(db, w, policy_overrides=retire + made, mandatory_rules=False)
        solve = lambda d, **o: cp_sat.generate(copy.deepcopy(d), 10, 1, deterministic=True, **o)  # noqa: E731
        solved = [solve(d) for d in (before, after)]
        r["cp_sat_status"] = solved[0].get("status")
        r["cp_sat_equal"] = _result(solved[0]) == _result(solved[1])
        if r["cp_sat_status"] not in SCHEDULED:
            # No schedule to compare: compare the run that fills what can be filled (open places allowed) instead.
            solved = [solve(d, diagnostic_proposal=True) for d in (before, after)]
            r["fill_status"] = solved[0].get("status")
            r["cp_sat_equal"] = r["cp_sat_equal"] and _result(solved[0]) == _result(solved[1])
        r["cp_sat_assignments"] = len(_result(solved[0])[1])
        greedy = [generation.generate(copy.deepcopy(d), 90, deterministic=True) for d in (before, after)]
        r["greedy_status"], r["greedy_assignments"] = greedy[0].get("status"), len(_result(greedy[0])[1])
        r["greedy_equal"] = _greedy_same(greedy)
        r["greedy_not_applied"] = greedy[0]['not_applied']
        # The rota scheduled again from empty (what is not locked): a whole schedule decided under each set of rules.
        empty = [_emptied(d) for d in (before, after)]
        refilled = [solve(d, diagnostic_proposal=True) for d in empty]
        r["refill_status"], r["refill_assignments"] = refilled[0].get("status"), len(_result(refilled[0])[1])
        r["refill_equal"] = _result(refilled[0]) == _result(refilled[1])
        greedy = [generation.generate(copy.deepcopy(d), 90, deterministic=True) for d in empty]
        r["refill_greedy_status"], r["refill_greedy_assignments"] = greedy[0].get("status"), len(_result(greedy[0])[1])
        r["refill_greedy_equal"] = _greedy_same(greedy)
        r["refill_greedy_not_applied"] = greedy[0]['not_applied']
    proof["identical"] = proof["identical"] and all(r[k] is not False for r in proof["rotas"]
                                                    for k in ("cp_sat_equal", "greedy_equal", "refill_equal", "refill_greedy_equal"))


def _emptied(data):
    import copy
    out = copy.deepcopy(data)
    for s in out["shifts"]:
        if not s.get("locked"):
            s["participants"] = []
    return out


def _greedy_same(runs):
    """Greedy generation stops at a wall-clock limit: a run cut short decides by time, so it is not compared (None)."""
    if any(x.get("status") == "unknown_time_limit" for x in runs):
        return None
    return _result(runs[0]) == _result(runs[1])


SCHEDULED = {"candidate_feasible", "candidate_with_gaps"}


def _result(run):
    """A scheduling run as what it decides: its status and every (session, person) it adds or removes."""
    def people(items):
        return sorted((x["shift_id"], (x.get("participant") or x).get("person_id"), str((x.get("participant") or x).get("start"))) for x in items or [])
    return run.get("status"), people(run.get("additions")) + [("removed", *p) for p in people(run.get("removals"))]


def catalogue_version(db, actor, catalogue_id, status):
    """Read the current lifecycle state, including earlier transitions in this batch."""
    return next((p for p in policies_with_status(db, actor, status)
                 if p.kind == "catalogue" and p.definition["rule"]["id"] == catalogue_id), None)


def waiting_proposal(db, actor, catalogue_id):
    """A proposed version of this catalogue rule waiting for approval (at most one)."""
    return catalogue_version(db, actor, catalogue_id, "proposed")


def migrate_to_catalogue(db, actor, command):
    """Propose the catalogue rules the migration plan makes (all, or the chosen entries). Nothing applies until an
    independent approver activates each one; activating retires the rules it takes over, retiring it brings them back."""
    if command.payload.get("version") == 2:
        from . import rule_catalogue_v2
        return rule_catalogue_v2.propose(db, actor, command)
    require_role(actor, "coordinator", "admin")
    value = s.RuleMigrationInput.model_validate(command.payload)
    rows, held, week_start = migration_inputs(db, actor)
    proposals, report = migration.plan(rows, held, week_start, actor.department_id)
    chosen = [x for x in proposals if not value.catalogue_ids or x["catalogue_id"] in value.catalogue_ids]
    require(not set(value.catalogue_ids) - {x["catalogue_id"] for x in proposals}, "GROUP_CHANGED",
            "These rules no longer move into the catalogue the same way; preview again", 409)
    waiting = any(p["kind"] == "catalogue" and p["status"] == "proposed" for p in rows)
    require(chosen, "PROPOSAL_WAITING" if waiting else "NOTHING_TO_MOVE",
            "Catalogue rules are already waiting for approval; approve or decline them first" if waiting
            else "Nothing more moves into the catalogue now", 409, {"report": report})
    created = []
    for x in chosen:
        require(waiting_proposal(db, actor, x["catalogue_id"]) is None, "PROPOSAL_WAITING",
                f"A change to {x['payload']['name']} is already waiting for approval", 409)
        payload = {**x["payload"], "source": f"{MIGRATED} ({len(x['rule_ids'])}): {value.reason}"[:500]}
        created.append(create_policy(db, actor, type("Command", (), {"payload": payload}), catalogue=True)["record"])
    moved = sum(len(x["rule_ids"]) for x in chosen)
    return {"records": created, "report": report,
            "message": f"{len(created)} catalogue rule{'s' if len(created) != 1 else ''} proposed in place of {moved} rules. "
                       "The rules keep applying until an approver activates each one; retiring it brings them back."}


# The catalogue page and its proposals ------------------------------------------------

def _rows(db, actor, *, with_versions=True):
    from .scoring_defaults import seeded_rows
    from .policies import policy_versions
    # Retired versions connect converted source bindings to their current replacement.
    query = select(m.Policy).where(m.Policy.department_id == actor.department_id,
        *([] if can_read_rules(actor) else [m.Policy.status != 'proposed']))
    rows = [record(p) for p in db.scalars(query)]
    rows = seeded_rows(db, rows)
    for row in rows:
        rule = row["definition"]["rule"]
        if rule.get("id") == "overtime_offers" and rule['kind'] == 'catalogue' and row['status'] in ('active', 'proposed'):
            effective = s.CatalogueRule.model_validate(rule, context={"historical": True})
            row["definition"] = {**row["definition"], "rule": {**rule, "lines": [
                {**line, "settings": value.model_dump(mode="json")["settings"]}
                for line, value in zip(rule["lines"], effective.lines)]}}
    if not with_versions:
        return rows
    current = {row['id']: row for row in policy_versions(db, actor, [row for row in rows if row['status'] in ('active', 'proposed')])}
    return [{**current.get(row['id'], row), 'safety': s.safety_rule(row['definition'])} for row in rows]


def _in_force_inputs(db, actor, day, *, for_reach=True):
    """Build the one evaluator context used by both catalogue projections and counts.

    ``_in_force`` first binds scheduling facts, then expands tag-backed selectors before
    applying local-day bounds.  The count path uses that exact minimal context; the full
    catalogue adds people data only after the shared in-force result for reach/attention.
    """
    if for_reach:
        from .policies import people_data
        return people_data(db, actor, day)
    department, organisation = db.execute(select(m.Department, m.Organisation).join(
        m.Organisation, m.Organisation.id == m.Department.organisation_id).where(
            m.Department.id == actor.department_id)).one()
    from apps.api.snapshots import scheduling_inputs, tag_data
    from packages.domain.time import local_midnight
    data = {'organisation': {'timezone': organisation.timezone},
            'tags': tag_data(db, [department.id]),
            **scheduling_inputs(db, [department.id])}
    zone = organisation.timezone
    return data, local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)


def in_force_dependency_stamp(day, timezone, input_revision, versions):
    """The count projection's exact invalidation inputs, from the catalogue versions map.

    The shared input builder consumes policy rows, scheduling-input families and tags;
    those source families advance the organisation input revision.  The policy, tag and
    tag-member fragments are the direct rows represented in the catalogue versions map.
    Workspaces and acknowledgements are deliberately absent because the in-force path
    never reads them.
    """
    return ('rules-in-force-v3', str(day), timezone, input_revision,
            tuple((kind, versions.get(kind)) for kind in ('policies', 'tags', 'tag_members')))


def _in_force(data, rows, a, b):
    """The department policies whose checker copies are in force on this read's day."""
    from packages.domain.conditions import active_rules, with_tags
    from packages.domain.rule_lines import bound_policies, read_quality_for
    from packages.domain.validation import within_dates
    from apps.api.snapshots import DomainSnapshot
    read_quality = read_quality_for(data)
    data = DomainSnapshot(data, read_quality=read_quality)
    policies = bound_policies([row for row in rows if not row['definition'].get('workspace_id')], data,
                              read_quality=read_quality)
    data['policies'] = policies
    data['_catalogue_v2_materialized'] = True
    data = with_tags(data)
    ids = {row['id'] for row in active_rules(data) if within_dates(row, a, b, data['organisation']['timezone'])}
    return data, [{**row, 'safety': s.safety_rule(row['definition'])} for row in policies if row['id'] in ids]


def _reach_scope_needs_duty_witness(scope):
    """Whether a scope can change with a synthetic duty context.

    Ordinary people/rank/programme/tag and clinic/location selectors are
    person-side reach. Capability and dated duty predicates need the existing
    coherent witness check because a person may hold a capability only at one
    clinic or on part of the window.
    """
    if scope.get('capability_ids'):
        return True
    pending = [scope.get('conditions')]
    while pending:
        node = pending.pop()
        if not node:
            continue
        if 'conditions' in node:
            pending.extend(node.get('conditions') or ())
            continue
        if node.get('field') in {'capability_ids', 'date', 'date_set_ids'}:
            return True
    return False


def _reach(data, rows, a, b, *, include_held=True):
    """Per catalogue line, on one day [a, b): (the people it would hold for alone, the people it holds for once more
    specific lines take theirs). Each distinct people side is asked once; a line naming people asks only them."""
    import json
    from packages.domain.quotas import MAX_SUBJECT_CONTEXTS
    from packages.domain.rule_lines import _earlier, _later, bound_policies, expand, scope_of
    from packages.domain.validation import index_person_rows, named_people, within_dates
    rows = bound_policies(rows, {**data, '_catalogue_v2_materialized': True})
    context_budget = [MAX_SUBJECT_CONTEXTS]
    data = {**data, 'policies': rows, '_person_rows': index_person_rows(data), '_person_dates_cache': {},
            '_line_index_cache': {}, '_quota_subject_context_budget': context_budget}
    zone, asked, would, held = data["organisation"]["timezone"], {}, {}, {}

    def people(scope, dated):
        if not within_dates(dated, a, b, zone):
            return set()
        # ``scope_of`` is the canonical who/where representation.  The cache
        # belongs to this read only: a later rules change must recompute from
        # its new stamp, never reuse a process-wide answer.
        key = (json.dumps(scope, sort_keys=True, default=str, separators=(",", ":")),
               a.date().isoformat())
        if key not in asked:
            named = named_people(scope)
            candidates = data["people"] if named is None else named & set(data["people"])
            if _reach_scope_needs_duty_witness(scope):
                asked[key] = {pid for pid in candidates
                              if scope_has_duty_witness(data, scope, pid, a, b, dated['department_id'], budget=context_budget)}
            else:
                asked[key] = {pid for pid in candidates
                              if scope_applies(data, scope, pid, None, a, b)}
        return asked[key]
    for row in rows:
        for c in expand(row, outrank=False):
            line = c["template_line"]["id"]
            would[line] = would.get(line, set()) | people(c["definition"]["scope"], c)
        lines = row["definition"]["rule"].get("lines") or []
        for line in lines:  # a line of explicit Nones writes no copy: who it names, on its dates
            if line["id"] not in would:
                would[line["id"]] = people(scope_of(line), {"starts_on": _later(row.get("starts_on"), line.get("starts_on")),
                                                            "ends_on": _earlier(row.get("ends_on"), line.get("ends_on")),
                                                            "department_id": row['department_id']})
        if not include_held:
            continue
        # One person can keep the default in one clinic and a more specific line in another. Ask the canonical
        # winning copies, including split numbers, rather than subtracting whole people across duty contexts.
        numeric = set()
        for copy in expand(row):
            line = copy['template_line']['id']
            numeric.add(line)
            held.setdefault(line, set()).update(people(copy['definition']['scope'], copy))
        for line in lines:
            if line['id'] not in numeric:
                held[line['id']] = would.get(line['id'], set())  # authored None-only lines still have reach
    return would, held


def _tagged(data, rows):
    """Catalogue rows as every check reads them: the tags their lines name read as their members (a #tag who is a
    group, never a person)."""
    from packages.domain.conditions import with_tags
    preview = {k: v for k, v in data.items() if k not in ('scheduling_facts', 'source_bindings', '_catalogue_v2_materialized')}
    return with_tags({**preview, "policies": rows})["policies"]


def _both(row, would, *, same=None, include_safety=False):
    """[(k, n, people)]: lines that both apply to people today."""
    from packages.domain.rule_lines import both_apply
    lines = row["definition"]["rule"].get("lines") or []
    pairs = both_apply(row, same=same, include_safety=include_safety)
    found = ((k, n, len(would.get(lines[k]["id"], set()) & would.get(lines[n]["id"], set()))) for k, n in pairs)
    return [x for x in found if x[2]]


def explained_conflicts(data, *, starts_on=None, ends_on=None):
    """Project the existing catalogue overlap/reach proof into Why conflicts.

    The overlap pairs and person reach come from the same helpers as the Rules
    catalogue.  Session reach is a bounded read over the already acquired
    session projection, so the client never infers it from line ids.
    """
    from datetime import date
    import uuid
    from packages.domain.conditions import scope_applies
    from packages.domain.rule_lines import scope_of
    from packages.domain.time import local_midnight
    from packages.domain.validation import within_dates

    workspace = data.get('workspace') or {}
    zone = data['organisation']['timezone']
    first = starts_on or date.fromisoformat(str(workspace['starts_on']))
    last = ends_on or date.fromisoformat(str(workspace['ends_on']))
    a, b = local_midnight(first, zone), local_midnight(last, zone)
    rows = [row for row in data.get('policies') or ()
            if row.get('status') == 'active' and row.get('active', True)
            and row.get('definition', {}).get('rule', {}).get('kind') == 'catalogue'
            and row.get('definition', {}).get('rule', {}).get('lines')]
    if not rows:
        return []
    tagged = _tagged(data, rows)
    would, _ = _reach(data, tagged, a, b, include_held=False)
    focus = set(data.get('focus_ids') or ())
    people = tuple(sorted(data.get('people') or {}))
    conflicts = []
    for row in sorted(tagged, key=lambda item: item['id']):
        lines = row['definition']['rule'].get('lines') or []
        for left, right, _ in _both(row, would, include_safety=True):
            first_line, second_line = lines[left], lines[right]
            line_ids = [first_line['id'], second_line['id']]
            line_refs = [left + 1, right + 1]
            sessions = 0
            for shift in sorted(data.get('shifts') or (), key=lambda item: (item.get('start', ''), item['id'])):
                if focus and shift['id'] not in focus:
                    continue
                if shift.get('cancelled') or not within_dates(shift, a, b, zone):
                    continue
                if any(scope_applies(data, scope_of(first_line), person_id, shift, a, b)
                       and scope_applies(data, scope_of(second_line), person_id, shift, a, b)
                       for person_id in people):
                    sessions += 1
            conflict_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                         'fmuc:why-conflict:' + ':'.join([row['id'], *line_ids])))
            conflicts.append({
                'conflict_id': conflict_id,
                'sentence': f"{row.get('name') or 'This rule'} has overlapping lines that apply together.",
                'line_ids': line_ids,
                'rule_ids': [row['id'], row['id']],
                'line_refs': line_refs,
                'reach': {'sessions_count': sessions},
            })
    return conflicts


def _question_group_key(label):
    from packages.contracts.rule_catalogue import QUESTION_GROUPS
    return next((key for key, value in QUESTION_GROUPS.items() if value == label), label)


def _question_groups():
    from packages.contracts.rule_catalogue import QUESTION_GROUPS, QUESTION_GROUP_ORDER
    return [{'key': next(key for key, value in QUESTION_GROUPS.items() if value == label),
             'letter': label[:1], 'label': label[2:].strip(), 'order': order}
            for order, label in enumerate(QUESTION_GROUP_ORDER)]


def _people_table(data, explained, actor, lo, hi, *, captured=False):
    """Build the D-group table from the same server-authored explainer cells; missing children inherit."""
    from packages.domain.rule_explainer import _scope_values
    from packages.domain.validation import exception_for

    workload = [row for row in explained
                if not row.get('unexplained')
                and _question_group_key(row['question_group']) in {'workload_person', 'workload_group'}]
    if not workload:
        return None
    originals = {p['id']: p for p in data.get('policies') or ()}
    memberships = [row for row in data.get('memberships') or ()
                   if row.get('department_id') == data.get('workspace', {}).get('department_id')]
    rank_ids = sorted(ident for ident, row in data.get('categories', {}).items()
                      if row.get('department_id') == data.get('workspace', {}).get('department_id'))
    people = sorted(data.get('people') or {}, key=lambda pid: (data['people'][pid].get('display_name', '').casefold(), pid))
    by_person = {}
    for membership in memberships:
        by_person.setdefault(membership['person_id'], []).append(membership)

    def actions(rule_id, line_id, safety, *, exception=False):
        if captured:
            return []
        can_adjust = bool(actor and (actor.permits('coordinator') or actor.permits('admin')))
        result = [{'action': 'open_rule', 'label': 'Open rule', 'enabled': True, 'line_id': line_id}]
        if safety:
            result.append({'action': 'rota_stricter', 'label': 'Adjust stricter for this rota',
                           'enabled': can_adjust, 'disabled_reason': None if can_adjust else 'Requires coordinator access',
                           'line_id': line_id})
        else:
            result.extend([
                {'action': 'rota_adjust', 'label': 'Adjust for this rota', 'enabled': can_adjust,
                 'disabled_reason': None if can_adjust else 'Requires coordinator access', 'line_id': line_id},
                {'action': 'propose_default', 'label': 'Make department default', 'enabled': can_adjust,
                 'disabled_reason': None if can_adjust else 'Requires coordinator access', 'line_id': line_id},
            ])
        return result

    def line_scope(line):
        from packages.domain.rule_lines import scope_of
        return scope_of(line)

    sentence_by_line = {}
    safety_by_rule = {rule['rule_id']: bool(rule.get('safety')) for rule in workload}
    for rule in workload:
        for sentence in rule.get('sentences') or ():
            for line_id in sentence.get('source_line_ids') or ():
                sentence_by_line[(rule['rule_id'], line_id)] = sentence

    def column_for(rule, sentence, line):
        from packages.domain.rule_lines import measure
        scope = line_scope(line)
        measured = measure((originals.get(rule['rule_id']) or {}).get('definition', {}).get('rule', {}), line, block=True)
        quota = measured.get('block_quota') or measured.get('max_duties') or measured.get('max_hours') or {}
        overtime = quota.get('overtime')
        where = sentence.get('where') or 'All clinics and duties'
        qualified = bool(sentence.get('qualified'))
        signature = json.dumps([where, overtime, quota.get('window_alignment'), quota.get('window_days'),
                                scope.get('assignment_type_ids') or scope.get('assignment_type_id')],
                               sort_keys=True, default=str)
        key = f"{rule['rule_id']}:{hashlib.sha256(signature.encode()).hexdigest()[:12]}"
        if not qualified:
            return rule['rule_id'], rule.get('title') or rule['summary_sentence'] or rule['question_group']
        parts = []
        if where != 'All clinics and duties':
            parts.append(where)
        if overtime == 'only':
            parts.append('overtime')
        elif overtime == 'exclude':
            parts.append('non-overtime')
        label = ' '.join(parts) or rule.get('title') or rule['question_group']
        if quota.get('window_alignment') == 'calendar_month':
            label += ' per month'
        elif quota.get('window_alignment') == 'calendar_week':
            label += ' per week'
        return key, label

    columns = []
    column_values = {}
    for rule in workload:
        policy = originals.get(rule['rule_id']) or {}
        lines = policy.get('definition', {}).get('rule', {}).get('lines') or []
        for line in lines:
            sentence = sentence_by_line.get((rule['rule_id'], line['id']))
            if sentence is None:
                continue
            key, label = column_for(rule, sentence, line)
            column = column_values.setdefault(key, {'label': label, 'rule_ids': [], 'values': {}})
            if rule['rule_id'] not in column['rule_ids']:
                column['rule_ids'].append(rule['rule_id'])
            scope = line_scope(line)
            who = _scope_values(scope, {'person_ids', 'category_ids'}, positive_only=True)
            specificity = (2 if who.get('person_ids') else 1 if who.get('category_ids') else 0,
                           len(who.get('person_ids') or ()) + len(who.get('category_ids') or ()))
            column['values'][line['id']] = (specificity, sentence, line, rule['rule_id'])
            if key not in columns:
                columns.append(key)

    def cell_for(key, person_id=None, rank_id=None):
        column = column_values[key]
        if not captured and person_id and data['exceptions']:
            exception = next((matched for rule_id in column['rule_ids'] if rule_id in originals
                              and (matched := exception_for(data, originals[rule_id], person_id, lo, hi))), None)
            if exception:
                rule_id = exception['policy_id']
                return {'display': 'Approved exception', 'cell': 'Approved exception', 'short': None,
                        'qualified': True, 'rule_id': rule_id, 'line_id': None,
                        'exception_id': exception['id'], 'kind': 'exception',
                        'actions': actions(rule_id, None, safety_by_rule.get(rule_id, False), exception=True)}
        candidates = []
        for specificity, sentence, line, rule_id in column['values'].values():
            scope = line_scope(line)
            selected = _scope_values(scope, {'person_ids', 'category_ids'}, positive_only=True)
            if person_id and selected.get('person_ids') and person_id in selected['person_ids']:
                candidates.append((specificity, sentence, line, rule_id))
            elif rank_id and selected.get('category_ids') and rank_id in selected['category_ids']:
                candidates.append((specificity, sentence, line, rule_id))
            elif not selected.get('person_ids') and not selected.get('category_ids'):
                candidates.append((specificity, sentence, line, rule_id))
        if not candidates:
            return None
        _, sentence, line, rule_id = max(candidates, key=lambda item: (item[0], item[2].get('id', '')))
        cell = sentence.get('cell') or sentence.get('number') or sentence.get('text')
        short = sentence.get('short')
        exception = None
        if captured and person_id and data['exceptions']:
            exception = exception_for(data, originals[rule_id], person_id, lo, hi)
        if exception:
            # Approval on the selected day does not establish coverage of a workload counting window.
            last_day = date.fromisoformat(exception['ends_on']) - timedelta(days=1)
            approval = f"Approved exception {exception['starts_on']} through {last_day}"
            cell = f'{cell} · {approval}'
            short = f'{short} · {approval}' if short else None
        return {'display': cell, 'cell': cell, 'short': short,
                'qualified': bool(sentence.get('qualified')), 'rule_id': rule_id, 'line_id': line['id'],
                'exception_id': exception['id'] if exception else None, 'kind': 'line',
                'actions': actions(rule_id, line['id'], safety_by_rule.get(rule_id, False))}

    everyone_id = 'everyone'
    full = {everyone_id: {key: cell_for(key) for key in columns}}
    rows = [{'kind': 'everyone', 'id': everyone_id, 'display': 'Everyone', 'rank_id': None, 'parent': None,
             'cells': {key: value for key, value in full[everyone_id].items() if value}}]
    used = set(key for key, value in full[everyone_id].items() if value)
    for rank_id in rank_ids:
        rank = data['categories'][rank_id]
        values = {key: cell_for(key, rank_id=rank_id) for key in columns}
        parent = full[everyone_id]
        cells = {key: value for key, value in values.items() if value and value != parent.get(key)}
        full[rank_id] = values
        used.update(key for key, value in values.items() if value)
        rows.append({'kind': 'rank', 'id': rank_id, 'display': rank.get('name') or rank.get('short_code') or rank_id,
                     'rank_id': rank_id, 'parent': everyone_id, 'cells': cells})
        for person_id in sorted((pid for pid in people if any(membership.get('category_id') == rank_id
                                                               for membership in by_person.get(pid, ()))),
                                key=lambda pid: (data['people'][pid].get('display_name', '').casefold(), pid)):
            person_values = {key: cell_for(key, person_id=person_id, rank_id=rank_id) for key in columns}
            person_parent = full[rank_id]
            person_cells = {key: value for key, value in person_values.items()
                            if value and value != person_parent.get(key)}
            if not person_cells:
                continue
            used.update(key for key, value in person_values.items() if value)
            full[person_id] = person_values
            person = data['people'][person_id]
            rows.append({'kind': 'person', 'id': person_id, 'display': person.get('display_name') or person_id,
                         'rank_id': rank_id, 'parent': rank_id, 'cells': person_cells})
    columns_out = [{'key': key, 'label': column_values[key]['label'], 'rule_ids': column_values[key]['rule_ids']}
                   for key in columns if key in used]
    return {'columns': columns_out, 'rows': rows}


def department_rules_explained(db, actor, day, *, workspace_id=None, baseline_id=None):
    """Common department-wide Why projection; no rota status is fabricated."""
    from datetime import timedelta
    from packages.domain.rule_explainer import explain_rules
    from packages.domain.validation import index_person_rows
    from packages.domain.time import local_midnight
    from apps.api.snapshots import shift_records
    from .policies import people_period
    from .scoring_defaults import seeded_rows
    from apps.api.serialization import record

    capture = rules_source(db, actor, workspace_id, baseline_id=baseline_id) if workspace_id is not None else None
    data, _, _ = people_period(db, actor, day, day + timedelta(days=1))
    if capture is None:
        policies = seeded_rows(db, [record(row) for row in db.scalars(select(m.Policy).where(
            m.Policy.department_id == actor.department_id))])
        policies = [row for row in policies if row.get('status') == 'active' and row.get('active', True)
                    and not row.get('definition', {}).get('workspace_id')]
        exceptions = [record(row) for row in db.scalars(select(m.PolicyException).where(
            m.PolicyException.department_id == actor.department_id, m.PolicyException.status == 'approved'))]
    else:
        policies = capture.policies
        data.update(capture.inputs)
        exceptions = capture.inputs['exceptions']
    department = db.get(m.Department, actor.department_id)
    org = db.get(m.Organisation, department.organisation_id)
    lo, hi = local_midnight(day, org.timezone), local_midnight(day + timedelta(days=1), org.timezone)
    shifts = shift_records(db, db.scalars(select(m.Shift).join(m.Workspace, m.Workspace.id == m.Shift.workspace_id).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None),
        m.Shift.start < hi, m.Shift.end > lo).order_by(m.Shift.start, m.Shift.id)))
    data = {**data, 'policies': policies, 'exceptions': exceptions,
            'shifts': shifts, 'focus_ids': [row['id'] for row in shifts],
            'workspace': {'id': None, 'department_id': actor.department_id,
                          'starts_on': day.isoformat(), 'ends_on': (day + timedelta(days=1)).isoformat(),
                          'status': None}, 'department': record(department)}
    from packages.domain.conditions import with_tags
    with _captured_projection() if capture is not None else nullcontext():
        data = with_tags(data)
        data['_person_rows'] = index_person_rows(data)
        result = explain_rules(data, rota=False)
        conflicts = explained_conflicts(data, starts_on=day, ends_on=day + timedelta(days=1))
        people_table = _people_table(data, result['rules'], actor, lo, hi, captured=capture is not None)
    return {'at': day, 'status': None, 'groups': _question_groups(),
            'rules': [{'rule_id': rule['rule_id'], 'catalogue_id': rule.get('catalogue_id'),
                       'question_group': _question_group_key(rule['question_group']),
                       'safety': rule['safety'], 'level': rule['level'],
                       'summary_sentence': rule['summary_sentence'] or (rule['sentences'][0]['text'] if rule['sentences'] else ''),
                       'summary_short': rule.get('summary_short') or rule['summary_sentence'] or (rule['sentences'][0]['text'] if rule['sentences'] else ''),
                       'unexplained': bool(rule.get('unexplained')),
                       'line_ids': rule.get('line_ids') or []} for rule in result['rules']],
            'conflicts': conflicts,
            'people_table': people_table}


def _line_overlaps(row, would, *, outranked=None, both=None):
    """Per line id: the lines it overlaps with — [{line_id: the other line, people: who both would hold for today,
    winner: the line that applies to them, or None when both apply (equal tiers)}]. A line that gives way lists the
    lines that win; a winning line lists the lines it outranks. The one reading of overlaps: counts derive from it."""
    from packages.domain.rule_lines import outranking
    lines, out = row["definition"]["rule"].get("lines") or [], {}
    for n, wins in (outranking(row) if outranked is None else outranked).items():
        loser = lines[n]["id"]
        for w in wins:
            winner = lines[w]["id"]
            people = len(would.get(loser, set()) & would.get(winner, set()))
            out.setdefault(loser, []).append({"line_id": winner, "people": people, "winner": winner})
            out.setdefault(winner, []).append({"line_id": loser, "people": people, "winner": winner})
    for k, n, people in (_both(row, would) if both is None else both):
        out.setdefault(lines[k]["id"], []).append({"line_id": lines[n]["id"], "people": people, "winner": None})
        out.setdefault(lines[n]["id"], []).append({"line_id": lines[k]["id"], "people": people, "winner": None})
    return out


def _line_replaces(row, would, *, by_number=None):
    """Per winning line: the broader lines and numeric slots replaced in the resolver's precedence partition."""
    from packages.domain.rule_lines import outranking_by_number
    lines, replaced = row['definition']['rule'].get('lines') or [], {}
    for (n, key), winners in (outranking_by_number(row) if by_number is None else by_number).items():
        for w in winners:
            loser, winner = lines[n]['id'], lines[w]['id']
            if would.get(loser, set()) & would.get(winner, set()):
                replaced.setdefault(winner, {}).setdefault(loser, []).append(key)
    return {winner: [{'replaced_line_id': loser, 'numbers': sorted(keys)} for loser, keys in broader.items()]
            for winner, broader in replaced.items()}


def _gives_way(line_overlaps):
    """How many lines give way to a more specific one."""
    return sum(any(o["winner"] not in (line, None) for o in items) for line, items in line_overlaps.items())


def with_blocks(catalogue_id, settings, lines, start, catalogue_version=1):
    """New undated repeating quotas follow line validity; explicit finite counting bodies retain their dates."""
    from datetime import date, timedelta
    from packages.contracts.rule_catalogue import catalogue_entry
    from packages.domain.rule_lines import _settings, undated_quota
    rule, out = {"id": catalogue_id, "settings": settings or {}, "catalogue_version": catalogue_version}, []
    for line in lines or []:
        try:
            if catalogue_version == 2 and line.get("measure") is not None:
                # A typed Workload line (measure × period) is read as the settings it stands for, as validation reads it.
                line = s.RuleLine.model_validate(line).model_dump(mode="json", exclude_unset=True)
            block = _settings(rule, line).get('block_quota', {})
            if block.get('repeat') and not (line.get('starts_on') or block.get('starts_on')):
                line = {**line, 'starts_on': str(start), 'ends_on': line.get('ends_on')}
            if undated_quota(rule, line):
                first = line.get("starts_on") or str(start)
                defaults = catalogue_entry(catalogue_id, catalogue_version)['default_settings'].get('block_quota', {})
                block = {**defaults, **_settings(rule, line).get('block_quota', {})}
                alignment = block.get('window_alignment', 'rolling')
                repeating = alignment in ('fixed', 'calendar_week', 'calendar_month') or bool(block.get('window_days'))
                if repeating and 'repeat' not in block:
                    own = {**defaults, **(line.get('settings') or {}).get('block_quota', {}), 'repeat': True}
                    line = {**line, 'starts_on': first, 'ends_on': line.get('ends_on'),
                            'settings': {**(line.get('settings') or {}), 'block_quota': own}}
                elif not repeating:
                    line = {**line, "starts_on": first, "ends_on": line.get("ends_on") or (date.fromisoformat(str(first)) + timedelta(days=365)).isoformat()}
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        out.append(line)
    return out


def _diff(old, new):
    """Lines added, removed and changed between two versions of a catalogue rule (by line id)."""
    before = {line["id"]: line for line in (old or {}).get("lines") or []}
    after = {line["id"]: line for line in new.get("lines") or []}
    return {"added": [i for i in after if i not in before], "removed": [i for i in before if i not in after],
            "changed": [i for i in after if i in before and after[i] != before[i]],
            "on": None if old is None or old.get("on", True) == new.get("on", True) else new.get("on", True),
            "settings": None if old is None or old.get("settings") == new.get("settings") else new.get("settings")}


def _proposal_line(line, previous):
    """Validate defaults without rewriting the stored representation of unchanged fields."""
    after = line.model_dump(mode="json")
    if previous is None:
        return after
    before = s.RuleLine.model_validate(previous).model_dump(mode="json")
    return {key: previous[key] if key in previous and value == before[key] else value
            for key, value in after.items() if key in previous or value != before[key]}


def _kept_safety_numbers(entry, rule, lines, previous):
    """Carry forward an unchanged legacy number at the mandatory level it already applies at."""
    from packages.contracts.rule_catalogue import slot_for_number

    if not previous:
        return lines
    out = []
    for line in lines:
        try:
            old = previous.get(line.get('id'), {})
            numbers = line.get('numbers') or {}
            kept = {}
            for key, number in numbers.items():
                if number == (old.get('numbers') or {}).get(key) and number.get('level') not in (None, 'mandatory'):
                    slot = slot_for_number(entry, key, rule, line, numbers)
                    if _safety_reason(slot):
                        kept[key] = {**number, 'level': 'mandatory'}
        except (AttributeError, KeyError, TypeError, ValueError):
            # The check endpoint accepts raw input. Let its normal validation report malformed lines.
            kept = {}
        out.append({**line, 'numbers': {**numbers, **kept}} if kept else line)
    return out


def _catalogue_definitions(rows, department_id=None):
    definitions = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('status') != 'active':
            continue
        if department_id is not None and row.get('department_id') != department_id:
            continue
        definition = row.get('definition')
        rule = definition.get('rule') if isinstance(definition, dict) else None
        if (isinstance(rule, dict) and rule.get('kind') == 'catalogue' and isinstance(rule.get('id'), str)
                and not definition.get('workspace_id')):
            definitions[rule['id']] = rule
    return definitions


def _captured_rules_problem():
    return Problem(409, 'RULES_SNAPSHOT_UNAVAILABLE', 'This draft baseline does not contain readable rules')


@contextmanager
def _captured_projection():
    """Translate malformed stored inputs at the captured read boundary."""
    try:
        yield
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise _captured_rules_problem() from error


def _captured_payload(baseline):
    from apps.api.workspace_baselines import verify
    try:
        return verify(baseline)
    except Problem as error:
        if error.code != 'BASELINE_INVALID':
            raise
        raise _captured_rules_problem() from error


def rules_source(db, actor, workspace_id, *, baseline_id=None) -> RulesCapture:
    """Read only the department rules and their retained dependencies from a visible draft."""
    from apps.api.workspace_baselines import latest
    from packages.domain.rule_lines import fact_policy, seeded_policies
    from .common import scoped

    require(actor.permits(*s.PERMISSIONS['planning.read']), 'NOT_FOUND', 'Record not found', 404)
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    require(workspace.status == 'draft' and workspace.archived_at is None, 'NOT_FOUND', 'Record not found', 404)
    baseline = latest(db, workspace.id)
    if baseline is None or 'rules' not in (baseline.included_sources or ()):
        raise _captured_rules_problem()
    if baseline_id is not None and baseline.id != baseline_id:
        raise Problem(409, 'RULES_SNAPSHOT_UNAVAILABLE', 'The draft rules capture changed. Retry to read the current capture')
    payload = _captured_payload(baseline)

    def records(key, default=None):
        rows = payload.get(key, default)
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise _captured_rules_problem()
        return rows

    policies = []
    for row in records('policies'):
        definition = row.get('definition')
        rule = definition.get('rule') if isinstance(definition, dict) else None
        if not isinstance(row.get('department_id'), str):
            raise _captured_rules_problem()
        if row['department_id'] != actor.department_id:
            continue
        if not (all(isinstance(row.get(key), str) for key in ('id', 'name', 'kind'))
                and row.get('status') in ('active', 'proposed', 'retired')
                and isinstance(rule, dict) and isinstance(rule.get('kind'), str)):
            raise _captured_rules_problem()
        if row['status'] in ('active', 'retired') and not definition.get('workspace_id'):
            policies.append(row)

    inputs = {key: [row for row in records(key, []) if row.get('department_id') == actor.department_id]
              for key in ('exceptions', 'scheduling_facts', 'type_lead_ownership')}
    policy_ids = {policy['id'] for policy in policies}
    with _captured_projection():
        seeds = [row for row in records('policy_seed_lines', [])
                 if row.get('department_id') == actor.department_id and row.get('policy_id') in policy_ids]
        approved = []
        for exception in inputs['exceptions']:
            if exception.get('status') != 'approved':
                continue
            if str(UUID(exception['policy_id'])) != exception['policy_id']:
                raise _captured_rules_problem()
            if exception['policy_id'] not in policy_ids:
                continue
            first, last = (date.fromisoformat(exception[key]) for key in ('starts_on', 'ends_on'))
            if (not all(str(UUID(exception[key])) == exception[key] for key in ('id', 'person_id'))
                    or type(exception['policy_revision']) is not int or exception['policy_revision'] < 1
                    or first.isoformat() != exception['starts_on'] or last.isoformat() != exception['ends_on']
                    or first >= last):
                raise _captured_rules_problem()
            approved.append(exception)
        inputs['exceptions'] = approved
        fact_ids = {row['id'] for row in inputs['scheduling_facts']}
        target_ids = {'policy': policy_ids, 'fact': fact_ids}
        bindings = []
        for binding in records('source_bindings', []):
            source = binding['source']
            if source['department_id'] != actor.department_id or source['definition'].get('workspace_id'):
                continue
            if (source['id'] != binding['source_policy_id'] or source['id'] not in policy_ids
                    or type(binding['source_order']) is not int
                    or not isinstance(binding['destinations'], list)
                    or any(link['id'] not in target_ids[link['kind']] for link in binding['destinations'])):
                raise _captured_rules_problem()
            bindings.append(binding)
        inputs['source_bindings'] = bindings
        policies = seeded_policies(policies, seeds)
        for policy in [*policies, *(fact_policy(row) for row in inputs['scheduling_facts'])]:
            definition = stored_definition(policy['definition'])
            s.PolicyDefinition.model_validate_json(json.dumps(definition), strict=True, context={'historical': True})
    inputs['policy_seed_lines'] = []
    captured_at = (baseline.provenance or {}).get('captured_at')
    if captured_at is None and baseline.created_at is not None:
        captured_at = baseline.created_at.isoformat()
    return RulesCapture(workspace.id, baseline.id, captured_at, policies, inputs)


def _source_options(db, actor, live):
    """List the live rules source and readable draft baselines for this department."""
    options = [{'workspace_id': None, 'label': 'Live', 'captured_at': None, 'changed': 0}]
    if not actor.permits(*s.PERMISSIONS['planning.read']):
        return options
    workspaces = db.scalars(select(m.Workspace).where(
        m.Workspace.department_id == actor.department_id,
        m.Workspace.status == 'draft', m.Workspace.archived_at.is_(None)).order_by(
            m.Workspace.name, m.Workspace.id)).all()
    if not workspaces:
        return options

    workspace_ids = [workspace.id for workspace in workspaces]
    with _catalogue_cache_lock:
        cached_keys = tuple(_baseline_catalogue_cache)
    baseline = m.WorkspaceBaseline
    latest = select(
        baseline.id,
        func.row_number().over(
            partition_by=baseline.workspace_id,
            order_by=(baseline.workspace_revision.desc(), baseline.created_at.desc(), baseline.id.desc()),
        ).label('position'),
    ).where(baseline.workspace_id.in_(workspace_ids)).subquery()
    known = tuple_(baseline.id, baseline.payload_hash).in_(cached_keys) if cached_keys else None
    miss = ~known if known is not None else literal(True)
    miss_order = case((miss, 0), else_=1)
    ranked = select(
        baseline.id.label('id'),
        baseline.workspace_id.label('workspace_id'),
        baseline.schema_version.label('schema_version'),
        baseline.payload_hash.label('payload_hash'),
        baseline.provenance.label('provenance'),
        baseline.created_at.label('created_at'),
        baseline.included_sources.label('included_sources'),
        m.Workspace.name.label('workspace_name'),
        miss.label('cache_miss'),
        func.row_number().over(order_by=(miss_order, m.Workspace.name, m.Workspace.id)).label('miss_position'),
    ).join(latest, latest.c.id == baseline.id).join(
        m.Workspace, m.Workspace.id == baseline.workspace_id).where(latest.c.position == 1).subquery()
    rows = db.execute(select(
        ranked,
        # Keep the read bounded: only the first three cache misses need payload verification this request.
        case((ranked.c.cache_miss & (ranked.c.miss_position <= 3), baseline.canonical_payload),
             else_=None).label('canonical_payload'),
    ).join(baseline, baseline.id == ranked.c.id).order_by(
        ranked.c.workspace_name, ranked.c.workspace_id)).mappings().all()
    workspaces_by_id = {workspace.id: workspace for workspace in workspaces}
    for row in rows:
        included_sources = row['included_sources'] or []
        if 'rules' not in included_sources:
            continue
        key = (row['id'], row['payload_hash'])
        with _catalogue_cache_lock:
            snapshot = _baseline_catalogue_cache.get(key)
            if snapshot is not None:
                _baseline_catalogue_cache.move_to_end(key)
        if snapshot is None and row['canonical_payload'] is not None:
            baseline_row = Line(
                id=row['id'], workspace_id=row['workspace_id'], schema_version=row['schema_version'],
                payload_hash=row['payload_hash'], provenance=row['provenance'], created_at=row['created_at'],
                included_sources=included_sources, canonical_payload=row['canonical_payload'],
            )
            try:
                payload = _captured_payload(baseline_row)
            except Problem as error:
                if error.code != 'RULES_SNAPSHOT_UNAVAILABLE':
                    raise
            else:
                source_rows = payload.get('policies')
                if isinstance(source_rows, (list, dict)):
                    snapshot = _catalogue_definitions(
                        source_rows.values() if isinstance(source_rows, dict) else source_rows, actor.department_id)
                    with _catalogue_cache_lock:
                        _baseline_catalogue_cache[key] = snapshot
                        _baseline_catalogue_cache.move_to_end(key)
                        while len(_baseline_catalogue_cache) > Settings().catalogue_cache_entries:
                            _baseline_catalogue_cache.popitem(last=False)
        workspace = workspaces_by_id[row['workspace_id']]
        captured_at = (row['provenance'] or {}).get('captured_at')
        if captured_at is None and row['created_at'] is not None:
            captured_at = row['created_at'].isoformat()
        changed = None if snapshot is None else (
            sum(snapshot.get(rule_id) != definition for rule_id, definition in live.items())
            + sum(rule_id not in live for rule_id in snapshot)
        )
        options.append({'workspace_id': workspace.id, 'label': workspace.name,
                        'captured_at': captured_at, 'changed': changed})
    return options


def _catalogue_uncached(db, actor, version=1, include_hidden=False, day=None, *, catalogue_id=None, people=None,
                        in_force_only=False, shared_body=False, capture: RulesCapture | None = None):
    """Every catalogue entry with its active editable rule and the version waiting for approval (with what it changes),
    each line's reach today and how many lines a more specific one outranks."""
    from packages.contracts.rule_catalogue import (FAMILIES, RULE_CATALOGUE, RULE_CATALOGUE_V2, CATALOGUE_MAPPING,
                                                 catalogue_counts, workload_fields)
    from packages.domain.rule_lines import attention, inherited, tier, line_validity
    from . import independence
    require_catalogue_read(actor, None if in_force_only else 'policies')
    rows = _rows(db, actor, with_versions=not in_force_only) if capture is None else capture.policies
    day = day or independence.today(db, actor.department_id)
    people = people or _in_force_inputs(db, actor, day, for_reach=not in_force_only)
    data, a, b = people
    if capture is not None:
        data.update(capture.inputs)
    data, in_force = _in_force(data, rows, a, b)
    if in_force_only:
        return {'catalogue_counts': {'in_force': len(in_force)}}
    active = [p for p in rows if p['status'] == 'active' and p['definition']['rule']['kind'] == 'catalogue'
              and not p['definition'].get('workspace_id')]
    registry = RULE_CATALOGUE_V2 if version == 2 else RULE_CATALOGUE
    entry_ids = {entry['id'] for entry in registry}
    by_entry, not_on_catalogue, projected = {}, [], []
    # Lossless legacy line projection is display-only; failed conversion never hides a binding policy.
    for policy in in_force:
        rule = policy['definition']['rule']
        try:
            entry_id, lines = ((rule['id'], rule.get('lines') or []) if rule['kind'] == 'catalogue'
                               else migration.lines_of(policy))
            display = policy if rule['kind'] == 'catalogue' else {**policy, 'definition': {
                **policy['definition'], 'rule': {'kind': 'catalogue', 'id': entry_id, 'lines': lines}}}
            projected.append(display)
            line_count = len(lines)
            if version == 2 and entry_id not in entry_ids:
                entry_id = CATALOGUE_MAPPING.get(entry_id)
        except (migration.Kept, ValueError, KeyError):
            entry_id, lines = None, policy['definition'].get('per_person') or []
            line_count = len(lines) + int(not policy['definition'].get('listed_only'))
        summary = {'id': policy['id'], 'name': policy['name'], 'kind': policy['kind'],
                   'lines': line_count,
                   'reach': 0, 'starts_on': policy.get('starts_on'), 'ends_on': policy.get('ends_on'),
                   'safety': s.safety_rule(policy['definition'])}
        if entry_id in entry_ids:
            by_entry.setdefault(entry_id, []).append(summary)
        else:
            not_on_catalogue.append(summary)
    authors = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(
        m.Identity.id.in_({p['created_by'] for p in rows if p['status'] == 'proposed' and p.get('created_by')}))).all())
    tagged = {p["id"]: p for p in _tagged(data, [*active, *projected])}
    would, reach = _reach(data, list(tagged.values()), a, b)
    live_definitions = _catalogue_definitions(rows) if capture is None else {}
    source_options = None if shared_body or capture is not None else _source_options(db, actor, live_definitions)
    entries = []
    for entry in registry:
        if catalogue_id is not None and entry['id'] != catalogue_id:
            continue
        rule = next((p for p in active if p["definition"]["rule"]["id"] == entry["id"]), None)
        waiting = next((p for p in rows if p["status"] == "proposed" and p["definition"]["rule"].get("id") == entry["id"]), None)
        overlaps = _line_overlaps(tagged[rule["id"]], would) if rule else {}
        lines = rule["definition"]["rule"]["lines"] if rule else []
        # What an approver sees on the waiting version: the same check its proposer saw (overlaps and warnings today).
        found = waiting and _check(db, actor, entry, {**waiting["definition"]["rule"],
            'balance_between_people': waiting['definition'].get('balance_between_people', True)}, people,
            policy_ends_on=waiting.get('ends_on'), stored=True)
        diff = waiting and _diff(rule and rule["definition"]["rule"], waiting["definition"]["rule"])
        if waiting and rule and rule['definition'].get('balance_between_people', True) != waiting['definition'].get('balance_between_people', True):
            diff['balance_between_people'] = waiting['definition'].get('balance_between_people', True)
        entries.append({**entry, "rule": rule,
                        'also_in_force': [row for row in by_entry.get(entry['id'], []) if not rule or row['id'] != rule['id']],
                        "proposed": waiting and {**waiting, "diff": diff,
                                                                         'created_by_name': authors.get(waiting.get('created_by')),
                                                                         "check": {k: found[k] for k in ("duplicates", "overlaps", "warnings", "issues", "counting_attention")},
                                                                         "line_specificity": found.get('line_specificity', {}),
                                                                         'relations': found.get('relations', {}),
                                                                         'line_replaces': found['line_replaces']},
                        "line_specificity": {line['id']: tier(line) for line in tagged[rule['id']]['definition']['rule']['lines']} if rule else {},
                        "line_validity": {line['id']: line_validity(rule, line, day) for line in lines} if rule else {},
                        "reach": {line["id"]: len(reach.get(line["id"], ())) for line in lines},
                        # The rule's reach: each person once, however many of its lines hold for them.
                        "people": len(set().union(*(reach.get(line["id"], ()) for line in lines))),
                        "line_overlaps": overlaps, "overlaps": _gives_way(overlaps),
                        'line_replaces': _line_replaces(tagged[rule['id']], would) if rule else {},
                        # Blank inherits: the numbers each line takes from a broader line (shown, never hidden).
                        "inherited": inherited(tagged[rule["id"]], would) if rule else {}})
        if entry['id'] == 'fair_share':
            from packages.domain.rule_lines import read_quality_for
            entries[-1]['findings'] = read_quality_for(data).get('_fair_share_goal_findings', [])
        if entry['id'] == 'team_leads' and capture is None:
            from .leadership import conversion_plan
            conversion = conversion_plan(db, actor, day)['legacy_lead_conversion'] if rule is None and waiting is None else None
            entries[-1]['legacy_leads'] = len(conversion['sources']) if conversion else 0
    primary_ids = {entry['rule']['id'] for entry in entries if entry['rule']}
    supplements = [row for group in by_entry.values() for row in group if row['id'] not in primary_ids] + not_on_catalogue
    if supplements:
        from packages.domain.rule_map import reach as policy_reach
        reached = policy_reach(data, in_force, a, b)
        for row in supplements:
            row['reach'] = reached.get(row['id'], 0)
    stored = {line["id"]: line for p in [*active, *projected] for line in p["definition"]["rule"].get("lines") or []}
    if version == 2:
        for entry in entries:
            if entry["rule"]:
                rule = entry["rule"]["definition"]["rule"]
                visible = [line for line in rule["lines"] if include_hidden or not line.get("ends_on") or line["ends_on"] > str(day)]
                entry["hidden_lines"] = len(rule["lines"]) - len(visible)
                entry["rule"] = {**entry["rule"], "definition": {**entry["rule"]["definition"], "rule": {
                    **rule, "lines": [{**line, **workload_fields(rule, line)} for line in visible]}}}
            # A proposal's lines read as the editor's do: the measure and period its settings stand for.
            if entry["proposed"]:
                rule = entry["proposed"]["definition"]["rule"]
                entry["proposed"] = {**entry["proposed"], "definition": {**entry["proposed"]["definition"], "rule": {
                    **rule, "lines": [{**line, **workload_fields(rule, line)} for line in rule.get("lines") or []]}}}
        if not include_hidden:
            entries = [entry for entry in entries if entry['also_in_force'] or entry["proposed"] or entry["rule"] and
                       entry["rule"]["definition"]["rule"].get('on', True) and entry["rule"]["definition"]["rule"]["lines"]]
    issues = attention(list(tagged.values()), would, day, draft_findings(db, actor) if capture is None else [])
    if capture is None:
        issues = _acknowledged(db, actor, issues, stored)
    safety = {row['id']: s.safety_rule(row['definition']) for row in [*rows, *in_force]}
    for issue in issues:
        issue['safety'] = safety.get(issue['rule_id'], False)
    pending = [issue for issue in issues if not issue.get('acknowledged')]
    source = {}
    if capture is not None:
        # Stored rows retain ended and folded lines. Effective rows also include independently approved facts.
        other_rules = {row['id']: row for row in in_force}
        other_rules.update({row['id']: row for row in capture.policies if row['status'] == 'active'})
        source = {'source': {'kind': 'snapshot', 'workspace_id': capture.workspace_id,
                             'baseline_id': capture.baseline_id, 'captured_at': capture.captured_at},
                  'captured_other_rules': [_captured_rule_details(row) for ident, row in other_rules.items()
                                           if ident not in primary_ids]}
    for entry in entries:
        entry['numbers'] = _display_safety_slots(entry['numbers'])
        for key in ('rule', 'proposed'):
            if entry[key]:
                entry[key] = _display_safety_levels(entry[key], captured=capture is not None)
    return {"catalogue_version": version, "catalogue_counts": {**catalogue_counts(), 'in_force': len(in_force),
                'lines': sum(row['lines'] for group in by_entry.values() for row in group)
                         + sum(row['lines'] for row in not_on_catalogue)},
            "authorable_entries": [{**entry, 'numbers': _display_safety_slots(entry['numbers'])}
                                   for entry in RULE_CATALOGUE_V2],
            "families": [{"id": f, "name": n} for f, n in FAMILIES], "entries": entries,
            'not_on_catalogue': not_on_catalogue if catalogue_id is None else [], "day": str(day),
            "attention": issues, "attention_counts": attention_counts(pending),
            **(source if capture is not None else
               {'_live_definitions': live_definitions} if shared_body else {'source_options': source_options})}


def _captured_rule_details(row):
    definition = row['definition']
    kind = definition['rule']['kind']
    details = {key: row.get(key) for key in ('id', 'name', 'starts_on', 'ends_on')}
    if kind == 'catalogue':
        from packages.contracts.rule_catalogue import workload_fields
        definition = _display_safety_levels(row, captured=True)['definition']
        rule = definition['rule']
        definition = {**definition, 'rule': {**rule, 'lines': [
            {**line, **workload_fields(rule, line)} for line in rule['lines']]}}
    else:
        stored = definition.get('severity', 'mandatory')
        level = effective_severity(kind, stored)
        details['effective_severity'] = level
        if level != stored:
            details['severity_reason'] = s.SAFETY_MANDATORY_REASON
    return {**details, 'definition': stored_definition(definition)}


def _safety_reason(slot):
    return (s.SAFETY_MANDATORY_REASON
            if effective_severity(slot['kind'], None, advisory_slot=slot['advisory']) == 'mandatory' else None)


def _display_safety_slots(numbers):
    return {key: [{**slot, 'mandatory_reason': reason} if (reason := _safety_reason(slot)) else slot
                  for slot in slots] for key, slots in numbers.items()}


def _display_safety_levels(policy, *, captured=False):
    from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number
    from packages.domain.rule_lines import has_value

    definition = policy['definition']
    rule = definition['rule']
    entry = catalogue_entry(rule['id'], rule.get('catalogue_version', 1))
    lines = []
    for line in rule.get('lines') or []:
        numbers = line.get('numbers') or {}
        if captured:
            default_level = s.LineNumber.model_fields['level'].default
            numbers = {key: {'level': default_level, **number} for key, number in numbers.items()}
            line = {**line, 'numbers': numbers}
        reasons = {}
        for key, number in numbers.items():
            if number.get('level') == 'mandatory' or not has_value(line, key):
                continue
            slot = slot_for_number(entry, key, rule, line, numbers)
            if reason := _safety_reason(slot):
                reasons[key] = reason
        lines.append({**line, 'mandatory_reasons': reasons} if reasons else line)
    return {**policy, 'definition': {**definition, 'rule': {**rule, 'lines': lines}}}


def _catalogue_stamp(db, actor):
    """Inputs that invalidate the read projection across all API processes."""
    department, organisation = db.execute(select(m.Department, m.Organisation).join(
        m.Organisation, m.Organisation.id == m.Department.organisation_id).where(
            m.Department.id == actor.department_id)).one()
    # Sums catch a lower-revision row changing when another row already has the
    # maximum revision, without materialising every rota or policy into the key.
    workspaces = db.execute(select(
        func.count(m.Workspace.id), func.coalesce(func.sum(m.Workspace.revision), 0),
        func.coalesce(func.sum(m.Workspace.content_revision), 0),
        func.max(m.Workspace.revision), func.max(m.Workspace.content_revision),
        func.max(m.Workspace.created_at)).where(
            m.Workspace.department_id == actor.department_id,
            m.Workspace.archived_at.is_(None))).one()
    policies = db.execute(select(func.count(m.Policy.id),
        func.coalesce(func.sum(m.Policy.revision), 0), func.max(m.Policy.revision), func.max(m.Policy.created_at)).where(
            m.Policy.department_id == actor.department_id)).one()
    acknowledgements = db.execute(select(func.count(m.RuleAcknowledgement.id),
        func.coalesce(func.sum(m.RuleAcknowledgement.revision), 0),
        func.max(m.RuleAcknowledgement.revision), func.max(m.RuleAcknowledgement.created_at)).where(
            m.RuleAcknowledgement.department_id == actor.department_id)).one()
    # SQLAlchemy returns aggregate rows as Row objects; convert the workspace
    # row to primitives before using the stamp in the canonical digest.
    return ('rules-in-force-v5', department.revision, department.catalogue_revision, organisation.revision,
            organisation.input_revision, organisation.availability_revision, tuple(workspaces), tuple(policies),
            tuple(acknowledgements))


def catalogue_stamp(db, actor):
    """Read the bounded invalidation stamp without constructing the catalogue body."""
    require_catalogue_read(actor, 'policies')
    return _catalogue_stamp(db, actor)


def catalogue_etag(actor, version, include_hidden, day, stamp):
    """Stable validator for the revision-keyed catalogue projection."""
    value = ["rules-catalogue", actor.department_id, actor.identity_id, actor.person_id,
             sorted(actor.roles), can_read_rules(actor), version, include_hidden, str(day), stamp]
    return '"' + hashlib.sha256(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32] + '"'


def _catalogue_overlay(db, actor, body):
    """Only the source picker is personal; approval grants/actions are not catalogue fields."""
    if '_live_definitions' not in body:  # Count-only projections have no source picker.
        return body
    return {**{key: value for key, value in body.items() if key != '_live_definitions'},
            'source_options': _source_options(db, actor, body['_live_definitions'])}


def catalogue(db, actor, version=1, include_hidden=False, day=None, *, catalogue_id=None, people=None, _stamp=None,
              build_wait_seconds=None, in_force_only=False, workspace_id=None):
    """Return the catalogue from a revision-keyed disposable projection when possible.

    ``people`` is an explicit detail-page context and is intentionally never cached. Every other input is covered
    by the stamp, which is read from the database on each request and therefore remains safe across API processes.
    """
    require_catalogue_read(actor, None if in_force_only else 'policies')
    if workspace_id is not None:
        capture = rules_source(db, actor, workspace_id)
        with _captured_projection():
            return _catalogue_uncached(db, actor, version, include_hidden, day, capture=capture)
    if (people is not None
            or db is not None and (db.connection().info.get('rota_write') or db.new or db.dirty)):
        return _catalogue_uncached(db, actor, version, include_hidden, day, catalogue_id=catalogue_id, people=people,
                                   in_force_only=in_force_only)
    build_wait_seconds = (Settings().catalogue_build_wait_seconds
                          if build_wait_seconds is None else build_wait_seconds)
    day = day or independence.today(db, actor.department_id)
    # The public ETag includes this reader's private draft revisions. The shared body does not.
    with shared_scope(db, actor):
        # The counts dependency stamp has no workspace or reader terms, so it is already shared-scope.
        stamp = (_catalogue_stamp(db, actor) if db is not None and actor.permits('coordinator') and not in_force_only
                 else _stamp if _stamp is not None else _catalogue_stamp(db, actor))
    from .read_projections import cached_department, department_audience
    audience_values = ('catalogue-shared-v1', version, include_hidden, str(day), catalogue_id, in_force_only)
    audience = department_audience(db, actor, audience_values, shared_body=True)
    key = (actor.department_id, version, include_hidden, str(day), catalogue_id, in_force_only, stamp, audience)
    with _catalogue_cache_lock:
        cached = _catalogue_cache.get(key)
        if cached is not None:
            _catalogue_cache.move_to_end(key)
            value = copy.deepcopy(cached)
        else:
            building = _catalogue_building.get(key)
            if building is None:
                building = Event()
                _catalogue_building[key] = building
                owner = True
            else:
                owner = False
    if cached is not None:
        return _catalogue_overlay(db, actor, value)
    if not owner:
        # A catalogue build is display-only.  Do not occupy a request worker for
        # an unbounded proof/reach calculation, and never serve an old rules
        # projection as current safety evidence.  The caller retries after this
        # bounded response; writes still validate the current revision.
        retry_after = max(1, int(build_wait_seconds))
        if not building.wait(build_wait_seconds):
            raise Problem(503, "RULE_CATALOGUE_BUSY",
                          "The rule catalogue is still updating; try again",
                          {"retry_after": retry_after})
        with _catalogue_cache_lock:
            cached = _catalogue_cache.get(key)
            if cached is not None:
                _catalogue_cache.move_to_end(key)
                value = copy.deepcopy(cached)
        if cached is not None:
            return _catalogue_overlay(db, actor, value)
        raise Problem(503, "RULE_CATALOGUE_BUSY",
                      "The rule catalogue could not be refreshed; try again",
                      {"retry_after": retry_after})
    try:
        def build():
            with shared_scope(db, actor):
                return _catalogue_uncached(db, actor, version, include_hidden, day, catalogue_id=catalogue_id,
                                          in_force_only=in_force_only, shared_body=True)

        # Durable hits and cold builds use the same existing single-flight guard.
        # This also leaves a bounded fallback for departments without a workspace FK anchor.
        with shared_scope(db, actor):
            value = (cached_department(db, actor, 'rule_catalogue', digest(stamp), build,
                                      audience_values=audience_values, audience=audience)
                     if db is not None else build())
        with _catalogue_cache_lock:
            _catalogue_cache[key] = value
            _catalogue_cache.move_to_end(key)
            while len(_catalogue_cache) > Settings().catalogue_cache_entries:
                _catalogue_cache.popitem(last=False)
        return _catalogue_overlay(db, actor, copy.deepcopy(value))
    finally:
        if owner:
            with _catalogue_cache_lock:
                _catalogue_building.pop(key, None)
                building.set()


def attention_counts(issues):
    return {'rules': len({issue['rule_id'] for issue in issues}),
            'lines': len({(issue['rule_id'], line) for issue in issues for line in issue['line_ids']})}


def detail(db, actor, catalogue_id, version=1, day=None, policy_id=None, line_id=None,
           workspace_id=None, starts_on=None, ends_on=None):
    """A deep link's saved entry, editor check and reached line in one department-scoped read."""
    from packages.contracts.rule_catalogue import catalogue_entry
    from .policies import people_data, policy_reach
    require_catalogue_read(actor, 'policies')
    require(catalogue_entry(catalogue_id, version) is not None, 'NOT_FOUND', 'Not a rule in the catalogue', 404)
    day = day or independence.today(db, actor.department_id)
    people = people_data(db, actor, day)
    [entry] = catalogue(db, actor, version, include_hidden=True, day=day, catalogue_id=catalogue_id, people=people)['entries']
    data, a, b = people
    _, policies = _in_force(data, _rows(db, actor), a, b)
    in_force_ids = {policy['id'] for policy in policies}
    choices = [policy for policy in (entry['rule'], entry['proposed']) if policy is not None]
    if policy_id in {row['id'] for row in entry['also_in_force']}:
        choices += [policy for policy in policies if policy['id'] == policy_id]
    selected = next((policy for policy in choices if policy_id is None or policy['id'] == policy_id), None)
    require(policy_id is None or selected is not None, 'NOT_FOUND', 'Rule not found', 404)
    require(line_id is None or selected is not None and any(line['id'] == line_id for line in selected['definition']['rule'].get('lines') or []),
            'NOT_FOUND', 'Rule line not found', 404)
    found = None
    if selected is not None and selected['definition']['rule']['kind'] == 'catalogue' and actor.permits('coordinator', 'approver'):
        rule = selected['definition']['rule']
        lines = [{k: v for k, v in line.items() if k != 'mandatory_reasons'} for line in rule.get('lines') or []]
        found = _check(db, actor, entry, {**rule, 'lines': lines,
            'balance_between_people': selected['definition'].get('balance_between_people', True)}, people=people,
            policy_ends_on=selected.get('ends_on'), stored=True)
    reached = policy_reach(db, actor, day, workspace_id, starts_on, ends_on, True, selected['id'], line_id,
                          people=people if starts_on is None else None) if selected is not None and (
                              workspace_id is not None or selected['id'] in in_force_ids or selected['status'] == 'proposed') else None
    if reached is not None and selected['id'] not in reached['reach']:
        reached = None
    return {'entry': entry, 'check': found, 'reach': reached, 'day': str(day)}


def _history_keys():
    """Literalize only fixed key expressions so SQLite can match the migrated expression index."""
    rule = m.Policy.definition["rule"]
    return tuple(replacement_traverse(key, {}, lambda part: part.render_literal_execute()
        if isinstance(part, BindParameter) else None) for key in (
            rule["id"].as_string(), func.coalesce(rule["catalogue_version"].as_integer(), 1)))


def entry_history(db, actor, catalogue_id, version=1, page=1, limit=50):
    """Page canonical catalogue versions in SQL, retaining the existing provenance/read-only projection."""
    from packages.contracts.rule_catalogue import catalogue_entry
    from .policies import policy_versions
    from .scoring_defaults import seeded_rows
    require_permission(actor, 'rules.read')
    require(catalogue_entry(catalogue_id, version) is not None, "NOT_FOUND", "Not a rule in the catalogue", 404)
    P = m.Policy
    entry_key, version_key = _history_keys()
    rows = list(db.scalars(select(P).where(P.department_id == actor.department_id, P.kind == "catalogue",
        entry_key == catalogue_id, version_key == version)
        .order_by(P.created_at.desc(), P.seq.desc()).offset((page - 1) * limit).limit(limit + 1)))
    return {"policies": policy_versions(db, actor, seeded_rows(db, [record(row) for row in rows[:limit]])),
            "page": page, "limit": limit, "has_more": len(rows) > limit}


def _acknowledged(db, actor, issues, stored):
    """Each issue with its fingerprint and, when someone in the department acknowledged it as it stands, who, when and
    their note (the same for everyone who opens Rules). A changed issue has a new fingerprint: its acknowledgement
    no longer matches, so it needs attention again."""
    from packages.domain.rule_lines import fingerprint
    for x in issues:
        x["fingerprint"] = fingerprint(x, stored)
    rows = _acknowledgements(db, actor, [x["fingerprint"] for x in issues])
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(
        m.Identity.id.in_({a.acknowledged_by for a in rows.values()} or {""}))).tuples().all())
    for x in issues:
        a = rows.get(x["fingerprint"])
        x["acknowledged"] = a and {"by": names.get(a.acknowledged_by, ""), "by_id": a.acknowledged_by, "at": a.created_at.isoformat(), "note": a.note}
    return issues


def _acknowledgements(db, actor, fingerprints):
    return {a.fingerprint: a for a in db.scalars(select(m.RuleAcknowledgement).where(
        m.RuleAcknowledgement.department_id == actor.department_id, m.RuleAcknowledgement.fingerprint.in_(list(fingerprints) or [""])))}


def _acknowledge(db, actor, fingerprints, note):
    """Acknowledge these fingerprints for the department; one already acknowledged keeps who and when. How many are new."""
    have = _acknowledgements(db, actor, fingerprints)
    new = [f for f in dict.fromkeys(fingerprints) if f not in have]
    from .common import history
    for f in new:
        row = m.RuleAcknowledgement(department_id=actor.department_id, fingerprint=f, acknowledged_by=actor.identity_id, note=note)
        db.add(row)
        history(db, actor, "rule_acknowledgement", row, note or "Acknowledged")
    return len(new)


def acknowledge(db, actor, command, on):
    """rule.acknowledge: issues of Rules › Needs attention as they stand now (refused when one changed since the page
    was read: 409) are acknowledged for everyone. rule.unacknowledge takes acknowledgements back (its Undo, and the
    Acknowledged view). Both are audited by the command bus."""
    require_role(actor, "coordinator", "admin")
    value = s.RuleAcknowledgementInput.model_validate(command.payload)
    if not on:  # who acknowledged it and when stay in definition_history
        from .common import history
        rows = _acknowledgements(db, actor, value.fingerprints)
        for a in rows.values():
            a.revision += 1
            history(db, actor, "rule_acknowledgement", a, "Taken back")
            db.delete(a)
        return {"unacknowledged": len(rows)}
    current = {x["fingerprint"] for x in catalogue(db, actor)["attention"]}  # shortcut: the whole page, rare command
    changed = [f for f in value.fingerprints if f not in current]
    require(not changed, "ISSUE_CHANGED", "This issue changed or is gone since the page was read; reload and look again", 409, changed)
    return {"acknowledged": _acknowledge(db, actor, value.fingerprints, value.note)}


def draft_findings(db, actor):
    """Every finding of the last check of each rota being planned (not discarded, not published, not over yet), with
    its rota (id and name)."""
    from . import independence
    from .schedule import LIVE
    today = independence.today(db, actor.department_id)
    rotas = db.scalars(select(m.Workspace).where(shared_clause(), m.Workspace.department_id == actor.department_id, m.Workspace.archived_at.is_(None),
                                                 m.Workspace.status.in_(LIVE), m.Workspace.ends_on > today).order_by(m.Workspace.starts_on))
    return [{**f, "workspace_id": w.id, "workspace": w.name} for w in rotas for f in (w.validation or {}).get("findings") or [] if f.get("rule_id")]


def _numbers_text(line):
    return ", ".join(f"{k} {n['value']:g}" if n.get("value") is not None else k for k, n in (line.get("numbers") or {}).items()) or "no number"


# An empty list or table is said in plain words, never as the validator's text ("List should have at least 1 item").
_EMPTY_WORDS = {'weekdays': 'Choose at least one day', 'numbers': 'Give the line a number',
                'conditions': 'Add a condition or remove the empty group'}


def _check_issue(error):
    """One validation error of a checked rule, as an issue the line editor shows."""
    message = error['msg'].removeprefix('Value error, ')
    if error['type'] == 'too_short':
        message = next((_EMPTY_WORDS[x] for x in reversed(error['loc']) if x in _EMPTY_WORDS), 'Choose at least one value')
    if error['type'] == 'invalid_number_range':
        return {'field': 'definition.rule.' + '.'.join(map(str, error['loc']))[len('rule.catalogue.'):], 'message': message}
    return {'field': '.'.join(map(str, error['loc'])), 'code': 'INVALID_RULE_DEFINITION', 'message': message}


def check(db, actor, catalogue_id, body):
    """What a catalogue rule with these lines would do, before anything is saved: duplicates (the same line twice on
    overlapping dates and days), overlaps (a more specific line outranking another, for how many people today, and what
    changes), warnings (a narrower safety line weaker than a broader one: the broader still binds; lines of equal tier
    that both apply to people today) and issues (each line through the old schemas and the solver). Pure."""
    from packages.contracts.rule_catalogue import catalogue_entry
    require_role(actor, "admin", "coordinator", "approver")
    entry = catalogue_entry(catalogue_id, body.get("catalogue_version", 1))
    require(entry is not None, "NOT_FOUND", "Not a rule in the catalogue", 404)
    if catalogue_id == 'team_leads' and body.get('legacy_effective_on'):
        from .leadership import conversion_plan
        preview = conversion_plan(db, actor, s.LegacyLeadCheckInput.model_validate(
            {'legacy_effective_on': body['legacy_effective_on']}).legacy_effective_on)
        found = _check(db, actor, entry, {**body, 'lines': preview['lines']}, reach_preview=True)
        return {**found, **preview}
    result = _check(db, actor, entry, body, reach_preview=True)
    if body.get('include_affected_drafts') and not result['issues'] and not result['duplicates']:
        from .rule_effects import affected_workspaces
        current = catalogue_version(db, actor, catalogue_id, 'active')
        candidate = m.Policy(id=m.uid(), department_id=actor.department_id, kind='catalogue', status='proposed', active=True,
            starts_on=current.starts_on if current else independence.today(db, actor.department_id),
            ends_on=current.ends_on if current else None, definition={
                **(current.definition if current else {}),
                'balance_between_people': body.get('balance_between_people', True),
                'merged_ids': [current.id] if current else [],
                'rule': {'kind': 'catalogue', 'id': catalogue_id, 'catalogue_version': body.get('catalogue_version', 1),
                         'on': body.get('on', True), 'settings': body.get('settings') or {}, 'lines': result['lines']}})
        result['affected_drafts'] = [{'workspace_id': w.id, 'name': w.name, 'revision': w.revision,
            'starts_on': str(w.starts_on), 'ends_on': str(w.ends_on)} for w in affected_workspaces(db, actor, [candidate])]
    return result


def _check(db, actor, entry, body, people=None, reach_preview=False, *, policy_ends_on=None, stored=False):
    from pydantic import ValidationError
    from packages.domain.rule_lines import _overlap, contradictions, counting_attention, inherited, measure, outranking, outranking_by_number, tier, when
    from packages.domain.solver_rules import definition_issues
    from . import independence
    from .policies import people_data
    catalogue_id = entry["id"]
    from packages.domain.rule_lines import default_line_strengths, line_relations
    # Only new omitted levels acquire defaults; sparse historical lines keep their stored meaning.
    current = catalogue_version(db, actor, catalogue_id, 'active')
    previous = {line['id']: line for line in current.definition['rule'].get('lines') or []} if current else {}
    previous_ids = previous.keys()
    given = _kept_safety_numbers(entry, body, body.get("lines") or [], previous)
    lines = with_blocks(catalogue_id, body.get("settings"), given, independence.today(db, actor.department_id), entry.get("catalogue_version", 1))
    dated = {line["id"]: [line["starts_on"], line["ends_on"]] for line, was in zip(lines, given) if isinstance(line, dict) and isinstance(was, dict)
             and (line.get("starts_on"), line.get("ends_on")) != (was.get("starts_on"), was.get("ends_on"))}
    try:
        value = s.PolicyDefinition.model_validate({'balance_between_people': body.get('balance_between_people', True),
                                                 "rule": {"kind": "catalogue", "id": catalogue_id, "catalogue_version": entry.get("catalogue_version", 1), "on": body.get("on", True),
                                                            "settings": body.get("settings") or {}, "lines": lines}})
    except ValidationError as error:
        if not stored and any(e['type'] == s.BALANCE_SHARE_OFFERED_METRIC for e in error.errors()):
            raise
        return {"duplicates": [], "overlaps": [], "line_overlaps": {}, "line_replaces": {}, "warnings": [], "counting_attention": [], "dated": dated, "inherited": {},
                "issues": [_check_issue(e) for e in error.errors(include_input=False, include_url=False)]}
    definition = value.model_dump(mode="json")
    lines = definition["rule"]["lines"]
    counting = counting_attention({'definition': definition, 'ends_on': policy_ends_on})
    from packages.domain.rule_lines import duplicates as same_lines
    issues = definition_issues(value)
    duplicate_pairs = same_lines(definition['rule']) if not issues else []
    duplicates = [{"lines": [k, n], "line_ids": [lines[k]["id"], lines[n]["id"]],
                   "message": f"Lines {k + 1} and {n + 1} are the same line (what they count, who, where, days and dates overlap)"}
                  for k, n in duplicate_pairs]
    overlaps, warnings, line_overlaps, inherited_now, line_replaces = [], [], {}, {}, {}
    details = {'lines': []}
    if not issues and not duplicates:
        row = {"id": "00000000-0000-4000-9000-000000000000", "name": entry["name"], "kind": "catalogue", "status": "active", "active": True,
               "revision": 1, "department_id": actor.department_id, "starts_on": "2000-01-01", "ends_on": None, "definition": definition}
        spans = [when(definition['rule'], line) for line in lines]
        bounds = [bound for span in spans for bound in span if bound]
        firsts = [start for start, _ in spans if start]
        lasts = [end for _, end in spans if end]
        first = min(bounds) if bounds else independence.today(db, actor.department_id).isoformat()
        last = max(lasts) if lasts else (date.fromisoformat(str(first)) + timedelta(days=1)).isoformat()
        if first >= last and bounds:
            first, last = min(bounds), max(bounds)
        if not firsts and str(last) <= str(first):
            first = (date.fromisoformat(str(last)) - timedelta(days=1)).isoformat()
        from .policies import people_period
        data, a, b = people_period(db, actor, date.fromisoformat(str(first)), date.fromisoformat(str(last)))
        # An isolated unsaved-rule check does not resolve unrelated personal goal references.
        data = {k: v for k, v in data.items() if k not in ('scheduling_facts', 'source_bindings', '_catalogue_v2_materialized')}
        from packages.domain.conditions import check_definition_count_budget
        try:
            check_definition_count_budget(data, definition)
        except ValueError as error:
            return {"duplicates": [], "overlaps": [], "line_overlaps": {}, "line_replaces": {}, "warnings": [], "counting_attention": counting, "dated": dated, "inherited": {},
                    "issues": [{"field": "rule.lines", "code": "INVALID_RULE_DEFINITION", "message": str(error)}]}
        [row] = _tagged(data, [row])  # a #tag who is a group, as every check reads it
        would, _ = _reach(data, [row], a, b, include_held=False)
        defaults_changed = False
        settings_available = definition['rule']['settings'] or any(line['settings'] for line in lines)
        levels = {number['level'] for line in lines for number in line['numbers'].values()}
        if settings_available or len(levels) > 1:
            authored = default_line_strengths(definition['rule'], given, previous_ids, would)
            for line, original, defaulted in zip(lines, given, authored):
                if line['id'] not in previous_ids:
                    settings = defaulted.get('settings')
                    if not original.get('settings') and settings and line['settings'] != settings:
                        line['settings'] = settings
                        defaults_changed = True
                    for key, number in line['numbers'].items():
                        level = defaulted['numbers'][key].get('level', number['level'])
                        if original['numbers'][key].get('level') is None and number['level'] != level:
                            number['level'] = level
                            defaults_changed = True
        if defaults_changed:
            # Inherited settings can change the counting body and expose duplicates or invalid combinations.
            try:
                value = s.PolicyDefinition.model_validate(definition)
            except ValidationError as error:
                if not stored and any(e['type'] == s.BALANCE_SHARE_OFFERED_METRIC for e in error.errors()):
                    raise
                issues = [_check_issue(e) for e in error.errors(include_input=False, include_url=False)]
            else:
                definition = value.model_dump(mode='json')
                lines = definition['rule']['lines']
                issues = definition_issues(value)
                try:
                    check_definition_count_budget(data, definition)
                except ValueError as error:
                    issues.append({'field': 'rule.lines', 'code': 'INVALID_RULE_DEFINITION', 'message': str(error)})
            counting = counting_attention({'definition': definition, 'ends_on': policy_ends_on})
            duplicate_pairs = same_lines(definition['rule']) if not issues else []
            duplicates = [{'lines': [k, n], 'line_ids': [lines[k]['id'], lines[n]['id']],
                           'message': f'Lines {k + 1} and {n + 1} are the same line (what they count, who, where, days and dates overlap)'}
                          for k, n in duplicate_pairs]
            if issues or duplicates:
                return {'duplicates': duplicates, 'overlaps': [], 'line_overlaps': {}, 'line_replaces': {}, 'warnings': [],
                        'counting_attention': counting, 'dated': dated, 'inherited': {}, 'issues': issues,
                        'lines': lines, 'relations': {}, 'line_specificity': {}}
            row = {**row, 'definition': definition}
            [row] = _tagged(data, [row])
        if reach_preview:
            from packages.domain.rule_map import reach
            details = reach(data, [row], a, b, details=True)['details'].get(row['id'], {'lines': []})
        rule, tagged = row["definition"]["rule"], row["definition"]["rule"]["lines"]
        if defaults_changed:
            would, _ = _reach(data, [row], a, b, include_held=False)
        from packages.contracts.rule_catalogue import slot_for_number, staffing_pool_message
        for n, line in enumerate(lines):
            minimum = line['numbers'].get('min') or {}
            if minimum.get('value') is None or minimum.get('level') == 'off':
                continue
            slot = slot_for_number(entry, 'min', rule, line, line['numbers'])
            if slot['kind'] not in {'minimum_cover', 'team_lead_requirement'}:
                continue
            # Union over the line's effective dates is a cheap, conservative upper bound.
            pool = len(would.get(line['id'], ()))
            if minimum['value'] > pool:
                warnings.append({'kind': 'pool_shortfall', 'line': n, 'line_id': line['id'],
                    'required': minimum['value'], 'eligible_people': pool,
                    'message': staffing_pool_message(entry['name'], minimum['value'], pool)})
        by_number = outranking_by_number(row)
        outranked, both = outranking(row, by_number=by_number), _both(row, would, same=duplicate_pairs if rule == definition['rule'] else None)
        line_overlaps = _line_overlaps(row, would, outranked=outranked, both=both)
        line_replaces = _line_replaces(row, would, by_number=by_number)
        replaced = {}
        for line_index, k in by_number:
            replaced.setdefault(line_index, set()).add(k)
        inherited_now = inherited(row, would)
        for n, wins in outranked.items():
            loser = lines[n]
            # Who has at least one of its numbers replaced, and which (a blank number of the winner stays this line's).
            gone = would.get(loser["id"], set()) & {p for w in wins for p in would.get(lines[w]["id"], ())}
            numbers = sorted(replaced[n])
            overlaps.append({"line": n, "line_id": loser["id"], "winners": wins, "winner_ids": [lines[w]["id"] for w in wins], "people": len(gone),
                             "numbers": numbers, "sessions": None,  # shortcut: sessions per overlap when the overlap popover asks for them
                             "message": f"Line {n + 1} ({_numbers_text(loser)}) gives way to " + ", ".join(
                                 f"line {w + 1} ({_numbers_text(lines[w])})" for w in wins) + f" on its {', '.join(numbers)}"
                                 f" for {len(gone)} {'person' if len(gone) == 1 else 'people'} today"})
        # A blank number taken from a broader line that contradicts the line's own (as Needs attention reads it).
        for n, bad, got in (contradictions(row, would) if inherited_now else []):
            source = next(i for i, x in enumerate(lines) if x["id"] == got[bad[0]]["from"])
            warnings.append({"kind": "inherits", "line": n, "line_id": lines[n]["id"], "other": source, "other_id": lines[source]["id"],
                             "numbers": bad, "inherited": {k: got[k]["value"] for k in bad},
                             "message": f"Line {n + 1} inherits " + " and ".join(f"{k} {got[k]['value']:g}" for k in bad)
                                        + f" from line {source + 1}, which contradicts its own numbers; give it None"})
        for k, n, count in both:
            warnings.append({"kind": "both", "line": n, "line_id": lines[n]["id"], "other": k, "other_id": lines[k]["id"], "people": count,
                             "message": f"Lines {k + 1} and {n + 1} both apply to {count} {'person' if count == 1 else 'people'} today: "
                                        "neither is more specific, so each of their numbers holds"})
        if entry["safety"]:
            # Where, days, hours and what a line counts; when it counts; how specific its who is.
            measures = [measure(rule, line) for line in tagged]
            keys = [(s.line_key(catalogue_id, {**line, "who": None}), json.dumps(m, sort_keys=True, default=str)) for line, m in zip(tagged, measures)]
            safety_kinds = [{key: slot_for_number(entry, key, rule, line, line['numbers'])['kind']
                             for key in line['numbers'] if key in ('min', 'max')} for line in tagged]
            dates, tiers, same = [when(rule, line) for line in tagged], [tier(line) for line in tagged], {}
            for n, key in enumerate(keys):
                same.setdefault(key, []).append(n)
            for n, line in enumerate(lines):
                for k in same[keys[n]]:
                    other = lines[k]
                    if (tiers[n] <= tiers[k] or not _overlap(dates[n], dates[k])
                            or not (would.get(line['id'], set()) & would.get(other['id'], set()))):
                        continue
                    # A narrower minimum cover counts a different pool, so it is a separate requirement.
                    weaker = []
                    for key, kind in safety_kinds[n].items():
                        if kind not in s.NEVER_OVERRIDDEN or kind != safety_kinds[k].get(key):
                            continue
                        value, broader = line['numbers'][key]['value'], other['numbers'][key]['value']
                        if value is None or broader is None:
                            continue
                        if (key == 'min' and kind != 'minimum_cover' and value < broader
                                or key == 'max' and value > broader):
                            weaker.append(key)
                    if weaker:
                        warnings.append({"kind": "weaker", "line": n, "line_id": line["id"], "broader": k, "other": k, "other_id": other["id"],
                                         "message": f"Line {n + 1} is narrower but weaker than line {k + 1} ({', '.join(weaker)}): "
                                                    f"safety lines all apply, so line {k + 1} still binds"})
    return {"duplicates": duplicates, "overlaps": overlaps, "line_overlaps": line_overlaps, "line_replaces": line_replaces, "warnings": warnings, "counting_attention": counting, "issues": issues, "dated": dated,
            'relations': line_relations(row, would) if not issues and not duplicates else {},
            'lines': lines,
            "inherited": inherited_now, **({'reach_details': details} if reach_preview else {}),
            "line_specificity": {line['id']: tier(line) for line in tagged} if not issues and not duplicates else {}}


def propose(db, actor, command):
    """A new version of a catalogue rule (or its first), waiting for an independent approver; activating it retires the
    version before, retiring it brings that back. Refused: a second waiting proposal (409), a stale version (409), the
    same line twice (422). Ordered overrides need no acknowledgement."""
    from packages.contracts.rule_catalogue import catalogue_entry
    from packages.domain.rule_lines import expand
    from . import independence
    from .common import history, omitted_update_fields, scoped
    from .policies import condition_reference_rows, condition_references, policy_references
    from .scoring_defaults import seeded_rows, seed_line_ids
    require_role(actor, "coordinator", "admin")
    value = s.RuleProposalInput.model_validate(command.payload)
    if value.catalogue_id == 'team_leads':
        db.scalar(select(m.Department).where(m.Department.id == actor.department_id).with_for_update(key_share=True))
    current = catalogue_version(db, actor, value.catalogue_id, "active")
    current_definition = seeded_rows(db, [record(current)])[0]['definition'] if current else {}
    current_rule = current_definition.get('rule', {})
    baseline = value.expected_line_ids if value.expected_line_ids is not None else [line.id for line in value.lines]
    inherited = omitted_update_fields(value, {
        **current_rule, 'balance_between_people': current_definition.get('balance_between_people', True),
    }) if current else {}
    version = inherited.get('catalogue_version', value.catalogue_version)
    if version == 2 and value.catalogue_id != 'team_leads' and department_version(db, actor.department_id) != 2:
        # A stored v2 default can coexist with legacy rules. Keep the legacy entry for callers omitting the version.
        require('catalogue_version' in inherited and catalogue_entry(value.catalogue_id, value.catalogue_version) is not None,
                'DEPARTMENT_CONVERSION_REQUIRED', 'Convert the department through one reviewed proposal before authoring v2 rules', 409)
        inherited.pop('catalogue_version')
    if inherited:
        value = s.RuleProposalInput.model_validate({**inherited, **command.payload})
    require(waiting_proposal(db, actor, value.catalogue_id) is None, "PROPOSAL_WAITING",
            "A change to this rule is already waiting for approval; approve or decline it first", 409)
    from .leadership import proposal_conversion
    conversion = proposal_conversion(db, actor, value, current)
    converted = db.scalar(select(m.CatalogueConversion.id).where(m.CatalogueConversion.department_id == actor.department_id,
        m.CatalogueConversion.status == 'active'))
    require(not converted or value.catalogue_version == 2, 'USE_CATALOGUE_V2', 'Historical catalogue entries are read-only in a converted department', 409)
    require(((current.id, current.revision) if current else (None, None))
            == (value.expected_policy_id, value.expected_revision), "STALE_REVISION",
            "This rule changed since you opened it; reload and try again", 409)
    seeds = seed_line_ids(db, [current.id] if current else [])
    require(seeds <= set(baseline), 'STALE_REVISION',
            'This rule changed since you opened it; reload and try again', 409)
    previous = {line["id"]: line for line in (current_rule.get("lines") or [])}
    stored = {ident: line.get("source_bindings") or {} for ident, line in previous.items()}
    require(all((line.model_dump(mode="json").get("source_bindings") or {}) == stored.get(line.id, {}) for line in value.lines),
            "SOURCE_BINDING_IMMUTABLE", "Only a reviewed department conversion may create or change source bindings", 409)
    require(all(not stored.get(line.id) or set(line.numbers) <= set(stored[line.id]) for line in value.lines),
            'SOURCE_NUMBER_IMMUTABLE', 'Add a new obligation on its own line; a converted source keeps its numeric slots', 409)
    defaults = {ident: line.get('source_default') for ident, line in previous.items()}
    require(all(line.model_dump(mode='json').get('source_default') == defaults.get(line.id) for line in value.lines),
            'SOURCE_BINDING_IMMUTABLE', 'Preserved source defaults cannot be changed through the line editor', 409)
    authored = [line.model_dump(mode='json') for line in value.lines]
    for authored_line, parsed in zip(authored, value.lines):
        if parsed.id not in previous:
            for key, number in parsed.numbers.items():
                if 'level' not in number.model_fields_set:
                    authored_line['numbers'][key].pop('level', None)
    edited = {line.id: line for line in value.lines}
    require(all(not default or ident in edited for ident, default in defaults.items()),
            'SOURCE_BINDING_IMMUTABLE', 'Keep the line carrying an approved source default', 409)
    # A quota line without a block gets one (a year from the rule's start), shown on the line, not hidden in the rule.
    start = current.starts_on if current else conversion['effective_on'] if conversion else independence.today(db, actor.department_id)
    body = {"on": value.on, "settings": value.settings, "catalogue_version": value.catalogue_version,
            "lines": with_blocks(value.catalogue_id, value.settings, [
                _proposal_line(line, previous[line.id]) if line.id in previous else raw
                for line, raw in zip(value.lines, authored)], start, value.catalogue_version)}
    entry = catalogue_entry(value.catalogue_id, value.catalogue_version)
    if entry is not None:
        body['lines'] = _kept_safety_numbers(entry, body, body['lines'], previous)
    if value.catalogue_id == "overtime_offers":
        prior = {line.id: line.settings["allocation_overtime"]["volunteer_required"] for line in
                 s.CatalogueRule.model_validate(current.definition["rule"], context={"historical": True}).lines} if current else {}
        default = value.settings.get("allocation_overtime", {}).get("volunteer_required")
        for line in body["lines"]:
            settings = line.get("settings") or {}
            line["settings"] = {**settings, "allocation_overtime": {**settings.get("allocation_overtime", {})}}
            line["settings"]["allocation_overtime"].setdefault(
                "volunteer_required", default if default is not None else prior.get(
                    line["id"], True))
    balance = value.balance_between_people
    found = check(db, actor, value.catalogue_id, {**body, 'balance_between_people': balance})
    for line, checked_line in zip(body['lines'], found.get('lines', [])):
        if line['id'] not in previous:
            line['numbers'] = checked_line['numbers']
            line['settings'] = checked_line['settings']
    require(not found["issues"], "SOLVER_RULE_INCOMPATIBLE", "Adjust the highlighted lines before proposing", 422, found["issues"])
    require(not found["duplicates"], "DUPLICATE_LINES", "Two lines are the same line; remove one or change its dates or days", 422, found["duplicates"])
    validated = s.PolicyDefinition.model_validate({"legacy_lead_conversion": conversion, "rule": {"kind": "catalogue", "id": value.catalogue_id, **body},
                                                    'balance_between_people': balance,
                                                    "merged_ids": [current.id] if current else [],
                                                    "seeded_line_ids": sorted(seeds),
                                                    "exception_allowed": bool(current and current.definition.get("exception_allowed")
                                                                              and not s.safety_rule(current.definition))})
    definition = validated.model_dump(mode="json", exclude={"rule": {"lines"}})
    # Keep the saved representation when lines were omitted, including sparse historical lines.
    definition["rule"]["lines"] = inherited["lines"] if "lines" in inherited else [
        _proposal_line(line, was) for line, was in zip(validated.rule.lines, body["lines"])]
    _, reference_rows = condition_reference_rows(db, Line(conditions=[s.Conditions.model_validate(line[side])
        for line in definition['rule']['lines'] for side in ('who', 'where') if line.get(side)]))
    for line in definition["rule"]["lines"]:  # everything a line names belongs to this department
        # None-only lines still replace broader numbers, so their scopes need the same reference admission checks.
        for side in ('who', 'where'):
            if line.get(side):
                condition_references(db, actor, s.Conditions.model_validate(line[side]), reference_rows=reference_rows)
        for c in expand({"id": "", "definition": {**definition, "rule": {**definition["rule"], "on": True, "lines": [line]}}}):
            policy_references(db, actor, s.PolicyDefinition.model_validate(
                {**c["definition"], "merged_ids": []}, context={'resolved_catalogue_line': True,
                    'advisory_catalogue_slot': c.get('_advisory_catalogue_slot', False)}), reference_rows=reference_rows)
        if line.get("calendar_entry_id"):
            scoped(db, m.ProgrammeCalendarEntry, line["calendar_entry_id"], actor)
    row = m.Policy(department_id=actor.department_id, kind="catalogue", status="proposed", created_by=actor.identity_id,
                   name=entry["name"], owner=current.owner if current else "Department", source=value.reason,
                   starts_on=current.starts_on if current else date.fromisoformat(conversion['effective_on']) if conversion else independence.today(db, actor.department_id),
                   ends_on=current.ends_on if current else None, active=True,
                   definition=stored_definition(definition))
    db.add(row)
    db.flush()
    history(db, actor, "policy", row, value.reason)
    return {"record": record(row), "check": found,
            "message": "Proposed; it applies once another authorised person approves it (retiring it later brings back the version before)."}
