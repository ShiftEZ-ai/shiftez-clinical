"""Bounded explanations of declined wishes from the returned proposal, never another solve.

Indexes are built once. Each declined day inspects one representative session and at most one
person-local trial; optional replacement proofs are capped per receipt. Evidence describes that
proposal; it is not proof that every alternative fails.
"""
import copy
from collections import defaultdict

from .patterns import breaches, day_of, wish_days, wish_evaluations, wish_shape
from .recurrence import inactive
from .time import aware


def pattern_reasons(wish, data, worked):
    """Observed personal shape breaches, not a counterfactual placement or cover explanation."""
    days = wish_days(wish, data)
    if not days:
        return []
    mine = worked.get(wish['person_id'], {})
    return [{'day': max(first, days[0]).isoformat(), 'kind': 'tradeoff', 'code': code,
             'message': words + '; this pattern was not honoured in the proposal. No single blocking rule or winning preference was established.'}
            for first, _last, code, words in breaches(wish_shape(wish), {day: mine[day] for day in days if mine.get(day)})]


def declined_wishes(data, validation=None, evidence=(), *, preferences_applied=True):
    from .conditions import active_rules
    from .generation import OWN_DUTY_CODES, Recheck, assignment_fixed, citation_finding_key
    from .participation import presence_intervals
    from .solver_summary import FINDING_NAMES, NOT_USABLE
    from .validation import evaluate, finding_observations, index_person_rows, is_open_extra_cover, rule_coverage_people

    check = data.get('_check_budget', lambda: None)
    check()
    zone = data['organisation']['timezone']
    if not data.get('wishes'):
        return []
    sessions, typed, duties, own = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(dict)
    worked = defaultdict(lambda: defaultdict(set))
    shifts = {}
    for shift in sorted(data['shifts'], key=lambda s: (s['start'], s['id'])):
        check()
        if inactive(shift):
            continue
        shifts[shift['id']] = shift
        day = day_of(shift['start'], zone)
        sessions[day].append(shift)
        typed[day, shift['assignment_type_id']].append(shift)
        for participant in shift['participants']:
            pid = participant['person_id']
            duties[pid, day_of(participant['start'], zone)].append((shift, participant))
            worked[pid][day_of(participant['start'], zone)].add(shift['assignment_type_id'])
            own[pid][shift['id']] = shift
    outcomes = wish_evaluations(data, worked)
    if not any(missed for _, _, missed in outcomes.values()):
        return []
    candidates = {(r['shift_id'], r['person_id']): r['exclusions'] for r in evidence}
    rules = {p['id']: p['name'] for p in data.get('policies', [])}
    facts = index_person_rows(data)
    context = ({**data, '_person_rows': facts, 'policies': active_rules(data['policies'], context=data)}
               if 'types' in data else None)
    needed_people = defaultdict(set)
    for row in (validation or {}).get('coverage', []):
        check()
        shift = shifts.get(row['shift_id'])
        if (not shift or is_open_extra_cover(shift['type_snapshot'].get('definition', {}))
                or not 0 < row.get('required', 0) or row['available_eligible'] > row['required']):
            continue
        a, b = aware(row['start']), aware(row['end'])
        if context is not None:
            present = rule_coverage_people(context, shift, a, b)
        elif row.get('scheduled') == row['available_eligible']:
            present = [p for p in shift['participants'] if any(lo <= a and b <= hi for lo, hi in presence_intervals(p))]
        else:
            present = []  # an older snapshot cannot identify which assigned people were eligible
        needed_people[shift['id']].update(p['person_id'] for p in present)
    existing = {citation_finding_key(f) for f in finding_observations(validation or {'findings': []})
                if f['severity'] == 'error'}
    # A fixed shortlist bounds explanation work even when everybody wants the same day.
    winners = defaultdict(list)
    for wish in data.get('wishes') or []:
        check()
        if wish['id'] not in outcomes or wish_shape(wish) is not None:
            continue
        _, count, missed = outcomes[wish['id']]
        for day in wish_days(wish, data):
            if day not in missed:
                winners[day].append((wish.get('weight', 0) / count, wish))
    for day in winners:
        winners[day].sort(key=lambda row: (-row[0], row[1]['id']))

    def session_fields(shift):
        return {'shift_id': shift['id'], 'session_label': shift['type_snapshot']['name'],
                'start': shift['start'], 'end': shift['end']}

    def rule_reason(exclusion, shift):
        code, rid = exclusion['code'], exclusion.get('rule_id')
        name = rules.get(rid) or FINDING_NAMES.get(code) or NOT_USABLE.get(code) or 'Session eligibility'
        return {'kind': 'rule', 'code': code, 'rule_id': rid, 'rule_name': name,
                'message': f"{name} prevents this assignment to {shift['type_snapshot']['name']} in this proposal.",
                **session_fields(shift)}

    capacity = {}
    recheck, replacement_checks, trial_shifts = None, 0, {}

    def placement_blocker(pid, shift):
        check()
        exclusions = candidates.get((shift['id'], pid))
        if exclusions:
            return rule_reason(next((e for e in exclusions if e.get('rule_id')), exclusions[0]), shift)
        if 'policies' not in data:
            return None  # old publication snapshots do not contain canonical evaluation inputs
        # Reuse the canonical validator on this person's commitments, not the department per wish.
        participant = {'id': 'wish-reason-' + pid, 'shift_id': shift['id'], 'person_id': pid,
                       'start': shift['start'], 'end': shift['end'], 'participation': 'staff', 'overtime': False}
        trial = {**own[pid], shift['id']: {**shift, 'participants': [*shift['participants'], participant]}}
        reduced = {**data, **{name: rows.get(pid, []) for name, rows in facts.items()},
                   'shifts': list(trial.values()), 'focus_ids': list(trial), 'skip_coverage': False,
                   '_partial': {'shifts': {shift['id']}, 'people': {pid}}}
        checked = evaluate(reduced, explain_findings=False)
        observations = list(finding_observations({'findings': [*checked['session_findings'], *checked['person_findings']]}))
        # One newly exhausted type place establishes that adding this person displaces an incumbent.
        capacity[pid, shift['id']] = [(aware(f['start']), aware(f['end'])) for f in observations
            if f['code'] == 'MAXIMUM_PARTICIPANTS' and f['severity'] == 'error'
            and f['detail']['scheduled'] == f['detail']['maximum'] + 1]
        errors = [f for f in observations if f['severity'] == 'error' and f.get('person_id') == pid
                  and citation_finding_key(f) not in existing
                  and f['code'] in OWN_DUTY_CODES | {'RESTRICTION', 'UNAVAILABLE', 'EXTERNAL_ROTATION'}]
        if errors:
            return rule_reason(min(errors, key=lambda f: (not bool(f.get('rule_id')), f['code'])), shift)
        return None

    def competing(wish, day, shift):
        nonlocal recheck, replacement_checks
        check()
        if not preferences_applied:
            return None
        daily = wish.get('weight', 0) / outcomes[wish['id']][1]
        for weight, other in winners.get(day, [])[:4]:
            if weight <= daily or other['person_id'] == wish['person_id']:
                continue
            pid = other['person_id']
            matches = (other['kind'] in {'prefer_on', 'prefer_shift_type'}
                       and any(p['person_id'] == pid for p in shift['participants']))
            if other['kind'] == 'prefer_shift_type':
                matches = matches and other.get('assignment_type_id') == shift['assignment_type_id']
            supporting = [s for s, _p in duties[pid, day] if other['kind'] == 'prefer_on'
                          or s['assignment_type_id'] == other.get('assignment_type_id')]
            matches = matches and bool(supporting) and all(s['id'] == shift['id'] for s in supporting) and any(
                lo <= a and b <= hi for p in shift['participants'] if p['person_id'] == pid
                for lo, hi in presence_intervals(p)
                for a, b in capacity.get((wish['person_id'], shift['id']), []))
            if not matches:
                continue
            if (shift.get('locked') or shift['id'] not in data.get('focus_ids', ())
                    or any(assignment_fixed(shift, p) for p in shift['participants'] if p['person_id'] == pid)):
                return None
            # Retained fill-gaps placements have no candidate row; only newly chosen incumbents can compete.
            if data.get('generation_options', {}).get('mode') == 'fill_gaps' and candidates.get((shift['id'], pid)) != []:
                return None
            # shortcut: 32 replacement proofs per receipt; raise only after rule-heavy profiling.
            if replacement_checks >= 32:
                return None
            replacement_checks += 1
            if recheck is None:
                # Deadline and cancellation checks belong to the live run, not the cloned proposal.
                proposal = copy.deepcopy(data, {id(check): check})
                trial_shifts.update((s['id'], s) for s in proposal['shifts'])
                recheck = Recheck(proposal)
            target = trial_shifts[shift['id']]
            before = target['participants']
            target['participants'] = [{**p, 'person_id': wish['person_id']} if p['person_id'] == pid else p for p in before]
            try:
                checked = recheck.trial([target], {wish['person_id'], *(p['person_id'] for p in before)})
            finally:
                target['participants'] = before
            if any(f['severity'] == 'error' and citation_finding_key(f) not in existing for f in finding_observations(checked)):
                return None
            name = data.get('people', {}).get(pid, {}).get('display_name', pid)
            return {'kind': 'competing_wishes', 'message': f"{name}'s honoured preference has higher daily weight "
                    f"({weight:g} versus {daily:g}) for this session in the proposal.", **session_fields(shift),
                    'competing_wishes': [{'wish_id': other['id'], 'person_id': pid, 'person_name': name,
                                         'kind': other['kind'], 'weight': other.get('weight', 0), 'daily_weight': weight}]}
        return None

    declined = []
    for wish in data.get('wishes') or []:
        check()
        share, _, missed = outcomes.get(wish['id'], (1, 0, []))
        if not missed:
            continue
        pattern = wish_shape(wish) is not None
        reasons = pattern_reasons(wish, data, worked) if pattern else []
        for day in (() if pattern else missed):
            check()
            assigned = duties[wish['person_id'], day]
            if wish['kind'] == 'avoid_shift_type':
                assigned = [(s, p) for s, p in assigned if s['assignment_type_id'] == wish['assignment_type_id']]
            options = typed[day, wish['assignment_type_id']] if wish['kind'] == 'prefer_shift_type' else sessions[day]
            shift = assigned[0][0] if wish['kind'] in {'prefer_off', 'avoid_shift_type'} and assigned else next(iter(options), None)
            on_session = bool(shift and any(p['person_id'] == wish['person_id'] for p in shift['participants']))
            reason = placement_blocker(wish['person_id'], shift) if shift and not on_session else None
            # Eligibility cannot prove a colleague replaces this person's cover, capability or supervision.
            reason = reason or (competing(wish, day, shift) if shift and not on_session else None)
            if not reason and on_session:
                if wish['person_id'] in needed_people[shift['id']]:
                    reason = {'kind': 'cover', 'message': f"You cover a required place on {shift['type_snapshot']['name']}.",
                              **session_fields(shift)}
            reason = reason or {'kind': 'tradeoff', 'message': 'This preference was not honoured in the returned proposal; '
                                'no single blocking rule or winning wish was established.'}
            reasons.append({'day': day.isoformat(), **reason})
        declined.append({'wish_id': wish['id'], 'person_id': wish['person_id'], 'kind': wish['kind'],
                         'weight': wish.get('weight', 0), 'honoured': share, 'reasons': reasons})
    return declined
