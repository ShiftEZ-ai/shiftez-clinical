"""Changed-line periods and observed effects; shared by review, impact and activation."""
import json
from copy import deepcopy
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import defer, undefer

from apps.api import models as m
from apps.api.auth import require_role, person_access
from apps.api.errors import require
from apps.api.serialization import json_value, record
from apps.api.snapshots import domain_snapshot, input_current, line_trial_snapshot, snapshot_inputs
from packages.contracts import schemas as s
from packages.domain.rule_lines import named_people, unconditional_people, when
from packages.domain.solver_rules import COMPILER_VERSION
from packages.domain.validation import evaluate, finding_observations
from .common import scoped
from .workspace_access import shared_clause


def effect_projection(before_data, before_validation, after_data, after_validation):
    """Shared saved-assignment comparison; maintenance attaches its owned Fix-list projection/delta here."""
    from packages.domain.rule_effects import observed_effect
    from packages.domain import rota_summary as summaries
    def phase(data, validation):
        focus = set(data['focus_ids'])
        shifts = [row for row in data['shifts'] if row['id'] in focus]
        return {'validation': validation, 'summary': summaries.rota_summary(validation, shifts)}
    effect = observed_effect(before_validation, after_validation)
    hook = getattr(summaries, 'fix_list_effect_projection', None)
    projections = (hook(before_data, before_validation, after_data, after_validation) if hook else
                   {'before': phase(before_data, before_validation), 'after': phase(after_data, after_validation)})
    return {**projections,
            'effect': effect, 'cleared_checks': effect['clears'], 'new_must_checks': effect['mandatory']['adds'],
            'summary': effect['summary']}


def adjustment_snapshot(base, policy, rows):
    """The base omits only this policy's rota adjustments; other policy scopes are already projected."""
    from .planning import adjusted_rules
    materialized = next((p for p in base['policies'] if p['id'] == policy['id']), None)
    adjusted = adjusted_rules(materialized, rows) if materialized else None
    return {**base, 'workspace': {**base['workspace'], 'rule_adjustments': rows},
            'policies': [p for p in (adjusted if p['id'] == policy['id'] else p for p in base['policies']) if p is not None]}


def ranked_adjustment_fit(base, before_validation, policy, workspace, value, identity_id):
    """A checked greedy lower bound for the explicit shortlist, reusing staffing and the D6 comparison."""
    from zoneinfo import ZoneInfo
    from apps.api.errors import Problem
    from packages.domain.conditions import active_rules
    from packages.domain.recurrence import inactive
    from packages.domain.rule_effects import observed_effect
    from packages.domain.rule_map import numeric_limit
    from packages.domain.rota_summary import rota_summary
    from packages.domain.time import aware, local_midnight
    from packages.domain.validation import policy_applies
    from .planning import rule_adjustment_rows
    from .staffing import check_people, people_index
    adjustment = value.adjustment
    bindings = [p for p in active_rules([p for p in base['policies'] if p['id'] == policy['id']], context=base)
                if (p.get('template_line') or {}).get('id') == adjustment.line_id and
                (p.get('template_line') or {}).get('slot') == adjustment.number]
    require(adjustment.mode == 'set' and bindings and all(numeric_limit(p['definition']['rule']) for p in bindings),
            'FIT_CAP_REQUIRED', 'Rank people for a numeric cap on a catalogue line', 422)
    department_people = {r['person_id'] for r in base['memberships'] if r['department_id'] == workspace.department_id}
    require(set(value.fit_person_ids) <= department_people & base['people'].keys(),
            'NOT_FOUND', 'Fit candidate not found in this department', 404)
    focus = set(base['focus_ids'])
    open_ids = {r['shift_id'] for r in rota_summary(before_validation, [r for r in base['shifts'] if r['id'] in focus])['sessions']
                if r['places_open'] > 0}
    candidates, excluded = [], []
    zone = ZoneInfo(base['organisation']['timezone'])
    for person_id in value.fit_person_ids:
        trial_input = adjustment.model_copy(update={'person_id': person_id})
        try:
            rows = rule_adjustment_rows(workspace, policy, trial_input, identity_id)
        except Problem as error:
            excluded.append({'person_id': person_id, 'code': error.code})
            continue
        candidate = adjustment_snapshot(base, policy, rows)
        current = evaluate(candidate)
        adjustment_effect = observed_effect(before_validation, current)
        if adjustment_effect['status'] != 'ready' or adjustment_effect['mandatory']['adds']:
            excluded.append({'person_id': person_id, 'code': 'NEW_MANDATORY_CHECKS' if adjustment_effect['mandatory']['adds'] else 'CHECK_INCOMPLETE'})
            continue
        entry = next(row for row in rows if row['policy_id'] == policy['id'] and row.get('person_id') == person_id
                     and row.get('line_id') == adjustment.line_id and row.get('number') == adjustment.number
                     and (not adjustment.starts_on or row.get('starts_on') == str(adjustment.starts_on)))
        first, last = (local_midnight(date.fromisoformat(entry[key]), zone.key) for key in ('starts_on', 'ends_on'))
        index, fitted = people_index(candidate), []
        for shift in sorted((r for r in base['shifts'] if r['id'] in open_ids), key=lambda r: (r['start'], r['id'])):
            a, b = aware(shift['start']), aware(shift['end'])
            if (shift.get('locked') or inactive(shift) or not first <= a < b <= last or
                    not any(policy_applies(base, p, person_id, shift, a, b) for p in bindings)):
                continue
            choices, slot = check_people(candidate, shift, None, [person_id], best=True, index=index)
            if not choices:
                continue
            chosen = choices[0]
            participant = (chosen.get('supervision') or {}).get('proposal') or {**slot, 'person_id': person_id}
            participant = {**participant, 'id': str(uuid4()), 'shift_id': shift['id'], 'fixed': False}
            proposed = {**candidate, 'shifts': [dict(row, participants=[*row['participants'], participant])
                        if row['id'] == shift['id'] else row for row in candidate['shifts']]}
            validation = evaluate(proposed)
            incremental = observed_effect(current, validation)
            if incremental['status'] != 'ready' or incremental['mandatory']['adds']:
                continue
            candidate, current = proposed, validation
            fitted.append(shift['id'])
            index = people_index(candidate)
        if not fitted:
            excluded.append({'person_id': person_id, 'code': 'NO_ELIGIBLE_OPEN_PLACES'})
            continue
        effect = observed_effect(before_validation, current)
        candidates.append({'person_id': person_id, 'display_name': base['people'][person_id]['display_name'],
            'eligible_open_places': len(fitted), 'shift_ids': fitted, 'cleared_checks': effect['clears'],
            'new_must_checks': effect['mandatory']['adds'], 'adjustment_effect': adjustment_effect, 'effect': effect, 'estimated': True})
    candidates.sort(key=lambda r: (-r['eligible_open_places'], -r['cleared_checks'], r['new_must_checks'],
                                   r['display_name'].casefold(), r['person_id']))
    return {'candidates': candidates, 'excluded': excluded, 'complete': True, 'optimal': False,
            'basis': 'Supplied shortlist; checked greedy fills in session date/ID order, keeping every existing assignment. Not an optimality or whole-rota feasibility proof.'}


def adjustment_effect(db, actor, workspace_id, value):
    from .planning import draft, rule_adjustment_rows
    from .policies import effective_source
    workspace = draft(db, actor, workspace_id, value.expected_revision)
    policy = effective_source(db, actor, scoped(db, m.Policy, value.adjustment.policy_id, actor))
    if value.adjustment.person_id:
        person_access(db, actor, value.adjustment.person_id)
    rows = rule_adjustment_rows(workspace, policy, value.adjustment, actor.identity_id)
    # Existing rule overrides suppress exactly this policy's adjustments and retain the complete dated context.
    base = domain_snapshot(db, workspace, policy_overrides=[policy])
    before = adjustment_snapshot(base, policy, workspace.rule_adjustments)
    after = adjustment_snapshot(base, policy, rows)
    previous, proposed = evaluate(before), evaluate(after)
    result = {'workspace_id': workspace.id, 'revision': workspace.revision, 'fingerprint': base['fingerprint'],
        'preview': True, 'committed': False, 'adjustment': value.adjustment.model_dump(mode='json'),
        **effect_projection(before, previous, after, proposed)}
    if value.fit_person_ids:
        result['ranked_fit'] = ranked_adjustment_fit(base, previous, policy, workspace, value, actor.identity_id)
    return result


def draft_line_effect(db, actor, catalogue_id, value):
    """Compare an admitted, unsaved line with the draft's current saved assignments."""
    from packages.domain.rule_effects import observed_effect
    from packages.domain.rule_map import line_check_scope
    from .rule_catalogue import catalogue_version, check
    from .common import expected
    from .independence import today
    from .planning import complete_check
    require_role(actor, 'coordinator', 'approver')
    current = catalogue_version(db, actor, catalogue_id, 'active')
    require((current.id if current else None) == value.expected_policy_id,
            'STALE_REVISION', 'Reload the current rule before previewing its line', 409)
    if current:
        expected(current, value.expected_revision)
        require(current.definition['rule'].get('catalogue_version', 1) == value.catalogue_version,
                'CATALOGUE_VERSION', 'Use the current catalogue version', 422)
    original = current.definition['rule'] if current else {}
    line = {**value.line.model_dump(mode='json', exclude_unset=True), 'id': value.line.id}
    lines = [line if row['id'] == line['id'] else row for row in original.get('lines', [])]
    if not any(row['id'] == line['id'] for row in original.get('lines', [])):
        lines.append(line)
    body = {**original, 'catalogue_version': value.catalogue_version, 'lines': lines,
            'balance_between_people': current.definition.get('balance_between_people', True) if current else True}
    checked = check(db, actor, catalogue_id, body)
    require(not checked['issues'] and not checked['duplicates'], 'RULE_CHECK_FAILED',
            'Resolve this line’s settings before checking its effect', 422,
            {'issues': checked['issues'], 'duplicates': checked['duplicates']})
    candidate = m.Policy(id=current.id if current else m.uid(), department_id=actor.department_id,
        kind='catalogue', status='active', active=True, revision=current.revision if current else 1,
        name=current.name if current else catalogue_id,
        starts_on=current.starts_on if current else today(db, actor.department_id),
        ends_on=current.ends_on if current else None,
        definition={**(current.definition if current else {}), 'balance_between_people': body['balance_between_people'],
            'merged_ids': [current.id] if current else [],
            'rule': {'kind': 'catalogue', 'id': catalogue_id, 'catalogue_version': value.catalogue_version,
                'on': original.get('on', True), 'settings': original.get('settings') or {}, 'lines': checked['lines']}})
    effects = []
    proposed_policy = record(candidate)
    workspaces = affected_workspaces(db, actor, [candidate])
    if not workspaces:
        return {'effects': {line['id']: effects}}
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    shared = snapshot_inputs(db, organisation)
    for workspace in workspaces:
        before = domain_snapshot(db, workspace, mandatory_rules=False, shared_inputs=shared)
        previous = workspace.validation
        if not complete_check(previous, before, before['department'].get('generation_defaults')):
            previous = evaluate(before, session_parts=True)
        after = line_trial_snapshot(before, workspace, proposed_policy, organisation.timezone)
        if after is None:
            after = domain_snapshot(db, workspace, policy_overrides=[proposed_policy], mandatory_rules=False,
                                    include_manifest=False, shared_inputs=shared)
        elif scope := line_check_scope(before, after, previous, candidate.id):
            after['_partial'] = scope
        effect = observed_effect(previous, evaluate(after))
        effects.append({'workspace_id': workspace.id, 'name': workspace.name,
                        'effect': {key: effect[key] for key in ('status', 'clears', 'adds', 'effect')}})
    return {'effects': {line['id']: effects}}


# SW3-B-19: a check job's frozen input (~20 MB) and full result (~90 MB at department scale, mostly candidate
# evidence) are never loaded to review or decide a rule change; read_metadata and result_summary carry every receipt
# field. Parsing the full rows held the GIL past uvicorn's worker health ping, so the review poll was killed mid-request.
CHECK_RECEIPT = (defer(m.Job.input_snapshot), defer(m.Job.result), undefer(m.Job.result_summary))


def check_result(job):
    """A check job's result without its candidate evidence (models.Job.result_summary); None until it finishes."""
    return (job.result_summary or {}).get('result')


def receipt_current(job, receipt, currency, source_hashes, checked_hashes):
    """Review and activation accept only the same current, unmodified rule bindings."""
    bindings = receipt.get('bindings', [])
    return (input_current(job.input_fingerprint, currency) is True
        and receipt.get('compiler_version') == COMPILER_VERSION
        and len(bindings) == len(source_hashes)
        and {b.get('policy_id') for b in bindings} == source_hashes.keys()
        and all(b.get('source_hash') == source_hashes[b['policy_id']]
            and b.get('checked_hash') == checked_hashes[b['policy_id']]
            and not b.get('temporary_change') for b in bindings))


def checked_effect(result):
    """Observed assignments alone cannot establish a complete rule comparison."""
    effect = result.get('effect') or {'status': 'unresolved'}
    if result.get('comparison_complete') is True:
        return effect
    timed_out = any(check.get('status') == 'unknown_time_limit' for check in result.get('checks', {}).values())
    return {**effect, 'status': 'unresolved',
            'summary': ('Time limit reached; the effect comparison is incomplete.' if timed_out else
                        'Effect comparison incomplete; full findings, candidate and quota evidence was not established.')}


def no_new_problems(result):
    return (result.get('comparison_complete') is True and result.get('no_new_problems') is True
            and result.get('new_or_worse_findings') == [])


def predecessor_rows(db, actor, policies, *, replaces_ids=()):
    ids = set(replaces_ids) | {ident for p in policies
        for ident in [*(p.definition.get('merged_ids') or []), p.definition.get('replaces_id')] if ident}
    return list(db.scalars(select(m.Policy).where(m.Policy.department_id == actor.department_id, m.Policy.id.in_(ids)))) if ids else []


def changed_periods(db, actor, policies, *, skipped_people=None, predecessors=None):
    if predecessors is None:
        predecessors = predecessor_rows(db, actor, policies)
    by_id = {p.id: p for p in predecessors}
    periods = []
    person_spans = []
    for policy in policies:
        previous = [by_id[ident] for ident in dict.fromkeys([*(policy.definition.get('merged_ids') or []), policy.definition.get('replaces_id')])
                    if ident in by_id]
        rows = [*previous, policy]
        # Historical sparse definitions and current default-filled proposals mean the same thing.
        definitions = [s.PolicyDefinition.model_validate(p.definition, context={'historical': True}).model_dump(mode='json') for p in rows]
        rules = [definition['rule'] for definition in definitions]
        catalogue = all(rule['kind'] == 'catalogue' for rule in rules)
        metadata = {'rule', 'merged_ids', 'replaces_id', 'seeded_line_ids', 'legacy_lead_conversion'}
        comparable = catalogue and len(previous) == 1 and all(
            {k: v for k, v in definition.items() if k not in metadata} ==
            {k: v for k, v in definitions[-1].items() if k not in metadata}
            and {k: v for k, v in rule.items() if k != 'lines'} ==
            {k: v for k, v in rules[-1].items() if k != 'lines'}
            and (p.starts_on, p.ends_on, p.active) == (policy.starts_on, policy.ends_on, policy.active)
            for p, definition, rule in zip(previous, definitions, rules))
        changed = None
        if comparable:
            before, after = ({line['id']: s.RuleLine.model_validate(line).model_dump(mode='json') for line in rule.get('lines') or []}
                             for rule in (rules[0], rules[-1]))
            changed = {ident for ident in before.keys() | after.keys() if before.get(ident) != after.get(ident)}
        for p in rows:
            rule = p.definition['rule']
            spans = [(line, when(rule, line)) for line in rule.get('lines') or []
                     if changed is None or line['id'] in changed] if catalogue else [(None, (None, None))]
            for line, (first, last) in spans:
                first = max(p.starts_on, date.fromisoformat(str(first)) if first else p.starts_on)
                last = min([d for d in (p.ends_on, date.fromisoformat(str(last)) if last else None) if d is not None], default=None)
                if last is None or first < last:
                    periods.append((first, last, p.definition.get('workspace_id')))
                    people = (named_people(line) if comparable and unconditional_people(line)
                              and not line.get('source_bindings') and not line.get('source_default') else [])
                    person_spans.append((people[0] if len(people) == 1 else None, first))
    if person_spans and all(pid for pid, _ in person_spans):
        from packages.domain.time import local_midnight
        from packages.domain.validation import valid_dates
        people = {pid for pid, _ in person_spans}
        M = m.Membership
        ends = {pid: last for pid, count, dated, last in db.execute(select(
            M.person_id, func.count(), func.count(M.ends_on), func.max(M.ends_on)).where(
                M.department_id == actor.department_id, M.person_id.in_(people)).group_by(M.person_id)) if count == dated}
        department = db.get(m.Department, actor.department_id)
        zone = db.get(m.Organisation, department.organisation_id).timezone
        # A positive duty interval reuses the solver's membership end boundary, including its exclusive last day.
        starts = [(pid, local_midnight(first, zone)) for pid, first in person_spans]
        if all(pid in ends and not valid_dates({'ends_on': str(ends[pid])}, at,
                   at + timedelta(microseconds=1), zone) for pid, at in starts):
            # Saved placements can still carry rule findings after membership ends; keep those checks.
            A, S, W = m.Assignment, m.Shift, m.Workspace
            last = None if any(last is None for _, last, _ in periods) else max(last for _, last, _ in periods)
            placed = db.scalar(select(A.id).join(S, S.id == A.shift_id).join(W, W.id == S.workspace_id).where(
                W.department_id == actor.department_id, A.person_id.in_(people), A.end > min(at for _, at in starts),
                *([A.start < local_midnight(last, zone)] if last else [])).limit(1))
            if placed is None:
                if skipped_people is not None:
                    skipped_people.update(ends)
                return []
    return sorted(set(periods), key=lambda span: (span[0], span[1] or date.max, span[2] or ''))


def affected_workspaces(db, actor, policies, *, periods=None):
    periods = changed_periods(db, actor, policies) if periods is None else periods
    if not periods:
        return []
    W = m.Workspace
    # One date envelope in SQL (a rule may have ~1,000 dated lines: an OR per line exceeds SQLite's expression depth
    # and bloats the statement); each rota is then kept only when a changed period overlaps it.
    first = min(p[0] for p in periods)
    last = None if any(p[1] is None for p in periods) else max(p[1] for p in periods)
    scoped_ids = {p[2] for p in periods}
    rows = [w for w in db.scalars(select(W).where(shared_clause(), W.department_id == actor.department_id,
        W.status.in_(('draft', 'submitted', 'approved')), W.archived_at.is_(None), W.ends_on > first,
        *([W.starts_on < last] if last else []), *([] if None in scoped_ids else [W.id.in_(scoped_ids)])).order_by(W.starts_on, W.id))
        if any(w.ends_on > a and (b is None or w.starts_on < b) and (ws is None or w.id == ws) for a, b, ws in periods)]
    rows = reachable_workspaces(db, actor, policies, rows)
    require(len(rows) <= 100, 'IMPACT_LIMIT', 'Too many affected current rotas for an interactive review', 409)
    return rows


def reachable_workspaces(db, actor, policies, workspaces):
    """Prune only proven disjoint duty scopes; zero-count quotas and unknown contexts remain reviewable."""
    if not workspaces:
        return []
    def policy_record(policy):
        # Contextual fact checks use a lightweight policy adapter rather than an ORM row.
        if isinstance(policy, m.Policy):
            return record(policy)
        if isinstance(policy, dict):
            return json_value(policy)
        return json_value(vars(policy))
    sources = [*predecessor_rows(db, actor, policies), *policies]
    if any(line.get('source_bindings') for policy in sources
           for line in policy.definition['rule'].get('lines') or []):
        return workspaces  # Bound catalogue sources require the complete scheduling context.
    # The public helper is also used by bounded impact tests with a department-only
    # actor adapter.  Resolve organisation through the department, which is the
    # authoritative visibility boundary for this query.
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    from apps.api.snapshots import tag_data
    from packages.domain.conditions import active_rules, with_tags
    from packages.domain.validation import session_matches, session_scope
    context = {'organisation': record(organisation),
        'tags': tag_data(db, [actor.department_id]),
        'locations': {row.id: record(row) for row in db.scalars(select(m.Location).where(
            m.Location.department_id == actor.department_id))},
        'policies': [{**policy_record(p), 'status': 'active', 'active': True}
                     for p in sources]}
    copies = active_rules(with_tags(context), overrides=False)
    # Period counts and goals can introduce obligations even without a matching duty.
    if not copies or any(p['kind'] not in {'restriction', 'minimum_cover', 'maximum_participants'} for p in copies):
        return workspaces
    # Dated catalogue lines share the same duty scope.  Keep one copy per
    # session projection so a thousand-line rule remains linear in line count.
    scopes = list({json.dumps(session_scope(p['definition']['scope']), sort_keys=True):
                   session_scope(p['definition']['scope']) for p in copies}.values())
    from packages.domain.conditions import has_session_lines, scope_tree
    if any(has_session_lines(scope_tree(scope), {'date', 'date_set_ids', 'usual_assignee_ids'}) for scope in scopes):
        return workspaces  # These selectors need recurrence/date-set context; keep their review conservative.
    S, SS = m.Shift, m.ShiftService
    shifts = {}
    for wid, sid, typ, location, service in db.execute(select(S.workspace_id, S.id, S.assignment_type_id,
            S.location_id, SS.service_id).outerjoin(SS, SS.shift_id == S.id).where(
            S.workspace_id.in_([w.id for w in workspaces]))):
        row = shifts.setdefault(wid, {}).setdefault(sid, {'id': sid, 'assignment_type_id': typ,
            'location_id': location, 'service_ids': []})
        if service:
            row['service_ids'].append(service)
    return [w for w in workspaces if w.id not in shifts or any(
        session_matches(context, scope, shift) for scope in scopes for shift in shifts[w.id].values())]


def review(db, actor, policy_id, *, decision=None):
    require_role(actor, 'coordinator', 'approver')
    policy = scoped(db, m.Policy, policy_id, actor)
    require(policy.status in {'active', 'proposed'}, 'WORKFLOW_STATE', 'Review an active or proposed rule', 409)
    if decision is not None:
        require(policy.status == 'proposed', 'WORKFLOW_STATE', 'Only a proposed policy can be activated', 409)
    from .policies import activation_weakens_safety, deferred_activation_retirements
    replaces_ids = decision.replaces_ids if decision is not None else ()
    predecessors = predecessor_rows(db, actor, [policy], replaces_ids=replaces_ids)
    weakens = policy.status == 'proposed' and activation_weakens_safety(db, actor, policy, replaces_ids, predecessors=predecessors)
    protection = {'policy_id': policy.id}
    if decision is not None:
        waiting = sorted(deferred_activation_retirements(db, actor, policy, replaces_ids, predecessors=predecessors))
        return {**protection, 'weakens_safety': weakens, **({'pending_retirement_ids': waiting} if waiting else {})}
    if weakens:
        protection['weakens_safety'] = True
    skipped_people = {}
    periods = changed_periods(db, actor, [policy], skipped_people=skipped_people, predecessors=predecessors)
    workspaces = affected_workspaces(db, actor, [policy], periods=periods)
    from apps.api.snapshots import cheap_fingerprints, currency_hint, currency_match, snapshot_hash
    source_hash, checked_hash = snapshot_hash(record(policy)), snapshot_hash({**record(policy), 'status': 'active'})
    frozen = m.Job.read_metadata['input_snapshot']
    candidate = [m.Job.department_id == actor.department_id, m.Job.kind == 'feasibility',
        m.Job.workspace_id.in_([workspace.id for workspace in workspaces]),
        frozen['bindings'][0]['policy_id'].as_string() == policy.id,
        frozen['bindings'][0]['source_hash'].as_string() == source_hash,
        frozen['bindings'][0]['checked_hash'].as_string() == checked_hash,
        frozen['bindings'][0]['temporary_change'].as_boolean().is_(False),
        frozen['bindings'][1]['policy_id'].as_string().is_(None),
        frozen['compiler_version'].as_string() == COMPILER_VERSION]
    # Read the latest matching job and just its baseline manifest, leaving both large job columns deferred.
    jobs, checks = {}, {}
    if workspaces:
        latest = select(m.Job.id, func.row_number().over(partition_by=m.Job.workspace_id,
            order_by=(m.Job.created_at.desc(), m.Job.seq.desc())).label('position')).where(*candidate).subquery()
        for job, manifest in db.execute(select(m.Job, m.Job.input_snapshot['baseline']['input_manifest'])
                .options(*CHECK_RECEIPT).join(latest, latest.c.id == m.Job.id).where(latest.c.position == 1)):
            jobs[job.workspace_id] = job
            checks[job.workspace_id] = {'input_manifest': manifest}
    currencies = cheap_fingerprints(db, workspaces, checks=checks, feasibility=True)
    # Recurring blocking events can change the check inputs at midnight without a revision write.
    hints = {workspace.id: currency_hint(db, workspace) for workspace in workspaces if workspace.id in currencies}

    def current_hint(workspace_id, recorded):
        expected = hints.get(workspace_id)
        return (expected is not None and isinstance(recorded, dict)
            and recorded.get('input_manifest_format') == expected['input_manifest_format']
            and currency_match({'event_local_date': recorded.get('event_local_date')},
                               {'event_local_date': expected.get('event_local_date')}))

    # A failed or unrelated later attempt cannot erase a proven contradiction for identical inputs.
    contradictions = set()
    if currencies:
        rows = db.execute(select(m.Job.workspace_id, m.Job.input_fingerprint, frozen['currency_hint']).where(
            *candidate, m.Job.status == 'succeeded',
            m.Job.result_summary['result']['compiler_version'].as_string() == COMPILER_VERSION,
            m.Job.result_summary['result']['newly_introduced_contradiction'].as_boolean().is_(True)))
        contradictions = {wid for wid, fingerprint, hint in rows
                          if input_current(fingerprint, currencies.get(wid)) and current_hint(wid, hint)}
    effects = []
    for workspace in workspaces:
        matching = jobs.get(workspace.id)
        result = check_result(matching) if matching else None
        if matching:
            metadata = (matching.read_metadata or {}).get('input_snapshot') or {}
            if (not current_hint(workspace.id, metadata.get('currency_hint'))
                    or not receipt_current(matching, result or metadata, currencies.get(workspace.id),
                                           {policy.id: source_hash}, {policy.id: checked_hash})):
                matching = None
        effect = (checked_effect(result or {}) if matching and matching.status == 'succeeded' else None)
        effect = effect or {'status': ('unresolved' if matching.status == 'succeeded' else matching.status)
                            if matching else 'not_checked'}
        from .policies import context_check_blocker
        blocker = context_check_blocker(result or {}) if matching and matching.status == 'succeeded' else None
        if workspace.id in contradictions:
            blocker = 'RULE_INTRODUCES_CONFLICT'
        completed_result = (result or {}) if matching and matching.status == 'succeeded' else {}
        complete = completed_result.get('comparison_complete') is True
        if not blocker and s.safety_rule(policy.definition) and not complete:
            blocker = 'RULE_CHECK_UNRESOLVED'
        effects.append({'workspace_id': workspace.id, 'name': workspace.name, 'revision': workspace.revision,
            'starts_on': str(workspace.starts_on), 'ends_on': str(workspace.ends_on),
            'job_id': matching.id if matching else None, 'started_at': json_value(matching.started_at) if matching else None,
            'created_at': json_value(matching.created_at) if matching else None,
            'comparison_complete': complete,
            'activation_blocker': blocker, **effect,
            'new_or_worse_count': len(completed_result.get('new_or_worse_findings') or []) if complete else 0,
            'requires_acknowledgement': effect['status'] != 'ready' or not no_new_problems(completed_result),
            'summary': effect.get('summary') or 'Effect not established; an approver may decide with a recorded reason.'})
    relations = {}
    if policy.kind == 'catalogue':
        # Use the editor's dated population reach as well as its limit resolver;
        # an unrelated rank's line must not be described as binding this person.
        from .rule_catalogue import _check
        from packages.contracts.rule_catalogue import catalogue_entry
        rule = policy.definition['rule']
        relations = _check(db, actor, catalogue_entry(rule['id'], rule.get('catalogue_version', 1)),
            {**rule, 'balance_between_people': policy.definition.get('balance_between_people', True)},
            policy_ends_on=policy.ends_on).get('relations', {})
    summary = {}
    not_checked_count = sum(effect['status'] == 'not_checked' for effect in effects)
    # A policy with no retained check has historically returned no summary
    # (for example an uncertain ended-person span). Keep that response shape;
    # stale or legacy retained receipts get the explicit re-check prompt.
    if not_checked_count and jobs:
        summary.update({'not_checked_count': not_checked_count,
                        'summary': f'{not_checked_count} drafts not yet re-checked · Check now'})
    if skipped_people:
        if len(skipped_people) == 1:
            from .people import visible_person
            person_id, ends_on = next(iter(skipped_people.items()))
            person = visible_person(actor, record(person_access(db, actor, person_id)))
            reason = f"No check needed: {person['display_name']} left on {ends_on - timedelta(days=1)} and has no later placements."
        else:
            reason = f'No check needed: {len(skipped_people)} people left before these lines start and have no later placements.'
        summary.setdefault('summary', reason)
    return {**protection, 'relations': relations, **summary, 'effect': effects,
            'ready': all(e['status'] == 'ready' and not e['activation_blocker'] for e in effects),
            'can_decide': all(e['status'] not in {'queued', 'running'} and not e['activation_blocker'] for e in effects),
            'requires_acknowledgement': any(e['requires_acknowledgement'] for e in effects)}


def relax_seed(db, actor, policy_id, value):
    require_role(actor, 'coordinator')
    person_access(db, actor, value.person_id)
    policy = scoped(db, m.Policy, policy_id, actor)
    require(policy.status == 'active' and policy.kind == 'catalogue', 'WORKFLOW_STATE', 'Choose an active catalogue line', 409)
    line = next((line for line in policy.definition['rule']['lines'] if line['id'] == value.binding_line_id), None)
    require(line is not None, 'NOT_FOUND', 'Rule line not found', 404)
    from packages.domain.rule_map import weakens_safety
    selected = value.number or (next(iter(line['numbers'])) if len(line['numbers']) == 1 else None)
    require(selected is None or selected in line['numbers'], 'BINDING_LINE_REQUIRED',
            'Choose a number on this binding line', 409)
    require(not (weakens_safety(record(policy), {'mode': 'off', 'line_id': line['id'], 'number': selected})
                 if selected else s.safety_rule({**policy.definition, 'rule': {**policy.definition['rule'], 'lines': [line]}})),
            'SAFETY_EXCEPTION', 'Safety rules cannot be relaxed for a person', 422)
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    require(workspace.status == 'draft' and workspace.archived_at is None, 'WORKFLOW_STATE', 'Only a current draft can be adjusted', 409)
    require(workspace.starts_on <= value.starts_on < value.ends_on <= workspace.ends_on,
            'RELAX_PERIOD', 'Choose the blocker period inside this rota', 422)
    data = domain_snapshot(db, workspace)
    validation = evaluate(data)
    no = policy.definition['rule']['lines'].index(line)
    blockers = [f for f in finding_observations(validation) if f.get('severity') == 'error' and
        f.get('rule_id') == policy.id and f.get('person_id') == value.person_id and
        ((f.get('detail') or {}).get('line') == no or (f.get('detail') or {}).get('line_id') == line['id'])]
    require(blockers, 'BINDING_LINE_REQUIRED', 'Choose the binding line of this person’s current blocker', 409)
    numbers = {key for finding in blockers for key in (finding.get('detail', {}).get('slot') or '').split('+')
               if key in line['numbers']}
    numbers = numbers or set(line['numbers'])
    number = value.number or (next(iter(numbers)) if len(numbers) == 1 else None)
    require(number in numbers, 'BINDING_LINE_REQUIRED', 'Choose the number of this binding blocker', 409)
    a, b = when(policy.definition['rule'], line)
    require((not a or str(a) <= str(value.starts_on)) and (not b or str(value.ends_on) <= str(b)),
            'RELAX_PERIOD', 'Choose dates within the binding line', 422)
    seed = {**deepcopy(line), 'id': str(uuid4()), 'who': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [value.person_id]}]},
        'starts_on': str(value.starts_on), 'ends_on': str(value.ends_on), 'source_bindings': {}, 'source_default': None}
    return {'catalogue_id': policy.definition['rule']['id'],
            'catalogue_version': policy.definition['rule'].get('catalogue_version', 1),
            'expected_policy_id': policy.id, 'expected_revision': policy.revision,
            'binding_line_id': line['id'], 'binding_line_no': no + 1, 'line': seed,
            'lines': [*policy.definition['rule']['lines'], seed], 'scope': 'rota', 'needs_approval': False,
            'adjustment': {'policy_id': policy.id, 'line_id': line['id'], 'person_id': value.person_id,
                'starts_on': str(value.starts_on), 'ends_on': str(value.ends_on), 'mode': 'set',
                'number': number, 'amount': line['numbers'][number].get('value')},
            'permanent_change': {'needs_approval': True}}


def promote_adjustment_seed(db, actor, policy_id, value):
    """Prepare a catalogue proposal from one saved rota line adjustment without writing anything."""
    require_role(actor, 'coordinator', 'admin')
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    require(workspace.status == 'draft' and workspace.archived_at is None,
            'WORKFLOW_STATE', 'Only a current draft can promote an adjustment', 409)
    policy = scoped(db, m.Policy, policy_id, actor)
    require(policy.status == 'active' and policy.kind == 'catalogue',
            'WORKFLOW_STATE', 'Choose an active catalogue rule', 409)
    adjustment = next((row for row in workspace.rule_adjustments or [] if row.get('id') == value.adjustment_id), None)
    require(adjustment is not None, 'NOT_FOUND', 'Rota adjustment not found', 404)
    require(adjustment.get('policy_id') == policy.id and adjustment.get('line_id')
            and adjustment.get('mode') == 'set' and adjustment.get('amount') is not None,
            'ADJUSTMENT_INVALID', 'Choose one catalogue line number adjustment', 409)
    require(adjustment.get('policy_revision') == policy.revision,
            'STALE_REVISION', 'The department rule changed; reload this rota before promoting the adjustment', 409)
    from packages.domain.rule_map import weakens_safety
    require(not weakens_safety(record(policy), adjustment),
            'SAFETY_EXCEPTION', 'A safety rule can only be promoted when the rota is stricter', 422)

    source_rule = policy.definition['rule']
    source_line = next((line for line in source_rule.get('lines') or []
                        if line['id'] == adjustment['line_id']), None)
    require(source_line is not None and adjustment.get('number') in source_line.get('numbers', {}),
            'NOT_FOUND', 'Rule line not found', 404)
    from .planning import adjusted_rule
    projected = adjusted_rule(record(policy), adjustment)
    adjusted_id = str(uuid5(NAMESPACE_URL, f"{source_line['id']}:{adjustment['id']}:adjusted"))
    projected_lines = [s.RuleLine.model_validate({key: item[key] for key in item if not key.startswith('_')}).model_dump(mode='json')
                       for item in projected['definition']['rule'].get('lines') or []]
    require(any(line.get('id') == adjusted_id for line in projected_lines),
            'ADJUSTMENT_INVALID', 'The rota adjustment no longer resolves to a proposal line', 409)

    from .rule_catalogue import _check
    from packages.contracts.rule_catalogue import catalogue_entry
    catalogue = catalogue_entry(source_rule['id'], source_rule.get('catalogue_version', 1))
    checked = _check(db, actor, catalogue, {
        'on': source_rule.get('on', True), 'settings': source_rule.get('settings') or {},
        'catalogue_version': source_rule.get('catalogue_version', 1), 'lines': projected_lines,
        'balance_between_people': policy.definition.get('balance_between_people', True)},
        policy_ends_on=policy.ends_on)
    require(not checked.get('issues') and not checked.get('duplicates'), 'RULE_CHECK_FAILED',
            'The adjusted line needs review before it can be proposed', 422,
            {'issues': checked.get('issues', []), 'duplicates': checked.get('duplicates', [])})
    lines = [s.RuleLine.model_validate(line).model_dump(mode='json') for line in checked.get('lines') or projected_lines]
    line = next((line for line in lines if line['id'] == adjusted_id), None)
    require(line is not None, 'ADJUSTMENT_INVALID', 'The rota adjustment no longer resolves to a proposal line', 409)
    return {'catalogue_id': source_rule['id'], 'expected_policy_id': policy.id,
            'expected_revision': policy.revision, 'line': line, 'lines': lines,
            'relation': checked.get('relations', {}).get(adjusted_id)}
