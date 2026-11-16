"""Personal patterns reuse shape rules and remain additive soft wishes in every backend evaluator."""
import copy

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.cp_sat_score import fractional_scale
from packages.domain.cp_sat_patterns import add_patterns
from packages.domain.generation import generate as greedy
from packages.domain.patterns import wish_outcomes, wish_shape
from packages.domain.score import schedule_score, settings_of
from packages.domain.time import aware
from packages.domain.validation import evaluate
from tests.scheduler_departments import apply


CASES = [
    pytest.param('max_days_in_row', 2, [4, 5, 6, 9, 10, 11], [4, 5, 9, 10], id='days-in-row'),
    pytest.param('min_days_off_together', 2, [4, 6, 8], [4, 7, 10], id='days-off-together'),
    pytest.param('max_weekends_in_row', 1, [9, 16, 23], [9, 23], id='weekends-in-row'),
    pytest.param('whole_weekends', None, [9, 16], [9, 10, 16, 17], id='whole-weekends'),
]


def pattern_wish(pid, kind, limit=None, **changes):
    return {'id': 'pattern-wish', 'person_id': pid, 'kind': kind, 'limit': limit,
            'first_day': '2026-10-04', 'last_day': '2026-10-31', 'assignment_type_id': None,
            'points': 3, 'weight': 0.3, **changes}


def month(h, worked):
    """Reuse a real snapshot, scaling its one duty into local dates without repeated API setup."""
    pid = h.seed['people'][0]
    workspace = h.shift(h.workspace(end='2026-11-01'), people=[pid])['workspace']
    data = h.snapshot(workspace)
    template = data['shifts'][0]
    data['shifts'] = []
    for day in range(4, 32):
        shift = copy.deepcopy(template)
        shift.update(id=f'pattern-day-{day}', start=f'2026-10-{day:02}T08:00:00+03:00',
                     end=f'2026-10-{day:02}T12:00:00+03:00')
        shift['participants'] = [{**p, 'id': f'pattern-person-{day}', 'shift_id': shift['id'],
                                  'start': shift['start'], 'end': shift['end']}
                                 for p in shift['participants']] if day in worked else []
        data['shifts'].append(shift)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data['policies'] = []
    data['wishes'] = []
    return data, pid


def wish_penalty(data, pid, fixed=False):
    """Build decisions before constraining them, or use only fixed duties for the immutable-breach regression."""
    model = cp_model.CpModel()
    rows, choices = [], []
    for shift in data['shifts']:
        worked = any(p['person_id'] == pid for p in shift['participants'])
        if shift['cancelled'] or fixed and not worked:
            continue
        flag = model.new_constant(1) if fixed else model.new_bool_var(shift['id'])
        rows.append((shift, None, aware(shift['start']), aware(shift['end']), flag))
        choices.append((flag, worked))
    parts, worst = add_patterns(model, data, {pid: rows}, settings_of(None)['strengths'])
    for flag, worked in choices:
        model.add(flag == worked)
    scale, *factors = fractional_scale([1, *(factor for factor, _ in parts)])
    model.minimize(sum(factor * sum(costs) for factor, (_, costs) in zip(factors, parts)))
    assert not model.validate()
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    return solver.objective_value / scale, worst


@pytest.mark.parametrize('kind,limit,broken,compliant', CASES)
@pytest.mark.parametrize('honoured', [False, True], ids=['broken', 'honoured'])
def test_pattern_wish_finding_score_and_solver_penalty_agree(h, kind, limit, broken, compliant, honoured):
    worked = compliant if honoured else broken
    data, pid = month(h, worked)
    wish = pattern_wish(pid, kind, limit)
    # A granted one-point daily wish establishes the same 3:1 preference weighting as the three-point pattern.
    daily = {**pattern_wish(pid, 'prefer_on'), 'id': 'granted-wish', 'points': 1, 'weight': 0.1,
             'first_day': f'2026-10-{worked[0]:02}', 'last_day': f'2026-10-{worked[0]:02}'}
    data['wishes'] = [wish, daily]
    validation = evaluate(data)
    findings = [f for f in validation['findings'] if f['code'] == 'WISH_UNMET']
    assert len(findings) == int(not honoured)  # Several broken runs/weekends still charge the wish once.
    if findings:
        assert findings[0]['severity'] == 'warning' and findings[0]['person_id'] == pid
        assert findings[0]['rule_id'] is None
        assert findings[0]['detail']['wish_id'] == wish['id'] and findings[0]['detail']['kind'] == kind
    assert wish_outcomes(data)[wish['id']] == (float(honoured), 28)
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 0, 'preferences': 1, 'stability': 0}}}
    preference = schedule_score(data, validation, options)['components']['preferences']
    penalty, worst = wish_penalty(data, pid)
    assert worst == 1000 * settings_of(None)['strengths']['medium'] * 0.4
    assert penalty == (0 if honoured else 1000 * settings_of(None)['strengths']['medium'] * 0.3)
    assert preference['value'] == pytest.approx(1 - penalty / worst)
    assert preference['lost'] == pytest.approx(100 * penalty / worst)


@pytest.mark.parametrize('kind,limit,broken,compliant', CASES)
def test_already_fixed_pattern_breach_keeps_its_penalty(h, kind, limit, broken, compliant):
    data, pid = month(h, broken)
    data['wishes'] = [pattern_wish(pid, kind, limit)]
    assert wish_outcomes(data)['pattern-wish'][0] == 0
    assert wish_penalty(data, pid, fixed=True) == (900, 900)


@pytest.mark.parametrize('kind,limit,broken,compliant', CASES)
def test_full_generation_retains_an_already_fixed_pattern_breach(h, kind, limit, broken, compliant):
    data, pid = month(h, broken)
    data['shifts'] = [{**shift, 'locked': True} for shift in data['shifts'] if shift['participants']]
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    data['wishes'] = [pattern_wish(pid, kind, limit)]
    result = generate(data, 10)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'] == [] and result['removals'] == []
    assert result['summary']['score']['components']['preferences']['value'] == 0
    assert result['compiled_component_values']['preferences'] == 0
    assert any(finding['code'] == 'WISH_UNMET' for finding in result['validation']['findings'])


@pytest.mark.parametrize('kind,limit,broken,compliant', CASES)
def test_fixed_history_pattern_and_granted_daily_wish_keep_their_weight_outside_focus(h, kind, limit, broken, compliant):
    data, pid = month(h, [*broken, 30])
    data['shifts'] = [{**shift, 'locked': True} for shift in data['shifts'] if shift['participants']]
    focused = next(shift for shift in data['shifts'] if shift['start'][8:10] == '30')
    focused['participants'][0]['person_id'] = h.seed['people'][1]
    data['focus_ids'] = [focused['id']]
    data['wishes'] = [pattern_wish(pid, kind, limit),
                      pattern_wish(pid, 'prefer_on', id='granted-daily', points=1, weight=0.1,
                                   first_day=f'2026-10-{broken[0]:02}', last_day=f'2026-10-{broken[0]:02}')]
    assert wish_outcomes(data)['pattern-wish'][0] == 0
    assert wish_outcomes(data)['granted-daily'][0] == 1
    result = generate(data, 10)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'] == [] and result['removals'] == []
    # History supplies one lost three-point item and one granted one-point item; focus work activates the component.
    public = result['summary']['score']['components']['preferences']['value']
    assert public == 0.25
    assert result['compiled_component_values']['preferences'] == pytest.approx(public, abs=0.000051)


@pytest.mark.parametrize('kind,limit,broken,compliant', CASES)
def test_solver_honours_each_personal_pattern_when_possible(h, kind, limit, broken, compliant):
    data, pid = month(h, [])
    data['shifts'] = [s for s in data['shifts'] if int(s['start'][8:10]) in set(broken) | set(compliant)]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    people = h.seed['people'][:2]
    data['people'] = {p: data['people'][p] for p in people}
    data['wishes'] = [pattern_wish(p, kind, limit, id=f'pattern-{p}') for p in people]
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result
    draft = apply(data, result)
    assert all(outcome[0] == 1 for outcome in wish_outcomes(draft).values())
    assert not [f for f in result['validation']['findings'] if f['code'] == 'WISH_UNMET']
    assert result['summary']['score']['components']['preferences']['value'] == 1
    assert result['compiled_component_values']['preferences'] == 1


@pytest.mark.parametrize('engine', [generate, greedy], ids=['cp-sat', 'greedy'])
def test_personal_pattern_never_relaxes_a_department_limit(h, engine):
    policy = h.policy({'kind': 'max_consecutive_days', 'maximum': 1})
    data, pid = month(h, [9])
    data['policies'] = [policy]
    data['shifts'] = [s for s in data['shifts'] if int(s['start'][8:10]) in (9, 10)]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data['shifts'][0]['locked'] = True
    other = h.seed['people'][1]
    data['people'] = {p: data['people'][p] for p in (pid, other)}
    data['wishes'] = [pattern_wish(pid, 'whole_weekends'),
                      pattern_wish(pid, 'max_days_in_row', 5, id='looser-personal-limit')]
    result = engine(data, 20)
    assert result['status'] == 'candidate_feasible', result
    draft = apply(data, result)
    assert evaluate(draft)['error_count'] == 0
    saturday = next(s for s in draft['shifts'] if s['start'].startswith('2026-10-10'))
    assert {p['person_id'] for p in saturday['participants']} == {other}
    assert wish_outcomes(draft)['pattern-wish'][0] == 0  # Whole weekend gives way to the hard department limit.
    assert wish_outcomes(draft)['looser-personal-limit'][0] == 1
    assert any(f['code'] == 'WISH_UNMET' for f in result['validation']['findings'])


@pytest.mark.parametrize('kind,limit,first,next_day', [
    ('max_days_in_row', 1, 4, 5), ('min_days_off_together', 2, 4, 6),
    ('max_weekends_in_row', 1, 9, 16), ('whole_weekends', None, 9, 10),
])
def test_greedy_uses_the_same_personal_pattern_outcome(h, kind, limit, first, next_day):
    data, pid = month(h, [first])
    other = h.seed['people'][1]
    # Balance alone would choose the person whose wish the new duty breaks.
    other_days = [] if kind == 'whole_weekends' else [20, 21]
    data['shifts'] = [s for s in data['shifts'] if int(s['start'][8:10]) in [first, next_day, *other_days]]
    for shift in data['shifts']:
        day = int(shift['start'][8:10])
        if day in other_days:
            shift['participants'] = [{**data['shifts'][0]['participants'][0], 'person_id': other,
                                      'id': f'other-{day}', 'shift_id': shift['id'],
                                      'start': shift['start'], 'end': shift['end']}]
        shift['locked'] = day != next_day
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    data['people'] = {p: data['people'][p] for p in (pid, other)}
    data['wishes'] = [pattern_wish(pid, kind, limit)]
    result = greedy(data, 20)
    assert result['status'] == 'candidate_feasible', result
    draft = apply(data, result)
    chosen = next(s for s in draft['shifts'] if int(s['start'][8:10]) == next_day)
    assert {p['person_id'] for p in chosen['participants']} == {pid if kind == 'whole_weekends' else other}
    assert wish_outcomes(draft)['pattern-wish'][0] == 1
    assert schedule_score(draft, result['validation'])['components']['preferences']['value'] == 1


@pytest.mark.parametrize('kind,limit', [('max_days_in_row', 10**6), ('min_days_off_together', 10**6),
                                        ('max_weekends_in_row', 10**6)])
def test_large_preference_limits_are_bounded_by_the_observed_period(h, kind, limit):
    data, pid = month(h, [4, 6])
    data['wishes'] = [pattern_wish(pid, kind, limit)]
    honoured = kind != 'min_days_off_together'
    assert wish_outcomes(data)['pattern-wish'][0] == int(honoured)
    penalty, _ = wish_penalty(data, pid)
    assert penalty == (0 if honoured else 900)


def test_pattern_dates_use_local_duty_start_and_clip_to_wish_and_rota(h):
    data, pid = month(h, [4, 5, 6])
    data['wishes'] = [pattern_wish(pid, 'max_days_in_row', 2, first_day='2026-10-05'),
                      pattern_wish(pid, 'max_days_in_row', 2, id='outside',
                                   first_day='2026-09-01', last_day='2026-10-03')]
    # A UTC start late on the 3rd is the local start of the 4th; duties outside the wish must not extend its run.
    first = data['shifts'][0]
    first['participants'][0]['start'] = '2026-10-03T22:00:00+00:00'
    assert wish_outcomes(data) == {'pattern-wish': (1.0, 27)}
    assert wish_penalty(data, pid)[0] == 0


def test_whole_weekends_uses_the_personal_weekend_definition(h):
    data, pid = month(h, [4, 9, 10])
    data['wishes'] = [pattern_wish(pid, 'whole_weekends', weekend_start=6, weekend_days=2)]
    assert wish_outcomes(data)['pattern-wish'][0] == 0  # Sunday-Monday; the complete Friday-Saturday is irrelevant.
    assert wish_penalty(data, pid)[0] == 900


@pytest.mark.parametrize('kind', ['max_weekends_in_row', 'whole_weekends'])
@pytest.mark.parametrize('settings', [{}, {'weekend_start': None, 'weekend_days': None},
                                     {'weekend_start': 0, 'weekend_days': 3}])
def test_weekend_wishes_share_department_defaults_and_keep_explicit_settings(kind, settings):
    from packages.contracts.schemas import ConsecutiveWeekends
    defaults = ConsecutiveWeekends(kind='max_consecutive_weekends')
    shape = wish_shape(pattern_wish('person', kind, 2 if kind == 'max_weekends_in_row' else None, **settings))
    for field in ('weekend_start', 'weekend_days'):
        assert shape[field] == (settings[field] if settings.get(field) is not None else getattr(defaults, field))


def test_cancelled_duties_do_not_break_a_personal_pattern(h):
    data, pid = month(h, [4, 5, 6])
    data['wishes'] = [pattern_wish(pid, 'max_days_in_row', 2)]
    next(s for s in data['shifts'] if s['start'].startswith('2026-10-05'))['cancelled'] = True
    assert wish_outcomes(data)['pattern-wish'][0] == 1
    assert not [f for f in evaluate(data)['findings'] if f['code'] == 'WISH_UNMET']
    assert wish_penalty(data, pid)[0] == 0


@pytest.mark.parametrize('kind', ['max_days_in_row', 'min_days_off_together', 'max_weekends_in_row', 'whole_weekends'])
def test_personal_patterns_require_a_range_instead_of_single_day_repeat_expansion(h, kind):
    payload = {'person_id': h.seed['people'][0], 'kind': kind,
               'repeat': {'weekdays': [0], 'starts_on': '2026-10-01'}}
    if kind != 'whole_weekends':
        payload['limit'] = 2
    result = h.command('staff', 'wish.create', payload, status=422)
    assert 'Pattern preferences use a date range' in str(result['error'])
    assert h.get('staff', '/api/wishes')['wishes'] == []


def test_pattern_objective_retains_small_positive_weights(h):
    data, pid = month(h, [4, 5, 6])
    data['wishes'] = [pattern_wish(pid, 'max_days_in_row', 1, weight=1 / 2_147_483_647)]
    penalty, _ = wish_penalty(data, pid)
    assert 0 < penalty < 1


def test_closed_duties_do_not_break_personal_patterns(h):
    data, pid = month(h, [4, 5])
    data['wishes'] = [pattern_wish(pid, 'max_days_in_row', 1)]
    assert wish_outcomes(data)['pattern-wish'][0] == 0
    data['shifts'][1]['closed'] = True
    assert wish_outcomes(data)['pattern-wish'][0] == 1


def test_pattern_admission_and_usage_reject_excessive_work_before_expansion(h, monkeypatch):
    from apps.api.services import wishes
    def unexpected_expansion(*args, **kwargs):
        raise AssertionError('An excessive range must fail before expanding wishes')
    monkeypatch.setattr(wishes, 'snapshot_wishes', unexpected_expansion)
    payload = {'person_id': h.seed['people'][0], 'kind': 'max_days_in_row', 'limit': 3,
               'first_day': '2026-10-01', 'last_day': '9998-12-31'}
    assert h.command('staff', 'wish.create', payload, status=422)['error']
    result = h.get('staff', '/api/wishes?start=2026-10-01&end=9998-12-31', status=422)
    assert result['error']['code'] == 'WISH_RANGE_LIMIT'


def test_pattern_range_execution_boundary_is_shared_with_usage():
    from datetime import date, timedelta
    from packages.contracts import schemas as s
    first = date(2026, 10, 1)
    payload = {'person_id': '00000000-0000-4000-8000-000000000001', 'kind': 'max_days_in_row',
               'limit': 3, 'first_day': first}
    assert s.StaffWishInput(**payload, last_day=first + timedelta(days=s.WISH_RANGE_MAX_DAYS - 1))
    with pytest.raises(ValueError):
        s.StaffWishInput(**payload, last_day=first + timedelta(days=s.WISH_RANGE_MAX_DAYS))
