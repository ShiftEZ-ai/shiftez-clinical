"""Bounded, deterministic greedy draft candidate producer, never a publisher.

Not an optimizer, not proof of feasibility/infeasibility. Every accepted addition is
checked against the canonical validator and the final candidate is checked again.
"""
import copy
import json
from collections import defaultdict
from fractions import Fraction
from datetime import date, timedelta
from zoneinfo import ZoneInfo
import time
import uuid
from packages.contracts.rule_catalogue import GREEDY_NOT_APPLIED
from packages.contracts.schemas import GENERATION_SCOPE_CAPABILITY
from . import leadership
from .generation_scope import population_allows, protects_existing, scope_receipt, selected_session_ids
from .validation import attached_to, evaluate, finding_observations, index_person_rows, person_rows, capabilities_for, categories_for, policy_applies, scope_intervals, valid_dates, qualified, qualified_supervisor, rotation_blocks, restriction_blocks, lead_qualified
from .validation import participation_for, trainee_intervals, named_people, session_matches, person_free
from .time import aware, duration_hours, local_midnight, union_hours, overlaps
from .conditions import active_rules, bucket_keys, counting_key, counting_views, rule_holds, scope_tree, scope_applies, scope_matches_throughout
from .supervision_levelling import levelled_order, session_spread_policies
from .patterns import day_of, wish_outcomes, wish_shape
from .recurrence import inactive, with_series_state, usual_assignee_fixed, series_candidate_allowed
from .fill_priority import STAGE_ORDER

# 43: reject proposals that introduce or worsen missing supervision.
ALGORITHM_VERSION = "bounded-greedy-43"
# Person-level errors that only the person's own duties decide (overlap, rest, travel, transitions, hours and duty limits).
OWN_DUTY_CODES = {'TIME_OVERLAP', 'MIN_REST', 'TRAVEL_BUFFER', 'FORBIDDEN_TRANSITION', 'MAX_HOURS', 'MAX_DUTIES',
                  'CONSECUTIVE_DAYS', 'CONSECUTIVE_WEEKENDS'}
COVERAGE_CODES = {"COVERAGE_GAP", "OPEN_EXTRA_COVER", "COVERAGE_TARGET", "CAPABILITY_GAP", "DISTINCT_PERSON_GAP", "SUPERVISION_GAP", "SCOPED_COVERAGE_GAP", "LEAD_GAP"}
MANIFEST = {
    "algorithm": ALGORITHM_VERSION,
    "generation_scope": GENERATION_SCOPE_CAPABILITY,
    "supported_rule_kinds": ["team_lead_requirement", "restriction", "max_hours", "max_duties", "min_rest", "forbidden_transition", "max_consecutive_days", "max_consecutive_weekends", "travel_buffer", "block_quota", "minimum_cover", "maximum_participants", "supervision_limit", "spread_evenly", "allocation_preference", "allocation_overtime", "balance_goal", "run_length", "days_off_run", "weekend_shape", "shift_succession", "together_apart"],
    "supported_type_schema_versions": [1],
    "max_sessions": 1500, "max_people": 500, "max_period_days": 31,
    "preserves_locked_assignments": True,
    "automatic_unavailability_repair": True,
    "redistribution": "One gap-first rebuild, then a bounded CP-SAT required-cover repair. Mandatory constraints remain blocking.",
    "quota_support": "Mandatory minima and advisory targets guide additions; mandatory maxima and safety constraints remain blocking. No optimality guarantee.",
    "capacity_overflow": "Normal capacity is attempted first; only explicitly permitted ranks can consume extra places, with visible warnings.",
    "auto_publish": False, "optimality_claim": "none",
    "not_applied": GREEDY_NOT_APPLIED,
}


def duty_labels(data, shift):
    return {'session_label': shift['type_snapshot']['name'],
            'service_names': [data['services'][sid]['name'] for sid in shift['service_ids']],
            'location_name': data['locations'][shift['location_id']]['name']}


def assignment_fixed(shift, participant):
    if usual_assignee_fixed(shift, participant.get('person_id')):
        return True
    # Earlier templates inherited fixed=True; an explicit edit marks the series modified.
    return participant.get('fixed', bool(shift.get('series_id'))) and not (
        shift.get('series_id') and not shift.get('series_modified'))


def keep_assignment(data, shift, participant):
    return assignment_fixed(shift, participant) or protects_existing(data, shift, participant) or (
        data.get('generation_options', {}).get('mode') == 'fill_gaps')


def replacement_summary(data, removals):
    replacements = [{**r, 'person_name': data['people'].get(r['person_id'], {}).get('display_name', '')}
                    for r in removals]
    return {'replaced_count': len(replacements), 'replacements': replacements,
            'replacement_message': f'Replaced {len(replacements)}'}


def citation_finding_key(f):
    """Fixed-rule solver guard: retain each constraint's citation so trials cannot transfer breaches.

    A person's window breach is independent of its anchor duty. Rule-change effects
    instead use finding_identity.finding_key, which compares obligation substance.
    """
    person_window = f.get('person_id') and f.get('start') and f.get('end')
    detail = f.get('detail')
    if detail:
        # Cause decoration is optional; it must not turn an unchanged absence into a new violation.
        decoration = {'derived_findings'} | ({'classification', 'leave_shift_ids'} if f['code'] == 'UNAVAILABLE' else set())
        detail = {k: v for k, v in detail.items() if k not in decoration} or None
    return repr([f.get(k) for k in ('code', 'person_id', 'rule_id', 'start', 'end')] + [detail]
                + ([] if person_window else [f.get('shift_id')]))


def introduced_supervision_gaps(before, after):
    """Existing gaps may improve; new trainees cannot consume supervision that is already missing."""
    previous = [f for f in finding_observations(before) if f['code'] == 'SUPERVISION_GAP']
    return [f for f in finding_observations(after) if f['code'] == 'SUPERVISION_GAP' and not any(
        old['shift_id'] == f['shift_id'] and aware(old['start']) <= aware(f['start'])
        and aware(f['end']) <= aware(old['end'])
        and old['detail']['trainees'] >= f['detail']['trainees']
        and old['detail']['qualified_supervisors'] <= f['detail']['qualified_supervisors'] for old in previous)]


def coverage_findings(validation):
    """Coverage needs remain usable when their visible issue is an assigned person's blocker."""
    for finding in finding_observations(validation):
        if finding['code'] in COVERAGE_CODES:
            yield finding


def unmet_requirements(validation):
    return [*coverage_findings(validation), *[f for f in finding_observations(validation)
            if f['code'] in {'QUOTA_BELOW_MINIMUM', 'QUOTA_BELOW_TARGET'}]]


def preference_window(data, policy, shift):
    rule, zone = policy['definition']['rule'], data['organisation']['timezone']
    day = aware(shift['start']).astimezone(ZoneInfo(zone)).date()
    first = (day - timedelta(days=(day.weekday() - data['organisation']['week_start']) % 7)
             if rule.get('balance_window', 'week') == 'week'
             else day - timedelta(days=rule.get('balance_window_days', 28) - 1))
    last = first + timedelta(days=7) if rule.get('balance_window', 'week') == 'week' else day + timedelta(days=1)
    if rule.get('balance_window') == 'rota':
        first, last = date.fromisoformat(data['workspace']['starts_on']), date.fromisoformat(data['workspace']['ends_on'])
    return local_midnight(first, zone), local_midnight(last, zone)


def allocation_policy_matches(data, policy, shift, pid):
    cache = data.get('_allocation_match_cache')
    scope = policy['definition']['scope']
    if cache is None:
        return session_matches(data, scope, shift) and _allocation_policy_matches(data, policy, shift, pid)
    session_key = id(policy), shift['id']
    entry = cache.get(session_key)
    if entry is None or entry[0] is not policy:
        signature = ('allocation_scope', policy['status'], policy.get('active', True),
                     policy['department_id'], policy.get('starts_on'), policy.get('ends_on'),
                     json.dumps(scope, sort_keys=True, default=str))
        entry = cache[session_key] = (policy, session_matches(data, scope, shift),
                                      person_free(scope), named_people(scope),
                                      cache.setdefault(signature, {}))
    if not entry[1]:
        return False
    if entry[3] is not None and pid is not None and pid not in entry[3]:
        return False
    key = shift['id'], pid is None if entry[2] else pid
    if key not in entry[4]:
        entry[4][key] = _allocation_policy_matches(data, policy, shift, pid)
    return entry[4][key]


def _allocation_policy_matches(data, policy, shift, pid):
    a, b = aware(shift['start']), aware(shift['end'])
    if not policy_applies(data, policy, pid, shift, a, b) or not scope_matches_throughout(
            data, policy['definition']['scope'], pid, shift, a, b):
        return False
    zone = data['organisation']['timezone']
    lo = max(a, local_midnight(date.fromisoformat(str(policy['starts_on'])), zone)) if policy.get('starts_on') else a
    hi = min(b, local_midnight(date.fromisoformat(str(policy['ends_on'])), zone)) if policy.get('ends_on') else b
    windows = scope_intervals(data, policy['definition']['scope'], pid, lo, hi) if hi > lo else []
    return union_hours(windows) >= duration_hours(a, b) - 1e-9


def preferred_supervisors(data, rule, shift, pid, supervisors):
    eligible = set(supervisors)
    if rule.get('supervisor_ids'):
        eligible &= set(rule['supervisor_ids'])
    if rule.get('supervisor_relationship') == 'mentor':
        eligible &= set(attached_to(data, pid, shift, aware(shift['start']), aware(shift['end'])))
    return eligible


def allocation_supervisor_present(data, shift, participant, a, b):
    """Allocation credit requires a qualified supervisor throughout the reserved duty."""
    return (qualified_supervisor(data, shift, participant, a, b)
            and aware(participant['start']) <= a and b <= aware(participant['end']))


def preference_load(data, policy, shift, pid, supervisors, history=None):
    """Count canonical matching duties once, within the configured local-day window."""
    rule = policy['definition']['rule']
    lo, hi = preference_window(data, policy, shift)
    entries = history if history is not None else [(s, p) for s in data['shifts'] if not inactive(s)
        for p in s['participants'] if p['person_id'] == pid]
    counts = {supervisor: set() for supervisor in supervisors} if rule['balance_by'] == 'supervisor' else {None: set()}
    mode_shares = {}
    def mode_share(duty):
        modes = {data['services'][sid].get('mode') for sid in duty['service_ids']}
        if not modes or not modes.issubset({'physical', 'virtual'}):
            return None
        return (1 if rule.get('balance_target_mode', 'virtual') in modes else 0) / len(modes)
    for old, participant in entries:
        a, b = aware(participant['start']), aware(participant['end'])
        if old['id'] == shift['id'] or not lo <= a < hi or not policy_applies(data, policy, pid, old, a, b):
            continue
        if not scope_intervals(data, policy['definition']['scope'], pid, a, b):
            continue
        if rule['balance_by'] == 'supervisor_group' and participant.get('supervisor_person_id') not in rule['supervisor_ids']:
            continue
        key = participant.get('supervisor_person_id') if rule['balance_by'] == 'supervisor' else None
        if key in counts:
            counts[key].add(old['id'])
        if rule['balance_by'] == 'service_mode':
            share = mode_share(old)
            if share is not None:
                mode_shares[old['id']] = share
    if rule['balance_by'] == 'service_mode':
        current = mode_share(shift)
        if current is None or not mode_shares:
            return 0
        actual = sum(mode_shares.values()) / len(mode_shares)
        return (actual - rule.get('balance_target_share', 0.5)) * (2 * current - 1)
    return min((len(duties) for duties in counts.values()), default=0)


def allocation_strengths(data):
    from packages.contracts.schemas import ScoringSettings
    options = data.get('generation_options', data.get('generation_defaults')) or {}
    return ScoringSettings.model_validate(options.get('scoring') or {}).strengths.model_dump()


def allocation_weight(policy, strengths):
    """Preference priority units relative to medium, retaining exact configured ratios."""
    return Fraction(strengths[policy['definition'].get('strength', 'medium')], strengths['medium'])


def overtime_cap_ids(data):
    """Required overtime limits in force, including the source names retained by grouped rules."""
    if '_overtime_cap_ids' in data:
        return data['_overtime_cap_ids']
    caps = [p for p in data['policies'] if p['status'] == 'active' and p.get('active', True)
            and (p['definition']['rule']['kind'] == 'block_quota' and p['definition']['rule']['comparison'] in {'maximum', 'range'}
                 or p['definition']['rule']['kind'] in {'forbidden_transition', 'max_consecutive_weekends'})
            and p['definition']['severity'] == 'mandatory']
    return {p['id'] for p in caps} | {i for p in caps for i in p['definition'].get('merged_ids') or []}


def allocation_index(data, first, last):
    """Index immutable resolved inputs once, rather than scan historical requests at every candidate place."""
    policies = data['policies']
    cached = data.get('_allocation_index')
    if (cached is not None and cached['policies'] is policies and cached['count'] == len(policies)
            and cached['first'] <= first and last <= cached['last']):
        return cached
    period = data.get('workspace') or {}
    first = min(first, date.fromisoformat(str(period['starts_on']))) if period.get('starts_on') else first
    last = max(last, date.fromisoformat(str(period['ends_on']))) if period.get('ends_on') else last
    if cached is not None and cached['policies'] is policies and cached['count'] == len(policies):
        first, last = min(first, cached['first']), max(last, cached['last'])
    rules, requests, overtime = [], {}, defaultdict(list)
    for policy in policies:
        if policy['definition']['rule']['kind'] not in {'allocation_preference', 'allocation_overtime'}:
            continue
        if not policy.get('_overtime_request'):
            rules.append(policy)
            if policy['definition']['rule']['kind'] == 'allocation_overtime':
                people = named_people(policy['definition']['scope'])
                for pid in people if people is not None else (None,):
                    overtime[pid].append(policy)
            continue
        scope = policy['definition']['scope']
        people = named_people(scope)
        people = data['people'] if people is None else people
        lo = max(first, date.fromisoformat(str(policy['starts_on']))) if policy.get('starts_on') else first
        hi = min(last, date.fromisoformat(str(policy['ends_on']))) if policy.get('ends_on') else last
        days = (map(date.fromisoformat, policy['_overtime_dates']) if policy.get('_overtime_dates') is not None else
                (lo + timedelta(days=n) for n in range((hi - lo).days)))
        for day in days:
            if lo <= day < hi and day.weekday() in scope.get('weekdays', range(7)):
                for pid in people:
                    requests.setdefault((pid, day), []).append(policy)
    cached = {'policies': policies, 'count': len(policies), 'first': first, 'last': last,
              'rules': rules, 'requests': requests, 'overtime': overtime, 'caps': overtime_cap_ids(data)}
    data['_allocation_index'] = cached
    return cached


def overtime_source_key(source):
    """Line ids survive proposals, inserted lines and catalogue conversion; policy/position are audit metadata."""
    return (('line', source['line_id']) if source.get('line_id') else
            ('legacy', source.get('policy_id'), source.get('line_index')))


def overtime_source_matches(source, policy):
    template = policy.get('template_line') or {}
    if source.get('line_id'):
        return template.get('id') == source['line_id']
    return policy['id'] == source.get('policy_id') and (
        source.get('line_index') is None or template.get('line') == source['line_index'])


def allocation_settings(data, shift, pid, supervisor_id=None, priority_field='priority', history=None, *, candidate_overtime=False):
    """Reuse rule scopes; permissions must cover the entire reserved duty."""
    a, b = aware(shift['start']), aware(shift['end'])
    priority, overtime = 0, False
    overtime_priority = 0
    strengths = data.get('_allocation_strengths') or allocation_strengths(data)
    zone = data['organisation']['timezone']
    first, last = a.astimezone(ZoneInfo(zone)).date(), (b - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date() + timedelta(days=1)
    index = allocation_index(data, first, last)
    offers = []
    for policy in index['rules']:
        rule = policy['definition']['rule']
        if not allocation_policy_matches(data, policy, shift, pid):
            continue
        if rule['kind'] == 'allocation_preference':
            selected = set(rule.get('supervisor_ids', []))
            present = {p['person_id'] for p in shift['participants'] if allocation_supervisor_present(data, shift, p, a, b)}
            eligible = preferred_supervisors(data, rule, shift, pid, present & {supervisor_id} if supervisor_id else present)
            if (selected or rule.get('supervisor_relationship') == 'mentor') and not eligible:
                continue
            weight = allocation_weight(policy, strengths)
            priority += weight * rule.get(priority_field, 0)
            overtime_priority += weight * rule.get('overtime_priority', 0)
            if priority_field == 'priority' and rule.get('balance_by', 'none') != 'none':
                priority -= float(weight) * rule.get('balance_weight', 1) * preference_load(data, policy, shift, pid, eligible, history)
        else:
            offers.append(policy)
    overtime = allocation_overtime_allowed(data, shift, pid, offers=offers)
    if priority_field == 'priority':
        from .fairness_distribution import marginal_priority
        priority += marginal_priority(data, shift, {
            'person_id': pid, 'start': shift['start'], 'end': shift['end'],
            'overtime': candidate_overtime, 'supervisor_person_id': supervisor_id}, history or [])
    return priority, overtime, overtime_priority


def allocation_overtime_allowed(data, shift, pid, *, offers=None):
    """Canonical overtime permission without calculating unrelated preference priorities."""
    a, b = aware(shift['start']), aware(shift['end'])
    zone = data['organisation']['timezone']
    first, last = a.astimezone(ZoneInfo(zone)).date(), (b - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date() + timedelta(days=1)
    index = allocation_index(data, first, last)
    if offers is None:
        rules = index['rules'] if pid is None else (*index['overtime'].get(None, ()), *index['overtime'].get(pid, ()))
        offers = [p for p in rules if p['definition']['rule']['kind'] == 'allocation_overtime'
                  and allocation_policy_matches(data, p, shift, pid)]
    active_caps = index['caps'] if offers else set()
    overtime = False
    for policy in offers:
        rule = policy['definition']['rule']
        if rule.get('volunteer_required', False):
            continue
        overtime = overtime or set(rule.get('required_policy_ids', [])) <= active_caps
    if not overtime and offers:
        windows = {}
        for n in range((last - first).days):
            day = first + timedelta(days=n)
            for policy in index['requests'].get((pid, day), ()):
                rule, source = policy['definition']['rule'], policy.get('_overtime_source') or {}
                if not set(rule.get('required_policy_ids', [])) <= active_caps or not any(
                        overtime_source_matches(source, p)
                        and set(p['definition']['rule'].get('required_policy_ids', [])) <= active_caps for p in offers):
                    continue
                lo, hi = max(a, local_midnight(day, zone)), min(b, local_midnight(day + timedelta(days=1), zone))
                if not policy_applies(data, policy, pid, shift, lo, hi):
                    continue
                key = overtime_source_key(source)
                windows.setdefault(key, []).extend(scope_intervals(data, policy['definition']['scope'], pid, lo, hi))
        # One overnight duty can be covered by adjacent approved daily offers, with no gap or scope widening.
        overtime = any(union_hours(parts) >= duration_hours(a, b) - 1e-9 for parts in windows.values())
    return overtime


def introduced_overflow(before, after):
    """A split of an existing overflow interval is not new capacity consumption."""
    old = [f for f in finding_observations(before) if f['code'] == 'CAPACITY_OVERFLOW']
    return [f for f in finding_observations(after) if f['code'] == 'CAPACITY_OVERFLOW' and not any(
        r['rule_id'] == f['rule_id']
        and (r['detail'].get('line'), r['detail'].get('slot'), counting_key(r['detail']))
            == (f['detail'].get('line'), f['detail'].get('slot'), counting_key(f['detail']))
        and r['detail'].get('supervisor_person_id') == f['detail'].get('supervisor_person_id')
        and (r['detail'].get('service_id') or r['shift_id']) == (f['detail'].get('service_id') or f['shift_id'])
        and aware(r['start']) <= aware(f['start']) and aware(f['end']) <= aware(r['end'])
        and r['detail']['scheduled'] >= f['detail']['scheduled'] for r in old)]


def deficit_score(validation):
    """Mandatory cover, mandatory quotas, then desired cover/quotas; never safety."""
    totals = [0.0] * len(STAGE_ORDER)
    stage_index = {name: index for index, name in enumerate(STAGE_ORDER)}
    for finding in finding_observations(validation):
        code, detail = finding['code'], finding.get('detail') or {}
        if code in COVERAGE_CODES:
            if code == 'COVERAGE_TARGET':
                deficit = max(0, detail.get('target', 0) - detail.get('available', 0))
            else:
                deficit = max(1, detail.get('required', 1) - detail.get('available', 0))
            totals[stage_index['mandatory_cover'] if finding['severity'] == 'error' else stage_index['desired']] += deficit
        elif code in {'QUOTA_BELOW_MINIMUM', 'QUOTA_BELOW_TARGET'}:
            totals[stage_index['mandatory_quota'] if finding['severity'] == 'error' else stage_index['desired']] += max(0, -detail.get('difference', 0))
    return tuple(round(v, 6) for v in totals)


def _sweep_modes(baseline):
    """Skip the strict pass when no mandatory stage is open; the later pass still fills desired demand."""
    return (True, False) if any(deficit_score(baseline)[:-1]) else (False,)


def generate(snapshot: dict, time_budget_seconds: float | None = None, *, cancelled=None) -> dict:
    if time_budget_seconds is None:
        from packages.contracts.schemas import GenerationOptions
        time_budget_seconds = GenerationOptions.model_validate(
            snapshot.get('generation_options', snapshot.get('generation_defaults')) or {}).search_minutes * 60
    started = time.monotonic()
    deadline = started + time_budget_seconds
    # Leave time for local validation and a joint repair if local choices strand required cover.
    result = _generate_with_redistribution(snapshot, time_budget_seconds / 4, cancelled=cancelled)
    remaining = deadline - time.monotonic()
    if (result.get('status') in {'candidate_with_gaps', 'unknown_time_limit'}
            and deficit_score(result['validation'])[0] and remaining > 0 and not (cancelled and cancelled())):
        try:
            from .cp_sat import generate as required_cover
            from .solver_control import SolverControl
            control = SolverControl(cancelled=lambda: time.monotonic() >= deadline or bool(cancelled and cancelled()))
            repaired = required_cover({**snapshot, '_greedy_required_cover': True}, remaining, workers=1, feasibility_only=True,
                                      _control=control)
            repair_timed_out = repaired.get('status') in {'cancelled', 'unknown_time_limit', 'needs_more_time'}
            if repair_timed_out and control.validated_result is not None:
                repaired = {**control.validated_result, 'solver_status': 'FEASIBLE', 'score_completed': False}
        except ImportError:
            repaired = {'status': 'unavailable'}
        if repaired.get('status') == 'candidate_feasible' and repaired.get('validation', {}).get('valid'):
            result = repaired
            result['required_cover_repair'] = {'algorithm_version': repaired['algorithm_version'],
                                               'solver_status': repaired['solver_status'],
                                               **{key: result.pop(key) for key in (
                                                   'phase_timings', 'first_solution_seconds', 'search_elapsed_seconds',
                                                   'stop_reason', 'search_stop', 'score_completion_seconds',
                                                   'live_score_metrics', 'improvements_found') if key in result}}
            result['algorithm_version'] = ALGORITHM_VERSION
            result['unmet_requirements'] = unmet_requirements(result['validation'])
            if repair_timed_out:
                result['status'] = 'unknown_time_limit'
            result.setdefault('warnings', []).append(
                'Required cover was repaired with CP-SAT. Advisory preferences were not optimized by the repair.')
        else:
            timed_out = repaired.get('status') in {'unknown_time_limit', 'needs_more_time', 'cancelled'}
            if timed_out:
                result['status'] = 'unknown_time_limit'
            reason = ('was cancelled' if cancelled and cancelled() else 'ran out of time' if timed_out
                      else 'is unavailable' if repaired.get('status') == 'unavailable'
                      else 'found no valid complete rota')
            result.setdefault('warnings', []).append(
                f'Required-cover repair {reason}; the best partial proposal is retained.')
    if cancelled and cancelled():
        result['status'] = 'cancelled'
    if result.get('status') in {'cancelled', 'unknown_time_limit'}:
        reason = 'Search cancelled.' if result['status'] == 'cancelled' else 'Search time expired.'
        result['message'] = (reason + ' The complete validated rota is retained; score optimization is unfinished.'
            if result.get('validation', {}).get('valid') else
            reason + ' The best validated partial proposal is retained, with unfilled places listed in unmet '
            'requirements. This does not prove the requirements conflict.')
    result.update(scope_receipt(snapshot))
    result['not_applied'] = copy.deepcopy(GREEDY_NOT_APPLIED)
    result['elapsed_seconds'] = time.monotonic() - started
    result['time_limit_seconds'] = time_budget_seconds
    return result


def _generate_with_redistribution(snapshot: dict, time_budget_seconds: float, *, cancelled=None) -> dict:
    deadline = time.monotonic() + time_budget_seconds
    first = _generate(snapshot, time_budget_seconds, cancelled=cancelled)
    if first.get('status') not in {'candidate_with_gaps', 'unknown_time_limit'} or not first.get('additions'):
        return first
    gaps = {f['shift_id'] for f in first['unmet_requirements'] if f.get('shift_id')}
    remaining = deadline - time.monotonic()
    if not gaps or remaining <= 0 or cancelled and cancelled():
        return first
    # shortcut: one alternative order, not an optimizer; extend only if measured gaps justify it.
    # Rebuild from the immutable input so only this run's suggestions can be redistributed.
    second = _generate(snapshot, remaining, gaps, cancelled=cancelled)
    better = (second.get('validation') and
              deficit_score(second['validation']) < deficit_score(first['validation']))
    chosen = second if better else first
    chosen['redistribution'] = {'attempted': True, 'improved': bool(better), 'passes': 1}
    if not chosen['validation']['valid'] and 'unknown_time_limit' in {first['status'], second['status']}:
        chosen['status'] = 'unknown_time_limit'
    return chosen


def own_duty_blocked(data, shift, participant, own, known):
    """Would adding this participant give them an error only their own duties decide (OWN_DUTY_CODES)? Checked by the
    shared validator on just their duties and this session (as the fill options check a candidate): milliseconds instead
    of a whole-rota check. Only ever used to skip a trial that the whole-rota check would refuse too. `known` keeps
    each person's errors without the trial until their duties change."""
    pid = participant['person_id']
    kept = {s['id']: s for s in [*own, shift]}
    reduced = {**data, 'shifts': list(kept.values()), 'focus_ids': [i for i in data['focus_ids'] if i in kept],
               '_partial': {'shifts': set(kept), 'people': {pid}}, 'quota_subjects': set(), 'skip_coverage': True}

    def errors():
        result = evaluate(reduced, explain_findings=False)
        return {(f['code'], f['rule_id'], f['start'], f['end'], f['shift_id'], counting_key(f.get('detail')))
                for f in [*result['session_findings'], *result['person_findings']]
                if f['person_id'] == pid and f['severity'] == 'error' and f['code'] in OWN_DUTY_CODES}
    if pid not in known:
        known[pid] = errors()
    shift['participants'].append(participant)
    try:
        return bool(errors() - known[pid])
    finally:
        shift['participants'].pop()


class Recheck:
    """The run's whole-rota check kept in parts (each session's own checks, each person's), so a trial re-checks only the
    sessions it touches and the person it moves. Every part comes from the shared validator, computed exactly as a whole
    check computes it (`_partial` in validation.evaluate); what a part leaves out (shapes and goals, advisory only;
    resources and session setup, which do not depend on who works) cannot change a decision. The candidate is still
    checked whole at the end. tests/test_generation_recheck.py compares every trial with a whole check."""

    def __init__(self, data):
        self.data = data
        focus = set(data['focus_ids'])
        live = [s for s in data['shifts'] if not inactive(s) and s['id'] in focus]
        self.spans = [(aware(s['start']), aware(s['end']), s['id']) for s in live]
        people = set(data['people']) | {p['person_id'] for s in data['shifts'] for p in s['participants']}
        self.parts = self._check({s['id'] for s in live}, people)

    def _check(self, shift_ids, people):
        r = evaluate({**self.data, '_partial': {'shifts': shift_ids, 'people': people}}, explain_findings=False)
        sessions, persons, rows = {i: [] for i in shift_ids}, {i: [] for i in [*people, None]}, {i: [] for i in people}
        for f in r['session_findings']:
            sessions[f['shift_id']].append(f)
        for f in r['person_findings']:
            persons.setdefault(f['person_id'], []).append(f)
        for row in (r['quota_report'] or {}).get('rows', []):
            rows[row['person_id']].append(row)
        return sessions, persons, rows

    def touched(self, changed):
        """The changed sessions and every session of this rota overlapping one (pooled cover and clinic-wide limits
        count the people on sessions at the same time)."""
        from .validation_scope import affected_sessions
        return affected_sessions(self.data['shifts'], changed, spans=self.spans) | {
            s['id'] for s in changed if any(i == s['id'] for _, _, i in self.spans)}

    def result(self, parts=None):
        merged = [{**whole, **part} for whole, part in zip(self.parts, parts or ({}, {}, {}))]
        return {'findings': [f for group in (merged[0], merged[1]) for items in group.values() for f in items],
                'quota_report': {'rows': [r for items in merged[2].values() for r in items]}, '_parts': parts}

    def trial(self, changed, people):
        return self.result(self._check(self.touched(changed), set(people)))

    def accept(self, result):
        for whole, part in zip(self.parts, result['_parts'] or ({}, {}, {})):
            whole.update(part)


def pair_attached(data, shifts, added, removals, period):
    """Pair each trainee with the consultant they are attached to when both are on a session (required first), where
    the rota check allows it. Only work this run may change: its own additions, or unfixed draft assignments."""
    mine = {id(r['participant']): r for r in added}
    for shift in shifts:
        if shift['locked'] or not shift['type_snapshot']['definition'].get('supervision'):
            continue
        a, b = aware(shift['start']), aware(shift['end'])
        here = {p['person_id']: p for p in shift['participants']}
        for index, p in enumerate(list(shift['participants'])):
            intervals = trainee_intervals(data, shift, p)
            if not intervals or not (id(p) in mine or not (assignment_fixed(shift, p) or protects_existing(data, shift, p))
                                                       and not period.get('base_publication_id')):
                continue
            attached = attached_to(data, p['person_id'], shift, a, b)
            choices = sorted((q for q in attached if q in here and all(
                qualified_supervisor(data, shift, here[q], lo, hi) for lo, hi in intervals)),
                             key=lambda q: (attached[q] != 'required', q))
            if not choices or p.get('supervisor_person_id') == choices[0]:
                continue
            before = evaluate(data, explain_findings=False)
            paired = {**p, 'supervisor_person_id': choices[0]} if id(p) in mine else {
                **p, 'id': str(uuid.uuid5(uuid.NAMESPACE_URL, data['fingerprint'] + shift['id'] + p['person_id'] + ':attached')),
                'supervisor_person_id': choices[0], 'fixed': False}
            shift['participants'][index] = paired
            after = evaluate(data, explain_findings=False)
            existing_errors = {citation_finding_key(f) for f in finding_observations(before) if f['severity'] == 'error'}
            if any(f['severity'] == 'error' and citation_finding_key(f) not in existing_errors for f in finding_observations(after)):
                shift['participants'][index] = p
                continue
            if id(p) in mine:
                mine[id(p)]['participant'] = paired
            else:
                removals.append({'shift_id': shift['id'], 'assignment_id': p['id'], 'person_id': p['person_id'],
                                 'start': p['start'], 'end': p['end'], 'reason': 'Paired with their attached consultant'})
                added.append({'shift_id': shift['id'], 'participant': paired, **duty_labels(data, shift)})


def _generate(snapshot: dict, time_budget_seconds: float, priority_shifts=(), *, cancelled=None) -> dict:
    deadline = time.monotonic() + time_budget_seconds
    def interrupted():
        return time.monotonic() >= deadline or bool(cancelled and cancelled())

    data = copy.deepcopy(snapshot)
    from .date_sets import prepare_date_sets
    data['_date_set_cache'] = prepare_date_sets(data)
    data = with_series_state(data, cache=True)
    from packages.contracts.schemas import GenerationOptions
    data['generation_options'] = GenerationOptions.model_validate(
        data.get('generation_options', data.get('generation_defaults')) or {}).model_dump(mode='json')
    focus = set(data["focus_ids"])
    selected = set(selected_session_ids(data))
    shifts = [s for s in data["shifts"] if s["id"] in focus & selected and not inactive(s)]
    period = data['workspace']
    from .solver_rules import session_span_days
    if session_span_days(shifts) > MANIFEST['max_period_days'] + 1:
        return {'status': 'unsupported_period', 'algorithm_version': ALGORITHM_VERSION, 'capability_manifest': MANIFEST}
    if len(shifts) > MANIFEST["max_sessions"] or len(data["people"]) > MANIFEST["max_people"]:
        return {"status": "unsupported_size", "algorithm_version": ALGORITHM_VERSION,
                "message": "Narrow the planning period or employee scope to the published bounds", "capability_manifest": MANIFEST}
    # Every rule as the checks read it: a catalogue rule's lines are the kinds they write.
    data['policies'] = active_rules(data['policies'], context=data)
    leadership.prepare(data)
    unsupported = [p["kind"] for p in data['policies'] if p["kind"] not in MANIFEST["supported_rule_kinds"]]
    if unsupported:
        return {"status": "unsupported_rules", "unsupported": sorted(set(unsupported)), "algorithm_version": ALGORITHM_VERSION}
    # Rule definitions stay fixed during trials; stable bucket views reuse the run's applicability cache.
    data['_counted_policies'] = counting_views(data, data['policies'])
    # Which rules and time windows match a session never depends on who is on it: answer each once for the whole run
    # (trials and settings), not once per trial. The final check below runs without them.
    data['_scope_interval_cache'], data['_policy_applies_cache'], data['_allocation_match_cache'] = {}, {}, {}
    data['_staffing_selection_cache'] = {}
    data['_bucket_keys_cache'] = {}
    data['_workload_matches_cache'] = {}
    data['_allocation_strengths'] = allocation_strengths(data)
    data['_policy_applies_people'] = True
    data['_quota_cache'] = {}
    data['_person_rows'] = index_person_rows(data)
    data['_person_dates_cache'] = {}
    data['_line_index_cache'] = {}
    data['_workload_normalization_cache'] = {}
    policies_by_id = {p['id']: p for p in data['policies']}
    counted_cover = {(p['id'], (p.get('template_line') or {}).get('line'), (p.get('template_line') or {}).get('slot'),
        p['definition']['severity'], p['definition'].get('strength', 'medium'), counting_key(p)): p
        for p in counting_views(data, [p for p in data['policies'] if p['definition']['rule']['kind'] == 'minimum_cover'])
        if 'count_bucket' in p}
    def cover_policy(f):
        detail = f.get('detail') or {}
        return counted_cover[f['rule_id'], detail.get('line'), detail.get('slot'), detail['rule_severity'],
                             detail.get('rule_strength', 'medium'), counting_key(detail)] if 'count_bucket' in detail else policies_by_id[f['rule_id']]
    removals = []
    if not period.get('base_publication_id'):
        for shift in shifts:
            if shift['locked']:
                continue
            for participant in list(shift['participants']):
                if keep_assignment(data, shift, participant):
                    continue
                pid, a, b = participant['person_id'], aware(participant['start']), aware(participant['end'])
                away = rotation_blocks(data, pid, a, b)
                absent = any(r['person_id'] == pid and overlaps(a, b, aware(r['start']), aware(r['end']))
                             for r in data['availability'])
                if away or absent:
                    shift['participants'].remove(participant)
                    removals.append({'shift_id': shift['id'], 'assignment_id': participant['id'],
                        'person_id': pid, 'start': participant['start'], 'end': participant['end'],
                        'reason': 'External rotation' if away else 'Recorded unavailability'})
    # Quotas cannot select a person-level repair. Recheck them together after
    # repairs instead of recalculating every block after each removed duty.
    repairing = not period.get('base_publication_id')
    repair_data = {**data, 'policies': [p for p in data['policies'] if p['kind'] != 'block_quota']} if (
        repairing and any(p['kind'] == 'block_quota' for p in data['policies'])) else data
    initial = evaluate(repair_data)
    # Repair only explicit person-level violations, never missing configuration or
    # resource conflicts. Every removal is reviewable and limited to editable duties.
    assignment_limits = {'MAXIMUM_PARTICIPANTS', 'SCOPED_PARTICIPANT_LIMIT'}
    repairable = OWN_DUTY_CODES | assignment_limits | {'NOT_ELIGIBLE', 'RESTRICTION', 'FULL_ASSIGNMENT_REQUIRED',
                                                       'SUPERVISOR_REQUIRED', 'SUPERVISION_PAIRING'}
    if repairing:
        editable = {s['id']: s for s in shifts if not s['locked']}
        editable_by_person = {}
        for shift in editable.values():
            for p in shift['participants']:
                editable_by_person.setdefault(p['person_id'], []).append(shift)

        def touches_finding(shift, participant, finding):
            if shift['id'] == finding.get('shift_id'):
                return True
            if finding['code'] not in OWN_DUTY_CODES:
                return False  # Qualification and assignment timing belong to this exact duty/person.
            if not finding.get('start') or not finding.get('end'):
                return False
            a, b = aware(participant['start']), aware(participant['end'])
            lo, hi = aware(finding['start']), aware(finding['end'])
            # Rest/travel findings describe the gap between two duties; the editable duty touches its boundary.
            return overlaps(a, b, lo, hi) or finding['code'] in {'MIN_REST', 'TRAVEL_BUFFER'} and (b == lo or a == hi)

        while not interrupted():
            # Independent people can be repaired together; re-evaluate before
            # removing another duty for the same person (rest chains can change).
            conflicts = {}
            for f in finding_observations(initial):
                if f['severity'] != 'error' or f['code'] not in repairable or f.get('person_id') in conflicts:
                    continue
                if not f.get('person_id') and f.get('shift_id') not in editable:
                    continue
                candidates = sorted(editable_by_person.get(f.get('person_id'),
                    [editable[f['shift_id']]] if f.get('shift_id') in editable else []),
                                    key=lambda s: s['id'] != f.get('shift_id'))
                for shift in candidates:
                    participant = next((p for p in shift['participants'] if (p['person_id'] == f.get('person_id')
                        or f['code'] in assignment_limits and shift['id'] == f.get('shift_id'))
                        and (not f.get('rule_id') or f['code'] not in assignment_limits or scope_matches_throughout(data, policies_by_id[f['rule_id']]['definition']['scope'], p['person_id'], shift, aware(f['start']), aware(f['end'])))
                        and not keep_assignment(data, shift, p) and touches_finding(shift, p, f)), None)
                    if participant:
                        conflicts[participant['person_id']] = f, shift, participant
                        break
            if not conflicts:
                break
            for conflict, shift, participant in conflicts.values():
                shift['participants'].remove(participant)
                removals.append({'shift_id': shift['id'], 'assignment_id': participant['id'],
                    'person_id': participant['person_id'], 'start': participant['start'], 'end': participant['end'],
                    'reason': conflict['message']})
            initial = evaluate(repair_data)
        if repair_data is not data:
            initial = evaluate(data)
    # A trainee not yet paired with their attached consultant is paired at the end (pair_attached), not an input conflict.
    initial_noncoverage = [f for f in finding_observations(initial) if f["severity"] == "error" and f["code"] not in COVERAGE_CODES | {"QUOTA_BELOW_MINIMUM", "ATTACHMENT_PAIRING"}]
    configuration = [f for f in initial_noncoverage if not f.get('person_id') and f['code'] not in assignment_limits]
    if configuration:
        return {"status": "invalid_inputs", "validation": initial, "algorithm_version": ALGORITHM_VERSION,
                "removals": [], "message": "Resolve configuration conflicts before scheduling; no changes proposed."}
    tolerated = {citation_finding_key(f) for f in initial_noncoverage}
    timed_out, added = interrupted(), []
    hours = {pid: sum(duration_hours(aware(p["start"]), aware(p["end"])) for s in data["shifts"] if not inactive(s)
                      for p in s["participants"] if p["person_id"] == pid) for pid in data["people"]}
    history = {pid: [] for pid in data['people']}
    own_errors = {}
    for shift in data['shifts']:
        if not inactive(shift):
            for participant in shift['participants']:
                history.setdefault(participant['person_id'], []).append((shift, participant))
    pattern_wishes = {}
    for wish in data.get('wishes') or []:
        if wish_shape(wish) is not None and wish.get('weight'):
            pattern_wishes.setdefault(wish['person_id'], []).append(wish)

    def pattern_priority(pid, shift):
        wishes = pattern_wishes.get(pid)
        if not wishes:
            return 0
        zone = data['organisation']['timezone']
        mine = {}
        a, b = aware(shift['start']), aware(shift['end'])
        # Match a trial's displacement of overlapping duties using only this person's history.
        after = {}
        for old, participant in history[pid]:
            d = day_of(participant['start'], zone)
            mine.setdefault(d, set()).add(old['assignment_type_id'])
            if not overlaps(a, b, aware(participant['start']), aware(participant['end'])):
                after.setdefault(d, set()).add(old['assignment_type_id'])
        trial = {**data, 'wishes': wishes}
        before = wish_outcomes(trial, {pid: mine})
        after.setdefault(day_of(a, zone), set()).add(shift['assignment_type_id'])
        changed = wish_outcomes(trial, {pid: after})
        return sum(w['weight'] * (changed[w['id']][0] - before[w['id']][0])
                   for w in wishes if w['id'] in before)
    # Only configured session preferences add work; keep the same scope evaluator.
    session_data = {**data, 'policies': [p for p in data['policies']
        if p['definition']['rule']['kind'] == 'allocation_preference'
        and p['definition']['rule'].get('session_priority', 0)]}
    session_priorities = {}
    from .fill_priority import allocation_tier, stage_progress
    tiered = data.get('generation_options', {}).get('allocation_order', 'weighted') == 'tiered'
    stage_people = defaultdict(list)
    if session_data['policies']:
        for shift in shifts:
            scores = []
            for pid, person in data['people'].items():
                if interrupted():
                    timed_out = True
                    break
                if person['active'] and population_allows(data, shift, pid):
                    score = allocation_tier(session_data, shift, pid)
                    scores.append(score)
                    if tiered:
                        stage_people[score, shift['id']].append(pid)
            session_priorities[shift['id']] = max(scores, default=0)
            if timed_out:
                break
    ordered_shifts = sorted(shifts, key=lambda s: (s['id'] not in priority_shifts,
        -session_priorities.get(s['id'], 0), s['start'], s['id']))
    stages = sorted({stage for stage, _ in stage_people}, reverse=True) or [None]
    overflow_rules = {p['id']: p for p in data['policies'] if p['status'] == 'active' and p['definition']['rule'].get('overflow_maximum', 0)}
    passes = [False, True] if overflow_rules else [False]
    recheck = Recheck(data)
    baseline = recheck.result()
    from .fairness_distribution import greedy_goals, marginal_priority
    data['_distribution_goals'] = greedy_goals(data)
    data['_distribution_cache'] = {}
    data['_distribution_revision'] = 0
    lead_goals = [p for p in data['_distribution_goals'] if p['definition']['rule']['metric'] == 'lead_turns']
    lead_turns = leadership.lead_turn_pairs(data)
    lead_turn_counts = defaultdict(int)
    for pid in (pid for _, pid in lead_turns):
        lead_turn_counts[pid] += 1

    def lead_rank(shift, pid):
        turns = lead_turn_counts[pid]
        participant = next((p for p in shift['participants'] if p['person_id'] == pid),
                           {'person_id': pid, 'start': shift['start'], 'end': shift['end']})
        priority = marginal_priority(data, shift, {**participant, 'team_lead': True}, goals=lead_goals)
        return (-priority, turns, pid)

    def needs_leads(shift):
        return any(f['code'] == 'LEAD_GAP' and f['shift_id'] == shift['id'] and 'required' in f['detail']
                   for f in finding_observations(baseline))

    def name_leads():
        """Name a team lead among people already on a session that lacks one (fewest lead turns first)."""
        nonlocal baseline
        mine = {id(r['participant']): r for r in added}
        for shift in ordered_shifts:
            lead = leadership.summary(data, shift)
            if shift['locked'] or not lead or not needs_leads(shift):
                continue
            a, b = aware(shift['start']), aware(shift['end'])
            options = sorted((p for p in shift['participants'] if not p.get('team_lead')
                              and (id(p) in mine or not p.get('fixed', bool(shift.get('series_id'))) and not protects_existing(data, shift, p) and not period.get('base_publication_id'))
                              and lead_qualified(data, p['person_id'], shift, lead, a, b)),
                             key=lambda p: lead_rank(shift, p['person_id']))
            for p in options:
                if interrupted() or not needs_leads(shift):
                    break
                promoted = {**p, 'team_lead': True} if id(p) in mine else {
                    **p, 'id': str(uuid.uuid5(uuid.NAMESPACE_URL, data['fingerprint'] + shift['id'] + p['person_id'] + ':lead')),
                    'team_lead': True, 'fixed': False}
                index = shift['participants'].index(p)
                shift['participants'][index] = promoted
                result = recheck.trial([shift], [p['person_id']])
                if any(f['severity'] == 'error' and (f['code'] not in COVERAGE_CODES | {'QUOTA_BELOW_MINIMUM'}
                            or f['code'] == 'LEAD_GAP' and 'maximum' in (f.get('detail') or {}))
                       and citation_finding_key(f) not in tolerated for f in finding_observations(result)) \
                        or deficit_score(result) >= deficit_score(baseline):
                    shift['participants'][index] = p
                    continue
                baseline = result
                recheck.accept(result)
                data['_distribution_revision'] += 1
                lead_turns = leadership.lead_turn_pairs(data)
                lead_turn_counts.clear()
                for _, lead_pid in lead_turns:
                    lead_turn_counts[lead_pid] += 1
                history[p['person_id']] = [(s, promoted if q is p else q) for s, q in history[p['person_id']]]
                if id(p) in mine:
                    mine[id(p)]['participant'] = promoted
                else:
                    removals.append({'shift_id': shift['id'], 'assignment_id': p['id'], 'person_id': p['person_id'],
                                     'start': p['start'], 'end': p['end'], 'reason': 'Named team lead'})
                    added.append({'shift_id': shift['id'], 'participant': promoted, **duty_labels(data, shift)})

    name_leads()
    for strict_first, overflow_phase, stage, shift in (
            (strict_first, phase, stage, shift)
            for strict_first in _sweep_modes(baseline)
            for phase in passes
            for stage in stages
            for shift in ordered_shifts if stage is None or (stage, shift['id']) in stage_people):
        if interrupted():
            timed_out = True
            break
        if shift["locked"]:
            continue
        # Who could take this session at all (active, qualified, not away, on leave or restricted) never changes during
        # the run: decide it once per session, not for every person at every seat.
        a, b = aware(shift['start']), aware(shift['end'])
        candidates = stage_people[stage, shift['id']] if stage is not None else data['people']
        pool = [pid for pid in candidates if population_allows(data, shift, pid) and data['people'][pid]['active'] and series_candidate_allowed(data, shift, pid)
                and qualified(data, pid, shift, shift['type_snapshot']['definition'], a, b)
                and not rotation_blocks(data, pid, a, b) and not restriction_blocks(data, pid, shift, a, b)
                and not any(overlaps(a, b, aware(r['start']), aware(r['end'])) for r in person_rows(data, 'availability', pid))]
        for _seat in range(100):
            if interrupted():
                timed_out = True
                break
            before_score = deficit_score(baseline)
            if not any(before_score):
                break
            chosen = None
            quota_need = {}
            a, b = aware(shift['start']), aware(shift['end'])
            zone = data['organisation']['timezone']
            for row in (baseline.get('quota_report') or {}).get('rows', []):
                # A clinic/week-specific shortfall must not trigger trials in an
                # unrelated duty. The final validator still calculates credit.
                if (row['status'] in {'below_minimum', 'below_target'}
                        and (not row['assignment_type_ids'] or shift['assignment_type_id'] in row['assignment_type_ids'])
                        and overlaps(a, b, local_midnight(date.fromisoformat(row['starts_on']), zone),
                                     local_midnight(date.fromisoformat(row['ends_on']), zone))
                        and scope_applies(data, row['scope'], row['person_id'], shift, a, b)
                        and ('count_bucket' not in row or counting_key(row) in bucket_keys(
                            scope_tree(row['scope']), rule_holds(data, row['person_id'], shift, a, b)))):
                    quota_need[row['person_id']] = quota_need.get(row['person_id'], 0) + max(0, -row['difference'])
            coverage_needed = [f for f in coverage_findings(baseline)
                if f['shift_id'] == shift['id'] or shift['id'] in (f.get('detail') or {}).get('affected_shift_ids', [])]
            if not quota_need and not coverage_needed:
                break
            current_ids = {p["person_id"] for p in shift["participants"]}
            settings = {}
            for pid in pool:
                if interrupted():
                    timed_out = True
                    break
                settings[pid] = allocation_settings(data, shift, pid, history=history[pid])
            if timed_out:
                break
            ordered = sorted(pool, key=lambda x: (-quota_need.get(x, 0),
                             -settings[x][0] - pattern_priority(x, shift), hours[x], x))
            # A session still short of its team lead fills a seat with someone who may lead first.
            lead = leadership.summary(data, shift)
            if lead and needs_leads(shift):
                a, b = aware(shift['start']), aware(shift['end'])
                ordered.sort(key=lambda pid: (0, lead_rank(shift, pid)) if lead_qualified(data, pid, shift, lead, a, b) else (1,))
            overtime_priorities = {}
            if data['_distribution_goals']:
                for pid in ordered:
                    if settings[pid][1]:
                        overtime_priorities[pid] = marginal_priority(data, shift, {
                            'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'overtime': True}, history[pid])
            overtime_order = sorted(ordered, key=lambda pid: -settings[pid][2] - overtime_priorities.get(pid, 0))
            candidates = ([] if shift['type_snapshot']['definition'].get('extra_cover', False)
                          else [(pid, False) for pid in ordered]) + [(pid, True) for pid in overtime_order if settings[pid][1]]
            for pid, overtime in candidates:
                if pid in current_ids or (not coverage_needed and pid not in quota_need):
                    continue
                # A resident/intern shortfall cannot be filled by another rank.
                # Reuse the exact scoped-cover selector; retain broader trials
                # for other cover kinds and for matching personal quotas.
                if pid not in quota_need and not any(f['code'] != 'SCOPED_COVERAGE_GAP' or (
                        pid not in cover_policy(f)['definition']['rule'].get('when_person_ids', [])
                        and scope_matches_throughout(data, cover_policy(f)['definition']['scope'], pid, shift, aware(f['start']), aware(f['end']))) for f in coverage_needed):
                    continue
                if interrupted():
                    timed_out = True
                    break
                definition = shift["type_snapshot"]["definition"]
                a, b = aware(shift['start']), aware(shift['end'])
                displaced = [(s, p) for s, p in history[pid] if overlaps(a, b, aware(p['start']), aware(p['end']))]
                if displaced and (period.get('base_publication_id')
                        or any(s['locked'] or s['id'] not in focus & selected or keep_assignment(data, s, p) for s, p in displaced)):
                    continue
                supervision = definition.get("supervision")
                participation = participation_for(data, pid, shift, a, b)
                participant = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, data["fingerprint"] + shift["id"] + pid)),
                               "person_id": pid, "shift_id": shift["id"], "start": shift["start"], "end": shift["end"],
                               "participation": participation, "overtime": overtime, "revision": 1}
                lead = leadership.summary(data, shift)
                if (lead and needs_leads(shift)
                        and lead_qualified(data, pid, shift, lead, a, b)):
                    participant['team_lead'] = True
                pairings = [None]
                intervals = trainee_intervals(data, shift, participant)
                attached = attached_to(data, pid, shift, a, b) if intervals else {}
                if intervals and (supervision.get('require_pairing') or attached or any(
                        p['status'] == 'active' and (p['kind'] in {'supervision_limit', 'spread_evenly'} or
                            p['kind'] == 'max_duties' and p['definition']['rule'].get('group_by') == 'supervisor' or
                            p['kind'] == 'allocation_preference' and (p['definition']['rule'].get('balance_by') == 'supervisor'
                                or p['definition']['rule'].get('supervisor_relationship') == 'mentor'
                                or p['definition']['rule'].get('supervisor_ids')))
                        for p in data['policies'])):
                    pairings = sorted(p['person_id'] for p in shift['participants'] if all(
                        qualified_supervisor(data, shift, p, lo, hi) for lo, hi in intervals))
                    level = any(session_spread_policies(
                        data['policies'],
                        lambda policy, member, duty, lo, hi: policy_applies(data, policy, member, duty, lo, hi),
                        pid, shift, lo, hi)
                        for lo, hi in intervals)
                    loads = [p.get('supervisor_person_id') for p in shift['participants']]
                    balanced = levelled_order(pairings, loads) if level else list(pairings)
                    pairings.sort(key=lambda supervisor_id: (supervisor_id not in attached, attached.get(supervisor_id) != 'required',
                        balanced.index(supervisor_id),
                        -allocation_settings(data, shift, pid, supervisor_id, history=history[pid],
                                             candidate_overtime=overtime)[0], supervisor_id))
                for other, prior in displaced:
                    other['participants'].remove(prior)
                if own_duty_blocked(data, shift, participant, [s for s, _ in history[pid]], own_errors):
                    pairings = []  # the whole-rota check below would refuse every pairing
                for supervisor_id in pairings:
                    if interrupted():
                        timed_out = True
                        break
                    if supervisor_id:
                        participant['supervisor_person_id'] = supervisor_id
                    shift['participants'].append(participant)
                    result = recheck.trial([shift, *(other for other, _ in displaced)], [pid])
                    # A consultant joining a session whose attached trainee is paired elsewhere is re-paired below.
                    blockers = [f for f in finding_observations(result) if f['severity'] == 'error'
                                and (f['code'] not in COVERAGE_CODES | {'QUOTA_BELOW_MINIMUM', 'ATTACHMENT_PAIRING'}
                                    or f['code'] == 'LEAD_GAP' and 'maximum' in (f.get('detail') or {}))
                                and citation_finding_key(f) not in tolerated]
                    blockers.extend(introduced_supervision_gaps(baseline, result))
                    overflow = introduced_overflow(baseline, result)
                    if overflow and (not overflow_phase or any(
                            not set(overflow_rules[f['rule_id']]['definition']['rule']['overflow_category_ids']).intersection(
                                categories_for(data, pid, aware(f['start']), aware(f['end']))) for f in overflow)):
                        blockers.extend(overflow)
                    score = deficit_score(result)
                    shift['participants'].pop()
                    if not blockers and stage_progress(before_score, score, strict_first):
                        chosen = (pid, participant, result, displaced)
                        break
                for other, prior in displaced:
                    other['participants'].append(prior)
                if chosen:
                    break
            if chosen is None:
                break
            # The accepted result already validates this exact candidate. Reuse
            # it until the next addition; rejected trials leave it unchanged.
            pid, participant, baseline, displaced = chosen
            recheck.accept(baseline)
            data['_distribution_revision'] += 1
            for other, prior in displaced:
                other['participants'].remove(prior)
                history[pid].remove((other, prior))
                hours[pid] -= duration_hours(aware(prior['start']), aware(prior['end']))
                suggestion = next((r for r in added if r['participant']['id'] == prior['id']), None)
                if suggestion:
                    added.remove(suggestion)
                else:
                    removals.append({'shift_id': other['id'], 'assignment_id': prior['id'],
                        'person_id': pid, 'start': prior['start'], 'end': prior['end'],
                        'reason': 'Moved to reduce uncovered duties'})
            shift["participants"].append(participant)
            history[pid].append((shift, participant))
            own_errors.pop(pid, None)
            added.append({"shift_id": shift["id"], "participant": participant, **duty_labels(data, shift)})
            hours[pid] += duration_hours(aware(participant["start"]), aware(participant["end"]))
        if timed_out:
            break
    if not timed_out:
        name_leads()
        pair_attached(data, shifts, added, removals, period)
    for cache in ('_scope_interval_cache', '_policy_applies_cache', '_policy_applies_people', '_allocation_match_cache', '_person_rows', '_person_dates_cache', '_line_index_cache', '_bucket_keys_cache', '_workload_matches_cache', '_quota_cache', '_counted_policies', '_workload_normalization_cache'):
        data.pop(cache)
    validation = evaluate(data)
    unexpected = [f for f in finding_observations(validation) if f['severity'] == 'error'
        and f['code'] not in COVERAGE_CODES | {'QUOTA_BELOW_MINIMUM', 'ATTACHMENT_PAIRING'}
        and citation_finding_key(f) not in tolerated]
    unexpected.extend(introduced_supervision_gaps(initial, validation))
    if unexpected:
        return {'status': 'translation_error', 'algorithm_version': ALGORITHM_VERSION,
                'input_fingerprint': snapshot['fingerprint'], 'additions': [], 'removals': [],
                'validation': validation, 'issues': unexpected,
                'message': 'The proposal introduced a new conflict; no changes can be applied.'}
    by_id = {s['id']: s for s in shifts}
    for removal in removals:
        source = by_id[removal['shift_id']]
        removal.update(duty_labels(data, source))
    from .wish_reasons import declined_wishes
    summary = replacement_summary(data, removals)
    if any(s['type_snapshot']['definition'].get('extra_cover', False) for s in shifts if s['id'] in focus):
        from .solver_summary import summarise
        summary.update(summarise(data, validation, [], focus & selected, trial_limit=0))
    return {"status": "unknown_time_limit" if timed_out else "candidate_feasible" if validation["valid"] else "candidate_with_gaps",
            "algorithm_version": ALGORITHM_VERSION, "input_fingerprint": snapshot["fingerprint"],
            "additions": added, "removals": removals, "validation": validation,
            "issues": [f for f in validation['findings'] if f['severity'] == 'error'
                       and f['code'] not in COVERAGE_CODES | {'QUOTA_BELOW_MINIMUM'}],
            "summary": summary,
            "declined_wishes": declined_wishes(data, validation, preferences_applied=False),
            "unmet_requirements": unmet_requirements(validation),
            "message": "Draft candidate only. No optimality or infeasibility proof; acceptance requires current-input validation and ordinary review/publication."}
