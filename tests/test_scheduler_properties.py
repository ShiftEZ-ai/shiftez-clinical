"""Seeded synthetic departments through the real auto-scheduler.

Each proposal is re-checked independently: by the canonical validator on a draft rebuilt
from the proposal (not the solver's own copy), and by plain-Python checks of the rules.
Infeasible departments must explain themselves instead of returning a partial rota.
"""
import copy
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from tests.scheduler_departments import START, apply, department

ZONE = ZoneInfo('Asia/Riyadh')
GAPS = {'COVERAGE_GAP', 'CAPABILITY_GAP', 'DISTINCT_PERSON_GAP', 'SCOPED_COVERAGE_GAP', 'QUOTA_BELOW_MINIMUM'}


def _local(value):
    return datetime.fromisoformat(value).astimezone(ZONE)


def _assert_no_mandatory_violation(data, result, info, allowed=frozenset()):
    """Independent validator run plus plain-Python checks of the configured department rules."""
    draft = apply(data, result)
    validation = evaluate(draft)
    errors = [f for f in validation['findings'] if f['severity'] == 'error' and f['code'] not in allowed]
    assert not errors, errors[:5]
    duties = defaultdict(list)
    for shift in draft['shifts']:
        for p in shift['participants']:
            duties[p['person_id']].append((shift, p))
    leave = defaultdict(list)
    for row in draft['availability']:
        leave[row['person_id']].append((_local(row['start']), _local(row['end'])))
    policies = info['policies']
    types = {v: k for k, v in info['types'].items()}
    for pid, work in duties.items():
        work.sort(key=lambda row: row[1]['start'])
        spans = [(_local(p['start']), _local(p['end'])) for _, p in work]
        assert pid != info['away'], 'Someone away on rotation was scheduled'
        for a, b in spans:
            assert not any(a < d and c < b for c, d in leave[pid]), 'Scheduled during leave'
        for (a, b), (c, d) in zip(spans, spans[1:]):
            assert b <= c, 'Overlapping duties'
            if 'min_rest' in policies:
                assert c - b >= timedelta(hours=11), 'Less than 11 hours rest'
        days = [a.date() for a, _ in spans]
        if 'one_a_day' in policies:
            assert len(days) == len(set(days)), 'Two duties on one day'
        if 'consecutive' in policies:
            worked = sorted({a.date() + timedelta(days=n) for a, b in spans
                             for n in range((((b - timedelta(microseconds=1)).date()) - a.date()).days + 1)})
            run = 1
            for x, y in zip(worked, worked[1:]):
                run = run + 1 if y - x == timedelta(days=1) else 1
                assert run <= 4, 'More than 4 consecutive days'
        if 'week_duties' in policies:
            weeks = defaultdict(int)
            for day in days:
                weeks[day - timedelta(days=(day.weekday() + 1) % 7)] += 1  # weeks start on Sunday
            assert max(weeks.values()) <= 5, 'More than 5 duties in a week'
        if 'night_then_morning' in policies:
            nights = {a.date() for (shift, _), (a, _) in zip(work, spans) if types.get(shift['assignment_type_id']) == 'night'}
            mornings = {a.date() for (shift, _), (a, _) in zip(work, spans) if types.get(shift['assignment_type_id']) == 'am'}
            assert not {day + timedelta(days=1) for day in nights} & mornings, 'Morning after a night'
        if 'monthly_cap' in policies:
            cap = next(p for p in draft['policies'] if p['id'] == policies['monthly_cap'])['definition']['rule']['amount']
            assert len(work) <= cap, 'Above the duty quota'
        if 'no_fridays' in policies and pid == next(p for p in draft['policies'] if p['id'] == policies['no_fridays'])['definition']['scope'].get('person_id'):
            assert all(a.weekday() != 4 for a, _ in spans), 'Restricted weekday worked'
    for shift in draft['shifts']:
        trainees = [p for p in shift['participants'] if p['person_id'] in info['trainees']]
        if trainees and 'supervision_cap' in policies and shift['type_snapshot']['definition'].get('supervision'):
            named = defaultdict(int)
            for p in trainees:
                assert p.get('supervisor_person_id') in {q['person_id'] for q in shift['participants']}, 'Trainee without a named supervisor'
                named[p['supervisor_person_id']] += 1
            assert max(named.values()) <= 2, 'Supervisor above capacity'
    return validation


# Even seeds are comfortably staffed; odd seeds are tight (5, 9 and 11 cannot be staffed under their rules).
@pytest.mark.parametrize('seed', (0, 1, 2, 3, 4, 5, 9, 11))
def test_varied_departments_never_break_a_mandatory_rule(h, seed):
    people = 10 + seed * 3 if seed % 2 == 0 else 6 + seed % 4
    days = (7, 10, 14)[seed % 3]
    data, info = department(h, seed, people=people, days=days, rules='random', leave_rate=0.1 if seed % 2 == 0 else 0.2)
    original = copy.deepcopy(data)
    result = generate(data, 3, deterministic=True)  # see the full-rule test: same result under any load
    assert data == original, 'The scheduler must not change its input'
    assert result['status'] in {'candidate_feasible', 'requirements_conflict'}, {k: result.get(k) for k in ('status', 'message', 'issues')}
    if result['status'] == 'candidate_feasible':
        validation = _assert_no_mandatory_violation(data, result, info)
        assert not [c for c in validation['coverage'] if c['status'] == 'gap']
        assert result['summary']['coverage_percent'] == 100 and result['summary']['open_place_count'] == 0
        assert result['model_size']['variables'] > 0
        return
    # Infeasible: explained, and nothing is proposed.
    assert result['additions'] == [] and result['removals'] == []
    assert result['diagnostics'] and all(d['message'] for d in result['diagnostics'])
    # "Fill what can be filled" still never breaks a mandatory rule; the open places carry reasons.
    partial = generate(data, 3, diagnostic_proposal=True, deterministic=True)
    assert partial['status'] == 'candidate_with_gaps', partial.get('message')
    _assert_no_mandatory_violation(data, partial, info, allowed=GAPS)
    assert partial['summary']['open_place_count'] > 0
    assert all(place['message'] for place in partial['summary']['open_places'])


def test_department_with_every_rule_kind_is_fully_covered_and_compliant(h):
    data, info = department(h, 3, people=32, days=14, rules='full', leave_rate=0.08)
    # Deterministic work budget (CP-SAT work units, one worker, fixed seed): the same schedule on any machine and under any
    # load. With a wall-clock budget a busy machine (pytest -n 4) searched less and sometimes missed full coverage.
    result = generate(data, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible', {k: result.get(k) for k in ('status', 'message', 'diagnostics')}
    validation = _assert_no_mandatory_violation(data, result, info)
    assert result['validation']['error_count'] == validation['error_count'] == 0
    summary = result['summary']
    assert summary['coverage_percent'] == 100 and summary['filled_places'] == summary['required_places'] > 0
    assert summary['workload']['people'] > 1 and summary['workload']['hours_max'] >= summary['workload']['hours_min']
    # Advisory rules may be bent, and each bent rule is reported by name with a count.
    warnings = [f for f in validation['findings'] if f['severity'] == 'warning']
    assert sum(item['count'] for item in summary['advisory']) == len(warnings)
    # Trainee pairing was chosen by counting capacity: every trainee is still named, within capacity.
    # Whether any trainee is chosen at all is not fixed: the duty types' ids are random per run and change CP-SAT's
    # variable order (CI chose none on some runs). Every trainee chosen must be named.
    assert info['trainees']
    trainees = [a for a in result['additions'] if a['participant']['participation'] == 'trainee']
    assert all(a['participant'].get('supervisor_person_id') for a in trainees)


def test_department_templates_preserve_published_neighbour_context(h):
    published = h.shift(h.workspace('Published neighbour'))['workspace']
    external = h.snapshot(published)['shifts'][0]
    h.publish(published)
    probe = h.workspace('Neighbour context probe', end='2026-11-01')
    before = h.snapshot(probe)
    assert before['focus_ids'] == []
    assert before['shifts'] == [external]

    data, info = department(h, 18, people=3, days=28, rules='none', leave_rate=0, clinics=7)
    assert len(data['shifts']) == len(set(data['focus_ids'])) == 420
    assert set(data['focus_ids']) == {shift['id'] for shift in data['shifts']}
    assert {shift['assignment_type_id'] for shift in data['shifts']} == set(info['types'].values())
    assert external['id'] not in data['focus_ids']

    original = h.snapshot(data['workspace'])
    own = [shift for shift in original['shifts'] if shift['id'] in original['focus_ids']]
    assert len(own) == len(original['focus_ids']) == 3
    assert {shift['assignment_type_id'] for shift in own} == set(info['types'].values())
    assert [shift for shift in original['shifts'] if shift['id'] not in original['focus_ids']] == [external]
    assert h.snapshot(probe)['shifts'] == before['shifts']


def test_everyone_on_leave_is_reported_as_a_verified_shortage(h):
    data, info = department(h, 11, people=8, days=7, rules='none', leave_rate=0)
    day = START + timedelta(days=2)
    for pid in data['people']:
        data['availability'].append({'id': 'all-away-' + pid, 'person_id': pid, 'kind': 'recorded_leave',
            'start': f'{day}T00:00:00+03:00', 'end': f'{day + timedelta(days=1)}T00:00:00+03:00'})
    lo, hi = _local(f'{day}T00:00:00+03:00'), _local(f'{day + timedelta(days=1)}T00:00:00+03:00')
    touches_day = {s['id'] for s in data['shifts'] if _local(s['start']) < hi and lo < _local(s['end'])}
    result = generate(data, 10)
    assert result['status'] == 'requirements_conflict'
    assert result['additions'] == [] and result['removals'] == []
    shortages = [d for d in result['diagnostics'] if d['code'] == 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES']
    assert {d['shift_id'] for d in shortages} == touches_day
    for shortage in shortages:
        assert shortage['available_upper_bound'] == 0 < shortage['required']
        assert 'nobody eligible is free' in shortage['message']
        assert all(any(e['code'] == 'UNAVAILABLE' for e in c['exclusions']) for c in shortage['candidates'])
    partial = generate(data, 10, diagnostic_proposal=True)
    assert partial['status'] == 'candidate_with_gaps'
    places = partial['summary']['open_places']
    assert {p['shift_id'] for p in places} == touches_day
    assert all('unavailable' in p['message'] for p in places)
    # Each person says why, with leave only as LEAVE (never its private reason).
    for place in places:
        assert 'LEAVE' in {p['kind'] for p in place['people']} <= {'LEAVE', 'AWAY'}  # some are away on rotation
        assert all(p['code'] in ('UNAVAILABLE', 'EXTERNAL_ROTATION') and p['rule'] is None for p in place['people'])
        assert {p['person_id'] for p in place['people']} <= set(data['people'])


def test_rule_conflict_names_the_rule_and_partial_fill_says_why(h):
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'})
    typ = h.configure('assignment_types', {'name': 'Pair clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    w = h.shift(w, typ=typ, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    assert len(data['people']) == 3  # four places, three people, one duty each per day
    result = generate(data, 10)
    assert result['status'] == 'requirements_conflict'
    assert result['additions'] == []
    conflict = next(d for d in result['diagnostics'] if d['code'] == 'CONFLICTING_REQUIREMENTS')
    source = next(p for p in data['policies'] if p['definition']['rule']['kind'] == 'max_duties')
    assert conflict['proven'] and any(r['kind'] == 'policy' and r['rule_id'] == source['id']
                                      and r['revision'] == source['revision'] and r['rule_kind'] == 'max_duties'
                                      and r['name'] == source['name'] for r in conflict['requirements'])
    assert all('definition' not in r for r in conflict['requirements'])
    partial = generate(data, 10, diagnostic_proposal=True)
    assert partial['status'] == 'candidate_with_gaps'
    assert len(partial['additions']) == 3
    (place,) = partial['summary']['open_places']
    assert place['open'] == 1 and place['required'] == 2
    rule_name = next(p['name'] for p in data['policies'] if p['definition']['rule']['kind'] == 'max_duties')
    assert place['blocked_by'] == [{'name': rule_name, 'people': 2}]  # both others already work that day
    assert rule_name in place['message'] and place['message'].startswith('1 of 2 places open')
    # By person: the two who already work that day, each with the rule that stops them (the fill panel's vocabulary).
    stopped = [p for p in place['people'] if p['kind'] != 'FREE']
    assert len(stopped) == 2 and all(p['kind'] == 'HOURS' and p['code'] == 'MAX_DUTIES' and p['rule'] == rule_name
                                     and p['rule_id'] for p in stopped)
    assert partial['summary']['coverage_percent'] == 75.0


@pytest.fixture(scope='module')
def staged_progress_run(scale_harness):
    data, _ = department(scale_harness('h'), 5, people=14, days=7, rules='full', leave_rate=0.05)
    updates = []
    result = generate(data, 30, progress=updates.append)
    return data, result, updates


def test_generation_reports_staged_progress_with_budget_and_best_so_far(staged_progress_run):
    data, result, updates = staged_progress_run
    assert result['status'] == 'candidate_feasible'
    stages = [u['stage'] for u in updates]
    # Scoring completes an incumbent after search expires; summarizing explains the validated result.
    order = ['checking', 'preparing', 'finding', 'improving', 'scoring', 'validating', 'summarizing', 'ready']
    assert set(order) - {'scoring'} <= set(stages)
    assert stages == sorted(stages, key=order.index), 'Stages go forward only'
    preparing = [u for u in updates if u['stage'] == 'preparing' and 'total_sessions' in u]
    assert preparing[-1]['total_sessions'] == len(data['shifts'])
    assert [u['prepared_sessions'] for u in preparing] == sorted(u['prepared_sessions'] for u in preparing)
    searching = [u for u in updates if u['stage'] in {'finding', 'improving'}]
    # Preparation has its own clock; the configured 30 seconds belongs to search.
    assert all(u['time_limit_seconds'] == 30
               and u['search_started_seconds'] >= 0 for u in searching)
    assert all(u['objective_names'] == ['coverage', 'weighted'] for u in searching)
    incumbents = [u for u in updates if u.get('objective_value') is not None]
    assert incumbents and all(u['best_bound'] <= u['objective_value'] + 1e-6 for u in incumbents)
    for tier in {u['tier'] for u in incumbents}:
        values = [u['objective_value'] for u in incumbents if u['tier'] == tier]
        assert values == sorted(values, reverse=True), 'Each objective only improves'
    assert updates[-1]['stage'] == 'ready' and updates[-1]['shared_validation_passed']


@pytest.mark.cpu_budget
def test_scoring_progress_respects_search_budget(staged_progress_run):
    _, _, updates = staged_progress_run
    assert all(u['search_elapsed_seconds'] >= u['time_limit_seconds']
               for u in updates if u['stage'] == 'scoring'), 'Scoring starts only after search expires'


def test_problems_in_kept_work_are_reported_without_stopping_other_sessions(h):
    people = h.seed['people']
    w = h.shift(h.workspace(), people=people[:2])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    # A locked duty keeps someone who is on leave (the other person still covers it).
    data['shifts'][0]['locked'] = True
    data['availability'].append({'id': 'kept-leave', 'person_id': people[0], 'kind': 'recorded_leave',
                                 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    updates = []
    result = generate(data, 5, progress=updates.append)
    assert result['status'] == 'candidate_with_gaps'
    assert {(i['code'], i['person_id'], i['protected']) for i in result['issues']} == {('UNAVAILABLE', people[0], True)}
    assert result['additions'] and not result['removals']
    assert result['validation']['error_count'] > 0
    # A clinic whose opening calendar cannot be resolved is a setting to fix, not a solver failure.
    data['availability'].pop()
    data['services'][data['shifts'][1]['service_ids'][0]]['calendar_id'] = 'retired-calendar'
    result = generate(data, 5)
    assert result['status'] == 'invalid_inputs'
    assert {i['code'] for i in result['issues']} == {'CALENDAR_UNKNOWN'}


def test_deterministic_budget_gives_the_same_schedule_every_time(h):
    """deterministic=True: work units and a fixed seed, no wall clock, so the property tests above are reproducible."""
    data, _ = department(h, 2, people=16, days=7, rules='full', leave_rate=0.05)
    runs = [generate(data, 2, deterministic=True) for _ in range(2)]
    picks = [sorted((a['shift_id'], a['participant']['person_id']) for a in r['additions']) for r in runs]
    assert runs[0]['status'] == runs[1]['status'] == 'candidate_feasible' and picks[0] == picks[1] and picks[0]


def test_same_meaning_gives_the_same_schedule_whatever_the_row_order_or_extra_fields(h):
    """The database returns rows in no fixed order (a Postgres update moves a row), and records grow fields the solver
    does not read (a pager number): neither may change the deterministic rota."""
    data, _ = department(h, 2, people=16, days=7, rules='full', leave_rate=0.05)
    picks = lambda d: sorted((a['shift_id'], a['participant']['person_id'])
                             for a in generate(d, 2, deterministic=True)['additions'])
    base = picks(copy.deepcopy(data))
    noisy = copy.deepcopy(data)
    noisy['people'] = {pid: {**p, 'pager': '4417', 'unused_note': 'x'} for pid, p in reversed(noisy['people'].items())}
    noisy['shifts'].reverse()
    noisy['policies'].reverse()
    assert base and picks(noisy) == base
# Include this file in hosted supervision and team-lead qualification.
