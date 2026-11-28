"""Explain existing lower-bound findings using canonical scope, credit and eligibility facts.

Opportunities are upper bounds: these hints never certify a feasible schedule or relax a rule.
All caches belong to one evaluation, including its current assignments and definitions.
"""
from datetime import date, timedelta
import json
import math
from zoneinfo import ZoneInfo

from .time import aware, local_midnight, overlaps, union_hours
from .recurrence import inactive, expand, stored_series_definition
from . import leadership

DIAGNOSTIC_VERSION = 'finding-causes-v3'

COVERAGE_CODES = frozenset({'COVERAGE_GAP', 'OPEN_EXTRA_COVER', 'COVERAGE_TARGET',
                           'SCOPED_COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP',
                           'SUPERVISION_GAP', 'LEAD_GAP'})

# Open-place coverage and person minima may worsen while a draft is rearranged.
# A new or worsened lead or supervision gap is a safety failure and refuses the edit.
MINIMUM_SHORTFALL_CODES = frozenset({'QUOTA_BELOW_MINIMUM'})
DRAFT_STAFFING_GAP_CODES = frozenset({'LEAD_GAP', 'SUPERVISION_GAP'})
EDIT_NON_REFUSING_CODES = (COVERAGE_CODES - DRAFT_STAFFING_GAP_CODES) | MINIMUM_SHORTFALL_CODES

# One server-owned classification for an approved leave that still has a duty.
# Home and the draft Fix list use the same value; the user-facing wording belongs
# to the projections, while this value identifies the underlying clinical fact.
DUTIES_DURING_APPROVED_LEAVE = 'duties_during_approved_leave'


def approved_leave_duty_ids(data, person_id):
    """Return assigned shifts for *person_id* that overlap approved leave.

    The snapshot contains only effective availability rows, so this helper is
    deliberately pure and does not infer leave from a quota or from a generic
    unavailable code.  A duty is included once, even when several leave rows
    overlap it.
    """
    leave = [row for row in data.get('availability', [])
             if row.get('person_id') == person_id and row.get('kind') == 'approved_leave']
    if not leave:
        return set()
    result = set()
    for shift in data.get('shifts', []):
        if shift.get('cancelled') or shift.get('closed'):
            continue
        if any(participant.get('person_id') == person_id and
               any(overlaps(aware(participant['start']), aware(participant['end']),
                             aware(row['start']), aware(row['end'])) for row in leave)
               for participant in shift.get('participants', [])):
            result.add(shift['id'])
    return result


def session_prefill_scope(scope):
    """Project people out of form defaults, retaining exact session condition alternatives."""
    from .conditions import SESSION_FIELDS, COUNT_BUCKET, has_session_lines
    from .validation import session_scope
    def tree(group):
        children = [tree(c) if 'conditions' in c else c
                    if c['field'] in SESSION_FIELDS or c['field'] == COUNT_BUCKET and has_session_lines({'conditions': [c]}) else None
                    for c in group.get('conditions', [])]
        if group.get('match', 'all') == 'any' and None in children:
            return None
        return {**group, 'conditions': [c for c in children if c is not None]} if any(children) else None
    result = session_scope(scope)
    if result.get('conditions'):
        projected = tree(result['conditions'])
        result = {k: v for k, v in result.items() if k != 'conditions'}
        if projected:
            result['conditions'] = projected
    return result


class FindingCauses:
    def __init__(self, data, *, coverage_availability=None):
        # Cause explanations are one evaluation. Keep their availability answers
        # off the caller's snapshot while sharing one map within this call.
        if coverage_availability is None and data.get('_coverage_availability_owned'):
            coverage_availability = data.get('_coverage_availability_cache')
        else:
            # A caller-supplied map has no safe lifetime proof; never share it across calls.
            coverage_availability = None
        if coverage_availability is None:
            coverage_availability = {}
        self.coverage_availability = coverage_availability
        self.data = {**data, '_coverage_availability_cache': self.coverage_availability}
        self.candidates = {}
        self.sessions = {}
        self.series = {}
        self.capacities = {}
        self.coverage_pools = {}
        self.coverage_roles = {}
        self.qualified_pools = {}
        self.quota_facts = {}
        self.shifts = {s['id']: s for s in data['shifts']}

    def leave_classification(self, person_id, shift_id=None):
        shift_ids = approved_leave_duty_ids(self.data, person_id)
        if shift_id is not None and shift_id not in shift_ids:
            return {}
        return ({'classification': DUTIES_DURING_APPROVED_LEAVE,
                 'leave_shift_ids': sorted(shift_ids)} if shift_ids else {})

    def exclusions(self, pid, shift):
        key = pid, shift['id']
        if key not in self.candidates:
            from .candidate_evidence import candidate_evidence
            self.candidates[key] = frozenset(r['code'] for r in candidate_evidence(self.data, pid, shift)['exclusions'])
        return self.candidates[key]

    def prefill(self, policy, a, b, pid=None, shifts=()):
        zone = ZoneInfo(self.data['organisation']['timezone'])
        scope = session_prefill_scope(policy['definition']['scope']) if policy else {}
        rule = policy['definition']['rule'] if policy else {}
        result = {'starts_on': a.astimezone(zone).date().isoformat(),
                'ends_on': ((b - timedelta(microseconds=1)).astimezone(zone).date() + timedelta(days=1)).isoformat(),
                'assignment_type_ids': list(rule.get('assignment_type_ids') or
                    ([scope['assignment_type_id']] if scope.get('assignment_type_id') else [])),
                'service_ids': list(scope.get('service_ids') or ([scope['service_id']] if scope.get('service_id') else [])),
                'location_id': scope.get('location_id'), 'weekdays': scope.get('weekdays'),
                'start_time': scope.get('start_time'), 'end_time': scope.get('end_time'),
                'person_id': pid, 'shift_ids': [s['id'] for s in shifts],
                'rule_id': policy['id'] if policy else None, 'scope': scope}
        from .conditions import absolute_values
        def required(group):
            if group.get('match', 'all') == 'any' and len(group.get('conditions', [])) > 1:
                return
            for line in group.get('conditions', []):
                if 'conditions' in line:
                    yield from required(line)
                elif line.get('op', 'any') in {'any', 'is', 'all'}:
                    yield line['field'], absolute_values(line)
        for field, values in required(scope.get('conditions') or {}):
            if values and field in {'assignment_type_ids', 'service_ids'} and not result[field]:
                result[field] = sorted(values)
            if values and field == 'location_ids' and len(values) == 1 and not result['location_id']:
                result['location_id'] = next(iter(values))
        if policy and policy.get('count_bucket'):
            from .conditions import bucket_atoms
            for field in ('assignment_type_ids', 'service_ids', 'location_ids'):
                values = bucket_atoms(policy['count_bucket'], field)
                if values and field in result:
                    result[field] = list(values)
                elif values and field == 'location_ids' and len(values) == 1:
                    result['location_id'] = values[0]
        return result

    def matching_series(self, prefill, matches):
        key = json.dumps({k: v for k, v in prefill.items() if k not in {'person_id', 'rule_id'}}, sort_keys=True)
        if key not in self.series:
            found = []
            for series in self.data.get('recurring_series', []):
                if series['department_id'] != self.data['workspace']['department_id']:
                    continue
                definition = stored_series_definition(series['definition'])
                # Extend the existing pattern into the requested dates without changing its stored state.
                occurrences = expand(definition, date.fromisoformat(prefill['starts_on']), date.fromisoformat(prefill['ends_on']))
                if not any(matches({'id': series['id'], 'assignment_type_id': series['assignment_type_id'],
                    'service_ids': definition.service_ids, 'location_id': series['location_id'],
                    'start': row['start'].isoformat(), 'end': row['end'].isoformat(),
                    'series_id': series['id'], 'series_snapshot': series,
                    'type_snapshot': self.data['types'][series['assignment_type_id']], 'participants': []}) for row in occurrences):
                    continue
                found.append({**{k: series.get(k) for k in ('id', 'revision', 'name', 'active', 'starts_on', 'ends_on')},
                    'action': 'reactivate' if not series['active'] else 'extend'
                    if series['starts_on'] > prefill['starts_on'] or series.get('ends_on') and series['ends_on'] < prefill['ends_on'] else 'expand'})
            self.series[key] = found
        return self.series[key]

    def explain(self, code, prefill, *, sessions, eligible, places, required, possible, days_available=None,
                unavailable=frozenset(), matches=None, actions=None):
        days = (date.fromisoformat(prefill['ends_on']) - date.fromisoformat(prefill['starts_on'])).days
        cause = {'code': code, 'sessions_found': len(sessions), 'eligible_people': len(eligible),
                 'places_available': places, 'days_in_window': days, 'days_available': days_available,
                 'required': required, 'possible': round(possible, 4), 'basis': 'known_records_not_solver_proof'}
        actions = actions or {'no_candidate_sessions': ['add_sessions'], 'no_eligible_people': ['open_eligibility'],
                   'empty_population': ['open_rotation'],
                   'capacity_short': ['raise_capacity', 'add_sessions'], 'person_unavailable': ['open_leave'],
                   'partial_window': ['add_sessions'], 'unfilled': ['run_auto_schedule'],  # Sessions, not rota dates.
                   'above_limit': ['review_assignments']}[code]
        if code == 'person_unavailable' and 'EXTERNAL_ROTATION' in unavailable:
            actions = (['open_leave'] if 'UNAVAILABLE' in unavailable else []) + ['open_rotation']
        hints = [{'action': action, 'prefill': prefill, 'matching_series':
                  self.matching_series(prefill, matches) if action == 'add_sessions' and matches else []}
                 for action in actions]
        return {'cause': cause, 'fix_hints': hints}

    def capacity(self, shift, a, b):
        """Configured upper headcount, never a minimum masquerading as a maximum."""
        from .validation import effective_type_definition, session_scope, scope_matches_throughout, within_dates
        key = shift['id'], a, b
        if key in self.capacities:
            return self.capacities[key]
        definition = effective_type_definition(self.data, shift)
        start = aware(shift['start'])
        maxima = [r['maximum'] for r in definition['requirements'] if r.get('maximum') is not None
                  and overlaps(a, b, start + timedelta(minutes=r['from_minute']),
                               start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else aware(shift['end']))]
        for policy in self.data['policies']:
            rule = policy['definition']['rule']
            if (rule['kind'] == 'maximum_participants' and policy['definition']['severity'] == 'mandatory'
                    and policy['status'] == 'active'
                    and policy['department_id'] == self.data['types'][shift['assignment_type_id']]['department_id']
                    and within_dates(policy, a, b, self.data['organisation']['timezone'])
                    and rule.get('aggregation', 'assignment') == 'assignment'
                    and scope_matches_throughout(self.data, session_scope(policy['definition']['scope']), None, shift, a, b)):
                # A person-specific ceiling cannot bound everybody's capacity.
                from .validation import person_free
                if person_free(policy['definition']['scope']):
                    maxima.append(rule['maximum'] + rule.get('overflow_maximum', 0))
        self.capacities[key] = min(maxima) if maxima else None
        return self.capacities[key]

    def quota(self, policy, pid, a, b, amount, *, state=None, actual=None, proration=None):
        from .quotas import quota_contribution
        from .validation import person_rows, participation_for, lead_qualified
        data, rule = self.data, policy['definition']['rule']
        selector = {**policy, 'definition': {**policy['definition'], 'scope': session_prefill_scope(policy['definition']['scope'])}}

        def contribution(shift, person):
            participant = {'id': 'opportunity', 'person_id': person or pid, 'start': shift['start'], 'end': shift['end'],
                           'participation': participation_for(data, person, shift, aware(shift['start']), aware(shift['end']))
                               if person is not None else (rule['participation'] or ['staff'])[0],
                           'overtime': rule['overtime'] == 'only', 'team_lead': rule['metric'] == 'lead_turns'}
            return quota_contribution(data, policy if person is not None else selector, person, shift, participant, [], a, b)

        def matches(shift):
            if inactive(shift) or data['types'][shift['assignment_type_id']]['department_id'] != policy['department_id']:
                return False
            result = contribution(shift, None)
            metric = rule['metric']
            return result is not None and (result['duty'] if metric in {'duties', 'lead_turns', 'session_units'}
                else bool(result['weekends'] if metric == 'weekends' else result['intervals']))

        weekend_shape = [rule.get('weekend_start'), rule.get('weekend_days')] if rule['metric'] == 'weekends' else None
        key = json.dumps([policy['department_id'], selector['definition']['scope'], weekend_shape,
                          {k: rule[k] for k in ('comparison', 'metric', 'assignment_type_ids', 'participation', 'overtime', 'starts_on', 'ends_on')},
                          a.isoformat(), b.isoformat(), policy.get('starts_on'), policy.get('ends_on')], sort_keys=True)
        if key not in self.sessions:
            self.sessions[key] = [s for s in data['shifts'] if matches(s)]
        sessions = self.sessions[key]
        prefill = self.prefill(policy, a, b, pid, sessions)
        first, last = date.fromisoformat(prefill['starts_on']), date.fromisoformat(prefill['ends_on'])
        if rule['comparison'] != 'maximum':
            prefill['starts_on'] = max(prefill['starts_on'], data['workspace']['starts_on'], policy.get('starts_on') or prefill['starts_on'])
            prefill['ends_on'] = min(prefill['ends_on'], data['workspace']['ends_on'], policy.get('ends_on') or prefill['ends_on'])
        key = json.dumps([pid, policy['definition']['scope'], weekend_shape, a.isoformat(), b.isoformat(),
            {k: rule[k] for k in ('metric', 'assignment_type_ids', 'participation', 'overtime')},
            rule['comparison'] == 'maximum', policy.get('starts_on'), policy.get('ends_on')], sort_keys=True)
        if key not in self.quota_facts:
            eligible, unavailable, intervals, possible, absence_only = set(), set(), [], 0, False
            places, blocked_capacity = 0, False
            weekends = set()
            for shift in sessions:
                data.get('_check_budget', lambda: None)()
                credit = contribution(shift, pid)
                if credit is None or rule['metric'] == 'weekends' and not credit['weekends']:
                    continue
                exclusions = self.exclusions(pid, shift)
                if rule['metric'] == 'lead_turns':
                    lead = leadership.summary(data, shift)
                    if not lead or lead.get('maximum') == 0 or not lead_qualified(data, pid, shift, lead, aware(shift['start']), aware(shift['end'])):
                        exclusions |= {'LEAD_NOT_QUALIFIED'}
                unavailable.update(exclusions & {'UNAVAILABLE', 'EXTERNAL_ROTATION'})
                absence_only |= bool(exclusions) and exclusions <= {'UNAVAILABLE', 'EXTERNAL_ROTATION'}
                if exclusions:
                    continue
                eligible.add(pid)
                capacity = self.capacity(shift, aware(shift['start']), aware(shift['end']))
                # Existing occupants may be moved by auto-schedule; count the installed capacity, not vacancy.
                if capacity == 0:
                    blocked_capacity = True
                    continue
                places += 1  # One person can earn at most one duty on one session, irrespective of its seats.
                metric = rule['metric']
                possible += (int(credit['duty']) if metric in {'duties', 'lead_turns'} else credit['session_units']
                             if metric == 'session_units' else credit['credits'] if metric.endswith('credit') else 0)
                intervals.extend(credit['intervals'])
                weekends.update(credit['weekends'])
            if rule['metric'] in {'scheduled_hours', 'reserved_hours'}:
                possible = union_hours(intervals)
            elif rule['metric'] == 'weekends':
                possible = len(weekends)
            zone = data['organisation']['timezone']
            workspace = data['workspace']
            days = 0
            for offset in range((last - first).days):
                day = first + timedelta(days=offset)
                lo, hi = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
                if (workspace['starts_on'] <= day.isoformat() < workspace['ends_on']
                        and any(m['department_id'] == policy['department_id'] and m['starts_on'] <= day.isoformat()
                                and (not m.get('ends_on') or day.isoformat() < m['ends_on']) for m in person_rows(data, 'memberships', pid))
                        and not any(not r.get('department_available', True) and r['starts_on'] <= day.isoformat()
                                    and (not r.get('ends_on') or day.isoformat() < r['ends_on']) for r in person_rows(data, 'rotations', pid))):
                    from .conditions import scope_applies
                    if scope_applies(data, policy['definition']['scope'], pid, None, lo, hi):
                        days += 1
            self.quota_facts[key] = (eligible, unavailable, possible, absence_only, places, blocked_capacity, days)
        eligible, unavailable, possible, absence_only, places, blocked_capacity, days = self.quota_facts[key]
        # Precedence: upper bounds first; an empty rota whose lower bound proration actually reduced keeps the
        # partial-window cause reviewed; otherwise proration is metadata and the real blocker wins.
        scaled = bool(proration) and proration['amount'] < proration['configured_amount']
        code = ('above_limit' if state in {'above_maximum', 'above_target'} else
                'partial_window' if not sessions and scaled else
                'no_candidate_sessions' if not sessions else 'person_unavailable' if not eligible and absence_only
                else 'no_eligible_people' if not eligible
                else 'capacity_short' if possible + 1e-9 < amount else 'unfilled')
        actions = (['raise_capacity', 'add_sessions'] if blocked_capacity else ['add_sessions']) if code == 'capacity_short' else None
        result = self.explain(code, prefill, sessions=sessions, eligible=eligible, places=places,
                              required=amount, possible=possible, days_available=days, unavailable=unavailable,
                              matches=matches, actions=actions)
        result['cause']['days_in_window'] = (last-first).days
        result['cause'].update(self.leave_classification(pid))
        if actual is not None:
            result['cause']['actual'] = actual
        if proration:
            result['cause']['proration'] = proration
        return result

    def coverage(self, code, sid, a, b, detail, policy=None):
        from .validation import (effective_type_definition, capabilities_for, staffing_credit, staffing_peers, matching_size, qualified,
                                 qualified_supervisor, lead_qualified, participation_for, rule_coverage_people,
                                 available_for_coverage, scope_matches_throughout)
        data, shift = self.data, self.shifts[sid]
        peers = staffing_peers(data, policy, a, b) if policy and policy['definition']['rule'].get('aggregation') == 'service_group' else [shift]
        if code == 'LEAD_GAP' and detail.get('affected_shift_ids'):
            peers = [self.shifts[peer_id] for peer_id in detail['affected_shift_ids'] if peer_id in self.shifts] or [shift]
        eligible, population, qualified_population, places = set(), set(), set(), 0
        unbounded = False
        definition = effective_type_definition(data, shift)
        for peer in peers:
            # The coverage evaluator checks each authority/absence segment, not whole-duty eligibility.
            key = peer['id'], a, b
            if key not in self.coverage_pools:
                interval = a, b
                keys = [(pid, a, b) for pid in data['people']]
                if any(cache_key not in self.coverage_availability for cache_key in keys):
                    for pid in data['people']:
                        available_for_coverage(data, pid, a, b)
                if not any(not self.coverage_availability[cache_key] for cache_key in keys):
                    self.coverage_pools[key] = {}
                else:
                    # Restrictions may inspect other participants: keep the complete hypothetical pool.
                    start, end = a.isoformat(), b.isoformat()
                    possible_peer = {**peer, 'participants': [{'person_id': pid, 'start': start, 'end': end,
                        'participation': participation_for(data, pid, peer, a, b), 'overtime': False} for pid in data['people']]}
                    self.coverage_roles[key] = {p['person_id']: p['participation']
                                                for p in possible_peer['participants']}
                    presence = {(peer['id'], pid): [(a, b)] for pid in data['people']}
                    self.coverage_pools[key] = {p['person_id']: 1 for p in rule_coverage_people(
                        data, possible_peer, a, b, presence=presence)}
            candidates = self.coverage_pools[key]
            if detail.get('capability_id'):
                candidates = {pid: 1 for pid in candidates if detail['capability_id'] in capabilities_for(data, pid, peer, a, b)}
            if policy and code != 'LEAD_GAP':
                candidates = staffing_credit(data, policy, peer, a, b, candidates)
            if code == 'LEAD_GAP':
                candidates = {pid: 1 for pid in candidates if leadership.coverage_qualified(data, pid, peer, a, b)}
            if code == 'SUPERVISION_GAP':
                candidates = {pid: 1 for pid in candidates if qualified_supervisor(data, peer,
                    {'person_id': pid, 'participation': self.coverage_roles[key][pid]}, a, b)}
            eligible.update(candidates)
            if not candidates:
                # Separate dated population, clinical authority and absence. These are necessary bounds only.
                population_choices = {pid: 1 for pid in data['people']}
                if policy:
                    # Conditional credit is not population membership: a missing companion does not
                    # mean that a programme has no people enrolled during this session.
                    population_choices = {pid: 1 for pid in population_choices
                        if pid not in policy['definition']['rule'].get('when_person_ids', [])
                        and scope_matches_throughout(data, policy['definition']['scope'], pid, peer, a, b)}
                population.update(population_choices)
                if key not in self.qualified_pools:
                    self.qualified_pools[key] = {pid for pid in data['people'] if qualified(
                        data, pid, peer, definition, a, b)}
                qualified_population.update(set(population_choices) & self.qualified_pools[key])
            capacity = self.capacity(peer, a, b)
            unbounded |= capacity is None
            places += capacity or 0
        required = detail.get('required', detail.get('target', 1))
        if code == 'SUPERVISION_GAP':
            required = math.ceil(detail['trainees'] / definition['supervision']['trainees_per_supervisor'])
        possible = len(eligible)
        if code == 'DISTINCT_PERSON_GAP':
            start = aware(shift['start'])
            requirements = [r for r in definition['requirements'] if start + timedelta(minutes=r['from_minute']) <= a
                and b <= (start + timedelta(minutes=r['to_minute']) if r.get('to_minute') is not None else aware(shift['end']))]
            seats = [c['capability_id'] for r in requirements for c in r['capabilities'] for _ in range(c['minimum'])]
            possible = matching_size(seats, {pid: capabilities_for(data, pid, shift, a, b) for pid in eligible})
        prefill = self.prefill(policy, a, b, shifts=peers)
        if not policy:
            prefill.update(assignment_type_ids=[shift['assignment_type_id']], service_ids=shift['service_ids'], location_id=shift['location_id'])
        absent = qualified_population and all(self.coverage_availability[pid, a, b] for pid in qualified_population)
        cause = (('empty_population' if not population else 'person_unavailable' if absent else 'no_eligible_people')
                 if not eligible else 'capacity_short' if possible < required or not unbounded and places < required else 'unfilled')
        actions = ['open_eligibility'] if cause == 'capacity_short' and possible < required else None
        unavailable = {dict(row).get('code') for pid in qualified_population if absent
                       for row in self.coverage_availability[pid, a, b]}
        result = self.explain(cause, prefill, sessions=peers, eligible=eligible,
                              places=None if unbounded else places, required=required, possible=possible,
                              unavailable=unavailable, actions=actions)
        if not eligible:
            result['cause'].update(population_people=len(population), qualified_people=len(qualified_population))
        result['cause'].update(actual=detail.get('available', detail.get('qualified_supervisors', 0)),
                               eligible_people_basis='potential_cover_known_records')
        return result
