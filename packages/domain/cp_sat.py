"""CP-SAT assignment translation.

Unsupported rules fail closed. Every proposal is checked by the canonical validator;
scoped proposals may retain unchanged outside-scope findings. New unexpected errors
are solver/validator disagreement, never a usable proposal.
"""
import copy
import time
import uuid
import zlib
from math import gcd, lcm
from fractions import Fraction
from collections import defaultdict
from functools import cache
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from ortools import __version__ as solver_version
from ortools.sat.python import cp_model

from .candidate_evidence import candidate_evidence, direct_coverage_shortages, focused_candidate_people
from . import leadership
from .validation import effective_type_definition, is_open_extra_cover
from .participation import presence_intervals
from .generation import allocation_overtime_allowed, duty_labels, assignment_fixed, keep_assignment, replacement_summary, citation_finding_key, coverage_findings
from .time import aware, duration_hours, local_midnight, overlaps
from .conditions import active_rules, counting_key, counting_views, scope_matches_throughout
from .validation import attached_to, evaluate, finding_observations, capabilities_for, categories_for, exception_for, named_people, policy_applies, applicable_restriction_intervals, scope_intervals, session_scope, within_dates, qualified, qualified_supervisor, rotation_blocks, restriction_blocks, lead_qualified
from .validation import validate_session_setup, capabilities_during, index_person_rows, person_rows, workload_gap_blockers, staffing_credit
from .validation import participation_for, trainee_intervals, is_trainee, supervision_commitments
from .supervision_levelling import levelled_pairing, session_spread_policies, spread_gap, spread_policy_key
from packages.contracts.schemas import safety_rule

from .solver_rules import COMPILER_VERSION, SUPPORTED_RULES, compatibility_issues, PLANNING_LIMITS, session_span_days
from .solver_control import SolverControl, Cancelled, TimeLimit, StopGraceExpired
from .solver_progress import precheck_issues
from .solver_provenance import policy_name
from .generation_scope import pooled_anchor, population_allows, protects_existing, scope_receipt, selected_session_ids
from .generation_outcome import DIAGNOSTIC_PROPOSAL_MESSAGE
from .cp_sat_score import ScorePrecisionError
from .recurrence import inactive, with_series_state, series_candidate_allowed, series_definition
from .fill_priority import STAGE_ORDER, stage_label

CHECKPOINT_INTERVAL_SECONDS = 60


def _coverage_floor_key(row):
    start, end = row.get('start'), row.get('end')
    if isinstance(start, str):
        start = aware(start)
    if isinstance(end, str):
        end = aware(end)
    return row.get('shift_id'), row.get('code'), start, end, row.get('rule_id')


def _coverage_base_key(row):
    return row.get('shift_id'), row.get('code'), row.get('rule_id')


def _required_coverage_deficit(validation):
    """Measure only recorded coverage floors in the same seat-minutes as the objective."""
    floor_rows = {_coverage_floor_key(row): row for row in validation.get('_coverage_floor_rows', ())}
    rows_by_base = defaultdict(list)
    for row in floor_rows.values():
        rows_by_base[_coverage_base_key(row)].append(row)
    total = 0
    for finding in finding_observations(validation):
        if not finding.get('start') or not finding.get('end'):
            continue
        detail = finding.get('detail') or {}
        if 'required' not in detail or 'available' not in detail:
            continue
        deficit = max(0, detail['required'] - detail['available'])
        if not deficit:
            continue
        finding_start, finding_end = aware(finding['start']), aware(finding['end'])
        for row in rows_by_base[_coverage_base_key(finding)]:
            row_start, row_end = row.get('start'), row.get('end')
            if row_start is None or row_end is None:
                minutes = (finding_end - finding_start) // timedelta(minutes=1)
            else:
                minutes = (min(finding_end, aware(row_end)) - max(finding_start, aware(row_start))) // timedelta(minutes=1)
            if minutes > 0:
                total += minutes * deficit * row.get('weight', 1)
    return total


def generate(snapshot, time_budget_seconds=None, workers=1, *, progress=None, cancelled=None, feasibility_only=False, diagnostic_proposal=False,
             deterministic=False, _on_model=None, _on_objectives=None,
             resume_state=None, checkpoint=None, search_control=None, _diagnostic_continuation=False, _candidate_evidence=None,
             _control=None):
    """`deterministic`: the budget is CP-SAT deterministic time (work units, about seconds on a reference machine) instead
    of wall-clock seconds, and the plateau stop is off, so a run gives the same schedule whatever else the machine is
    doing. For reproducible checks (tests, benchmarks); department runs keep the wall clock people wait on.
    `_on_model` is an internal structural-proof handoff: after validation, return without searching. It is not a
    generation option, and `model_ready` does not claim a feasible schedule.
    `_diagnostic_continuation`: live generation jobs reuse this preparation for a diagnostic proposal after a proof
    of infeasibility; direct solver and rule-proof callers retain strict results by default."""
    control = _control or SolverControl(progress, cancelled, search_control=search_control)
    control.context['diagnostic_proposal'] = diagnostic_proposal
    from packages.contracts.schemas import GenerationOptions, AUTO_SCHEDULE_LIMITS
    options = GenerationOptions.model_validate(snapshot.get('generation_options', {}))
    # The frozen run option is the policy owner.  The private flag remains an
    # escape hatch for existing proof callers and for a resumed "continue" run.
    _diagnostic_continuation = _diagnostic_continuation or options.on_conflict == 'continue_flagged'
    control.configure_stop_conditions(options, (snapshot.get('organisation') or {}).get('timezone', 'UTC'))
    if time_budget_seconds is None:
        time_budget_seconds = options.search_minutes * 60
    if not 0 < time_budget_seconds <= AUTO_SCHEDULE_LIMITS['maximum_minutes'] * 60 or not 1 <= workers <= 8:
        raise ValueError('Search budget is outside the supported range or workers are outside 1-8')
    if diagnostic_proposal and feasibility_only:
        raise ValueError("A diagnostic proposal cannot certify feasibility")
    if _on_model is not None and not feasibility_only:
        raise ValueError('Model capture requires feasibility-only compilation')
    if _on_objectives is not None and feasibility_only:
        raise ValueError('Objective capture requires full compilation')
    stop_message = None
    try:
        if snapshot.get('planning_group_id') and snapshot.get('lookahead') == 'coupled' and not feasibility_only:
            from .group_generation import generate as group_generate, witness_progress
            def solve_joint(joint):
                report = control.progress
                control.progress = witness_progress(progress) or report
                try:
                    return generate(joint, time_budget_seconds, workers, cancelled=cancelled, deterministic=deterministic,
                        diagnostic_proposal=diagnostic_proposal, _diagnostic_continuation=_diagnostic_continuation,
                        _control=control)
                finally:
                    control.progress = report
                    # A joint witness is not an applyable actual-only proposal.
                    control.validated_result = None
            result = group_generate(snapshot, solve_joint, control=control)
        else:
            result = _generate(snapshot, time_budget_seconds, workers, control, options, feasibility_only, diagnostic_proposal, deterministic, _on_model, _on_objectives, resume_state, checkpoint, _diagnostic_continuation, _candidate_evidence)
        if result['status'] == 'requirements_conflict' and not feasibility_only and 'summary' not in result:
            control.phase('summary')
            control.stage('summarizing')
            from .solver_summary import summarise
            current = canonical_order(copy.deepcopy(snapshot))
            current['_check_budget'] = control.check
            selected = set(selected_session_ids(snapshot))
            editable = {s['id'] for s in current['shifts'] if s['id'] in selected and not s['locked']
                        and not snapshot['workspace'].get('base_publication_id')}
            result['summary'] = summarise(current, evaluate(current), result.get('candidate_evidence', []), editable,
                                           diagnostics=result.get('diagnostics', []))
        control.check()
    except ScorePrecisionError as error:
        result = {'algorithm_version': COMPILER_VERSION, 'solver_version': solver_version,
                'input_fingerprint': snapshot['fingerprint'], 'status': 'invalid_inputs',
                'additions': [], 'removals': [], 'optimality_proven': False, 'weighted_status': 'UNKNOWN',
                'issues': [{'code': 'SCORE_PRECISION_UNSUPPORTED', 'message': str(error)}]}
    except (Cancelled, TimeLimit) as exc:
        result = {'algorithm_version': COMPILER_VERSION, 'solver_version': solver_version,
                'input_fingerprint': snapshot['fingerprint'],
                'status': 'cancelled' if isinstance(exc, Cancelled) else 'unknown_time_limit' if feasibility_only else 'needs_more_time',
                'additions': [], 'removals': [], 'optimality_proven': False,
                'solver_status': 'UNKNOWN', 'weighted_status': 'UNKNOWN'}
        if isinstance(exc, StopGraceExpired):
            result = {**(control.validated_result or result), 'score_completed': False}
            stop_message = ('Stopped; final score not completed.' if control.validated_result else
                            'Stopped; canonical validation not completed. The incumbent remains available to resume.')
            if control.validated_result is not None:
                control.progress({**control.context, 'stage': 'ready', 'best_score': None,
                                  'shared_validation_passed': result['validation']['valid'],
                                  'elapsed_seconds': time.monotonic() - control.started,
                                  'search_elapsed_seconds': control.search_spent,
                                  'phase_timings': control.timings()})
    result.setdefault('score_completed', False)
    from .generation_outcome import SCORE_OPTIMISATION_UNFINISHED, generation_outcome, score_value, solver_status
    if control.stopped and result['status'] == 'candidate_feasible' and not result['score_completed']:
        if not result.get('optimality_proven'):
            stop_message = SCORE_OPTIMISATION_UNFINISHED
        if (saved := (result.get('summary') or {}).get('score_saved')) is not None:
            saved['score_completed'] = False
    if stop_message and stop_message not in (result.get('message') or ''):
        result['message'] = ' '.join(filter(None, (result.get('message'), stop_message)))
    control.phase(None)
    result['solver_status'] = solver_status(result)
    result['score'] = score_value(result)
    outcome = generation_outcome(result)
    final_stop_reason = control.stop_reason
    if control.stopped and result.get('status') != 'cancelled':
        final_stop_reason = 'optimal' if result.get('optimality_proven') else 'operator_stop'
    if result.get('status') == 'requirements_conflict':
        search_stop_reason = 'conflict'
        search_stop_detail = (result.get('conflict') or {}).get('message', 'The strict requirements conflict.')
    elif result.get('status') == 'cancelled':
        search_stop_reason, search_stop_detail = 'cancelled', 'The run was cancelled.'
    elif result.get('status') in {'translation_error', 'model_invalid', 'invalid_inputs'}:
        search_stop_reason, search_stop_detail = 'failed', result.get('message', 'The run stopped with an error.')
    else:
        search_stop_reason = {'target_gap': 'gap', 'target_score': 'score', 'stop_by': 'clock',
                              'slice_limit': 'time_limit'}.get(final_stop_reason, final_stop_reason)
        if search_stop_reason == 'optimal' and outcome['status'] != 'OPTIMAL':
            search_stop_reason = 'model_proven'
        if search_stop_reason not in {'optimal', 'model_proven', 'time_limit', 'plateau', 'gap', 'score', 'clock', 'operator_stop'}:
            search_stop_reason = 'no_search' if not control.search_spent and not control.improvements_found else 'time_limit'
        search_stop_detail = result.get('message', '')
    search_seconds = result.get('search_elapsed_seconds', control.search_spent)
    if not isinstance(search_seconds, (int, float)) or search_seconds < 0:
        search_seconds = 0
    return {**result, 'on_conflict': options.on_conflict, 'stop_reason': final_stop_reason, 'search_stop': {
                'reason': search_stop_reason, 'search_seconds': search_seconds, 'detail': search_stop_detail},
            'generation_outcome': outcome, **scope_receipt(snapshot), 'phase_timings': control.phase_timings,
            'score_completion_seconds': control.completion_spent,
            'live_score_metrics': control.score_metrics,
            'improvements_found': control.improvements_found,
            'search_elapsed_seconds': control.search_spent,
            'time_limit_seconds': control.search_budget if control.search_budget is not None else time_budget_seconds,
            'elapsed_seconds': time.monotonic() - control.started}


def validation_reserve(data, measured_seconds):
    """Three final-check passes, scaled from measured input validation to the projected assignment count.

    Final validation, score-context validation and summaries share this allowance; expiry still fails closed.
    """
    shifts = data['shifts']
    existing = sum(len(s['participants']) for s in shifts)
    projected = sum(max(len(s['participants']), sum(r.get('maximum') or r.get('target') or r['minimum']
                    for r in s['type_snapshot']['definition']['requirements'])) for s in shifts)
    return 3 * measured_seconds * (len(shifts) + projected) / max(1, len(shifts) + existing)


def keep_no_worse(model, exempt, exempt_indexes=()):
    """A scoped run cannot change work outside its sessions. A hard constraint that kept work already breaks whatever
    the run decides (two kept duties too close, a kept week over its hours, an understaffed session outside the
    scope) would make every proposal impossible, so it is loosened to "no worse than now": the bound moves to the
    best value the run can still reach. Variable bounds come from the domains plus the max/min flags the builders use
    ("some duty in this window"), so a breach behind such a condition is found too. Linear, at-most-one and
    folded-constant constraints are loosened, never the ones named in `exempt` or listed in `exempt_indexes`.
    Returns how many were.
    shortcut: bound propagation through lin_max only; a breach implied through other derived constraints still blocks
    the run (the final validation reports nothing new either way). Extend `tighten` if one shows up."""
    known = [(list(v.domain)[0], list(v.domain)[-1]) for v in model.proto.variables]

    def bounds(ref):
        lo, hi = known[ref if ref >= 0 else -ref - 1]
        return (lo, hi) if ref >= 0 else (-hi, -lo)

    def span(expr):
        low = high = expr.offset
        for ref, coeff in zip(expr.vars, expr.coeffs):
            lo, hi = bounds(ref)
            low, high = low + min(coeff * lo, coeff * hi), high + max(coeff * lo, coeff * hi)
        return low, high

    def tighten():
        """target = max(exprs): the target lies between the largest lower and the largest upper bound."""
        changed = False
        for constraint in model.proto.constraints:
            if not constraint.has_lin_max() or constraint.enforcement_literal or len(constraint.lin_max.target.vars) != 1:
                continue
            spans = [span(e) for e in constraint.lin_max.exprs]
            if not spans:
                continue
            target = constraint.lin_max.target
            ref, coeff, offset = target.vars[0], target.coeffs[0], target.offset
            lo, hi = max(x for x, _ in spans), max(y for _, y in spans)
            index = ref if ref >= 0 else -ref - 1
            sign = coeff if ref >= 0 else -coeff
            if abs(sign) != 1:
                continue
            new = (lo - offset, hi - offset) if sign == 1 else (offset - hi, offset - lo)
            old = known[index]
            merged = (max(old[0], new[0]), min(old[1], new[1]))
            if merged != old and merged[0] <= merged[1]:
                known[index], changed = merged, True
        return changed
    for _ in range(4):  # the builders nest these flags at most a couple of levels deep
        if not tighten():
            break

    def fixed_true(literal):
        lo, hi = bounds(literal) if literal >= 0 else (1 - bounds(-literal - 1)[1], 1 - bounds(-literal - 1)[0])
        return lo == hi == 1
    loosened = 0
    for index, constraint in enumerate(model.proto.constraints):
        strict_floor = constraint.name in exempt and not (
            constraint.has_linear() and constraint.linear.domain[0] < 0)
        if index in exempt_indexes or strict_floor or not all(fixed_true(literal) for literal in constraint.enforcement_literal):
            continue
        if constraint.has_linear() and len(constraint.linear.domain) == 2:
            low = high = 0
            for ref, coeff in zip(constraint.linear.vars, constraint.linear.coeffs):
                lo, hi = bounds(ref)
                low, high = low + min(coeff * lo, coeff * hi), high + max(coeff * lo, coeff * hi)
            floor, ceiling = list(constraint.linear.domain)
            if high < floor or low > ceiling:
                constraint.linear.domain.clear()
                constraint.linear.domain.extend([min(floor, high), max(ceiling, low)])
                loosened += 1
        elif constraint.has_at_most_one() and sum(fixed_true(literal) for literal in constraint.at_most_one.literals) > 1:
            # Two kept duties already overlap: nothing else in the group may be chosen.
            free = [-literal - 1 for literal in constraint.at_most_one.literals if not fixed_true(literal)]
            constraint.clear_at_most_one()
            constraint.bool_and.literals.extend(free)
            loosened += 1
        elif constraint.has_bool_or() and all(fixed_true(-literal - 1) for literal in constraint.bool_or.literals):
            constraint.clear_bool_or()  # a constant shortfall in kept work (Python folded it to False)
            loosened += 1
    return loosened


def runs(rows, key):
    """Merge equivalent rows in each time slot into a choice equal to their sum.

    Rows are sorted by start/end; sharing a non-None key must mean the same
    rule-relevant facts and mutually exclusive choices. Regular/overtime forms
    can interleave those keys across parallel clinics, so adjacency is not enough.
    """
    merged, groups, last_times = [], {}, None
    for row in rows:
        times = row[2], row[3]
        if times != last_times:
            groups = {}
            last_times = times
        # Kept focus duties may already overlap: their choices are both one,
        # so they must stay separate in the latest-end sweep and advisory bounds.
        domain = row[4].domain
        k = None if domain.min() == domain.max() == 1 else key(row)
        if k is not None and k in groups:
            groups[k].append(row)
        else:
            group = [row]
            merged.append(group)
            if k is not None:
                groups[k] = group
    return [group[0] if len(group) == 1 else (*group[0][:4], sum(row[4] for row in group)) for group in merged]


def supervised_goal(policy):
    rule = policy['definition']['rule']
    return rule['kind'] == 'balance_goal' and (rule['metric'] == 'supervision' or
            rule['metric'] == 'pair_exposure' and rule.get('pairing') == 'supervised')


def mix(parts):
    """Soft parts with integer multipliers up to a million; apply them only after the objective bound check."""
    ratios = [(weight / max(1, worst) if weight else 0, terms) for weight, worst, terms in parts if terms]
    top = max((r for r, _ in ratios), default=0) or 1
    return [(max(1, round(1_000_000 * r / top)), terms) for r, terms in ratios if r]


def objective_bound(best, tolerance):
    """The runtime and structural proof share the exact tier-tolerance rounding."""
    return best + int(best * Fraction(str(tolerance)) / 100)


def canonical_order(data):
    """Same meaning, same schedule: the model is built walking people, sessions and rules in order and CP-SAT breaks ties
    by it, so that order is by id, never the order rows happened to come back from the database (an update moves a
    Postgres row, so editing an unrelated field such as a pager number used to change the rota)."""
    data['people'] = dict(sorted(data['people'].items()))
    data['shifts'] = sorted(data['shifts'], key=lambda s: s['id'])
    data['policies'] = sorted(data['policies'], key=lambda p: p['id'])
    return data


def _proposal_checks(data, selected, tolerated, gap_before, *, editable=None):
    shifts = [shift for shift in data['shifts'] if not inactive(shift)]
    by_id, local = {s['id']: s for s in shifts}, ZoneInfo(data['organisation']['timezone'])
    editable = set(selected if editable is None else editable)
    workspace_id = (data.get('workspace') or {}).get('id')
    context_ids = {s['id'] for s in shifts if s['id'] not in editable and s.get('workspace_id') and workspace_id
                   and s['workspace_id'] != workspace_id}

    def where(f):
        shift = by_id.get(f['shift_id'])
        person = data['people'].get(f['person_id'] or '', {}).get('display_name')
        label = shift and f"{shift['type_snapshot']['name']} {aware(shift['start']).astimezone(local):%a %d %b %H:%M}"
        return ' · '.join(x for x in (label, person) if x)
    # Only problems inside the chosen sessions block a run. A problem elsewhere in a large rota (a rest breach on a
    # kept duty last week) is reported with the proposal, not a dead end: the run cannot change it, and the final
    # validation below still refuses any proposal that adds or worsens a problem (unchanged ones are tolerated).
    scope_shifts = [s for s in shifts if s['id'] in selected]
    scope_from = min((aware(s['start']) for s in scope_shifts), default=None)
    scope_to = max((aware(s['end']) for s in scope_shifts), default=None)

    def in_scope(f):
        if f.get('shift_id') in by_id:
            return f['shift_id'] in selected
        return not f.get('start') or not f.get('end') or scope_from is None or overlaps(
            aware(f['start']), aware(f['end']), scope_from, scope_to)
    def issue(f):
        row = {**{k: f[k] for k in ('code', 'shift_id', 'person_id', 'start', 'end', 'rule_id')},
               'message': (where(f) + ': ' if where(f) else '') + f['message'], 'detail': f['detail'], 'protected': True}
        if f.get('shift_id') in context_ids:
            shift = by_id[f['shift_id']]
            month = aware(shift['start']).astimezone(local).strftime('%B')
            row['context'] = True
            row['message'] = f'{month} (context, fixed): {row["message"]}'
        return row

    def issues_where(check, applies):
        return [issue(f) for f in (check or {}).get('findings', [])
                if any(applies(observation) for observation in finding_observations({'findings': [f]}))]

    def kept_finding(f):
        shift = by_id.get(f.get('shift_id'))
        policy = next((p for p in data.get('policies', ()) if p['id'] == f.get('rule_id')), None)
        if (shift and shift.get('locked') and policy
                and policy['definition']['severity'] == 'mandatory'
                and policy['definition']['rule']['kind'] == 'supervision_limit'
                and f.get('code') == 'SUPERVISOR_SCOPED_CAPACITY'):
            return False
        if citation_finding_key(f) in tolerated:
            return True
        detail = f.get('detail') or {}
        previous = gap_before.get(citation_finding_key({**f, 'detail': {**detail, 'available': None}}))
        return previous is not None and detail.get('available', -1) >= previous

    return in_scope, issues_where, kept_finding


def _prepare(snapshot, time_budget_seconds, workers, control, options, feasibility_only, diagnostic_proposal, deterministic=False, _on_model=None, _on_objectives=None, _diagnostic_continuation=False, _candidate_evidence=None):
    started = time.monotonic()
    result = {'algorithm_version': COMPILER_VERSION, 'solver_version': solver_version,
              'input_fingerprint': snapshot['fingerprint'], 'additions': [], 'removals': [],
              'effective_options': options.model_dump(mode='json'),
              'solver_workers': workers, 'diagnostic_proposal': diagnostic_proposal,
              'weighted_status': 'UNKNOWN', 'optimality_proven': False}
    control.stage("checking")
    def publish_issues(findings, classification, stage='checking'):
        if findings:
            control.context['precheck_issues'] = precheck_issues(findings, classification,
                previous=control.context.get('precheck_issues', []))
            control.stage(stage)

    issues = compatibility_issues({**snapshot, '_check_budget': control.check})
    if issues:
        publish_issues(issues, 'blocking')
        return {**result, 'status': 'unsupported_rules', 'issues': issues}
    period = snapshot['workspace']
    focus = set(snapshot['focus_ids'])
    selected = set(selected_session_ids(snapshot))
    # The limit is on the sessions being scheduled, so a long rota is scheduled a month at a time; sessions outside
    # the chosen month keep their assignments and still count for the rules.
    days = session_span_days([s for s in snapshot['shifts'] if s['id'] in selected])
    if not selected <= focus or not days <= PLANNING_LIMITS['max_period_days'] + 1 or len(selected) > PLANNING_LIMITS['max_sessions'] or len(snapshot['people']) > PLANNING_LIMITS['max_people']:
        return {**result, 'status': 'unsupported_size', 'message': 'Choose up to a month of sessions (31 days), 1,500 duties and 500 people at a time.'}
    # Only ordering and top-level preparation state are mutable here. Copy the
    # person records because tag materialisation writes their tag lists; avoid
    # cloning every nested shift/policy row on every probe.
    data = canonical_order({**snapshot,
                            'people': {pid: dict(person) for pid, person in snapshot['people'].items()},
                            'shifts': [{**shift, 'participants': [dict(participant)
                                                                    for participant in shift.get('participants', [])]}
                                       for shift in snapshot['shifts']],
                            'policies': list(snapshot['policies'])})
    data['_check_budget'] = control.check
    data['_categories_cache'] = {}
    data['_capabilities_cache'] = {}
    data['_qualification_cache'] = {}
    data['_qualification_failures_cache'] = {}
    data['_qualification_evidence_cache'] = data['_qualification_cache']
    data['_participation_cache'] = {}
    data['_series_candidate_cache'] = {}
    data['_candidate_absence_cache'] = {}
    from .date_sets import prepare_date_sets
    data['_date_set_cache'] = prepare_date_sets(data)
    data = with_series_state(data, cache=True)
    data['generation_options'] = options.model_dump(mode='json')
    data['_scope_interval_cache'] = {}
    data['_bucket_keys_cache'] = {}
    data['_person_rows'] = index_person_rows(data)
    data['_person_dates_cache'] = {}
    data['_line_index_cache'] = {}
    data['_scope_membership_index'] = defaultdict(list)
    for field, rows in (('programme_id', data.get('programme_memberships', [])),
                        ('rotation_type_id', data.get('rotations', []))):
        for row in rows:
            data['_scope_membership_index'][field, row['person_id'], row.get(field)].append(row)
    from .conditions import resolved_rule_context
    data = resolved_rule_context(data)
    relative_fair_share = not feasibility_only and options.scoring.weights.fairness > 0 and any(
        p['definition']['rule']['kind'] == 'balance_goal'
        and p['definition']['rule'].get('aim', 'equal_share') in {'equal_share', 'max_min'}
        and p['definition'].get('strength') != 'off'
        for p in data['policies'])
    mode_balance = []
    if not feasibility_only and options.scoring.weights.preferences > 0:
        mode_balance = [p for p in data['policies']
                        if p['definition']['rule']['kind'] == 'allocation_preference'
                        and p['definition']['rule'].get('balance_by') == 'service_mode'
                        and p['definition'].get('strength') != 'off']
    data['_candidate_evidence_cache'] = {}
    data['_candidate_evidence_policy_source'] = data['policies']
    data['_policy_name_static'] = {}
    leadership.prepare(data)
    shifts = [s for s in data['shifts'] if not inactive(s)]
    setup_issues = []
    def setup_issue(code, message, shift=None, person=None, a=None, b=None, severity='error', rule=None, extra=None):
        if severity == 'error':
            setup_issues.append({'code': code, 'message': message, 'shift_id': shift,
                'start': a.isoformat() if a else None, 'end': b.isoformat() if b else None, 'detail': extra})
    validation_started = time.monotonic()
    for shift in shifts:
        control.check()
        if shift['id'] in selected:
            validate_session_setup(data, shift, shifts, shift['type_snapshot']['definition'], setup_issue)
    setup_seconds = time.monotonic() - validation_started
    if setup_issues:
        publish_issues(setup_issues, 'blocking')
        return {**result, 'status': 'invalid_inputs', 'issues': setup_issues,
                'phase_timings': {'preparation_seconds': time.monotonic() - started, 'search_seconds': 0}}
    # Existing publication content and explicitly protected work are never rebalanced.
    editable = {s['id'] for s in shifts if s['id'] in focus & selected and not s['locked']
                and not period.get('base_publication_id')}
    # Work auto-schedule must keep (fixed, locked, outside the chosen sessions) and session-level setup it
    # cannot change (opening hours, resource pools) are checked now, so a problem there is reported as
    # what it is rather than surfacing after the search as a solver/validator disagreement. Rule checks
    # (feasibility only) keep reporting a rule that contradicts protected work as a requirements conflict.
    protected = {**data, 'shifts': [{**s, 'participants': [p for p in s['participants'] if s['id'] not in editable
                 or keep_assignment(data, s, p)]} for s in data['shifts']]}
    keep_all = all(s['participants'] == kept['participants'] for s, kept in zip(data['shifts'], protected['shifts']))
    fillable = {'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SCOPED_COVERAGE_GAP', 'QUOTA_BELOW_MINIMUM',
                'SUPERVISION_PAIRING', 'SUPERVISION_GAP', 'LEAD_GAP'}
    tolerated, gap_before = set(), {}
    in_scope, issues_where, kept_finding = _proposal_checks(data, selected, tolerated, gap_before, editable=editable)
    validation_started = time.monotonic()
    # Strict probes still need the fixed-context findings so unchanged
    # published/locked breaches can be reported without becoming a false model
    # conflict.  The selected editable sessions remain strict below.
    protected_validation = evaluate(protected, explain_findings=False)
    control.reserve_seconds = validation_reserve(data, setup_seconds if feasibility_only else time.monotonic() - validation_started)
    kept_findings = [f for f in finding_observations(protected_validation or {}) if f['severity'] == 'error']
    repairable_attachments = {(s['id'], p['person_id']) for s in shifts if s['id'] in editable
                             for p in s['participants'] if not (assignment_fixed(s, p) or protects_existing(data, s, p))}
    locked_ids = {s['id'] for s in shifts if s['locked'] and s['id'] in selected}
    locked_staffing = [f for f in coverage_findings(protected_validation or {'findings': []})
                      if f['severity'] == 'error' and f['shift_id'] in locked_ids and f['code'] in {
        'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SCOPED_COVERAGE_GAP', 'LEAD_GAP'}]
    locked_staffing.extend(f for f in kept_findings
                            if f['severity'] == 'error' and f['shift_id'] in locked_ids
                            and f['code'] == 'SUPERVISOR_SCOPED_CAPACITY'
                            and any(p['id'] == f.get('rule_id')
                                    and p['definition']['severity'] == 'mandatory'
                                    and p['definition']['rule']['kind'] == 'supervision_limit'
                                    for p in data.get('policies', ())))
    locked_gap_ids = {f['shift_id'] for f in locked_staffing}
    kept_issues = [f for f in kept_findings if f['code'] not in fillable and in_scope(f)
                   and not (f['code'] == 'ATTACHMENT_PAIRING'
                            and (f['shift_id'], f['person_id']) in repairable_attachments)]
    assignment_limits = {'MAXIMUM_PARTICIPANTS', 'SCOPED_PARTICIPANT_LIMIT'}
    configuration = [i for i in kept_issues if i['code'] == 'ATTACHMENT_PAIRING'
                     or not i.get('person_id') and i['code'] not in assignment_limits]
    if configuration:
        publish_issues(configuration, 'blocking')
        configuration_keys = {citation_finding_key(f) for f in configuration}
        return {**result, 'status': 'invalid_inputs',
                'issues': issues_where(protected_validation, lambda f: citation_finding_key(f) in configuration_keys),
                'message': 'Fix these first: they are in work or settings auto-schedule must keep.',
                'phase_timings': {'preparation_seconds': time.monotonic() - started, 'search_seconds': 0}}
    outside = [f for f in kept_findings if not in_scope(f)]
    context_ids = {s['id'] for s in shifts if s['id'] not in editable and s.get('workspace_id') and period.get('id')
                   and s['workspace_id'] != period['id']}
    if any(f['code'] not in fillable or f.get('shift_id') in context_ids for f in outside):
        outside_keys = {citation_finding_key(f) for f in outside
                        if f['code'] not in fillable or f.get('shift_id') in context_ids}
        result['outside_scope_issues'] = issues_where(protected_validation, lambda f: citation_finding_key(f) in outside_keys)
        publish_issues([f for f in outside if f['code'] not in fillable or f.get('shift_id') in context_ids], 'outside_scope')
    protected_conflicts = [f for f in kept_findings if in_scope(f) and (
        f.get('person_id') and f['code'] not in fillable or f['code'] in assignment_limits
        or f.get('shift_id') in selected - editable)
        and not (f['code'] == 'ATTACHMENT_PAIRING'
                 and (f['shift_id'], f['person_id']) in repairable_attachments)]
    if protected_conflicts:
        protected_keys = {citation_finding_key(f) for f in protected_conflicts}
        result['issues'] = issues_where(protected_validation, lambda f: citation_finding_key(f) in protected_keys)
        publish_issues(protected_conflicts, 'protected')
    tolerated.update(citation_finding_key(f) for f in [*outside, *protected_conflicts])
    kept_sessions = {f['shift_id'] for f in protected_conflicts}
    # Greedy repair uses the original generation boundaries and can move its own suggestions.
    greedy_repair = snapshot.get('_greedy_required_cover', False)
    generating = not feasibility_only or greedy_repair
    greedy_placements = snapshot.get('_greedy_placements', frozenset())
    if generating:
        # Fixed usual pools cannot substitute absent members; retain only existing gaps as visible diagnostics.
        kept_sessions.update(s['id'] for s in shifts if s['id'] in selected
            and series_definition(s).get('usual_assignees_fixed') and s.get('usual_assignee_unavailability'))
    retained_gap_codes = {'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SCOPED_COVERAGE_GAP', 'LEAD_GAP'}
    original_validation = evaluate({**snapshot, '_check_budget': control.check}, explain_findings=False) if kept_sessions else None
    retained_gaps = [f for f in coverage_findings(original_validation or {}) if f['severity'] == 'error'
        and f['code'] in retained_gap_codes and f.get('shift_id') in kept_sessions] if kept_sessions else []
    retained_gap_sessions = {f['shift_id'] for f in retained_gaps}
    retained_gap_sessions.update(locked_gap_ids)
    tolerated.update(citation_finding_key(f) for f in retained_gaps)
    tolerated.update(citation_finding_key(f) for f in locked_staffing)
    gap_before.update({citation_finding_key({**f, 'detail': {**f['detail'], 'available': None}}): f['detail']['available']
                       for f in retained_gaps if 'available' in (f.get('detail') or {})})

    if retained_gaps:
        retained_keys = {citation_finding_key(f) for f in retained_gaps} - {citation_finding_key(f) for f in protected_conflicts}
        publish_issues([f for f in retained_gaps if citation_finding_key(f) in retained_keys], 'protected')
        displayed = [*result.get('issues', []), *issues_where(original_validation, lambda f: citation_finding_key(f) in retained_keys)]
        result['issues'] = list({tuple(f.get(k) for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start')): f
                                for f in displayed}.values())
    control.stage('preparing')
    data['_policy_applies_cache'] = {}  # model building only; the final validation below runs without it
    data['_allocation_match_cache'] = {}
    data['_workload_matches_cache'] = {}  # regular and overtime choices share the same dated scope facts
    has_overtime = any(p['definition']['rule']['kind'] == 'allocation_overtime' for p in data['policies'])
    model = cp_model.CpModel()
    choices, participants, evidence, overtime_choices = {}, {}, [], {}
    reusable_evidence = {(row['shift_id'], row['person_id']): row for row in _candidate_evidence or []}
    result['candidate_evidence_reused'] = 0
    decisions = set()  # (sid, pid) whose assignment the search decides
    by_person, by_shift = defaultdict(list), defaultdict(list)
    # Coverage stays first; advisory instances feed the same normalized components as the score.
    coverage, desired_coverage, diagnostic_minima, coverage_score_rows = [], [], [], []
    staffing_targets = [] if relative_fair_share else None
    staffing_choices = []
    capped = False
    cross_duty_demand = (relative_fair_share or bool(mode_balance)) and any(
        rule['kind'] == 'team_lead_requirement'
        or rule['kind'] == 'block_quota' and rule['comparison'] != 'maximum'
        or (options.scoring.weights.fairness > 0 and rule['kind'] == 'balance_goal'
            and rule.get('aim') in {'target', 'min_per_pair'})
        for rule in (p['definition']['rule'] for p in data['policies']))
    coverage_floor_rows = []
    diagnostic_domains = {}
    live_coverage = []
    from .cp_sat_score import ScoreModel, SCORE_SCALE, rule_component, stability_component, coverage_component, weighted_component
    from .score import rule_copy_key
    score_math = ScoreModel(model, options.scoring.model_dump())
    score_breaches = defaultdict(list)
    quota_items, quota_strengths = [], []
    strengths = options.scoring.strengths.model_dump()
    data['_allocation_strengths'] = strengths

    def append_coverage_score_row(cover, requirements, duration):
        coverage_score_rows.append((cover, max((r['minimum'] for r in requirements), default=0),
            [r['target'] for r in requirements if r['target'] is not None], duration))

    def append_objective(terms, term):
        terms.append(term)
        return terms, term

    def penalise(expression, policy, worst=1, bound=1, subject=None):
        if feasibility_only:
            return
        if policy['definition']['severity'] == 'mandatory':
            # Keep diagnostic quota pressure independent of advisory score normalization.
            return append_objective(diagnostic_minima, score_math.positive(expression))
        subject = subject or policy.get('_score_person')
        key = rule_copy_key(policy), subject
        return append_objective(score_breaches[key], score_math.positive(expression))
    # Only kept work occupies supervisors while candidate assignments are being prepared.
    data['_supervision_commitments'] = supervision_commitments(protected)
    for done, shift in enumerate(shifts):
        control.stage('preparing', prepared_sessions=done, total_sessions=len(shifts))
        sid = shift['id']
        original = {p['person_id']: p for p in shift['participants']}
        a, b = aware(shift['start']), aware(shift['end'])
        definition = shift['type_snapshot']['definition']
        restriction_policies = []
        for policy in data['policies']:
            if (policy['definition']['rule']['kind'] != 'restriction'
                or policy['definition']['severity'] != 'mandatory'
                or policy['department_id'] != data['types'][shift['assignment_type_id']]['department_id']
                or not within_dates(policy, a, b, data['organisation']['timezone'])):
                continue
            selector = session_scope(policy['definition']['scope'])
            if scope_matches_throughout(data, selector, None, shift, a, b):
                restriction_policies.append(policy)
        candidate_data = {**data, 'policies': restriction_policies}
        fixed_people = {pid for pid, participant in original.items() if sid not in editable or (
            keep_assignment(data, shift, participant) if generating else assignment_fixed(shift, participant))}
        reports = {}
        for pid in focused_candidate_people(data, shift):
            control.check()
            if generating and (not population_allows(data, shift, pid)
                    or pid not in fixed_people and (sid not in editable
                        or pid not in original and not series_candidate_allowed(data, shift, pid))):
                continue
            report = reusable_evidence.get((sid, pid))
            if (report is None or report.get('fixed') or report.get('start') != shift['start']
                    or report.get('end') != shift['end']):
                report = candidate_evidence(data if definition.get('extra_cover', False) else candidate_data, pid, shift)
            else:
                result['candidate_evidence_reused'] += 1
            if pid in fixed_people:
                reason = ('PUBLISHED_ASSIGNMENT' if period.get('base_publication_id') or period.get('status') == 'published'
                          else 'SESSION_LOCKED' if shift['locked']
                          else 'OUTSIDE_EDITABLE_SCOPE' if sid not in editable
                          else 'ASSIGNMENT_FIXED' if assignment_fixed(shift, original[pid])
                          else 'SCOPE_PROTECTED' if protects_existing(data, shift, original[pid])
                          else 'FILL_GAPS')
                report = {**report, 'fixed': True, 'fixed_reason': reason}
            reports[pid] = report
            evidence.append(report)
        for pid in data['people'] if sid in editable else original:
            control.check()
            if pid not in original and (not population_allows(data, shift, pid) or not series_candidate_allowed(data, shift, pid)):
                continue
            fixed = pid in fixed_people
            if sid in editable and not fixed:
                if not population_allows(data, shift, pid):
                    continue
                report = reports[pid]
                if not report['eligible_and_available']:
                    continue
                role = participation_for(data, pid, shift, a, b)
                participant = {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, snapshot['fingerprint'] + sid + pid)),
                    'shift_id': sid, 'person_id': pid, 'start': shift['start'], 'end': shift['end'],
                    'participation': role, 'overtime': definition.get('extra_cover', False), 'revision': 1,
                    'team_lead': bool(original.get(pid, {}).get('team_lead'))}
                if original.get(pid, {}).get('segments'):
                    # Rebalance may move people; it does not edit an existing person's booked breaks.
                    participant.update({k: original[pid][k] for k in ('start', 'end', 'segments')})
                x = model.new_bool_var(f'{sid}:{pid}')
                decisions.add((sid, pid))
                if greedy_repair:
                    model.add_hint(x, int((sid, pid) in greedy_placements))
                elif pid in original:
                    model.add_hint(x, 1)
            else:
                participant, x = {**original[pid]}, model.new_constant(1)
            choices[sid, pid], participants[sid, pid] = x, participant
            if sid in editable and not fixed and definition.get('extra_cover', False):
                overtime_choices[sid, pid] = x
                by_person[pid].append((shift, participant, aware(participant['start']), aware(participant['end']), x))
            elif sid in editable and not fixed and has_overtime and allocation_overtime_allowed(data, shift, pid):
                regular, overtime = model.new_bool_var('regular'), model.new_bool_var('overtime')
                model.add(regular + overtime == x)
                overtime_choices[sid, pid] = overtime
                for is_overtime, choice in ((False, regular), (True, overtime)):
                    by_person[pid].append((shift, {**participant, 'overtime': is_overtime}, a, b, choice))
            else:
                by_person[pid].append((shift, participant, aware(participant['start']), aware(participant['end']), x))
            by_shift[sid].append(pid)

    shortages = direct_coverage_shortages({**data, 'focus_ids': selected}, evidence, editable)
    publish_issues(shortages, 'unresolved', 'preparing')
    if shortages:
        result.update(diagnostics=shortages, diagnostics_complete=False)
    # Overlap applies to the reserved envelope, not just presence/coverage segments.
    # Rows sorted by start: the rows overlapping row j are the earlier ones still running at its start, and those
    # all overlap each other too (an interval graph), so focus rows get one at-most-one per maximal clique instead of
    # one constraint per pair. A pair with a row outside the focus (fixed work) stays a pair: two such rows are never
    # exclusive of each other. Same-duty regular/overtime forms are exclusive already, so a clique may hold both.
    exclusive_rest, exclusive_transition = set(), set()
    for _, entries in sorted(by_person.items()):
        control.check()
        entries.sort(key=lambda row: (row[2], row[3]))
        running, pending = [], False  # pending: the current clique is maximal so far and not yet added
        for row in entries:
            still = [r for r in running if r[3] > row[2]]
            if pending and len(still) < len(running):
                model.add_at_most_one([r[4] for r in running if r[0]['id'] in focus])
                pending = False
            for r in still:
                if (row[0]['id'] in focus) != (r[0]['id'] in focus):
                    model.add(row[4] + r[4] <= 1)
            running = still + [row]
            pending = pending or row[0]['id'] in focus and sum(r[0]['id'] in focus for r in running) > 1
        if pending:
            model.add_at_most_one([r[4] for r in running if r[0]['id'] in focus])

    # The canonical latest-end sweep: nested work cannot replace a later occupied end.
    policies_by_severity = sorted(data['policies'], key=lambda p: p['definition']['severity'] != 'mandatory')
    pair_policies = [p for p in policies_by_severity if p['definition']['rule']['kind'] in {'min_rest', 'forbidden_transition'}]
    focal_of = {}  # (policy, person) -> the focus duties the rule applies to
    for policy in pair_policies:
        control.check()
        rule, named = policy['definition']['rule'], named_people(policy['definition']['scope'])
        for pid, entries in sorted(by_person.items()):
            if named is not None and pid not in named:
                continue  # the rule names other people
            # Regular and overtime forms of one duty share the answer: check each duty once.
            candidates = {(s['id'], a, b): s for s, p, a, b, _ in entries if s['id'] in focus
                          and (rule.get('include_overtime', True) or not p['overtime'])}
            focal_of[id(policy), pid] = {sid for (sid, a, b), s in sorted(candidates.items()) if policy_applies(data, policy, pid, s, a, b)}
    # Per person, the pair rules run over "runs": focus duties at identical times that every pair rule treats alike
    # (type, overtime form, which rules apply) merge into one row whose choice is their sum. Rows at identical times
    # exclude each other, so a pair constraint over two runs is exactly the constraints over all their row pairs, and
    # parallel clinics cost one pair per pair of time slots instead of one per pair of sessions.
    timeline = {}
    for pid, entries in sorted(by_person.items()):
        control.check()
        flags = [focal_of[id(p), pid] for p in pair_policies if focal_of.get((id(p), pid))]
        if not flags:
            continue
        ordered = entries + [({'id': 'external:' + r['id']}, {}, aware(r['start']), aware(r['end']), model.new_constant(1))
            for r in person_rows(data, 'availability', pid) if r['person_id'] == pid and r['kind'] == 'external_commitment']
        ordered.sort(key=lambda row: (row[2], row[3]))
        timeline[pid] = runs(ordered, lambda row: (row[2], row[3], row[0]['assignment_type_id'], row[1]['overtime'],
                                                  tuple(row[0]['id'] in f for f in flags))
                             if row[0]['id'] in focus and row[2] < row[3] else None)
    for policy in policies_by_severity:
        control.check()
        rule = policy['definition']['rule']
        if rule['kind'] not in {'restriction', 'min_rest', 'forbidden_transition'}:
            continue
        named = named_people(policy['definition']['scope'])
        mandatory = policy['definition']['severity'] == 'mandatory'
        for pid, entries in sorted(by_person.items()):
            control.check()
            if named is not None and pid not in named:
                continue  # the rule names other people
            if rule['kind'] == 'restriction':
                if policy['definition']['severity'] != 'advisory':
                    continue  # Mandatory restrictions are excluded in shared preparation.
                for shift, _, a, b, x in entries:
                    if shift['id'] in focus:
                        for c, d in applicable_restriction_intervals(data, policy, pid, shift, a, b):
                            if not exception_for(data, policy, pid, c, d):
                                penalise(x, policy, subject=pid)
                continue
            focal = focal_of.get((id(policy), pid))
            if not focal:
                continue
            rows = timeline[pid]
            if rule['kind'] == 'forbidden_transition':
                local = ZoneInfo(data['organisation']['timezone'])
                for i, (s, sp, a, b, x) in enumerate(rows):
                    if s.get('assignment_type_id') not in rule['from_type_ids']:
                        continue
                    next_day = a.astimezone(local).date() + timedelta(days=1)
                    for j, (t, tp, c, d, y) in enumerate(rows):
                        if (t.get('assignment_type_id') not in rule['to_type_ids'] or c.astimezone(local).date() != next_day
                            or s['id'] not in focal and t['id'] not in focal
                            or rule.get('overtime_only') and not (sp['overtime'] or tp['overtime'])
                            or exception_for(data, policy, pid, a, d)
                            or a < d and c < b and (s['id'] in focus or t['id'] in focus)):  # overlapping: exclusive already
                            continue
                        pair = pid, min(i, j), max(i, j)
                        if mandatory:
                            model.add(x + y <= 1).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[s['id'], t['id']], start=a, end=d))
                            exclusive_transition.add(pair)
                        elif pair not in exclusive_transition:
                            violation = model.new_bool_var('transition_preference')
                            model.add(x + y <= 1 + violation)
                            model.add(x + y >= 2).only_enforce_if(violation)
                            penalise(violation, policy, subject=pid)
                for r in person_rows(data, 'availability', pid):
                    if r['person_id'] != pid or r['kind'] != 'external_commitment':
                        continue
                    day = aware(r['start']).astimezone(local).date()
                    for s, _, a, b, x in rows:
                        target_day = a.astimezone(local).date()
                        if s['id'] in focal and (
                            s['assignment_type_id'] in rule['to_type_ids'] and (target_day - day).days == 1
                            or s['assignment_type_id'] in rule['from_type_ids'] and (day - target_day).days == 1):
                            if mandatory:
                                model.add(x == 0).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[s['id']], start=a, end=b))
                            else:
                                penalise(x, policy, subject=pid)
                continue
            def exclusive(u, v):
                """Rows u < v are never both chosen: overlapping work, or the two forms of one duty."""
                (s, _, _, b, _), (t, _, c, _, _) = rows[u], rows[v]
                return s['id'] == t['id'] or c < b and (s['id'] in focus or t['id'] in focus)

            def rested(u, v):
                """The rule keeps rows u < v apart: too close, one of them in its scope, no exception."""
                (s, _, _, b, _), (t, _, c, _, _) = rows[u], rows[v]
                return (duration_hours(b, c) < rule['hours'] and (s['id'] in focal or t['id'] in focal)
                        and not exception_for(data, policy, pid, min(b, c), max(b, c)))
            window_end = -1  # the rows up to here are in the last window added
            for i, (s, _, a, b, x) in enumerate(rows):
                blockers = None
                last = i  # the rows starting too soon after this one ends
                while last + 1 < len(rows) and duration_hours(b, rows[last + 1][2]) < rule['hours']:
                    last += 1
                window = range(i, last + 1)
                if mandatory and window_end < last > i and all(
                        exclusive(u, v) or rested(u, v) for u in window for v in window if u < v):
                    # When every pair in the window is kept apart, two chosen rows in it would include two with
                    # nothing chosen between them: the pairs mean exactly "at most one of the window".
                    model.add(sum(row[4] for row in rows[i:last + 1]) <= 1).with_name(policy_name(
                        policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in rows[i:last + 1]], start=a, end=rows[last][3]))
                    exclusive_rest.update((pid, u, v) for u in window for v in window if u < v)
                    window_end = last
                    continue
                for j in range(i + 1, last + 1):
                    if mandatory and j <= window_end or exclusive(i, j) or not rested(i, j):
                        continue  # in the last window, or exclusive already, or not the rule's concern
                    if blockers is None:
                        blockers = workload_gap_blockers(rows, i)
                    expression = x + rows[j][4] - sum(choice for k, choice in blockers if k < j)
                    if mandatory:
                        model.add(expression <= 1).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[s['id'], rows[j][0]['id']], start=a, end=rows[j][3]))
                        exclusive_rest.add((pid, i, j))
                    elif (pid, i, j) not in exclusive_rest and (pid, i, j) not in exclusive_transition:
                        violation = model.new_bool_var('rest_preference')
                        model.add(expression <= 1 + violation)
                        model.add(expression >= 2).only_enforce_if(violation)
                        penalise(violation, policy, subject=pid)

    zone = data['organisation']['timezone']
    authority = {local_midnight(date.fromisoformat(r[field]), zone)
        for rows in (data['memberships'], data['grants'], data['policies'], data['eligibilities'],
                     data.get('programme_memberships', []), data.get('rotations', []),
                     list(data.get('programmes', {}).values()), list(data.get('cohorts', {}).values()),
                     list(data.get('rotation_types', {}).values()), list(data['categories'].values()), list(data['capabilities'].values()))
        for r in rows for field in ('starts_on', 'ends_on') if r.get(field)}
    peer_bounds = {aware(s[k]) for s in shifts for k in ('start', 'end')}
    peer_bounds.update(aware(p[k]) for p in participants.values() for k in ('start', 'end'))
    shift_lookup = {s['id']: s for s in shifts}
    @cache
    def presence(sid, pid):
        return presence_intervals(participants[sid, pid])

    @cache
    def is_supervisor(sid, pid, a, b):
        return qualified_supervisor(data, shift_lookup[sid], participants[sid, pid], a, b)

    def present_at(shift, a, b, available=False):
        return present_people(shift['id'], a, b, available)

    @cache
    def present_people(sid, a, b, available):
        # Shared coverage revisits the same intervals; inputs stay fixed during model construction.
        shift = shift_lookup[sid]
        people = [pid for pid in by_shift[shift['id']] if any(lo <= a and b <= hi
                  for lo, hi in presence(sid, pid))]
        if available:
            # New choices already passed canonical whole-duty evidence; only kept work needs another check.
            people = [pid for pid in people if (sid, pid) in decisions or (qualified(data, pid, shift, shift['type_snapshot']['definition'], a, b)
                and not rotation_blocks(data, pid, a, b) and not restriction_blocks(data, pid, shift, a, b)
                and not any(r['person_id'] == pid and overlaps(a, b, aware(r['start']), aware(r['end']))
                            for r in person_rows(data, 'availability', pid)))]
        return people

    def distinct(terms):
        grouped = defaultdict(list)
        for pid, x in terms:
            grouped[pid].append(x)
        counts = {}
        for pid, xs in sorted(grouped.items()):
            if len(xs) == 1:
                counts[pid] = xs[0]
            else:
                counts[pid] = model.new_bool_var('distinct_person')
                model.add_max_equality(counts[pid], xs)
        return counts

    strict_constraints = set()

    def limit(expression, bound, policy, capacity, minutes, kept_gap=False, editable_cover=False, subject=None,
              people=(), sessions=(), start=None, end=None, record_objective=True):
        mandatory = policy['definition']['severity'] == 'mandatory'
        diagnostic_cover = mandatory and policy['definition']['rule']['kind'] == 'minimum_cover' and not feasibility_only
        name = policy_name(policy, data=data, person_ids=people, session_ids=sessions, start=start, end=end) if mandatory else None
        if mandatory and not diagnostic_cover:
            model.add(expression <= bound).with_name(name)
            if editable_cover and policy['definition']['rule']['kind'] == 'minimum_cover':
                strict_constraints.add(len(model.proto.constraints) - 1)
            return
        deficit = model.new_int_var(0, max(0, capacity), 'preference_deficit')
        if mandatory:
            # Keep the dormant deficit in a linear bound so unchanged outside-scope gaps can be tolerated.
            constraint = model.add(expression - deficit <= bound).with_name(name)
            if editable_cover:
                strict_constraints.add(constraint.index)
            if not kept_gap:
                diagnostic_domains[deficit.index] = max(0, capacity)
            if subject in selected:
                pooled_anchors = {sid for sid in sessions if sid in selected} or {subject}
                coverage_weight = len(pooled_anchors)
                coverage_floor_rows.append({'shift_id': subject, 'code': 'SCOPED_COVERAGE_GAP',
                                            'minimum': capacity, 'minutes': minutes,
                                            'weight': coverage_weight, 'start': start.isoformat(), 'end': end.isoformat(),
                                            'rule_id': policy['id']})
                term = minutes * deficit
                return append_objective(coverage, term) if record_objective else (coverage, term)
        else:
            model.add_max_equality(deficit, [0, expression - bound])
            if (staffing_targets is not None and options.scoring.weights.rules
                    and policy['definition']['rule']['kind'] == 'minimum_cover'):
                strength = strengths.get(policy['definition'].get('strength', 'medium'), 0)
                if strength:
                    staffing_targets.append(strength * score_math.ratio(deficit, max(1, capacity)))
            return penalise(deficit, policy, max(1, capacity), capacity, subject=subject)

    staffing = counting_views(data, [p for p in data['policies']
        if p['definition']['rule']['kind'] in {'minimum_cover', 'maximum_participants'}])
    data['_staffing_selection_cache'] = {}
    supervision_limits = counting_views(data, [p for p in data['policies']
        if p['definition']['rule']['kind'] == 'supervision_limit'])

    def conditional_credit(people, others, minimum):
        enough = model.new_bool_var('credit_companions')
        model.add(others >= minimum).only_enforce_if(enough)
        model.add(others < minimum).only_enforce_if(enough.Not())
        credited = {}
        for pid, chosen in people.items():
            credited[pid] = model.new_bool_var('conditional_credit')
            model.add_min_equality(credited[pid], [chosen, enough])
        return credited

    # Required and desired seat-minutes belong to separate lexicographic stages.
    def coverage_floor(expression, minimum, sid, minutes, open_extra=False, staffed=None, code='COVERAGE_GAP',
                       start=None, end=None, rule_id=None):
        may_keep_gap = open_extra or diagnostic_proposal or sid in retained_gap_sessions
        deficit = model.new_int_var(0, minimum, 'unfilled_coverage') if may_keep_gap or not feasibility_only else 0
        if not feasibility_only and not open_extra and sid not in retained_gap_sessions:
            diagnostic_domains[deficit.index] = minimum
        floor = model.add(expression + deficit >= minimum).with_name('duty:' + sid)
        if staffed is not None:
            floor.only_enforce_if(staffed)
        if not isinstance(deficit, int) and sid in selected:
            coverage.append(minutes * deficit)
            coverage_floor_rows.append({'shift_id': sid, 'code': code, 'minimum': minimum, 'minutes': minutes,
                                        'start': start.isoformat(), 'end': end.isoformat(), 'rule_id': rule_id})

    # Team lead: a variable only for people allowed to lead throughout the duty. Existing leads are hints
    # (or constants when kept as they are); fixed people and fixed leads never change; without a lead
    # setting the existing flag rides along with the person.
    lead_terms, lead_vars = {}, {}
    for shift in shifts:
        sid, lead = shift['id'], leadership.summary(data, shift)
        control.check()
        start, end = aware(shift['start']), aware(shift['end'])
        for pid in by_shift[sid]:
            p = participants[sid, pid]
            was_lead, decided = bool(p.get('team_lead')), (sid, pid) in decisions
            may_lead = (lead and sid in editable and leadership.qualified(data, pid, shift, aware(p['start']), aware(p['end']))
                        and (lead['continuity'] != 'whole_duty' or leadership.covers_required(data, p, shift)
                             and leadership.qualified(data, pid, shift, start, end)))
            if not decided and not (may_lead and not was_lead and not (assignment_fixed(shift, p) or protects_existing(data, shift, p))):
                lead_terms[sid, pid] = int(was_lead)  # kept exactly (fixed, locked, or fill-gaps with its lead)
            elif not lead:
                lead_terms[sid, pid] = choices[sid, pid] if was_lead else 0
            elif may_lead:
                if not decided:
                    participants[sid, pid] = dict(p)  # fill-gaps may name a kept person as lead: a changed copy
                var = lead_terms[sid, pid] = lead_vars[sid, pid] = model.new_bool_var(f'lead:{sid}:{pid}')
                model.add(var <= choices[sid, pid])
                model.add_hint(var, int(was_lead))
            else:
                lead_terms[sid, pid] = 0

    for shift in shifts:
        lead = leadership.summary(data, shift)
        if shift['id'] not in editable:
            continue
        local_requirements = [r for r in (lead or {}).get('_requirements', ())
                              if r.get('count_scope', 'duty') == 'duty']
        if local_requirements and lead and lead['continuity'] == 'whole_duty':
            sid = shift['id']
            maximums = [r['maximum'] for r in local_requirements if r['maximum'] is not None]
            if maximums:
                model.add(sum(lead_terms[sid, pid] for pid in by_shift[sid]) <= min(maximums)).with_name('duty:' + sid)
            for pid in by_shift[sid]:
                if not leadership.covers_required(data, participants[sid, pid], shift):
                    model.add(lead_terms[sid, pid] == 0).with_name('duty:' + sid)

    # A concurrent-duty line is one safety obligation over the matching duties.
    # Distinct-person ORs keep a duplicate flag on two duties from counting twice.
    shift_lookup = {s['id']: s for s in shifts}
    shared_seen = set()
    for shift in shifts:
        for group in leadership.concurrent_groups(data, shift, aware(shift['start']), aware(shift['end'])):
            marker = (group['key'], group['start'], group['end'])
            if marker in shared_seen:
                continue
            shared_seen.add(marker)
            if not any(duty['id'] in editable for duty, _ in group['duties']):
                continue
            req = group['requirement']
            leads_by_person = defaultdict(list)
            for duty, duty_req in group['duties']:
                sid = duty['id']
                if sid not in shift_lookup:
                    continue
                for pid in by_shift.get(sid, ()):
                    term = lead_terms.get((sid, pid), 0)
                    if isinstance(term, int) and term == 0:
                        continue
                    if not any(lo <= group['start'] and group['end'] <= hi
                               for lo, hi in presence_intervals(participants[sid, pid])):
                        continue
                    if leadership.matches(data, pid, duty, duty_req, group['start'], group['end']):
                        if req['continuity'] == 'whole_duty' and not leadership.covers_required(data, participants[sid, pid], duty):
                            model.add(term == 0).with_name('duty:' + sid)
                            continue
                        leads_by_person[pid].append(term)
                    else:
                        model.add(term == 0).with_name('duty:' + sid)
            distinct_terms = {}
            for pid, terms in sorted(leads_by_person.items()):
                distinct_terms[pid] = terms[0] if len(terms) == 1 else model.new_bool_var('lead_shared_person')
                if len(terms) > 1:
                    model.add_max_equality(distinct_terms[pid], terms)
            expression = sum(distinct_terms.values())
            if req['minimum']:
                coverage_floor(expression, req['minimum'], min(s['id'] for s, _ in group['duties']),
                               max(1, int((group['end'] - group['start']).total_seconds() // 60)),
                               code='LEAD_GAP', start=group['start'], end=group['end'],
                               rule_id=(req.get('policy') or {}).get('id'))
            if req['maximum'] is not None:
                model.add(expression <= req['maximum']).with_name(
                    'duty:' + min(s['id'] for s, _ in group['duties']))

    pairings = {}
    pairs_by_trainee = defaultdict(list)
    counted_shifts = {}  # sid -> (trainees, supervisor capacities, rule) for shifts paired by counting
    # Rules that care *which* supervisor a trainee has need one variable per trainee-supervisor pair.
    pairing_sensitive = any(
        p['definition']['rule']['kind'] == 'max_duties' and p['definition']['rule'].get('group_by') == 'supervisor'
        or p['definition']['rule']['kind'] == 'allocation_preference' and (
            p['definition']['rule'].get('supervisor_ids') or p['definition']['rule'].get('supervisor_relationship') == 'mentor'
            or p['definition']['rule'].get('balance_by') in {'supervisor', 'supervisor_group'})
        or supervised_goal(p)
        for p in data['policies'])
    # Trainee-consultant attachments (mentorships): per session, each trainee candidate's attached consultants who are
    # candidates too. They pair the two (required: always when both are on; preferred: scored) and keep them together.
    attaching, attachment_cache = bool(data.get('mentorships')), {}

    def attachments_on(shift):
        sid = shift['id']
        if not attaching:
            return {}
        if sid not in attachment_cache:
            start, end = aware(shift['start']), aware(shift['end'])
            found = {}
            for pid in by_shift[sid]:
                if trainee_intervals(data, shift, participants[sid, pid]):
                    here = {q: s for q, s in attached_to(data, pid, shift, start, end).items() if (sid, q) in choices}
                    if here:
                        found[pid] = here
            attachment_cache[sid] = found
        return attachment_cache[sid]

    def counted_pairing(shift, supervision, intervals):
        """Supervisor capacities when any trainee may pair with any supervisor present.

        Then a valid named pairing exists exactly when total capacity covers the trainees (the marriage theorem's
        condition on a complete bipartite graph), so the model counts and names pairs after solving.
        Anything that distinguishes supervisors or trainees keeps the exact pair model (None).
        """
        sid = shift['id']
        spread_present = any(p['definition']['rule']['kind'] == 'spread_evenly'
                             and any(policy_applies(data, p, pid, shift, a, b)
                                     for pid in by_shift[sid] for a, b in intervals)
                             for p in data['policies'])
        if pairing_sensitive or spread_present or attachments_on(shift) or any((assignment_fixed(shift, p) or protects_existing(data, shift, p)) and (
                p['participation'] == 'trainee' or p.get('supervisor_person_id')) for p in shift['participants']):
            return None
        start, end = aware(shift['start']), aware(shift['end'])
        trainees = sorted(pid for pid in by_shift[sid] if participants[sid, pid]['participation'] == 'trainee')
        supervisors = sorted(pid for pid in by_shift[sid]
                             if is_supervisor(sid, pid, start, end))
        caps, named, named_sources = None, None, set()
        for a, b in intervals:
            available = present_at(shift, a, b, True)
            here = [pid for pid in available if is_trainee(data, supervision, participants[sid, pid], a, b)]
            leads = [pid for pid in available if pid not in here
                     and is_supervisor(sid, pid, a, b)]
            if sorted(here) != trainees or sorted(leads) != supervisors:
                return None
            interval_caps, interval_named = {}, None
            for supervisor in leads:
                cap = supervision['trainees_per_supervisor']
                for policy in supervision_limits:
                    rule = policy['definition']['rule']
                    if rule['kind'] != 'supervision_limit' or not policy_applies(data, policy, supervisor, shift, a, b):
                        continue
                    if 'count_bucket' in policy:
                        return None  # Keep an independently attributed constraint for each matching bucket.
                    ranks = set(rule['trainee_category_ids'])
                    if rule['mode'] != 'capacity' or rule.get('overflow_maximum', 0) or ranks and any(
                            not ranks & categories_for(data, pid, a, b) for pid in here):
                        return None
                    if rule['maximum'] < cap:
                        cap, interval_named = rule['maximum'], policy
                        line = policy.get('template_line') or {}
                        named_sources.add((policy['id'], line.get('id'), line.get('line'), line.get('slot')))
                        if len(named_sources) > 1:
                            return None  # Preserve separate source lines in the exact pairing constraints.
                interval_caps[supervisor] = cap
            if caps is not None and caps != interval_caps:
                return None
            caps, named = interval_caps, named or interval_named
        return trainees, caps or {}, named

    from .fill_priority import FillPriority
    fill_priority = FillPriority(model, data, choices, {s['id']: s for s in shifts}, options.allocation_order == 'tiered')
    cover_triggers = {}
    pooled_cover_cells = {}

    @cache
    def rule_boundaries(start, end):
        return {boundary for policy in data['policies']
                for interval in scope_intervals(data, policy['definition']['scope'], None, start, end)
                for boundary in interval}

    for shift in shifts:
        control.check()
        sid = shift['id']
        start, end = aware(shift['start']), aware(shift['end'])
        definition = effective_type_definition(data, shift)
        staffed = None
        if sid in editable and is_open_extra_cover(definition):
            staffed = model.new_bool_var('staffed:' + sid)
            for pid in by_shift[sid]:
                model.add(staffed >= choices[sid, pid])
            model.add(staffed <= sum(choices[sid, pid] for pid in by_shift[sid]))
        bounds = {start, end, *(t for lo, hi, _ in leadership.segments(data, shift, start, end) for t in (lo, hi))} | {t for t in authority | peer_bounds if start < t < end}
        bounds.update(rule_boundaries(start, end))
        for r in definition['requirements']:
            bounds.update(t for t in (start + timedelta(minutes=r['from_minute']),
                start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else end) if start < t < end)
        for pid in by_shift[sid]:
            for a, b in presence(sid, pid):
                bounds.update(t for t in (a, b) if start < t < end)
        if sid not in editable:
            if sid in focus:
                for a, b in zip(sorted(bounds), sorted(bounds)[1:]):
                    interval_requirements = [r for r in definition['requirements']
                        if start + timedelta(minutes=r['from_minute']) <= a and b <= (
                            start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else end)]
                    append_coverage_score_row(sum(choices[sid, pid] for pid in present_at(shift, a, b, True)),
                        interval_requirements, (b - a) // timedelta(microseconds=1))
            continue
        repair_targets = {}
        supervision = definition.get('supervision')
        mutable_people = [pid for pid in by_shift[sid] if (sid, pid) in decisions] if relative_fair_share or mode_balance else []
        whole_duty = bool(mutable_people) and all(presence(sid, pid) == [(start, end)] for pid in mutable_people)
        can_cap = (whole_duty and not cross_duty_demand and not supervision and not definition.get('lead')
                   and not is_open_extra_cover(definition) and all(not r['capabilities'] for r in definition['requirements']))
        places_to_fill = 0
        maximum_places = len(mutable_people)
        needs_pairing = supervision and (bool(attachments_on(shift)) or supervision.get('require_pairing') or any(
            (p['definition']['rule']['kind'] in {'supervision_limit', 'spread_evenly'} or
            p['definition']['rule']['kind'] == 'max_duties' and p['definition']['rule'].get('group_by') == 'supervisor'
            or p['definition']['rule']['kind'] == 'allocation_preference' and (
                p['definition']['rule'].get('supervisor_ids') or p['definition']['rule'].get('supervisor_relationship') == 'mentor'
                or p['definition']['rule'].get('balance_by') in {'supervisor', 'supervisor_group'}) or supervised_goal(p))
            and any(policy_applies(data, p, pid, shift, a, b)
                    for pid in by_shift[sid] for a, b in zip(sorted(bounds), sorted(bounds)[1:]))
            for p in data['policies']))
        pairing = supervision and sid in editable and needs_pairing and counted_pairing(
            shift, supervision, list(zip(sorted(bounds), sorted(bounds)[1:])))
        if pairing:
            trainees, caps, named = pairing
            counted_shifts[sid] = (trainees, caps, named['id'] if named else None)
            constraint = model.add(sum(choices[sid, pid] for pid in trainees)
                                   <= sum(cap * choices[sid, pid] for pid, cap in sorted(caps.items())))
            strict_constraints.add(constraint.index)
            if named:
                constraint.with_name(policy_name(named, data=data, person_ids=[*trainees, *caps], session_ids=[sid], start=start, end=end))
        elif supervision and sid in editable and needs_pairing:
            required_intervals = {pid: trainee_intervals(data, shift, participants[sid, pid]) for pid in by_shift[sid]}
            spread_pairing_for = {}
            for pid, intervals in required_intervals.items():
                spread_pairing_for[pid] = any(
                    policy['definition']['severity'] == 'mandatory'
                    for lo, hi in intervals
                    for policy in session_spread_policies(
                        data['policies'],
                        lambda candidate, member, duty, span_start, span_end:
                            policy_applies(data, candidate, member, duty, span_start, span_end),
                        pid, shift, lo, hi))
            for pid, intervals in required_intervals.items():
                if not intervals:
                    continue
                pairs = []
                for supervisor in by_shift[sid]:
                    if supervisor == pid or not all(is_supervisor(sid, supervisor, a, b) for a, b in intervals):
                        continue
                    pair = model.new_bool_var(f'pair:{sid}:{pid}:{supervisor}')
                    original = next((p for p in shift['participants'] if p['person_id'] == pid), None)
                    if original and (assignment_fixed(shift, original) or protects_existing(data, shift, original)):
                        model.add(pair == int(original.get('supervisor_person_id') == supervisor))
                    pairings[sid, pid, supervisor] = pair
                    pairs_by_trainee[sid, pid].append(pair)
                    model.add(pair <= choices[sid, supervisor])
                    pairs.append(pair)
                for q, strength in sorted(attachments_on(shift).get(pid, {}).items()):
                    pair = pairings.get((sid, pid, q))
                    if pair is None:
                        continue
                    if strength == 'required':  # both on: the trainee is the consultant's
                        model.add(pair >= choices[sid, pid] + choices[sid, q] - 1).with_name('attachment:' + sid)
                if supervision.get('require_pairing') or spread_pairing_for.get(pid):
                    constraint = model.add(sum(pairs) == choices[sid, pid]).with_name('duty:' + sid)
                    strict_constraints.add(constraint.index)
                else:
                    model.add(sum(pairs) <= choices[sid, pid])
        for a, b in zip(sorted(bounds), sorted(bounds)[1:]):
            minutes = max(1, (b - a) // timedelta(minutes=1))
            present = present_at(shift, a, b)
            available = present_at(shift, a, b, True)
            caps = {pid: capabilities_for(data, pid, shift, a, b) for pid in present}
            total = sum(choices[sid, pid] for pid in present)
            cover = sum(choices[sid, pid] for pid in available)
            for policy in staffing:
                rule, scope = policy['definition']['rule'], policy['definition']['scope']
                selector = session_scope(scope)
                if (policy['department_id'] != data['types'][shift['assignment_type_id']]['department_id']
                    or not within_dates(policy, a, b, zone) or not scope_matches_throughout(data, selector, None, shift, a, b)):
                    continue
                if rule['kind'] == 'minimum_cover':
                    # Scoped seats may need different people from the seats counted by the duty type.
                    can_cap = False
                peers = [shift]
                if rule.get('aggregation') in {'service', 'service_group'}:
                    peers = [s for s in shifts if data['types'][s['assignment_type_id']]['department_id'] == policy['department_id']
                        and aware(s['start']) < b and a < aware(s['end'])
                        and scope_matches_throughout(data, selector, None, s, a, b)]
                if rule['kind'] == 'minimum_cover' and rule.get('aggregation') == 'service_group':
                    cell_key = (policy['id'], (policy.get('template_line') or {}).get('line'), counting_key(policy), a, b)
                    if cell_key in pooled_cover_cells:
                        limit_receipt, fill_receipt = pooled_cover_cells[cell_key]
                        # Keep every anchor's objective weight without rebuilding the pooled cell.
                        if limit_receipt is not None and sid in selected:
                            append_objective(*limit_receipt)
                        for costs, terms in fill_receipt:
                            costs.extend(terms)
                        continue
                if rule['kind'] == 'minimum_cover':
                    triggers = set(rule.get('when_person_ids', []))
                    cover_terms = [(s['id'], pid, chosen) for s in peers for pid, chosen in staffing_credit(
                        data, policy, s, a, b, {pid: choices[s['id'], pid] for pid in present_at(s, a, b, True)},
                        conditional_credit=conditional_credit).items()]
                    counted = distinct((pid, chosen) for _, pid, chosen in cover_terms)
                    active = 1
                    if triggers:
                        trigger_terms = [choices[s['id'], pid] for s in peers for pid in present_at(s, a, b) if pid in triggers]
                        key = frozenset(term.index for term in trigger_terms)
                        if key not in cover_triggers:
                            cover_triggers[key] = model.new_bool_var('cover_trigger')
                            model.add_max_equality(cover_triggers[key], trigger_terms or [0])
                        active = cover_triggers[key]
                    # The solver and validator share the same selected pooled subject.
                    subject = (pooled_anchor([s['id'] for s in peers], selected, focus)
                               if rule.get('aggregation') == 'service_group' else sid)
                    limit_receipt = limit(rule['minimum'] * active - sum(counted.values()), 0, policy, rule['minimum'], minutes,
                          kept_gap=all(s['id'] in retained_gap_sessions for s in peers),
                          editable_cover=any(s['id'] in editable for s in peers), subject=subject,
                          people=counted, sessions=[s['id'] for s in peers], start=a, end=b,
                          record_objective=sid in selected)
                    fill_receipt = ()
                    if fill_priority.enabled:
                        cover_sources = defaultdict(list)
                        for peer, pid, chosen in cover_terms:
                            cover_sources[pid].append(((peer, pid), chosen))
                        before = {stage: len(costs) for stage, costs in fill_priority.costs.items()}
                        fill_priority.target([(1, counted[pid], cover_sources[pid]) for pid in sorted(counted)],
                            rule['minimum'], active,
                            stage='mandatory_cover' if policy['definition']['severity'] == 'mandatory' else 'desired')
                        fill_receipt = tuple((costs, tuple(costs[before.get(stage, 0):]))
                                             for stage, costs in fill_priority.costs.items()
                                             if len(costs) > before.get(stage, 0))
                    if rule.get('aggregation') == 'service_group':
                        pooled_cover_cells[cell_key] = limit_receipt, fill_receipt
                else:
                    services = set(shift['service_ids']) & set(scope.get('service_ids') or
                        ([scope['service_id']] if scope.get('service_id') else shift['service_ids']))
                    groups = [[s for s in peers if service in s['service_ids']] for service in sorted(services)] if rule.get('aggregation') == 'service' else [peers]
                    for group in groups:
                        counted = distinct((pid, choices[s['id'], pid]) for s in group for pid in present_at(s, a, b)
                            if scope_matches_throughout(data, scope, pid, s, a, b))
                        extra = 0  # no overflow configured: no variable (most rules)
                        if rule.get('overflow_maximum', 0):
                            extra = model.new_int_var(0, rule['overflow_maximum'], 'permitted_overflow')
                            model.add(extra <= sum(x for pid, x in counted.items()
                                if set(rule.get('overflow_category_ids', [])) & categories_for(data, pid, a, b)))
                        limit(sum(counted.values()), rule['maximum'] + extra, policy, len(counted), minutes, subject=sid,
                              people=counted, sessions=[s['id'] for s in group], start=a, end=b)
            interval_requirements = []
            for i, r in enumerate(definition['requirements']):
                if not (start + timedelta(minutes=r['from_minute']) <= a and b <= (
                        start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else end)):
                    continue
                interval_requirements.append(r)
                coverage_floor(cover, r['minimum'], sid, minutes, is_open_extra_cover(definition),
                               code='OPEN_EXTRA_COVER' if is_open_extra_cover(definition) else 'COVERAGE_GAP',
                               start=a, end=b)
                fill_priority.target([(1, choices[sid, pid], [((sid, pid), choices[sid, pid])])
                    for pid in available], r['minimum'], stage='mandatory_cover')
                if r['target'] is not None and r['target'] > r['minimum']:
                    fill_priority.target([(1, choices[sid, pid], [((sid, pid), choices[sid, pid])])
                        for pid in available], r['target'])
                if r['maximum'] is not None:
                    model.add(total <= r['maximum']).with_name('duty:' + sid)
                    if whole_duty:
                        kept = sum((sid, pid) not in decisions for pid in present)
                        maximum_places = min(maximum_places, r['maximum'] - kept)
                if r['target'] is not None and r['target'] > r['minimum']:
                    if greedy_repair:
                        kept_target = min(r['target'], sum((sid, pid) in greedy_placements for pid in available))
                        if kept_target > r['minimum']:
                            if i not in repair_targets:
                                # Count the worst loss once across splits of this configured target.
                                repair_targets[i] = model.new_int_var(0, r['target'], 'dropped_desired_cover')
                                desired_coverage.append(repair_targets[i])
                            model.add(cover + repair_targets[i] >= kept_target)
                    else:
                        deficit = model.new_int_var(0, r['target'], 'desired_cover')
                        model.add(cover + deficit >= r['target'])
                        desired_coverage.append(minutes * deficit)
                seats = defaultdict(list)
                for clause in r['capabilities']:
                    eligible = [pid for pid in available if clause['capability_id'] in caps[pid]]
                    coverage_floor(sum(choices[sid, pid] for pid in eligible), clause['minimum'], sid, minutes,
                                   staffed=staffed, code='CAPABILITY_GAP', start=a, end=b)
                    if r['distinct_capability_seats']:
                        allocated = []
                        for pid in eligible:
                            seat = model.new_bool_var('capability_seat')
                            model.add(seat <= choices[sid, pid])
                            seats[pid].append(seat)
                            allocated.append(seat)
                        coverage_floor(sum(allocated), clause['minimum'], sid, minutes,
                                       staffed=staffed, code='DISTINCT_PERSON_GAP', start=a, end=b)
                        fill_priority.target([(1, seat, [((sid, pid), choices[sid, pid])])
                            for pid, seat in zip(eligible, allocated)], clause['minimum'], stage='mandatory_cover')
                    else:
                        fill_priority.target([(1, choices[sid, pid], [((sid, pid), choices[sid, pid])])
                            for pid in eligible], clause['minimum'], stage='mandatory_cover')
                for same_person in seats.values():
                    model.add(sum(same_person) <= 1)
            if whole_duty:
                requested = max((max(r['minimum'], r['target'] or 0) for r in interval_requirements), default=0)
                kept = sum((sid, pid) not in decisions for pid in available)
                places_to_fill = max(places_to_fill, requested - kept)
            append_coverage_score_row(cover, interval_requirements, (b - a) // timedelta(microseconds=1))
            if sid in selected:
                live_coverage.append((sid, cover, max((r['minimum'] for r in interval_requirements), default=0)))
            if supervision:
                trainees = [pid for pid in available if is_trainee(data, supervision, participants[sid, pid], a, b)]
                supervisors = [pid for pid in available if pid not in trainees
                    and is_supervisor(sid, pid, a, b)]
                for trainee in trainees:
                    for supervisor in by_shift[sid]:
                        pair = pairings.get((sid, trainee, supervisor))
                        if pair is not None and supervisor not in supervisors:
                            model.add(pair == 0).with_name('duty:' + sid)
                constraint = model.add(sum(choices[sid, pid] for pid in trainees) <=
                          supervision['trainees_per_supervisor'] * sum(choices[sid, pid] for pid in supervisors)).with_name('duty:' + sid)
                if sid in selected:
                    strict_constraints.add(constraint.index)
                limited = []  # supervisors with a supervision limit: their trainees need a named pairing
                for supervisor in supervisors:
                    paired = {pid: pairings.get((sid, pid, supervisor), int(
                        participants[sid, pid].get('supervisor_person_id') == supervisor)) for pid in trainees}
                    used = sum(paired.values())
                    if not isinstance(used, int) or used > supervision['trainees_per_supervisor']:
                        model.add(used <= supervision['trainees_per_supervisor'])
                    for policy in supervision_limits:
                        rule = policy['definition']['rule']
                        if rule['kind'] != 'supervision_limit' or not policy_applies(data, policy, supervisor, shift, a, b):
                            continue
                        limited.append(choices[sid, supervisor])
                        ranks = set(rule['trainee_category_ids'])
                        if rule['mode'] == 'allowed_ranks':
                            for pid, pair in sorted(paired.items()):
                                if not ranks & categories_for(data, pid, a, b) and (
                                        not isinstance(pair, int) or pair):  # a kept pairing of 0 forbids nothing
                                    model.add(pair == 0).with_name(policy_name(policy, data=data, person_ids=[supervisor, pid], session_ids=[sid], start=a, end=b))
                        else:
                            counted = {pid: pair for pid, pair in sorted(paired.items())
                                if not ranks or ranks & categories_for(data, pid, a, b)}
                            extra = 0
                            if rule.get('overflow_maximum', 0):
                                extra = model.new_int_var(0, rule['overflow_maximum'], 'supervision_overflow')
                                model.add(extra <= sum(pair for pid, pair in sorted(counted.items())
                                    if set(rule.get('overflow_category_ids', [])) & categories_for(data, pid, a, b)))
                            used = sum(counted.values())
                            if not isinstance(used, int) or used > rule['maximum']:  # kept pairings within the limit: nothing to add
                                model.add(used <= rule['maximum'] + extra).with_name(policy_name(
                                    policy, data=data, person_ids=[supervisor, *counted], session_ids=[sid], start=a, end=b))
                # A generic Spread evenly line selects the trainees in its who
                # scope.  For the first holder kind, add a hard pair-load gap
                # only when every supervisor has the same eligibility/capacity.
                spread_lines = {}
                for trainee in trainees:
                    for spread in session_spread_policies(
                            data['policies'],
                            lambda policy, member, duty, lo, hi: policy_applies(data, policy, member, duty, lo, hi),
                            trainee, shift, a, b):
                        spread_lines.setdefault(spread_policy_key(spread), spread)
                for spread in spread_lines.values():
                    members = [trainee for trainee in trainees if spread in session_spread_policies(
                        data['policies'],
                        lambda policy, member, duty, lo, hi: policy_applies(data, policy, member, duty, lo, hi),
                        trainee, shift, a, b)]
                    if (spread['definition']['severity'] != 'mandatory' or not members or sid not in editable
                            or any((assignment_fixed(shift, data_participant) or protects_existing(data, shift, data_participant))
                                   and data_participant['person_id'] in members
                                   for data_participant in shift['participants'])):
                        continue
                    allowed_by_supervisor = []
                    for candidate in supervisors:
                        ranks = set().union(*(p['definition']['rule'].get('trainee_category_ids') or ()
                                             for p in supervision_limits
                                             if p['definition']['rule'].get('mode') == 'allowed_ranks'
                                             and policy_applies(data, p, candidate, shift, a, b)))
                        allowed_by_supervisor.append(tuple(sorted(ranks)))
                    uniform = len(set(allowed_by_supervisor)) == 1
                    if allowed_by_supervisor and allowed_by_supervisor[0]:
                        ranks = set(allowed_by_supervisor[0])
                        uniform = uniform and all(ranks.intersection(categories_for(data, trainee, a, b)) for trainee in members)
                    effective_caps, variable_capacity = {}, False
                    for candidate in supervisors:
                        values = [supervision['trainees_per_supervisor']]
                        for policy in supervision_limits:
                            rule = policy['definition']['rule']
                            if rule.get('mode') == 'capacity' and policy_applies(data, policy, candidate, shift, a, b):
                                if rule.get('overflow_maximum', 0):
                                    variable_capacity = True
                                selected = set(rule.get('trainee_category_ids') or ())
                                if not selected or all(selected.intersection(categories_for(data, trainee, a, b))
                                                       for trainee in members):
                                    values.append(rule['maximum'])
                        effective_caps[candidate] = min(values)
                    uniform = uniform and not variable_capacity and len(set(effective_caps.values())) == 1
                    if uniform and all(all((sid, trainee, candidate) in pairings
                                           for candidate in supervisors) for trainee in members):
                        proof = levelled_pairing(members, supervisors, capacities=effective_caps)
                        if proof['reason'] is None:
                            gap = spread_gap(spread)
                            if proof['maximum'] - proof['minimum'] > gap:
                                # The configured gap is impossible for this
                                # attendance size; leave the precise advisory
                                # finding to validation instead of making the
                                # whole candidate infeasible.
                                continue
                            maximum = len(members)
                            loads = {candidate: sum(pairings[sid, trainee, candidate]
                                                     for trainee in members) for candidate in supervisors}
                            for left in supervisors:
                                for right in supervisors:
                                    if left >= right:
                                        continue
                                    present_left, present_right = choices[sid, left], choices[sid, right]
                                    inactive_terms = maximum * (2 - present_left - present_right)
                                    model.add(loads[left] - loads[right] <= gap + inactive_terms)
                                    model.add(loads[right] - loads[left] <= gap + inactive_terms)
                if limited and sid in editable and sid not in counted_shifts:
                    # Any limited supervisor present means every trainee present needs a named pairing.
                    # One shared flag, not one copy of each trainee's pairing sum per supervisor.
                    flag = model.new_bool_var('limited_supervisor_present')
                    model.add_max_equality(flag, limited)
                    for trainee in trainees:
                        model.add(sum(pairs_by_trainee[sid, trainee]) == choices[sid, trainee]).only_enforce_if(flag)
            lead = leadership.summary(data, shift, a, b)
            local_requirements = [r for r in (lead or {}).get('_requirements', ())
                                  if r.get('count_scope', 'duty') == 'duty']
            if lead and local_requirements:
                for pid in present:
                    if not lead_qualified(data, pid, shift, lead, a, b):
                        model.add(lead_terms[sid, pid] == 0).with_name('duty:' + sid)
                local_minimum = max(r['minimum'] for r in local_requirements)
                # One floor binds the maximum minimum; key it to that requirement so other findings do not double-count.
                binding_requirement = max(local_requirements,
                                          key=lambda r: (r['minimum'], str((r.get('policy') or {}).get('id'))))
                local_maximums = [r['maximum'] for r in local_requirements if r['maximum'] is not None]
                coverage_floor(sum(lead_terms[sid, pid] for pid in available
                                   if lead_qualified(data, pid, shift, lead, a, b)), local_minimum, sid, minutes,
                               staffed=staffed, code='LEAD_GAP', start=a, end=b,
                               rule_id=(binding_requirement.get('policy') or {}).get('id'))
                fill_priority.target([(1, lead_terms[sid, pid], [((sid, pid), lead_terms[sid, pid])])
                    for pid in available if lead_qualified(data, pid, shift, lead, a, b)], local_minimum,
                    stage='mandatory_cover')
                if local_maximums:
                    model.add(sum(lead_terms[sid, pid] for pid in present) <= min(local_maximums)).with_name('duty:' + sid)
        # Service-mode balance can improve by adding work to a filled duty. Only cap
        # ordinary duties to which that preference applies, with no new staffing tier.
        if can_cap and maximum_places > places_to_fill and (relative_fair_share or any(
                policy_applies(data, policy, pid, shift, start, end) for policy in mode_balance for pid in mutable_people)):
            # A mutable whole duty fills all its staffed intervals. Cover needed in
            # any interval may require a place even if another is already staffed.
            model.add(sum(choices[sid, pid] for pid in mutable_people) <= places_to_fill).with_name('duty:' + sid)
            capped = True
        elif relative_fair_share and not can_cap and (not whole_duty or maximum_places > places_to_fill):
            staffing_choices.extend(choices[sid, pid] for pid in mutable_people)

    control.check()
    from .cp_sat_workload import add_workload_limits
    # Workload and quota translators need to distinguish mutable sessions from
    # fixed draft/publication context.  Keep the full focus set for scoring and
    # live previews, but expose the actual write scope for context baselines.
    data['_generation_editable'] = editable
    add_workload_limits(model, data, by_person, pairings, penalise)
    control.check()
    from .cp_sat_quotas import add_quotas
    issues = add_quotas(model, data, by_person, penalise, diagnostic_proposal=diagnostic_proposal, leads=lead_terms,
                        score_items=None if feasibility_only else quota_items,
                        score_strengths=quota_strengths, target_group=fill_priority.target if fill_priority.enabled else None, strict_minima=strict_constraints,
                        diagnostic_domains=diagnostic_domains if not feasibility_only else None,
                        staffing_targets=staffing_targets if options.scoring.weights.rules else None)
    if issues:
        return {**result, 'status': 'invalid_inputs', 'issues': issues}
    control.check()
    components = {}
    score_baseline = {}
    score_program = None
    score_variable_start = len(model.proto.variables)
    score_constraint_start = len(model.proto.constraints)
    def compile_components(target_model, target_data, target_options, *, stability, staffing_targets=None):
        from .cp_sat_score_preferences import add_score_preferences
        from .cp_sat_goals import add_score_goals
        q = ScoreModel(target_model, target_options.scoring.model_dump())
        levels = target_options.scoring.strengths.model_dump()
        parts = {'coverage': coverage_component(target_model, coverage_score_rows,
                    target_options.scoring.desired_cover_factor, check=control.check),
                 'rules': rule_component(target_model, target_data, choices, participants, score_breaches,
                    [(levels[level], loss, present) for (_, loss, present), level in zip(quota_items, quota_strengths)], levels)}
        goal_presence, fixed_goals = [], []
        goals = add_score_goals(target_model, target_data, choices, participants, overtime_choices, pairings,
                               focus, levels, leads=lead_terms, presence=goal_presence,
                               fixed_goals=fixed_goals if staffing_targets is not None else None)
        if fixed_goals:
            staffing_targets.append(q.mean(fixed_goals))
        # Normalize exact goal weights; the complete component is inactive until focused work exists.
        fairness_numerator = fairness_denominator = 0
        from fractions import Fraction
        from math import gcd, lcm
        from functools import reduce
        fractions = [Fraction(str(w)) for w, _ in goals]
        weight_unit = lcm(*(weight.denominator for weight in fractions))
        weights = [int(weight * weight_unit) for weight in fractions]
        divisor = reduce(gcd, weights, 0) or 1
        numerator_bound = sum(weight // divisor * q.bounds(loss)[1] for weight, (_, loss) in zip(weights, goals))
        if numerator_bound >= 2**62:
            raise ScorePrecisionError('Fair-share weights exceed supported integer precision; simplify their ratios.')
        for weight, (_, loss), present in zip(weights, goals, goal_presence):
            weight //= divisor
            fairness_numerator += weight * loss
            fairness_denominator += weight * present
        parts['fairness'] = q.divide(fairness_numerator, fairness_denominator), q.positive(fairness_denominator)
        parts['preferences'] = add_score_preferences(target_model, target_data, choices, participants, pairings,
                                                     overtime_choices, by_person, levels)
        baseline = {}
        if stability:
            loss, present, baseline = stability_component(target_model, {**snapshot, '_check_budget': control.check},
                                                         choices, overtime_choices, lead_terms, participants=participants)
            parts['stability'] = loss, present
        assigned = q.any([choice for (sid, _), choice in choices.items() if sid in focus])
        for name in ('fairness', 'preferences'):
            loss, present = parts[name]
            parts[name] = loss, q.minimum([present, assigned], 1)
        return parts, baseline, assigned

    if not feasibility_only:
        from .cp_sat_score import begin_score_program, finish_score_program
        from .fairness_distribution import PairLimitBudgetExceeded
        begin_score_program(model, score_variable_start)
        try:
            components, score_baseline, any_assignment = compile_components(
                model, data, options, stability=True, staffing_targets=staffing_targets)
        except PairLimitBudgetExceeded as error:
            return {**result, 'status': 'invalid_inputs', 'issues': [error.issue]}
    weighted = []
    weighted_prefix = []
    if components:
        # Exact weighted ties prefer fewer assignments; one loss tick always outweighs every possible addition.
        focused_choices = [choice for (sid, _), choice in choices.items() if sid in focus]
        losses, loss_scale = weighted_component(model, components, options.scoring.weights.model_dump(), len(focused_choices) + 1)
        weighted_prefix = [('weighted_prefix', [losses[0]], 0)] if len(losses) > 1 else []
        weighted = [(len(focused_choices) + 1) * losses[-1] + sum(focused_choices)]
        result['weighted_loss_scale'] = loss_scale
        result['weighted_tie_multiplier'] = len(focused_choices) + 1
        if weighted_prefix:
            from math import isqrt
            result['weighted_loss_radix'] = isqrt(loss_scale // SCORE_SCALE)
    score_constraint_end = len(model.proto.constraints)
    # Coverage is immutable first priority. Neither legacy order nor tolerance can move or relax it.
    stage_tiers = {
        STAGE_ORDER[0]: [('coverage', coverage, 0)],
        STAGE_ORDER[1]: ([('diagnostic_minima', diagnostic_minima, 0)]
                         if diagnostic_minima and diagnostic_proposal else []),
        STAGE_ORDER[2]: [('desired_coverage', desired_coverage, 0)] if desired_coverage else [],
    }
    tier_list = [tier for stage in STAGE_ORDER for tier in stage_tiers[stage]]
    # Fix demand before choosing who fills it, so minimum-seat credits cannot block requested extra seats.
    tier_list.extend(fill_priority.objectives())
    if staffing_choices:
        if staffing_targets:
            tier_list.append(('staffing_targets', staffing_targets, 0))
        # Independent requests may require extra places. Find the smallest roster
        # that meets those requests before sharing the work by weighted score.
        tier_list.append(('staffing', staffing_choices, 0))
    tier_list.extend(weighted_prefix)
    tier_list.append(('weighted', weighted, 0))
    result['optimality_boundary'] = 'Coverage first, then the score-weighted objective at one-millionth component resolution; full-candidate proof only.'
    if diagnostic_minima and diagnostic_proposal:
        result['optimality_boundary'] = 'Coverage first, then diagnostic mandatory minimum breaches, then the score-weighted objective at one-millionth component resolution; full-candidate proof only.'
    if fill_priority.enabled:
        result['optimality_boundary'] += ' Configured fill-priority stages precede weighted quality.'
    if staffing_choices or capped:
        result['optimality_boundary'] += ' Requested work and minimum staffing precede weighted quality.'
    if desired_coverage:
        result['optimality_boundary'] += ' Required cover and mandatory minima precede desired cover; no coverage weight ratio applies.'
    objective_order = [name for name, _, _ in tier_list]
    objectives = [penalties for _, penalties, _ in tier_list]
    if diagnostic_proposal:
        result['diagnostic_objective_order'] = objective_order
    if feasibility_only:
        if greedy_repair:
            moves = [1 - choices[pair] for pair in sorted(greedy_placements & decisions)]
            additions = [choices[pair] for pair in sorted(decisions - greedy_placements)]
            # Each weight exceeds the full range of all lower priorities. Break ties by adding only needed work.
            move_weight = len(additions) + 1
            desired_weight = (len(moves) + 1) * move_weight
            objectives = [[desired_weight * cp_model.LinearExpr.sum(desired_coverage),
                           move_weight * cp_model.LinearExpr.sum(moves), *additions]]
            model.minimize(cp_model.LinearExpr.sum(objectives[0]))
        else:
            objectives = [[]]
            # The structural proof captures the same zero objective as the feasibility search.
            model.minimize(0)
    # Compile score bounds with the full domains; only search mode fixes the dormant deficits.
    if not diagnostic_proposal:
        for index in diagnostic_domains:
            model.proto.variables[index].domain.clear()
            model.proto.variables[index].domain.extend([0, 0])
    if (selected != focus or protected_conflicts) and not feasibility_only:
        # The chosen sessions' own staffing stays strict: a shortfall there is a requirements conflict.
        result['loosened_constraints'] = keep_no_worse(model, {'duty:' + sid for sid in selected & editable}, strict_constraints)
    invalid = model.validate()
    if invalid:
        return {**result, 'status': 'model_invalid', 'message': invalid}
    if _on_objectives is not None:
        control.check()
        _on_objectives(model, tier_list)
        return {**result, 'status': 'model_ready', 'optimality_proven': False}
    if _on_model is not None:
        control.check()
        _on_model(model)
        return {**result, 'status': 'model_ready', 'optimality_proven': False}
    from .score import schedule_score
    # Public scores use the saved rota's context; measure once and reuse the unchanged before score.
    before_data = canonical_order({**snapshot, **snapshot.get('score_context', {}), '_check_budget': control.check})
    context_changed = any(before_data.get(key) != data.get(key) for key in ('wishes', 'lead_balance', 'fairness_history'))
    before_validation = protected_validation if keep_all and protected_validation is not None and not context_changed else evaluate(before_data, explain_findings=False)
    scoring = snapshot.get('generation_defaults') or options.model_dump(mode='json')
    scoring_started = time.monotonic()
    before_score = schedule_score(before_data, before_validation, scoring)
    # Two proposal scores remain, in addition to the validation/summary allowance measured above.
    control.reserve_seconds += 2 * validation_reserve(data, time.monotonic() - scoring_started) / 3
    if not feasibility_only:
        from packages.contracts.schemas import GenerationOptions
        from .solver_resume import SEMANTIC_PREPARATION_KEYS
        from .score import publication_baseline
        publication_stability = publication_baseline(before_data) is not None
        footer_options = GenerationOptions.model_validate(scoring)
        score_model, footer_components, footer_assigned = model, components, any_assignment
        if context_changed or (footer_options.scoring != options.scoring
                               or footer_options.balance_measure != options.balance_measure):
            # Footer settings/history can differ from the search objective; record them without changing its model.
            score_model = model.clone()
            begin_score_program(score_model, score_variable_start)
            footer_data = {key: value for key, value in data.items()
                           if not key.startswith('_') or key in SEMANTIC_PREPARATION_KEYS}
            footer_data.update(snapshot.get('score_context', {}))
            footer_data.update(generation_options=footer_options.model_dump(mode='json'),
                _score_options=footer_options.model_dump(mode='json'),
                _allocation_strengths=footer_options.scoring.strengths.model_dump(), _check_budget=control.check)
            try:
                footer_components, _, footer_assigned = compile_components(score_model, footer_data,
                                                                          footer_options, stability=publication_stability)
            except (ScorePrecisionError, PairLimitBudgetExceeded):
                footer_components = None
        if footer_components is not None:
            score_program = finish_score_program(score_model,
                {name: pair for name, pair in footer_components.items()
                 if name != 'stability' or publication_stability}, footer_assigned)
            if score_program is not None:
                score_program['has_sessions'] = any(s['id'] in focus for s in shifts)
                score_program['publication_stability'] = publication_stability
    return {
        'result': result,
        'data': data,
        'selected': selected,
        'editable': editable,
        'tolerated': tolerated,
        'gap_before': gap_before,
        'choices': choices,
        'live_coverage': live_coverage,
        'score_program': score_program,
        'pairings': pairings,
        'overtime_choices': overtime_choices,
        'lead_vars': lead_vars,
        'participants': participants,
        'by_shift': by_shift,
        'decisions': decisions,
        'counted_shifts': counted_shifts,
        'objectives': objectives,
        'objective_order': objective_order,
        'components': components,
        'score_variable_start': score_variable_start,
        'score_constraint_start': score_constraint_start,
        'score_constraint_end': score_constraint_end,
        'evidence': evidence,
        'shortages': shortages,
        'original_validation': original_validation,
        'protected_conflicts': protected_conflicts,
        'locked_staffing': locked_staffing,
        'score_baseline': score_baseline,
        'before_data': before_data,
        'context_changed': context_changed,
        'before_score': before_score,
        'scoring': scoring,
        'model': model,
        'diagnostic_domains': diagnostic_domains,
        'diagnostic_minima': diagnostic_minima,
        'coverage_floor_rows': coverage_floor_rows,
    }


def _diagnostic_mode(prepared, enabled):
    """Switch only the permitted shortfalls; assignments and safety ceilings keep the same model."""
    for index, maximum in prepared['diagnostic_domains'].items():
        domain = prepared['model'].proto.variables[index].domain
        domain.clear()
        domain.extend([0, maximum if enabled else 0])
    order, objectives = prepared['objective_order'], prepared['objectives']
    if enabled and prepared['diagnostic_minima'] and 'diagnostic_minima' not in order:
        order.insert(1, 'diagnostic_minima')
        objectives.insert(1, prepared['diagnostic_minima'])
    elif not enabled and 'diagnostic_minima' in order:
        index = order.index('diagnostic_minima')
        order.pop(index)
        objectives.pop(index)
    prepared['result']['diagnostic_proposal'] = enabled
    if enabled:
        prepared['result']['diagnostic_objective_order'] = list(order)
        if prepared['diagnostic_minima']:
            prepared['result']['optimality_boundary'] = prepared['result']['optimality_boundary'].replace(
                'Coverage first, then the score-weighted', 'Coverage first, then diagnostic mandatory minimum breaches, then the score-weighted')
    else:
        prepared['result'].pop('diagnostic_objective_order', None)


def _generate(snapshot, time_budget_seconds, workers, control, options, feasibility_only, diagnostic_proposal,
              deterministic=False, _on_model=None, _on_objectives=None, resume_state=None, checkpoint=None, _diagnostic_continuation=False, _candidate_evidence=None):
    from .solver_resume import resume_key, prepared_state, restore, decompress_state
    def check_resume():
        # Stop must restore an incumbent for score completion; cancellation must avoid decoding it.
        if control.cancelled():
            raise Cancelled()
        control.poll()
        control.check_stop_grace()
    check_resume()
    resume_state = decompress_state(resume_state, check=check_resume)
    saved_checkpoint = dict(resume_state or {})
    check_resume()
    key = resume_key(snapshot, options, feasibility_only, diagnostic_proposal)
    check_resume()
    prepared = restore(resume_state, key, check=check_resume) if _on_model is None and _on_objectives is None else None
    saved_checkpoint.pop('model', None)
    saved_checkpoint.pop('prepared', None)
    check_resume()
    reused = prepared is not None
    if prepared is None:
        prepared = _prepare(snapshot, time_budget_seconds, workers, control, options, feasibility_only,
                            diagnostic_proposal, deterministic, _on_model, _on_objectives, _diagnostic_continuation, _candidate_evidence)
        if 'status' in prepared:
            return prepared
        if checkpoint is not None:
            control.phase('checkpoint_preparation')
        state = prepared_state(key, prepared) if checkpoint is not None else {'key': key, 'hint': []}
        # A changed rule/input fingerprint invalidates the serialized model, but
        # the compact assignment-id checkpoint is still useful.  It is repaired
        # against the freshly prepared model below instead of being discarded.
        if saved_checkpoint.get('checkpoint_assignments'):
            state['checkpoint_assignments'] = saved_checkpoint['checkpoint_assignments']
            state['has_solution'] = True
        state['search_elapsed_seconds'] = control.search_spent
        if checkpoint is not None:
            checkpoint(state)
            control.phase('preparation')
    else:
        state = dict(resume_state)
        issues = []
        for findings, classification in ((prepared['result'].get('issues', []), 'protected'),
                (prepared['result'].get('outside_scope_issues', []), 'outside_scope'),
                (prepared['shortages'], 'unresolved')):
            issues = precheck_issues(findings, classification, previous=issues)
        control.context['precheck_issues'] = issues
    diagnostic_proposal = diagnostic_proposal or bool(state.get('diagnostic_proposal'))
    _diagnostic_mode(prepared, diagnostic_proposal)
    control.context['diagnostic_proposal'] = diagnostic_proposal
    state['diagnostic_proposal'] = diagnostic_proposal
    # The durable copy is already stored; callbacks only need changing hints and the final validated result.
    state.pop('model', None)
    state.pop('prepared', None)
    resume_state = None
    check_resume()
    prepared['result'] = copy.deepcopy(prepared['result'])
    if state.get('strict_conflict'):
        prepared['result']['conflict'] = state['strict_conflict']
        prepared['result']['diagnostics'] = [*prepared['shortages'], state['strict_conflict']]
    check_resume()
    prepared['result']['preparation_reused'] = reused
    prepared['result']['effective_options'] = options.model_dump(mode='json')
    prepared['result']['solver_workers'] = workers
    prepared['data']['generation_options'] = options.model_dump(mode='json')
    prepared['data']['_check_budget'] = control.check
    prepared['before_data']['_check_budget'] = control.check

    hint_repaired = False
    if not reused and state.get('checkpoint_assignments'):
        selected_pairs = {tuple(pair) for pair in state['checkpoint_assignments']
                          if isinstance(pair, (list, tuple)) and len(pair) == 2}
        repaired = [0] * prepared['score_variable_start']
        # Preserve the preparation's safe defaults (lead/supervision and
        # existing assignments), then overlay only assignment ids that still
        # exist in the new model.  Ineligible/stale checkpoint pairs are left
        # out and the normal search can repair them.
        original_hint = prepared['model'].proto.solution_hint
        for index, value in zip(original_hint.vars, original_hint.values):
            if index < len(repaired):
                repaired[index] = value
        for pair, variable in prepared['choices'].items():
            if variable.index < len(repaired):
                repaired[variable.index] = int(pair in selected_pairs)
        if repaired:
            state['hint'] = repaired
            state['has_solution'] = True
            hint_repaired = True

    last_checkpoint = [None]

    def checkpoint_candidate(values):
        """Build a small, applyable incumbent envelope without running the validator."""
        selected_pairs = sorted((sid, pid) for (sid, pid), variable in prepared['choices'].items()
                                if sid in prepared['selected'] and values[variable.index])
        selected_set = set(selected_pairs)
        original = {(shift['id'], participant['person_id']): participant
                    for shift in snapshot['shifts'] for participant in shift['participants']
                    if shift['id'] in prepared['selected']}
        additions = [{'shift_id': sid, 'participant': copy.deepcopy(prepared['participants'][sid, pid])}
                     for sid, pid in selected_pairs if (sid, pid) not in original and (sid, pid) in prepared['participants']]
        removals = [{'shift_id': sid, 'assignment_id': participant['id'], 'person_id': pid}
                    for (sid, pid), participant in sorted(original.items()) if (sid, pid) not in selected_set]
        return {'status': 'candidate_with_gaps', 'checkpointed': True,
                'input_fingerprint': snapshot['fingerprint'], 'algorithm_version': COMPILER_VERSION,
                'additions': additions, 'removals': removals,
                'checkpoint_assignments': [[sid, pid] for sid, pid in selected_pairs]}

    def keep_hint(values, force=False):
        # Score arithmetic is completed outside the search budget before a proposal is validated.
        state['hint'] = values[:prepared['score_variable_start']]
        state['has_solution'] = True
        state['checkpoint_assignments'] = [[sid, pid] for (sid, pid), variable in sorted(prepared['choices'].items())
            if sid in prepared['selected'] and values[variable.index]]
        state['result'] = checkpoint_candidate(values)
        state['search_elapsed_seconds'] = control.timings()['search_seconds']
        now = time.monotonic()
        if checkpoint is not None and ((force and last_checkpoint[0] is None)
                or last_checkpoint[0] is None or now - last_checkpoint[0] >= CHECKPOINT_INTERVAL_SECONDS):
            checkpoint(state)
            last_checkpoint[0] = now

    control.checkpoint_solution = keep_hint
    result = _search(snapshot, time_budget_seconds, workers, control, options, feasibility_only,
                     diagnostic_proposal, deterministic, hint=state.get('hint', []),
                     has_solution=state.get('has_solution', bool(state.get('hint'))),
                     hint_repaired=hint_repaired,
                     strict_probe_seconds=(options.diagnostic_seconds
                                           if options.on_conflict == 'report' and not feasibility_only
                                           and not diagnostic_proposal and not _diagnostic_continuation else None),
                     _diagnostic_continuation=_diagnostic_continuation, **prepared)
    if result['status'] == 'requirements_conflict' and checkpoint is not None and not _diagnostic_continuation:
        # A stopped strict proof keeps its prepared model and explanation so
        # "Continue anyway" can switch to diagnostic mode without rebuilding.
        checkpoint({**state, 'strict_conflict': result.get('conflict'),
                    'search_elapsed_seconds': control.search_spent})
    if (result['status'] == 'requirements_conflict' and _diagnostic_continuation and not feasibility_only and not diagnostic_proposal
            and prepared['diagnostic_domains']):
        diagnostic_budget = max(0, time_budget_seconds - result.get('_deterministic_search_seconds', 0))
        _diagnostic_mode(prepared, True)
        control.context['diagnostic_proposal'] = True
        prepared['result']['preparation_reused'] = True
        prepared['result']['diagnostic_continued'] = True
        if result.get('conflict'):
            prepared['result']['conflict'] = result['conflict']
            prepared['result']['diagnostics'] = result['diagnostics']
            state['strict_conflict'] = result['conflict']
        prepared['model'].clear_hints()
        state.update(diagnostic_proposal=True, hint=[], has_solution=False)
        if checkpoint is not None:
            checkpoint(state)
        result = _search(snapshot, diagnostic_budget, workers, control, options, feasibility_only,
                         True, deterministic, hint=[], has_solution=False,
                         strict_probe_seconds=None, **prepared)
    result.pop('_deterministic_search_seconds', None)
    if result['status'] in {'candidate_feasible', 'candidate_with_gaps'} and checkpoint is not None:
        checkpoint({**state, 'search_elapsed_seconds': control.search_spent,
                    'result': {**result, 'phase_timings': control.timings(),
                                      'search_elapsed_seconds': control.search_spent,
                                      'time_limit_seconds': control.search_budget or time_budget_seconds}})
    return result


def _search(snapshot, time_budget_seconds, workers, control, options, feasibility_only, diagnostic_proposal,
            deterministic, *, hint, has_solution, hint_repaired=False, result, data, selected, editable, tolerated, gap_before, choices, pairings,
            overtime_choices, lead_vars, participants, by_shift, decisions, counted_shifts, objectives, objective_order,
            components, score_variable_start, score_constraint_start, score_constraint_end, evidence, shortages,
            original_validation, protected_conflicts, locked_staffing, score_baseline, before_data, context_changed,
            before_score, scoring, model, live_coverage, diagnostic_domains, diagnostic_minima,
            coverage_floor_rows, strict_probe_seconds=None, _diagnostic_continuation=False, score_program=None):
    from .cp_sat_score import SCORE_SCALE
    from .score import publication_baseline, schedule_score
    if not deterministic:
        # Install the bounded search budget before proposal validation can poll a stopped continuation.
        control.search_budget = time_budget_seconds
        control.poll()
    in_scope, issues_where, kept_finding = _proposal_checks(data, selected, tolerated, gap_before, editable=editable)
    shifts = [shift for shift in data['shifts'] if not inactive(shift)]
    from packages.contracts.schemas import AutoscheduleAnimationSettings
    animation = AutoscheduleAnimationSettings.model_validate(snapshot.get('autoschedule_animation', {}))
    # Published context stays in the constraint model, never in the draft's live preview.
    focus = set(snapshot['focus_ids'])
    control.live_solutions({pair: variable for pair, variable in choices.items() if pair[0] in focus},
        {(s['id'], p['person_id']) for s in shifts if s['id'] in focus for p in s['participants']},
        live_coverage, animation.max_updates_per_second)
    if (publication_baseline(before_data) is not None) != ((score_program or {}).get('publication_stability') is True):
        score_program = None
    control.score_program, control.score_options = (score_program
        if control.has_progress or control.target_score is not None else None), scoring
    from .fairness import goal_groups, goal_people
    fair_share_active = any(goal_people(data, p)['who'] for group in goal_groups(data) for p in group)
    started = control.started
    prepared = time.monotonic()
    control.phase('search')
    # A stopped continuation may still finish checking the decisions already saved.
    control.stage('preparing', **({'best_score': None} if control.has_progress
        and not isinstance((score_program or {}).get('goal_cost_checks'), list) else {}))
    result['model_size'] = {'variables': len(model.proto.variables), 'constraints': len(model.proto.constraints)}
    control.context.update(time_limit_seconds=time_budget_seconds if deterministic else control.search_budget, search_started_seconds=prepared - control.started,
                           objective_names=['feasibility'] if feasibility_only else objective_order)
    # The search clock: wall seconds, or (deterministic) the work CP-SAT reports, summed over the solves so far.
    spent = [0.0]
    clock = (lambda: spent[0]) if deterministic else time.monotonic
    def new_solver(deficits=False):
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = workers
        if deterministic:
            solver.parameters.random_seed = 1
        # Keep default probing: OR-Tools 9.15 can falsely reject feasible fairness arithmetic at level 0.
        # Retain the other measured presolve limits.
        solver.parameters.symmetry_level = 0
        solver.parameters.max_presolve_iterations = 1
        solver.parameters.find_big_linear_overlap = False
        if deficits:
            # Coverage and rule breaches are deficits whose best is usually 0: the core-based worker reaches it
            # 5-10x sooner than the LP worker (it rarely proves a best above 0; the plateau stop ends those).
            solver.parameters.subsolvers.append('core')
        return solver
    solver = new_solver()
    deadline = clock() + time_budget_seconds if deterministic else None
    remaining_search = (lambda: deadline - clock()) if deterministic else control.remaining
    values, outcome, status, incumbent = None, {}, cp_model.UNKNOWN, []
    strict_probe_deadline = (clock() + strict_probe_seconds) if strict_probe_seconds is not None else None

    def available_search():
        if control.stopped:
            return 0
        remaining = remaining_search()
        if strict_probe_deadline is not None and values is None:
            remaining = min(remaining, strict_probe_deadline - clock())
        return remaining
    strict_constraint_count = len(model.proto.constraints)
    names = ['feasibility'] if feasibility_only else objective_order
    warning_keys = set()

    def solver_warning(tier, kind, message):
        key = (names[tier], tier, kind)
        if key in warning_keys:
            return
        warning_keys.add(key)
        result.setdefault('warnings', []).append(message)

    def block_later_tiers():
        nonlocal coverage_gate_blocked
        coverage_gate_blocked = True
        coverage_tier = next((index for index, name in enumerate(names) if name == 'coverage'), None)
        if coverage_tier is None:
            return
        skipped = []
        for blocked in range(coverage_tier + 1, len(names)):
            if blocked in outcome and outcome[blocked].get('search_status') != 'NOT_RUN':
                continue
            skipped.append(names[blocked])
            flat = cp_model.FlatIntExpr(cp_model.LinearExpr.sum(objectives[blocked]))
            value = (flat.offset + sum(coefficient * incumbent[variable.index]
                                      for variable, coefficient in zip(flat.vars, flat.coeffs))
                     if incumbent else None)
            outcome[blocked] = {'tier': blocked, 'name': names[blocked],
                               'value': value, 'proven_optimal': False,
                               'stopped_without_improvement': False, 'search_status': 'NOT_RUN'}
        if skipped and not any(warning.startswith('Skipped later stages after')
                               for warning in result.get('warnings', ())):
            result.setdefault('warnings', []).append(
                f"Skipped later stages after {stage_label(names[coverage_tier])}: "
                f"{', '.join(skipped)} were not run because coverage was not proven.")

    def complete_score(partial):
        """Evaluate derived score variables at fixed coverage decisions, keeping a valid full-model hint."""
        control.check()
        if control.stopped and snapshot.get('_group_witness_population') is not None:
            # Leave the grace for group validation and scoring.
            return None, cp_model.UNKNOWN
        completed = model.clone()
        completed.clear_objective()
        completed.clear_hints()
        for index, variable in enumerate(completed.proto.variables):
            if index >= score_variable_start:
                break
            if index % 1000 == 0:
                control.check()
            variable.domain.clear()
            variable.domain.extend([partial[index], partial[index]])
        # control.solve enforces diagnostic_seconds across completions, separately from search minutes.
        budget = options.diagnostic_seconds
        evaluator = new_solver()
        evaluator.parameters.num_workers = 1
        evaluator.parameters.max_time_in_seconds = budget if not deterministic else 60 + 20 * budget
        if deterministic:
            evaluator.parameters.max_deterministic_time = budget
        # Completion has its own finite allowance; every accepted candidate still passes canonical validation.
        control.phase('validation')
        try:
            completion_status = control.solve(evaluator, completed, None)
        finally:
            control.phase('search')
        if deterministic:
            spent[0] += control.last_solve_deterministic_time
        return evaluator, completion_status

    def keep_incumbent(solution=None):
        """Read the solution and hint all of it, so the next objective starts from a complete, valid schedule.
        A partial or stale hint (one that breaks the previous objective's bound) left each objective searching
        for any solution for minutes, so the plateau stop never started."""
        solution = solver.response_proto.solution if solution is None else solution
        found = [{key: solution[x.index] for key, x in sorted(variables.items())}
                 for variables in (choices, pairings, overtime_choices, lead_vars)]
        incumbent[:] = solution
        model.clear_hints()
        hint = model.proto.solution_hint
        hint.vars.extend(range(len(incumbent)))
        hint.values.extend(incumbent)
        control.checkpoint_solution(incumbent, force=True)
        return found

    compiled_score_complete = True
    if has_solution:
        if len(hint) != score_variable_start:
            raise ValueError('Prepared solution hint does not match its model')
        if available_search() <= 0:
            control.stage('scoring')
        solver, status = complete_score(hint)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            values, pairs, overtime_values, lead_values = keep_incumbent()
        elif status == cp_model.UNKNOWN and control.stopped:
            values, pairs, overtime_values, lead_values = keep_incumbent(hint)
            compiled_score_complete = False
        elif status == cp_model.INFEASIBLE:
            if hint_repaired:
                # Rules or eligibility may have changed since the checkpoint.
                # A repaired hint is advisory; discard it and solve the new
                # model rather than turning a stale hint into a translation
                # failure.
                has_solution = False
                hint = []
                model.clear_hints()
            else:
                return {**result, 'status': 'translation_error', 'message': 'The saved solution no longer satisfies its prepared model.'}
        else:
            return {**result, 'status': 'model_invalid' if status == cp_model.MODEL_INVALID else
                    'unknown_time_limit' if feasibility_only else 'needs_more_time', 'candidate_evidence': evidence}

    # Two passes over the priorities. The first searches a shortlist of candidates per duty (a few times its places,
    # spread so everyone gets a similar number of chances, always with whoever is there now and enough people with
    # each required skill or lead permission): the other choices are fixed to "not assigned", so presolve drops them
    # and the search works on a model the size of the rota rather than of people x duties. The second pass lets
    # every candidate back in and improves each priority from that schedule, never making an earlier one worse.
    # Every rule is in the model in both passes; the shortlist only narrows who the first pass may choose.
    control.phase('preparation')
    width = options.shortlist_width
    chances, shortlisted = defaultdict(int), set(snapshot.get('_greedy_placements', ()))
    for shift in sorted(shifts, key=lambda s: (aware(s['start']), s['id'])):
        control.check()
        sid = shift['id']
        people = [pid for pid in by_shift[sid] if (sid, pid) in decisions]
        if not people or not width:
            continue
        definition, start, end = effective_type_definition(data, shift), aware(shift['start']), aware(shift['end'])
        lead = leadership.summary(data, shift) or {}
        places = max([1] + [max(r['minimum'], r['target'] or 0) for r in definition['requirements']]) + lead.get('minimum', 0)
        present = {p['person_id'] for p in shift['participants']}
        people.sort(key=lambda pid: (pid not in present, chances[pid], zlib.crc32(f'{sid}:{pid}'.encode())))
        chosen = people[:width * places + 2]
        needs = [(clause['minimum'], lambda pid, c=clause['capability_id']: c in capabilities_for(data, pid, shift, start, end))
                 for r in definition['requirements'] for clause in r['capabilities']]
        if lead.get('minimum'):
            needs.append((lead['minimum'], lambda pid: lead_qualified(data, pid, shift, lead, start, end)))
        for minimum, able in needs:
            short = width * minimum - sum(map(able, chosen))
            chosen += [pid for pid in people if pid not in chosen and able(pid)][:max(0, short)]
        for pid in chosen:
            chances[pid] += 1
            shortlisted.add((sid, pid))
    left_out = {}
    if width:
        for index, key in enumerate(sorted(decisions - shortlisted)):
            if index % 1000 == 0:
                control.check()
            left_out[choices[key].index] = choices[key].domain.flattened_intervals()
    control.phase('search')
    control.context['search_started_seconds'] = time.monotonic() - control.started

    def narrow(on):
        variables = model.proto.variables
        for offset, (index, domain) in enumerate(sorted(left_out.items())):
            if offset % 1000 == 0:
                control.check()
            target = variables[index].domain
            target.clear()
            target.extend([0, 0] if on else domain)

    def floor():
        """The objective's least possible value from the variables' full domains (0 for a sum of penalties)."""
        objective, variables = model.proto.objective, model.proto.variables
        low = int(objective.offset)
        for ref, coefficient in zip(objective.vars, objective.coeffs):
            index = ref if ref >= 0 else -ref - 1
            domain = left_out.get(index) or list(variables[index].domain)  # the proto field has no negative indexing
            lo, hi = (domain[0], domain[-1]) if ref >= 0 else (-domain[-1], -domain[0])
            low += min(coefficient * lo, coefficient * hi)
        return low

    def current():
        """The objective's value for the schedule in hand."""
        objective = model.proto.objective
        return int(objective.offset) + sum(c * (incumbent[r] if r >= 0 else -incumbent[-r - 1])
                                      for r, c in zip(objective.vars, objective.coeffs))

    def fix(flag, value):
        model.proto.variables[flag.index].domain.clear()
        model.proto.variables[flag.index].domain.extend([value, value])
        # Bound switches are bookkeeping, not assignment decisions. Keep the
        # complete incumbent hint consistent when a bound is added or retired.
        if incumbent:
            hint = model.proto.solution_hint
            if flag.index == len(incumbent):
                incumbent.append(value)
                hint.vars.append(flag.index)
                hint.values.append(value)
            else:
                incumbent[flag.index] = value
                hint.values[flag.index] = value
    bounds = defaultdict(list)  # tier -> the switches of the bounds that keep it at its best so far

    def bound(tier, objective, value):
        """Keep this priority at `value` or better from now on (replacing its earlier bound)."""
        for flag in bounds.pop(tier, []):
            fix(flag, 0)
        flag = model.new_bool_var(f'priority_bound:{tier}')
        fix(flag, 1)
        model.add(objective <= value).only_enforce_if(flag)
        bounds[tier].append(flag)

    # A constant objective still needs a hard-feasibility solve before any
    # incumbent or optimality receipt exists (for example an empty locked rota).
    searched = [tier for tier, penalties in enumerate(objectives) if penalties or feasibility_only] or [0]
    if names[searched[0]].startswith('fill:'):
        searched.insert(0, 0)  # Establish a feasible incumbent before spending optional fill time.
    if control.stopped and values is not None:
        searched = []
    fill_budget = None
    fill_spent = 0
    coverage_gate_blocked = False
    fill_tiers_present = any(name.startswith('fill:') for name in names)
    if fill_tiers_present:
        fill_budget = available_search() * options.fill_tier_time_share
    for shortlist in [True, False] if left_out and values is None else [False]:
        if control.stopped or coverage_gate_blocked:
            break
        narrow(shortlist)
        if values is None:
            status = cp_model.UNKNOWN  # a shortlist without a schedule says nothing about the whole department
        for tier in searched:
            coverage_tier = next((index for index, name in enumerate(names) if name == 'coverage'), None)
            if (values is not None and coverage_tier is not None and tier > coverage_tier and coverage_tier in searched
                    and not outcome.get(coverage_tier, {}).get('proven_optimal')):
                if shortlist:
                    break
                block_later_tiers()
                break
            if outcome.get(tier, {}).get('proven_optimal'):
                if tier not in bounds:  # dropped while an earlier priority improved, which it survived unchanged
                    bound(tier, sum(objectives[tier]), outcome[tier]['value'])
                continue  # proven for every candidate already
            if not shortlist and values is not None:
                # Widening improves each priority in order as the priorities say: a later one may give way (its
                # bound is dropped and set again when its turn comes), an earlier one never does.
                for later in [t for t in bounds if t > tier]:
                    for flag in bounds.pop(later):
                        fix(flag, 0)
            objective = cp_model.FlatIntExpr(cp_model.LinearExpr.sum(objectives[tier]))
            model.minimize(objective)
            if values is not None:
                bound(tier, objective, current())
            if values is not None and current() <= floor():
                best = round(current())
                outcome[tier] = {'tier': tier, 'name': names[tier], 'value': best, 'proven_optimal': True,
                                 'stopped_without_improvement': False}
                bound(tier, objective, best)
                continue
            control.check()
            remaining = available_search()
            if remaining <= 0:
                break
            # Every priority left in this pass gets a fair share of the time, so the last ones are always reached;
            # time a priority does not need (a proven optimum, no better schedule for a while) goes to the rest.
            open_tiers = [t for t in searched if not outcome.get(t, {}).get('proven_optimal')]
            share = remaining / (sum(1 for t in open_tiers if t >= tier) + (len(open_tiers) if shortlist else 0))
            # Coverage and rule breaches are deficits whose best is usually 0: the core-based worker reaches it
            # 5-10x sooner. The wall-clock shortlist pays one cold start; deterministic work budgets
            # and the full-candidate pass retain the core/portfolio fallback.
            deficit = names[tier] in {'coverage', 'desired_coverage', 'rules'}
            diagnostic_desired = diagnostic_proposal and names[tier] == 'desired_coverage'
            limit = (remaining * (.5 if shortlist else 1)
                     if names[tier] in {'coverage', 'desired_coverage'} or values is None else share)
            if names[tier] == 'desired_coverage' and fill_budget is not None:
                limit = min(limit, max(0, remaining - max(0, fill_budget - fill_spent)))
            is_fill = names[tier].startswith('fill:')
            if is_fill:
                if fill_budget is None:
                    fill_budget = remaining * options.fill_tier_time_share
                open_fill = sum(names[t].startswith('fill:') for t in open_tiers if t >= tier)
                open_fill += sum(names[t].startswith('fill:') for t in open_tiers) if shortlist else 0
                limit = min(limit, max(0, fill_budget - fill_spent) / open_fill)
                if limit <= 0 and values is not None:
                    outcome[tier] = {'tier': tier, 'name': names[tier], 'value': round(current()),
                                     'proven_optimal': False, 'stopped_without_improvement': False,
                                     'search_status': 'NOT_RUN'}
                    continue  # The incumbent bound above preserves this tier while quality uses the reserve.
            until = clock() + limit
            attempted = False
            for core in ([True] if shortlist and not deterministic else [True, False]) if deficit and values is None else [deficit]:
                trial_solver = new_solver(core)
                budget = max(0, limit / 2 if core and values is None and (not shortlist or deterministic) else until - clock())
                if deterministic:
                    # Work units decide; the wall-clock cap only guards against a runaway (never reached in practice).
                    trial_solver.parameters.max_deterministic_time = budget
                    trial_solver.parameters.max_time_in_seconds = 60 + 20 * budget
                else:
                    trial_solver.parameters.max_time_in_seconds = budget
                # A diagnostic required-cover hint can leave all desired seats empty. Presolve the
                # desired search under the required-cover bound even when the hint is complete.
                trial_solver.parameters.cp_model_presolve = shortlist or values is None or diagnostic_desired
                control.context.update(tier=tier, candidates='shortlist' if shortlist else 'all')
                control.stage("finding" if values is None else "improving", tier=tier)
                # Give diagnostic desired cover its allocated slice so the inherited hint cannot trigger a plateau.
                control.stall_seconds = (None if feasibility_only or deterministic or names[tier] == 'coverage' or diagnostic_desired
                                         else options.stop_without_improvement_seconds or None)
                # Widen the provisional shortlist on its ordinary plateau. Spend the fairness extension
                # comparing every eligible candidate, not polishing an artificially restricted audience.
                control.fairness_plateau = fair_share_active and names[tier] == 'weighted' and not shortlist and not feasibility_only and not deterministic
                control.fairness_stall_seconds = options.fairness_plateau_seconds
                control.fairness_loss = components['fairness'][0] if control.fairness_plateau and 'fairness' in components else None
                search_model = model
                if components and names[tier] in {'coverage', 'desired_coverage'}:
                    # Only total, derived score arithmetic is omitted; every hard constraint remains in this clone.
                    control.check()
                    search_model = model.clone()
                    for index in range(score_constraint_start, score_constraint_end):
                        if index % 1000 == 0:
                            control.check()
                        constraint = search_model.proto.constraints[index]
                        constraint.copy_from(type(constraint)())
                # Native initialization/termination can exceed its requested time limit. Once a full-model
                # incumbent exists, do not spend the final-check reserve on an improvement slice shorter than
                # the largest observed time to a first incumbent. This estimate includes initial search.
                if (values is not None and not deterministic
                        and min(budget, control.remaining(control.reserve_seconds)) < control.incumbent_latency_seconds):
                    status = cp_model.UNKNOWN
                    break
                solver = trial_solver
                attempted = True
                solve_started = clock()
                status = control.solve(solver, search_model, tier)
                if deterministic:
                    spent[0] += control.last_solve_deterministic_time
                if names[tier] == 'coverage' and values is not None and status in (cp_model.UNKNOWN, cp_model.INFEASIBLE):
                    break
                if is_fill:
                    fill_spent += max(0, clock() - solve_started)
                if search_model is not model and status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    evaluator, completion_status = complete_score(solver.response_proto.solution)
                    if completion_status == cp_model.INFEASIBLE:
                        return {**result, 'status': 'translation_error', 'optimality_proven': False,
                                'message': 'A coverage candidate could not be evaluated by the compiled score.'}
                    if completion_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                        if completion_status == cp_model.UNKNOWN and control.stopped:
                            values, pairs, overtime_values, lead_values = keep_incumbent(solver.response_proto.solution)
                            compiled_score_complete = False
                            break
                        status = completion_status
                        if values is None:
                            # A saved decision hint can resume; more search cannot replenish completion time.
                            return {**result, 'status': 'model_invalid' if status == cp_model.MODEL_INVALID else
                                    'unknown_time_limit' if feasibility_only else 'needs_more_time',
                                    'candidate_evidence': evidence}
                    else:
                        solver = evaluator
                    del evaluator  # Do not retain a second solver reference across later trials.
                if status != cp_model.UNKNOWN or values is not None:
                    break
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                if values is not None:  # out of time before anything better: the schedule in hand stands
                    outcome[tier] = {'tier': tier, 'name': names[tier], 'value': round(current()),
                                     'proven_optimal': False, 'stopped_without_improvement': False,
                                     'search_status': solver.status_name(status) if attempted else 'NOT_RUN'}
                    if status == cp_model.INFEASIBLE:
                        # The incumbent satisfies this tier's bound; infeasibility cannot be a proof.
                        outcome[tier]['solver_inconsistency'] = True
                        solver_warning(tier, 'infeasible_incumbent',
                            f'Solver inconsistency in {stage_label(names[tier])}: search reported INFEASIBLE despite a '
                            'feasible incumbent. The incumbent was kept; this stage has no optimality proof.')
                    elif names[tier] == 'coverage':
                        if round(current()) <= floor():
                            outcome[tier]['proven_optimal'] = True
                            coverage_gate_blocked = False
                    if names[tier] == 'coverage' and not outcome[tier]['proven_optimal']:
                        block_later_tiers()
                    if control.fairness_plateau:
                        outcome[tier].update(search=control.last_search)
                    if ((names[tier] != 'coverage' or outcome[tier]['proven_optimal'])
                            and status in (cp_model.UNKNOWN, cp_model.INFEASIBLE)
                            and compiled_score_complete and not control.stopped and available_search() > 0):
                        bound(tier, objective, current())
                        continue
                break
            if not compiled_score_complete:
                # The kept coverage incumbent has a deficit value even without compiled score values.
                outcome = {t: row for t, row in outcome.items() if t < tier}
                outcome[tier] = {'tier': tier, 'name': names[tier], 'value': round(current()),
                                 'proven_optimal': False, 'stopped_without_improvement': control.stalled,
                                 'search_status': solver.status_name(status)}
                break
            values, pairs, overtime_values, lead_values = keep_incumbent()
            best = round(current())
            for t, row in outcome.items():  # every receipt describes the final incumbent, including tolerance spent
                now = solver.value(sum(objectives[t]))
                if now != row['value']:
                    row.update(value=now, proven_optimal=False)
            outcome[tier] = {'tier': tier, 'name': names[tier], 'value': best,
                             # A stopped search claims no proof for a later tier it was improving; coverage at its floor
                             # stays proven, since later tiers and the coverage warning depend on it.
                             'proven_optimal': status == cp_model.OPTIMAL and not shortlist or best <= floor() and (
                                 not control.stopped or names[tier] == 'coverage'),
                             'stopped_without_improvement': control.stalled, 'search_status': solver.status_name(status)}
            if control.fairness_plateau:
                outcome[tier]['search'] = control.last_search
            if feasibility_only:
                break
            bound(tier, objective, objective_bound(best, 0))
    if (values is None and status in (cp_model.UNKNOWN, cp_model.INFEASIBLE)
            and any(name == 'coverage' for name in names)):
        block_later_tiers()
        coverage_gate_blocked = False
    if values is not None and compiled_score_complete:  # only complete score values can prove objective bounds
        for tier, penalties in enumerate(objectives):
            if (not penalties and not feasibility_only and outcome.get(tier, {}).get('search_status') != 'NOT_RUN'
                    and all(t in outcome for t in searched if t < tier)):
                outcome[tier] = {'tier': tier, 'name': names[tier], 'value': 0, 'proven_optimal': True}
    if coverage_gate_blocked:
        block_later_tiers()
    tiers = [outcome[tier] for tier in sorted(outcome)]
    desired_row = next((row for row in tiers if row['name'] == 'desired_coverage'), None)
    if values is not None and desired_row and desired_row['value']:
        reason = ('This shortfall is proven unavoidable at the preceding priorities under the hard constraints.'
                  if desired_row['proven_optimal'] else
                  'Search stopped without further improvement; this shortfall was not proved unavoidable.'
                  if desired_row['stopped_without_improvement'] else
                  'Search ended without proving this shortfall unavoidable.')
        result.setdefault('warnings', []).append(
            f"Desired cover remains short by {desired_row['value']} seat-minutes. {reason}")
    result['_deterministic_search_seconds'] = spent[0] if deterministic else 0
    solved = time.monotonic()
    control.phase('validation')
    control.stall_seconds = None  # a conflict explanation runs to its own budget
    weighted_row = next((row for row in tiers if row['name'] == 'weighted'), None)
    result['weighted_status'] = ('UNKNOWN' if coverage_gate_blocked else
                                 'OPTIMAL' if weighted_row and weighted_row['proven_optimal']
                                 and all(row['proven_optimal'] for row in tiers) else
                                 'FEASIBLE' if values is not None else
                                 'INFEASIBLE' if status == cp_model.INFEASIBLE else 'UNKNOWN')
    result.update(objectives=tiers, phase_timings={'preparation_seconds': prepared - started,
                  'search_seconds': solved - prepared}, candidate_evidence=evidence)
    if fair_share_active and not feasibility_only and values is not None:
        from .solver_diagnostics import fair_share_search_notice
        result['fair_share_search'], notice = fair_share_search_notice(
            weighted_row, options.fairness_plateau_seconds, result['weighted_status'])
        if notice:
            result.setdefault('warnings', []).append(notice)
    if values is None and status == cp_model.INFEASIBLE and not result.get('conflict'):
        control.phase('diagnostic')
        from .solver_diagnostics import explain_conflict
        # Restore the full candidate domains and exclude search-only objective fixations from the proof.
        narrow(False)
        strict_model = model.clone()
        for constraint in list(strict_model.proto.constraints)[strict_constraint_count:]:
            constraint.copy_from(type(constraint)())
        explanation = explain_conflict(strict_model, data, control, options.diagnostic_seconds)
        control.phase('validation')
        result.update(conflict=explanation, diagnostics=[*shortages, explanation], diagnostics_complete=False)
        result['phase_timings']['diagnostic_seconds'] = explanation['elapsed_seconds']
        if explanation['implementation_error']:
            return {**result, 'status': 'translation_error', 'message': explanation['message'], 'optimality_proven': False}
    if values is None:
        return {**result, 'status': 'requirements_conflict' if status == cp_model.INFEASIBLE else
                'model_invalid' if status == cp_model.MODEL_INVALID else 'unknown_time_limit' if feasibility_only else 'needs_more_time'}
    chosen_pairs = {(s, person): supervisor for (s, person, supervisor), chosen in sorted(pairs.items()) if chosen}

    def name_pairs(sid, old, values):
        """Name a supervisor for each trainee of a counted shift: keep the current one where it
        still fits, then the supervisor with most room. The model guarantees enough capacity."""
        trainees, caps, _ = counted_shifts[sid]
        room = {pid: cap for pid, cap in sorted(caps.items()) if values[sid, pid]}
        named, waiting = {}, []
        for pid in trainees:
            if not values[sid, pid]:
                continue
            current = old.get(pid, {}).get('supervisor_person_id')
            if room.get(current, 0) > 0:
                named[sid, pid] = current
                room[current] -= 1
            else:
                waiting.append(pid)
        for pid in waiting:
            supervisor = max(room, key=lambda s: (room[s], s))
            named[sid, pid] = supervisor
            room[supervisor] -= 1
        return named

    original_findings = None
    for shift in shifts:
        control.check()
        sid = shift['id']
        if sid not in editable:
            continue
        old = {p['person_id']: p for p in shift['participants']}
        proposed = []
        if sid in counted_shifts:
            chosen_pairs.update(name_pairs(sid, old, values))
        for pid in by_shift[sid]:
            if not values[sid, pid]:
                continue
            p = participants[sid, pid]
            if pid in old and protects_existing(data, shift, old[pid]):
                proposed.append(old.pop(pid))
                continue
            if (sid, pid) in overtime_values:
                p['overtime'] = bool(overtime_values[sid, pid])
            pair = chosen_pairs.get((sid, pid))
            if pair:
                p['supervisor_person_id'] = pair
            if (sid, pid) in lead_values:
                p['team_lead'] = bool(lead_values[sid, pid])
            original = old.get(pid)
            same = original and all(original.get(k) == p.get(k) for k in
                ('start', 'end', 'participation', 'overtime', 'supervisor_person_id')) and (
                bool(original.get('team_lead')) == bool(p.get('team_lead')))
            same = same and (original.get('segments') or []) == (p.get('segments') or [])
            proposed.append(original if same else p)
            if same:
                del old[pid]
            else:
                result['additions'].append({'shift_id': sid, 'participant': p, **duty_labels(data, shift)})
        if old and original_findings is None:
            original_findings = defaultdict(list)
            for f in finding_observations(original_validation or evaluate({**snapshot, '_check_budget': control.check}, explain_findings=False)):
                if f['severity'] == 'error' and f.get('person_id'):
                    original_findings[f['person_id']].append(f)
        result['removals'].extend({'shift_id': sid, 'assignment_id': p['id'], 'person_id': pid,
            'start': p['start'], 'end': p['end'], 'reason': '; '.join(dict.fromkeys(
                f['message'] for f in original_findings[pid]
                if f.get('shift_id') == sid or f.get('start') and f.get('end') and overlaps(
                    aware(p['start']), aware(p['end']), aware(f['start']), aware(f['end'])))) or 'Rebalanced automatic proposal',
            **duty_labels(data, shift)} for pid, p in old.items())
        shift['participants'] = proposed
    control.stage("validating")
    data.pop('_policy_applies_cache', None)
    data.pop('_line_index_cache', None)
    if hasattr(data, '_fair_share_measurement_cache'):
        del data._fair_share_measurement_cache
    data.pop('_workload_matches_cache', None)
    data.pop('_supervision_commitments', None)
    for key in ('_candidate_evidence_cache', '_candidate_evidence_policy_source', '_policy_name_static',
                '_qualification_cache', '_qualification_evidence_cache', '_qualification_failures_cache',
                '_participation_cache', '_series_candidate_cache', '_candidate_absence_cache'):
        data.pop(key, None)
    permitted_deficits = {'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SCOPED_COVERAGE_GAP', 'QUOTA_BELOW_MINIMUM', 'LEAD_GAP'}
    validation = evaluate(data)
    control.check()
    result['phase_timings']['validation_seconds'] = time.monotonic() - solved
    workspace_id = (data.get('workspace') or {}).get('id')
    context_ids = {s['id'] for s in shifts if s['id'] not in editable and s.get('workspace_id') and workspace_id
                   and s['workspace_id'] != workspace_id}
    for finding in validation.get('findings', []):
        if finding.get('shift_id') in context_ids:
            finding['context'] = True
            month = aware(next(s for s in shifts if s['id'] == finding['shift_id'])['start']).astimezone(
                ZoneInfo(data['organisation']['timezone'])).strftime('%B')
            finding['message'] = f'{month} (context, fixed): {finding["message"]}'
    result['validation'] = validation
    coverage_row = next((row for row in tiers if row['name'] == 'coverage'), None)
    if coverage_row is not None:
        validator_deficit = _required_coverage_deficit({**validation, '_coverage_floor_rows': coverage_floor_rows})
        if validator_deficit != coverage_row['value']:
            coverage_tier = next(index for index, row in enumerate(tiers) if row is coverage_row)
            coverage_row['solver_inconsistency'] = True
            coverage_row['proven_optimal'] = False
            solver_warning(coverage_tier, 'required_cover_mismatch',
                           f'Solver inconsistency in {stage_label(coverage_row["name"])}: validator required-cover '
                           f'deficit was {validator_deficit} seat-minutes, but the CP-SAT coverage stage reported '
                           f'{coverage_row["value"]}. The incumbent was kept; this stage has no optimality proof.')
    validation_observations = list(finding_observations(validation))
    if diagnostic_proposal:
        # A continuation is deliberately reviewable even when it breaks a
        # non-safety mandatory rule. Keep the canonical validation findings
        # (severity=error) and expose a compact, deduplicated break receipt.
        breaks = {}
        for finding in validation_observations:
            if finding.get('severity') != 'error' or kept_finding(finding):
                continue
            key = (finding.get('rule_id'), finding.get('shift_id'), finding.get('person_id'))
            breaks[key] = {'rule_id': finding.get('rule_id'), 'session_id': finding.get('shift_id'),
                           **({'person_id': finding['person_id']} if finding.get('person_id') else {})}
        result['breaks'] = list(breaks.values())

    policies_by_id = {p['id']: p for p in data.get('policies', [])}
    safety_codes = {'TIME_OVERLAP', 'MIN_REST', 'TRAVEL_BUFFER', 'FORBIDDEN_TRANSITION',
                    'SUPERVISION_GAP', 'SUPERVISION_PAIRING', 'SUPERVISOR_SCOPED_CAPACITY', 'LEAD_GAP'}
    def safety_finding(finding):
        policy = policies_by_id.get(finding.get('rule_id'))
        if policy is not None:
            try:
                return safety_rule(policy.get('definition') or {})
            except (KeyError, TypeError, ValueError):
                return True
        return finding.get('code') in safety_codes

    locked_staffing_keys = {citation_finding_key(f) for f in locked_staffing}
    locked_supervision_keys = {citation_finding_key(f) for f in locked_staffing
                               if f['code'] == 'SUPERVISOR_SCOPED_CAPACITY'}
    fixed_breaches = [
        {key: finding.get(key) for key in ('code', 'rule_id', 'person_id', 'shift_id', 'start', 'end', 'message')}
        for finding in validation_observations
        if finding['severity'] == 'error' and (
            citation_finding_key(finding) in locked_supervision_keys
            or (feasibility_only and kept_finding(finding)
                and citation_finding_key(finding) not in locked_staffing_keys))
    ]
    if fixed_breaches:
        result['fixed_breaches'] = fixed_breaches
        result['fixed_breach_count'] = len(fixed_breaches)
        result['message'] = (f'Feasible except {len(fixed_breaches)} fixed breaches outside auto-schedule control.')
    if protected_conflicts:
        result['issues'] = issues_where(validation, lambda f: f['severity'] == 'error' and in_scope(f) and kept_finding(f))
    if feasibility_only:
        # A strict probe reports unchanged fixed workload breaches but must still reject a locked coverage gap.
        blocking_validation = [f for f in validation_observations
                               if f['severity'] == 'error' and f.get('shift_id') not in context_ids
                               and (citation_finding_key(f) in locked_staffing_keys or not kept_finding(f))]
    else:
        blocking_validation = [f for f in validation_observations
                               if f['severity'] == 'error' and f.get('shift_id') not in context_ids]
    validation_ok = validation['valid'] or not blocking_validation
    result['optimality_proven'] = (not diagnostic_proposal and not feasibility_only and not locked_staffing
                                   and compiled_score_complete and validation_ok and len(tiers) == len(objectives) and all(t['proven_optimal'] for t in tiers))
    unexpected = [f for f in validation_observations if f['severity'] == 'error' and not kept_finding(f)
                  and citation_finding_key(f) not in locked_supervision_keys
                  and (not diagnostic_proposal or (f['code'] not in permitted_deficits and safety_finding(f)))]
    if unexpected:
        return {**result, 'status': 'translation_error', 'additions': [], 'removals': [],
                'optimality_proven': False, 'message': 'Solver and validation disagree; no changes can be applied.'}
    control.validated_result = {**result,
        **({'warnings': result['warnings'].copy()} if 'warnings' in result else {}),
        'status': 'candidate_with_gaps' if diagnostic_proposal or not validation_ok else 'candidate_feasible',
        'first_solution_seconds': control.first_solution_seconds}
    if not feasibility_only:
        from .solver_diagnostics import explain_unmet_requirements
        result['allocation_diagnostics'] = explain_unmet_requirements(data, validation, tiers)
        control.validated_result['allocation_diagnostics'] = result['allocation_diagnostics']
        if result['allocation_diagnostics']:
            result.setdefault('warnings', []).extend(dict.fromkeys(row['message'] for row in result['allocation_diagnostics']))
            control.validated_result['warnings'] = result['warnings'].copy()
    if not feasibility_only:
        from .score import schedule_score
        result['objective_components'] = schedule_score(data, validation, options.model_dump(mode='json'), baseline=score_baseline)['components']
        control.validated_result['objective_components'] = result['objective_components']
        def incumbent_value(expression):
            if isinstance(expression, int):
                return expression
            flat = cp_model.FlatIntExpr(expression)
            return flat.offset + sum(coefficient * incumbent[variable.index]
                                     for variable, coefficient in zip(flat.vars, flat.coeffs))
        result['compiled_component_values'] = {name: (round(1 - incumbent_value(loss) / SCORE_SCALE, 6)
                                                     if incumbent_value(present) else None)
                                                 for name, (loss, present) in components.items()} if compiled_score_complete else {}

    control.check()
    from .wish_reasons import declined_wishes
    result['declined_wishes'] = declined_wishes(data, validation, evidence, preferences_applied=not feasibility_only)
    control.validated_result['declined_wishes'] = result['declined_wishes']
    control.check()
    if not any(f['severity'] == 'error' and f['code'] not in permitted_deficits and not kept_finding(f)
               and citation_finding_key(f) not in locked_supervision_keys
               for f in validation_observations):
        control.phase('summary')
        control.stage('summarizing')
        from .solver_summary import summarise
        result['summary'] = summarise(data, validation, evidence, editable, diagnostics=result.get('diagnostics', []))
        result['summary'].update(replacement_summary(data, result['removals']))
        after_data = {**before_data, 'shifts': data['shifts']}
        after_validation = evaluate(after_data, explain_findings=False) if context_changed else validation
        result['summary']['score_before'] = before_score
        saved = schedule_score(after_data, after_validation, scoring)
        control.check()
        result['score_completed'] = compiled_score_complete
        if compiled_score_complete and control.has_progress:
            from .score import progress_score
            live_score = progress_score(saved)
            if live_score is not None:
                control.publish_score(control.improvements_found, live_score, final=True)
        result['summary']['score_saved'] = saved
        result['summary']['score'] = saved  # Compatibility alias for existing clients and stored-result readers.
        before_score = result['summary']['score_before']
        result['summary']['score_delta'] = (round(saved['uncapped_score'] - before_score['uncapped_score'], 1)
                                           if saved['uncapped_score'] is not None and before_score['uncapped_score'] is not None else None)
        existing = [p for s in before_data['shifts'] if s['id'] in before_data['focus_ids'] and not inactive(s)
                    for p in s['participants']]
        removed = {r['assignment_id'] for r in result['removals']}
        result['summary']['existing_assignments'] = len(existing)
        result['summary']['changes_kept'] = sum(p['id'] not in removed for p in existing)
    if fixed_breaches and result.get('summary'):
        result['summary']['fixed_breaches'] = fixed_breaches
        result['summary']['fixed_breach_count'] = len(fixed_breaches)
        result['summary']['message'] = result['message']
    ready_score = {} if compiled_score_complete else {'best_score': None}
    if diagnostic_proposal or (locked_staffing and not validation_ok):
        control.stage('ready', shared_validation_passed=validation_ok, **ready_score)
        return {**result, 'status': 'candidate_with_gaps', 'optimality_proven': False,
                'message': (DIAGNOSTIC_PROPOSAL_MESSAGE
                            if diagnostic_proposal else 'Locked sessions still have open places. The editable sessions have been scheduled; no locked assignments were changed.'),
                'first_solution_seconds': control.first_solution_seconds,
                'elapsed_seconds': time.monotonic() - control.started}
    control.stage('ready', shared_validation_passed=validation_ok,
                  selected_scope_validation_passed=not any(f['severity'] == 'error' and in_scope(f)
                                                          for f in validation_observations), **ready_score)
    return {**result, 'status': 'candidate_feasible' if validation_ok else 'candidate_with_gaps',
            'first_solution_seconds': control.first_solution_seconds,
            'elapsed_seconds': time.monotonic() - control.started}
