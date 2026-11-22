"""Workload limits on the same reserved windows used by validation."""
from bisect import bisect_left
from .solver_provenance import policy_name
from collections import defaultdict
from functools import cache
from datetime import timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model

from .cp_sat import runs

from .conditions import counting_views, has_session_lines, scope_tree
from .time import aware, local_midnight
from .validation import (ancestor_ids, exception_for, named_people, policy_applies,
                         workload_matches, workload_windows, person_rows, workload_gap_blockers,
                         workload_day_groups, workload_weekend_groups, consecutive_day_count)


def add_workload_limits(model, data, by_person, pairings, penalise):
    """`penalise(expression, policy, worst, bound)` puts an advisory breach on the common scale."""
    check = data.get('_check_budget', lambda: None)
    original_penalise = penalise
    zone, focus = data['organisation']['timezone'], set(data['focus_ids'])
    editable = set(data.get('_generation_editable', focus))

    def fixed_context(row):
        """A non-editable assignment that is already present in the draft."""
        value = row[4]
        return (row[0]['id'] not in editable and
                hasattr(value, 'domain') and value.domain.min() == value.domain.max() == 1)

    @cache
    def windows_of(a, b, window, days):
        return workload_windows(a, b, {'window': window, 'window_days': days}, zone, data['organisation']['week_start'])
    pairs_by_trainee = defaultdict(list)
    for (sid, pid, supervisor), pair in sorted(pairings.items()):
        pairs_by_trainee[sid, pid].append((supervisor, pair))

    flags = {}

    def either(xs):
        """A literal that is true exactly when any of xs is; one per distinct set of duties."""
        xs = [model.new_constant(x) if isinstance(x, int) else x for x in xs]
        # Keep fixed work explicit for keep_no_worse's bound propagation.
        if any(x.domain.min() == x.domain.max() == 1 for x in xs):
            return model.new_constant(1)
        xs = [x for x in xs if x.domain.min() != 0 or x.domain.max() != 0]
        if not xs:
            return model.new_constant(0)
        if all(isinstance(x, cp_model.IntVar) and x.is_boolean for x in xs):
            if any(x.domain.min() == 1 for x in xs):
                return model.new_constant(1)
            key = frozenset(x.index for x in xs)
            if len(key) == 1:
                return xs[0]
            if key in flags:
                return flags[key]
        else:
            key = None
        flag = model.new_bool_var('work_present')
        if key is not None:
            # Preserve the cached literal without expanding Boolean max into integer rows.
            model.add_bool_or(xs).only_enforce_if(flag)
            model.add_bool_and([x.Not() for x in xs]).only_enforce_if(flag.Not())
            flags[key] = flag
        else:
            model.add_max_equality(flag, xs or [0])
        return flag

    def soft_excess(expression, maximum, active, bound, name):
        excess = model.new_int_var(0, bound, name)
        raw = model.new_int_var(0, bound, name + '_ungated') if active else excess
        model.add_max_equality(raw, [0, expression - maximum])
        if active:
            model.add(excess == raw).only_enforce_if(active)
            for flag in active:
                model.add(excess == 0).only_enforce_if(flag.Not())
        return excess

    for policy in counting_views(data, data['policies']):
        check()
        rule, scope = policy['definition']['rule'], policy['definition']['scope']
        if rule['kind'] not in {'max_hours', 'max_duties', 'max_consecutive_days', 'travel_buffer', 'max_consecutive_weekends'}:
            continue
        named = named_people(scope)
        for pid, entries in sorted(by_person.items()):
            check()
            def penalise(expression, policy, worst=1, bound=1):
                original_penalise(expression, {**policy, '_score_person': pid}, worst, bound)
            if named is not None and pid not in named:
                continue  # the rule names other people (policy_applies would say no to every duty)
            applies = {}  # regular and overtime forms of a duty share one answer
            for row in entries:
                key = row[0]['id'], row[2], row[3]
                if row[0]['id'] in focus and key not in applies:
                    applies[key] = policy_applies(data, policy, pid, row[0], row[2], row[3])
            focal = [row for row in entries if row[0]['id'] in focus and applies[row[0]['id'], row[2], row[3]]
                and (rule.get('include_overtime', True) or not row[1]['overtime'])]
            if not focal:
                continue
            selected = [row for row in entries if workload_matches(data, policy, pid, row[0], row[2], row[3])
                and (rule.get('include_overtime', True) or not row[1]['overtime'])]
            for r in person_rows(data, 'availability', pid):
                if r['person_id'] != pid or r['kind'] != 'external_commitment':
                    continue
                a, b = aware(r['start']), aware(r['end'])
                if not workload_matches(data, policy, pid, None, a, b):
                    continue
                unknown = (rule.get('overtime_only') or rule.get('group_by') == 'supervisor' or scope.get('service_id') or scope.get('service_ids')
                    or scope.get('location_id') or scope.get('assignment_type_id')
                    or scope.get('weekdays', list(range(7))) != list(range(7))
                    or scope.get('start_time', '00:00') != '00:00' or scope.get('end_time', '24:00') != '24:00'
                    or not rule.get('include_overtime', True) or has_session_lines(scope_tree(scope)))
                if unknown:
                    near = [x for _, _, c, d, x in focal if a < d + timedelta(days=31) and c - timedelta(days=31) < b]
                    if policy['definition']['severity'] == 'mandatory':
                        model.add(sum(near) == 0).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal]))
                    else:
                        for x in near:
                            penalise(x, policy)
                    continue
                selected.append(({'id': 'external:' + r['id'], 'location_id': None}, {'overtime': False}, a, b, model.new_constant(1)))
            if rule['kind'] == 'travel_buffer':
                # Focus duties at identical times and place are one run (see cp_sat.runs): one pair per pair of slots.
                applying = {(row[0]['id'], row[2], row[3], row[1]['overtime']) for row in focal}
                ordered = runs(sorted(selected, key=lambda row: (row[2], row[3])), lambda row: (
                    row[2], row[3], row[0]['location_id'], (row[0]['id'], row[2], row[3], row[1]['overtime']) in applying)
                    if row[0]['id'] in focus and row[2] < row[3] else None)
                enabled = None
                for i, (s, sp, a, b, x) in enumerate(ordered):
                    blockers = None
                    for j in range(i + 1, len(ordered)):
                        t, tp, c, d, y = ordered[j]
                        if c - b >= timedelta(minutes=rule['minutes']):
                            break
                        if s['id'] not in focus and t['id'] not in focus:
                            continue
                        if c < b:
                            continue  # overlapping work is exclusive already
                        known = s['location_id'] and t['location_id']
                        if known and not (rule['from_location_id'] in ancestor_ids(data, s['location_id'])
                                          and rule['to_location_id'] in ancestor_ids(data, t['location_id'])):
                            continue
                        if exception_for(data, policy, pid, min(b, c), max(b, c)):
                            continue
                        if blockers is None:
                            blockers = workload_gap_blockers(ordered, i)
                        expression = x + y - sum(choice for k, choice in blockers if k < j)
                        # The rule is on while any duty it applies to is chosen: when one of this pair is such a
                        # duty, the pair itself switches it on, so no condition is needed.
                        if ((s['id'], a, b, sp['overtime']) in applying or (t['id'], c, d, tp['overtime']) in applying):
                            active = []
                        else:
                            if enabled is None:
                                enabled = either([x for _, _, _, _, x in focal])
                            active = [enabled]
                        if policy['definition']['severity'] == 'mandatory':
                            model.add(expression <= 1).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if(active)
                        else:
                            violation = soft_excess(expression, 1, active, 1, 'travel_preference')
                            penalise(violation, policy)
                continue
            if rule['kind'] == 'max_consecutive_weekends':
                weekends, focused, overtime = defaultdict(list), defaultdict(list), defaultdict(list)
                context_weekends, context_overtime = defaultdict(bool), defaultdict(bool)
                focal_ids = {id(row) for row in focal}
                for first, rows in workload_weekend_groups(data, policy, pid, selected).items():
                    for row in rows:
                        x = row[4]
                        weekends[first].append(x)
                        if id(row) in focal_ids:
                            focused[first].append(x)
                        if row[1]['overtime']:
                            overtime[first].append(x)
                        if fixed_context(row):
                            context_weekends[first] = True
                            if row[1]['overtime']:
                                context_overtime[first] = True
                worked = {day: either(weekends[day]) for day in sorted(weekends)}
                any_focal = either([x for _, _, _, _, x in focal])
                for last in sorted(weekends):
                    window = [last - timedelta(weeks=i) for i in range(rule['maximum'], -1, -1)]
                    if not all(day in worked for day in window) or exception_for(data, policy, pid,
                        local_midnight(window[0], zone), local_midnight(last + timedelta(days=rule['weekend_days']), zone)):
                        continue
                    enabled = [any_focal, either([x for day in window for x in focused[day]])]
                    if rule['overtime_only']:
                        enabled.append(either([x for day in window for x in overtime[day]]))
                    expression = sum(worked[day] for day in window)
                    if policy['definition']['severity'] == 'mandatory':
                        context_count = sum((context_overtime if rule['overtime_only'] else context_weekends).get(day, False)
                                            for day in window)
                        # A fixed context breach is report-only.  A new duty may
                        # not add another worked weekend to that same window.
                        bound = max(rule['maximum'], context_count)
                        model.add(expression <= bound).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if(enabled)
                    else:
                        violation = soft_excess(expression, rule['maximum'], enabled, 1, 'weekend_preference')
                        penalise(violation, policy)
                continue
            if rule['kind'] == 'max_consecutive_days':
                groups, focus_groups = workload_day_groups(selected, zone), workload_day_groups(focal, zone)
                day_terms = {day: [row[4] for row in rows] for day, rows in groups.items()}
                focal_terms = {day: [row[4] for row in focus_groups.get(day, [])] for day in groups}
                days = sorted(day_terms)
                worked = {day: either(day_terms[day]) for day in days}
                extension = rule.get('overtime_extension_days', 0)
                ordinary = {day: either([row[4] for row in rows if not row[1]['overtime']])
                            for day, rows in groups.items()} if extension else {}
                has_exceptions = policy['definition'].get('exception_allowed') and any(
                    e['policy_id'] == policy['id'] and e['person_id'] == pid for e in data['exceptions'])
                context_days = {day: any(fixed_context(row) for row in rows)
                                for day, rows in groups.items()}
                if policy['definition']['severity'] == 'mandatory' and not has_exceptions and not extension:
                    # A run longer than the maximum that touches rota work always contains maximum+1
                    # consecutive worked days with rota work among them, and vice versa: one sliding
                    # window per day instead of run-length counters.
                    k = rule['maximum']
                    focal_index = {x.index for xs in focal_terms.values() for x in xs}
                    for i in range(len(days) - k):
                        window = days[i:i + k + 1]
                        if window[-1] - window[0] != timedelta(days=k):
                            continue
                        focal_here = [x for day in window for x in focal_terms[day]]
                        if not focal_here:
                            continue
                        context_count = sum(context_days.get(day, False) for day in window)
                        constraint = model.add(sum(worked[day] for day in window) <= max(k, context_count)).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal]))
                        if not all(x.index in focal_index for day in window for x in day_terms[day]):
                            constraint.only_enforce_if(either(focal_here))
                    continue
                previous_run, previous_focus, previous_ordinary = 0, 0, 0
                for i, day in enumerate(days):
                    if not i or day - days[i - 1] != timedelta(days=1):
                        previous_run, previous_focus, previous_ordinary = 0, 0, 0
                    length = model.new_int_var(0, len(days), 'consecutive_days')
                    model.add(length == previous_run + 1).only_enforce_if(worked[day])
                    model.add(length == 0).only_enforce_if(worked[day].Not())
                    counted = length
                    if extension:
                        ordinary_length = model.new_int_var(0, len(days), 'ordinary_consecutive_days')
                        model.add(ordinary_length == previous_ordinary + ordinary[day]).only_enforce_if(worked[day])
                        model.add(ordinary_length == 0).only_enforce_if(worked[day].Not())

                        def maximum_of(*expressions):
                            maximum = model.new_int_var(0, len(days), 'counted_consecutive_days')
                            model.add_max_equality(maximum, expressions)
                            return maximum

                        counted = consecutive_day_count(rule, length, ordinary_length, max_of=maximum_of)
                        previous_ordinary = ordinary_length
                    current_focus = either([previous_focus] + focal_terms[day])
                    focused_run = model.new_bool_var('focused_run')
                    model.add_min_equality(focused_run, [worked[day], current_focus])
                    # Without exceptions one counter suffices. Exceptions are
                    # tied to the actual run start, not arbitrary sliding windows.
                    if has_exceptions:
                        for n in range(rule['maximum'] + 1, i + 2):
                            first = day - timedelta(days=n - 1)
                            if first not in worked or exception_for(data, policy, pid,
                                    local_midnight(first, zone), local_midnight(day + timedelta(days=1), zone)):
                                continue
                            exact = model.new_bool_var('run_length')
                            model.add(length == n).only_enforce_if(exact)
                            model.add(length != n).only_enforce_if(exact.Not())
                            if policy['definition']['severity'] == 'mandatory':
                                model.add(counted <= rule['maximum']).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if([exact, focused_run])
                            elif extension:
                                excess = soft_excess(counted, rule['maximum'], [exact, focused_run], len(days), 'consecutive_preference')
                                penalise(excess, policy, min(3, max(1, len(days) - rule['maximum'])), len(days))
                            else:
                                violation = model.new_bool_var('consecutive_preference')
                                model.add_min_equality(violation, [exact, focused_run])
                                penalise((n - rule['maximum']) * violation, policy,
                                    min(3, max(1, len(days) - rule['maximum'])), len(days))
                    elif policy['definition']['severity'] == 'mandatory':
                        model.add(counted <= rule['maximum']).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if(focused_run)
                    else:
                        excess = soft_excess(counted, rule['maximum'], [focused_run], len(days), 'consecutive_preference')
                        # Excess days along a run; three days over is the worst plausible value.
                        penalise(excess, policy, min(3, max(1, len(days) - rule['maximum'])), len(days))
                    previous_run, previous_focus = length, focused_run
                continue
            windows = defaultdict(list)
            for _, _, a, b, x in selected:
                for interval in sorted(windows_of(a, b, rule['window'], rule.get('window_days'))):
                    windows[interval].append(x)
            # Rows sorted by start, so each window looks only at nearby rows (not every row per window).
            selected.sort(key=lambda row: (row[2], row[3]))
            starts = [row[2] for row in selected]
            longest = max((row[3] - row[2] for row in selected), default=timedelta(0))
            focal_ids = {id(row) for row in focal}
            for (a, b), origins in sorted(windows.items()):
                nearby = [row for row in selected[bisect_left(starts, a - longest):bisect_left(starts, b)] if a < row[3]]
                focal_here = [row[4] for row in nearby if id(row) in focal_ids]
                if not focal_here or exception_for(data, policy, pid, a, b):
                    continue
                gate = []

                def enabled(origins=origins, focal_here=focal_here):
                    """The window is on: some duty touching it and some duty the rule applies to (made when needed)."""
                    if not gate:
                        gate.extend([either(origins), either(focal_here)])
                    return gate
                groups = defaultdict(list)
                focused_groups = defaultdict(list)
                context_totals = defaultdict(int)
                if rule['kind'] == 'max_hours':
                    matching = nearby
                    counted_rows = matching
                    boundaries = sorted({a, b} | {max(a, c) for _, _, c, d, _ in matching}
                                        | {min(b, d) for _, _, c, d, _ in matching})
                    for lo, hi in zip(boundaries, boundaries[1:]):
                        covering = [row for row in matching if row[2] <= lo and hi <= row[3]]
                        # Work covering one stretch of time overlaps, so at most one of it is chosen (unless two
                        # rows are fixed work outside the focus): the sum is the "any" literal, without a new one.
                        if sum(row[0]['id'] not in focus for row in covering) <= 1:
                            present = sum(row[4] for row in covering)
                        else:
                            present = either([row[4] for row in covering])
                        minutes = (hi - lo) // timedelta(microseconds=1)
                        groups[None].append(minutes * present)
                        if any(fixed_context(row) for row in covering):
                            context_totals[None] += minutes
                    # Integer microseconds preserve timestamps; canonical comparison allows 1e-9 hours.
                    maximum = int(Fraction(str(rule['hours'])) * 3_600_000_000 + Fraction(18, 5))
                    ceiling = (b - a) // timedelta(microseconds=1)
                else:
                    maximum, ceiling = rule['maximum'], len(selected)
                    counted_rows = selected[bisect_left(starts, a):bisect_left(starts, b)]
                    for row in counted_rows:
                        shift, participant, c, d, x = row
                        group = rule.get('group_by', 'all')
                        if group == 'supervisor':
                            pairs = pairs_by_trainee[shift['id'], pid]
                            if not pairs and participant.get('supervisor_person_id'):
                                pairs = [(participant['supervisor_person_id'], x)]
                            if not pairs:
                                if policy['definition']['severity'] == 'mandatory':
                                    model.add(x == 0).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if(enabled())
                                else:
                                    penalise(soft_excess(x, 0, enabled(), 1, 'supervisor_preference'), policy)
                            if pairs:
                                missing = model.new_bool_var('supervisor_attribution_missing')
                                model.add_max_equality(missing, [0, x - sum(pair for _, pair in pairs)])
                                if policy['definition']['severity'] == 'mandatory':
                                    model.add(missing == 0).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if(enabled())
                                else:
                                    penalise(soft_excess(missing, 0, enabled(), 1, 'supervisor_preference'), policy)
                            for supervisor, pair in pairs:
                                counted = model.new_bool_var('supervised_work')
                                model.add_min_equality(counted, [pair, x])
                                groups[supervisor].append(counted)
                                if id(row) in focal_ids:
                                    focused_groups[supervisor].append(counted)
                        else:
                            key = c.astimezone(ZoneInfo(zone)).weekday() if group == 'weekday' else None
                            groups[key].append(x)
                            if fixed_context((shift, participant, c, d, x)):
                                context_totals[key] += 1
                            if id(row) in focal_ids:
                                focused_groups[key].append(x)
                # Calendar windows contain every duty that touches them, so when all counted work is in the
                # rota being scheduled the window is necessarily active: a plain (stronger) linear constraint.
                implied = (rule['window'] != 'rolling' and rule.get('group_by', 'all') != 'supervisor'
                           and all(id(row) in focal_ids for row in counted_rows))
                for key, terms in sorted(groups.items()):
                    active = [] if implied else enabled()
                    if not implied and rule['kind'] == 'max_duties' and rule.get('group_by') in {'supervisor', 'weekday'}:
                        active = enabled() + [either(focused_groups[key])]
                    if policy['definition']['severity'] == 'mandatory':
                        bound = max(maximum, context_totals.get(key, 0)) if rule['kind'] in {'max_hours', 'max_duties'} else maximum
                        model.add(sum(terms) <= bound).with_name(policy_name(policy, data=data, person_ids=[pid], session_ids=[row[0]['id'] for row in focal])).only_enforce_if(active)
                    else:
                        excess = soft_excess(sum(terms), maximum, active, ceiling, 'workload_preference')
                        # Over the limit by the limit itself (at least one duty or one hour) is fully broken.
                        penalise(excess, policy, max(maximum, 3_600_000_000 if rule['kind'] == 'max_hours' else 1), ceiling)
