"""Staff wishes: budget, permissions, locking, the solver and score, and declined-wish debt (apps/api/services/wishes.py)."""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.score import schedule_score
from apps.api.services.wishes import period_of
from packages.contracts.schemas import GENERATION_WEIGHT_PRESETS, ScoringSettings, StaffWishInput


def wish(h, day, kind='prefer_off', points=1, role='staff', person=None, last=None, status=200, **extra):
    return h.command(role, 'wish.create', {'person_id': person or h.seed['people'][0], 'first_day': day, 'last_day': last or day,
                                           'kind': kind, 'points': points, **extra}, status=status)


@pytest.mark.postgres
@pytest.mark.parametrize('number,status', [(2_147_483_647, 200), (2_147_483_648, 422)])
@pytest.mark.parametrize('is_override', [False, True], ids=['create', 'override'])
def test_standing_wish_number_postgres_integer_boundary(pg, number, status, is_override):
    payload = {'person_id': pg.seed['people'][0], 'kind': 'prefer_off',
               'repeat': {'weekdays': [0], 'starts_on': '2026-10-01'}}
    if is_override:
        pg.workspace(start='2026-10-01', end='2026-11-01')
        saved = pg.command('staff', 'wish.create', payload)['wish']
        pg.command('staff', 'wish.override', {'period_start': '2026-10-01', 'number': number},
                   saved['id'], saved['revision'], status=status)
    else:
        payload['repeat']['number'] = number
        pg.command('staff', 'wish.create', payload, status=status)
    listed = pg.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    if status == 200:
        stored = listed['overrides'][0]['number'] if is_override else listed['wishes'][0]['repeat']['number']
        assert stored == number
    else:
        assert not listed['overrides']
        assert len(listed['wishes']) == int(is_override)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_zero_point_wishes_are_rejected_on_create_and_update(request, database):
    h = request.getfixturevalue(database)
    wish(h, '2026-10-05', points=0, status=422)
    saved = wish(h, '2026-10-05')['wish']
    h.command('staff', 'wish.update', {'person_id': saved['person_id'], 'first_day': saved['first_day'],
        'last_day': saved['last_day'], 'kind': saved['kind'], 'points': 0}, saved['id'], saved['revision'], status=422)
    listed = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert [(w['id'], w['points'], w['revision']) for w in listed['wishes']] == [(saved['id'], 1, saved['revision'])]
    assert listed['usage'][0]['used'] == 1


def test_budget_ownership_overlap_and_listing(h):
    me, other = h.seed['people'][:2]
    saved = wish(h, '2026-10-05', points=3)
    assert saved['message'] == 'Preference saved.'
    first = saved['wish']
    wish(h, '2026-10-06', last='2026-10-08', points=6)
    refused = wish(h, '2026-10-20', points=2, status=409)  # 3 + 6 + 2 > 10 in October
    assert refused['error']['code'] == 'WISH_BUDGET' and '1 of 10 left' in refused['error']['message']
    wish(h, '2026-11-02', points=5)  # November has its own budget
    overlap = wish(h, '2026-10-06', status=409)['error']
    assert overlap['code'] == 'WISH_OVERLAP'
    assert overlap['message'] == 'There is already a preference on one of these days; change that one instead'
    assert wish(h, '2026-10-09', person=other, status=403)['error']['code'] == 'FORBIDDEN'
    wish(h, '2026-10-09', person=other, role='coordinator', kind='prefer_on')
    # A duty-type wish names its duty type, and only then.
    invalid = wish(h, '2026-10-10', kind='prefer_shift_type', status=422)
    assert 'Choose a duty type for a duty-type preference (and only then)' in str(invalid['error'])
    too_long = wish(h, '2026-11-01', last='2026-11-15', status=422)
    assert 'A preference covers one day or up to 14 days in a row' in str(too_long['error'])
    wish(h, '2026-10-10', kind='avoid_shift_type', assignment_type_id=h.seed['type'])
    mine = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert {w['person_id'] for w in mine['wishes']} == {me} and mine['budget_points'] == 10 and mine['period'] == 'month'
    assert mine['multipliers'] == {me: 1.0}
    everyone = h.get('coordinator', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert {w['person_id'] for w in everyone['wishes']} == {me, other}
    # Edit (points count once), then withdraw frees the points.
    changed = h.command('staff', 'wish.update', {'person_id': me, 'first_day': '2026-10-05', 'last_day': '2026-10-05', 'kind': 'prefer_off',
                                                 'points': 2, 'note': 'School play'}, first['id'], first['revision'])['wish']
    assert changed['points'] == 2
    h.command('staff', 'wish.withdraw', {'reason': 'Plans changed'}, changed['id'], changed['revision'])
    wish(h, '2026-10-21', points=3)


def test_preference_schema_labels_keep_wish_fields():
    settings = ScoringSettings.model_json_schema()['properties']
    assert {key: settings[key]['title'] for key in ('wish_budget_points', 'wish_period', 'wish_debt_cap')} == {
        'wish_budget_points': 'Preference Budget Points', 'wish_period': 'Preference Period',
        'wish_debt_cap': 'Preference Debt Cap'}
    schema = StaffWishInput.model_json_schema()
    assert schema['description'].startswith('A soft preference for a day or days')
    assert set(schema['properties']) == {'person_id', 'first_day', 'last_day', 'kind', 'assignment_type_id', 'points', 'note', 'repeat', 'limit', 'weekend_start', 'weekend_days'}


def test_periods():
    settings = ScoringSettings().model_copy(update={'wish_period': 'week'})
    assert period_of(date(2026, 10, 7), settings, 6) == (date(2026, 10, 4), date(2026, 10, 11))  # weeks start on Sunday
    settings = settings.model_copy(update={'wish_period': 'quarter'})
    assert period_of(date(2026, 11, 7), settings, 6) == (date(2026, 10, 1), date(2027, 1, 1))
    assert period_of(date(2026, 12, 31), ScoringSettings(), 6) == (date(2026, 12, 1), date(2027, 1, 1))


def test_wishes_lock_when_the_rota_is_submitted(h):
    w = h.shift(h.workspace('October team rota'))['workspace']
    open_wish = wish(h, '2026-10-06')['wish']
    h.lifecycle(w, 'submit')
    locked = wish(h, '2026-10-07', status=409)['error']
    assert locked['code'] == 'WISH_LOCKED'
    assert locked['message'] == (
        f"The rota for these days ({w['name']}) has been submitted; preferences for them are closed")
    withdrawn = h.command('staff', 'wish.withdraw', {'reason': 'Too late'}, open_wish['id'], open_wish['revision'], status=409)['error']
    assert withdrawn['code'] == 'WISH_LOCKED'
    assert withdrawn['message'] == 'A rota for these days has been submitted; this preference can no longer change'
    wish(h, '2026-10-12')  # the next rota's days are still open
    # SW3-C-05: the list names the settled rota over the days read, by the same overlap, so the form says it first.
    settled = h.get('staff', '/api/wishes?start=2026-10-07&end=2026-10-08')['settled']
    assert [r['name'] for r in settled] == [w['name']]
    ends = settled[0]['ends_on']
    assert h.get('staff', f'/api/wishes?start={ends}&end=2026-11-01')['settled'] == []
    assert h.get('staff', '/api/wishes')['settled'] == []  # no range read, none listed


def test_solver_and_score_honour_wishes(h):
    a, b = h.seed['people'][:2]
    w = h.workspace()
    for day in (4, 5):
        w = h.shift(w, start=f'2026-10-0{day}T08:00:00+03:00', end=f'2026-10-0{day}T12:00:00+03:00', people=[])['workspace']
    wish(h, '2026-10-04', person=a, role='coordinator', points=3)
    wish(h, '2026-10-05', person=b, role='coordinator', kind='prefer_on')
    data = h.snapshot(w)
    assert {x['kind']: x['weight'] for x in data['wishes']} == {'prefer_off': 0.3, 'prefer_on': 0.1}
    assert 'note' not in data['wishes'][0]
    data['people'] = {pid: data['people'][pid] for pid in (a, b)}
    # C1 trades wishes against balance; this journey explicitly emphasises preferences.
    data['generation_options'] = {'scoring': {'weights': GENERATION_WEIGHT_PRESETS['preferences_first']['weights']}}
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible'
    placed = {x['participant']['start'][:10]: x['participant']['person_id'] for x in result['additions']}
    assert placed == {'2026-10-04': b, '2026-10-05': b}  # a is off on the 4th and b wants the 5th
    assert result['summary']['wishes'] == {'met': 2, 'total': 2}  # the review's "Wishes: 2 of 2 met"
    # The planner sees the same wishes; a manual rota that ignores them loses preference points, with the reason.
    assert len(h.detail(w)['wishes']) == 2
    w = h.shift(w, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[a])['workspace']
    bad = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[a])
    score = bad['validation']['score']
    assert score['components']['preferences']['lost'] > 0
    assert any("preference 'off Sun 4 Oct' not granted" in line['text'] for line in score['why'])


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('budget', [2_000_001, 2_147_483_647])
@pytest.mark.parametrize('person_index', [0, 1])
def test_one_point_wish_survives_large_budget(request, database, budget, person_index):
    h = request.getfixturevalue(database)
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'wish_budget_points': budget}}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    people = h.seed['people'][:2]
    person, other = people[person_index], people[1 - person_index]
    typ = h.configure('assignment_types', {'name': 'Equal candidate clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    wish(h, '2026-10-04', person=person, role='coordinator', points=1)
    data = h.snapshot(workspace)
    weight = data['wishes'][0]['weight']
    assert weight > 0
    assert weight == 1 / budget
    data['people'] = {pid: data['people'][pid] for pid in people}

    result = generate(data, 20, workers=1, deterministic=True)
    assert result['status'] == 'candidate_feasible'
    assert [row['participant']['person_id'] for row in result['additions']] == [other]
    assert result['summary']['wishes'] == {'met': 1, 'total': 1}

    # Both consumers use the same snapshot weight, including below one solver coefficient unit.
    data['shifts'][0]['participants'] = [result['additions'][0]['participant']]
    granted = schedule_score(data, {'findings': []})
    data['shifts'][0]['participants'] = [{**data['shifts'][0]['participants'][0], 'person_id': person}]
    declined = schedule_score(data, {'findings': []})
    assert declined['score'] < granted['score']
    assert declined['components']['preferences']['lost'] > 0
    assert any("preference 'off Sun 4 Oct' not granted" in line['text'] for line in declined['why'])


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('points', [(1, 100_000), (100_000, 1)])
def test_competing_positive_wishes_keep_their_relative_costs(request, database, points):
    h = request.getfixturevalue(database)
    budget = 2_147_483_647
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'wish_budget_points': budget}}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    a, b = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Equal candidate clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'})
    workspace = h.workspace()
    for day, value in zip(('2026-10-04', '2026-10-05'), points):
        workspace = h.shift(workspace, typ=typ['id'], people=[], start=day + 'T08:00:00+03:00',
                            end=day + 'T12:00:00+03:00')['workspace']
        wish(h, day, person=a, role='coordinator', points=value)
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (a, b)}
    assert sorted(w['weight'] for w in data['wishes']) == [1 / budget, 100_000 / budget]
    expected = dict(zip(('2026-10-04', '2026-10-05'), (a, b) if points[0] == 1 else (b, a)))
    for wishes in (data['wishes'], list(reversed(data['wishes']))):
        result = generate({**data, 'wishes': wishes}, 20, workers=1, deterministic=True)
        assert result['status'] == 'candidate_feasible'
        assert result['optimality_proven'] and result['validation']['valid']
        assert {row['participant']['start'][:10]: row['participant']['person_id']
                for row in result['additions']} == expected
    for shift in data['shifts']:
        shift['participants'] = [row['participant'] for row in result['additions'] if row['shift_id'] == shift['id']]
    preferred = schedule_score(data, {'findings': []})
    for shift in data['shifts']:
        for participant in shift['participants']:
            participant['person_id'] = b if participant['person_id'] == a else a
    assert preferred['score'] > schedule_score(data, {'findings': []})['score']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('multiplier', [1, 3])
def test_max_point_reserved_wish_survives_budget_cut_and_precise_allocation_preference(request, database, multiplier):
    h = request.getfixturevalue(database)
    person, other = h.seed['people'][:2]
    wish(h, '2026-10-04', points=4)
    h.publish(h.shift(h.workspace(), people=[person])['workspace'])  # Prior declined points exercise the catch-up cap.
    for budget in (2_147_483_647, 1):
        h.command('admin', 'department.settings', {**department_settings(h),
            'generation_defaults': {'scoring': {'wish_budget_points': budget, 'wish_debt_cap': multiplier}}},
            h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
        if budget > 1:
            saved = wish(h, '2026-11-01', last='2026-11-14', points=budget)['wish']
            reserved = h.shift(h.workspace(start='2026-11-01', end='2026-11-08'), people=[other],
                start='2026-11-01T08:00:00+03:00', end='2026-11-01T12:00:00+03:00')['workspace']
            h.lifecycle(reserved, 'submit')  # Reserve points without settling a new catch-up ledger period.
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'service_mode',
              'balance_weight': 0.0001, 'balance_target_share': 0.0001}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Equal candidate clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    workspace = h.shift(h.workspace(start='2026-11-08', end='2026-11-15'), typ=typ['id'], people=[],
                        start='2026-11-08T08:00:00+03:00', end='2026-11-08T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    [current] = data['wishes']
    assert (current['id'], current['points'], current['multiplier'], current['weight']) == (
        saved['id'], 2_147_483_647, multiplier, multiplier)
    data['people'] = {pid: data['people'][pid] for pid in (person, other)}
    data['services'][h.seed['services'][0]]['mode'] = 'physical'

    def check_objectives(model, tiers):
        assert [name for name, _, _ in tiers] == ['coverage', 'weighted']
        for name, terms, _ in tiers:
            if name == 'weighted':
                assert terms
            model.minimize(sum(terms))
            assert not model.validate()
            assert all(abs(coefficient) <= cp_model.INT_MAX // 2 for coefficient in model.proto.objective.coeffs)

    assert generate(data, 20, deterministic=True, _on_objectives=check_objectives)['status'] == 'model_ready'
    result = generate(data, 20, deterministic=True)
    assert result['status'] == 'candidate_feasible'
    assert [row['participant']['person_id'] for row in result['additions']] == [other]
    assert result['summary']['wishes'] == {'met': 1, 'total': 1}
    data['shifts'][0]['participants'] = [result['additions'][0]['participant']]
    granted = schedule_score(data, {'findings': []})
    data['shifts'][0]['participants'][0]['person_id'] = person
    declined = schedule_score(data, {'findings': []})
    assert declined['components']['preferences']['lost'] > granted['components']['preferences']['lost']

    # Previously saved snapshots must fail clearly before native coefficient multiplication can wrap.
    data['shifts'][0]['participants'] = []
    current['weight'] = 2_147_483_647 * multiplier
    refused = generate(data, 20)
    assert refused['status'] == 'invalid_inputs'
    assert refused['issues'] == [{'code': 'SCORE_PRECISION_UNSUPPORTED',
                                 'message': 'Score quantity exceeds supported integer precision'}]
    assert refused['additions'] == refused['removals'] == []


def test_publication_records_outcomes_and_debt_raises_later_wishes(h):
    me = h.seed['people'][0]
    declined = wish(h, '2026-10-04', points=4)['wish']
    granted = wish(h, '2026-10-06')['wish']
    w = h.shift(h.workspace(), people=[me])['workspace']  # works the 4th: the wish is declined
    h.publish(w)
    mine = {x['id']: x for x in h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['wishes']}
    assert mine[declined['id']]['outcome'] == 'declined' and mine[granted['id']]['outcome'] == 'granted'
    # Debt: (4 declined - 0.5 x 1 granted) / 10 = 0.35 -> weight x 1.35 for later wishes, capped at 3.
    later = h.get('staff', '/api/wishes?start=2026-10-12&end=2026-11-01')
    assert later['multipliers'][me] == 1.35
    nxt = h.workspace(start='2026-10-11', end='2026-10-18')
    wish(h, '2026-10-12', points=2)
    snapshot = h.snapshot(h.shift(nxt, start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[])['workspace'])
    assert snapshot['wishes'][0]['weight'] == 0.2 * 1.35
    # An amendment replaces the superseded publication's ledger rows instead of adding to them.
    publication = h.get('coordinator', '/api/publications')['publications'][0]
    amendment = h.workspace(start='2026-10-04', end='2026-10-11', base=publication['id'])
    detail = h.detail(amendment)
    assert detail['shifts']
    h.publish(amendment)
    assert h.get('staff', '/api/wishes?start=2026-10-12&end=2026-11-01')['multipliers'][me] == 1.35


def test_debt_decays_with_age_and_is_capped():
    from apps.api.services.wishes import multipliers

    class Ledger:  # rows as the query returns them: newest period first
        def __init__(self, rows):
            self.rows = rows

        def execute(self, _):
            return self.rows
    newer, older = date(2026, 11, 1), date(2026, 10, 1)
    rows = [('p', 'wishes_declined', 4.0, newer), ('p', 'wishes_granted', 2.0, newer), ('p', 'wishes_declined', 10.0, older)]
    # (4 - 0.5 x 2) + 0.5 x 10 = 8 points owed over a budget of 10 -> 1.8.
    assert multipliers(Ledger(rows), 'd', {'p', 'q'}, ScoringSettings(), None) == {'p': 1.8, 'q': 1.0}
    assert multipliers(Ledger(rows * 5), 'd', {'p'}, ScoringSettings(), None) == {'p': 3.0}  # the cap
    assert multipliers(Ledger(rows), 'd', {'p'}, ScoringSettings(wish_debt_cap=1), None) == {'p': 1.0}  # catch-up off


def test_list_carries_period_usage_lock_and_point_limit(h):
    """The screen shows what the server enforces: points per budget period (a quarter counts beyond the month shown),
    which wishes are settled by a submitted rota, and the largest wish."""
    from apps.api.services.wishes import MAX_POINTS
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'wish_period': 'quarter'}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    wish(h, '2026-11-20', points=4)
    first = wish(h, '2026-10-06', points=2)['wish']
    h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')
    october = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert october['usage'] == [{'start': '2026-10-01', 'end': '2027-01-01', 'used': 6,
                                'standing_points': 0, 'one_off_points': 6,
                                'unfunded_standing_points': 0, 'unfunded_one_off_points': 0}]
    assert october['max_points'] == MAX_POINTS == 2_147_483_647
    assert {w['id']: w['locked'] for w in october['wishes']} == {first['id']: True}
    assert wish(h, '2026-12-01', points=5, status=409)['error']['code'] == 'WISH_BUDGET'  # 6 + 5 > 10 in the quarter


def test_no_wish_on_leave_and_a_change_returns_what_it_replaced(h):
    me = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': me, 'kind': 'recorded_leave', 'start': '2026-10-06T00:00:00+03:00',
                                                     'end': '2026-10-08T00:00:00+03:00', 'private_reason': 'Annual leave'})
    for day, last in (('2026-10-06', None), ('2026-10-05', '2026-10-07')):
        refused = wish(h, day, kind='prefer_on', last=last, status=409)['error']
        assert refused['code'] == 'WISH_ON_LEAVE'
        assert refused['message'] == 'There is leave on one of these days already; preferences are for days you could work'
    saved = wish(h, '2026-10-08', points=2)['wish']  # the day after leave is free
    changed = h.command('staff', 'wish.update', {'person_id': me, 'first_day': '2026-10-09', 'last_day': '2026-10-09', 'kind': 'prefer_on',
                                                 'points': 1, 'note': ''}, saved['id'], saved['revision'])
    assert changed['previous'] == {'person_id': me, 'first_day': '2026-10-08', 'last_day': '2026-10-08', 'kind': 'prefer_off',
                                   'assignment_type_id': None, 'limit': None, 'weekend_start': None, 'weekend_days': None,
                                   'points': 2, 'note': '', 'repeat': None}


def test_publication_notice_tells_staff_how_their_wishes_went(h):
    from apps.worker.main import process_one
    me = h.seed['people'][0]
    wish(h, '2026-10-04', points=2)
    wish(h, '2026-10-06')
    wish(h, '2026-10-05', person=h.seed['people'][1], role='coordinator')  # not the reader's
    h.publish(h.shift(h.workspace(), people=[me])['workspace'])  # works the 4th
    process_one(h.app.state.engine)
    notes = h.get('staff', '/api/notifications')['notifications']
    assert len(notes) == 1 and notes[0]['message'].endswith('Your preferences: 1 granted, 1 not granted. Open Requests for each one.')


def department_settings(h):
    me = h.get('admin', '/api/me')
    return {'department_name': me['department']['name'], 'department_revision': me['department']['revision'],
            'organisation_name': me['organisation']['name'], 'timezone': me['organisation']['timezone'],
            'week_start': me['organisation']['week_start'], 'reason': 'Constructed settings change'}


def test_run_catch_up_is_snapshotted_without_changing_staff_budgets_or_history(h):
    import copy
    from dataclasses import replace
    from apps.api import models as m
    from apps.api.auth import authenticate
    from apps.api.db import transaction
    from apps.api.services.jobs import create_job
    from apps.worker.main import claim_job, prepare_deferred_generation, process_one
    from packages.contracts.schemas import Command, GenerationOptions

    settings = replace(h.settings, generation_engine='cp_sat')
    person = h.seed['people'][0]
    wish(h, '2026-10-04', points=4)
    wish(h, '2026-10-06')
    h.publish(h.shift(h.workspace(), people=[person])['workspace'])
    nxt = h.workspace(start='2026-10-11', end='2026-10-18')
    wish(h, '2026-10-12', points=2)
    nxt = h.shift(nxt, start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[])['workspace']
    endpoint = '/api/wishes?start=2026-10-12&end=2026-11-01'
    staff = h.get('staff', endpoint)
    publications = h.get('coordinator', '/api/publications')
    department = h.get('admin', '/api/me')['department']['generation_defaults']
    weights = GenerationOptions.model_validate(department).scoring.weights.model_dump()
    saved = []
    for cap, multiplier in [(1, 1), (3, 1.35)]:
        client = h.client('coordinator')
        with transaction(h.app.state.engine, write=True) as db:
            actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
            job = create_job(db, actor, Command(kind='job.create', expected_revision=nxt['revision'], payload={
                'workspace_id': nxt['id'], 'kind': 'generation', 'generation_options': {'scoring': {'wish_debt_cap': cap}}}),
                settings)['job']
            queued = db.get(m.Job, job['id'])
            # adf40abbe introduced worker-time snapshot preparation; intent: 18b89db3e on
            # backup/mac-integration-cycle2, landed via adf40abbe.
            assert queued.input_snapshot['_deferred_generation']
            assert 'wishes' not in queued.input_snapshot
            claim = claim_job(db)
        assert claim and claim['id'] == job['id']
        prepare_deferred_generation(h.app.state.engine, claim, settings)
        with transaction(h.app.state.engine) as db:
            snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
        assert snapshot['generation_options']['scoring']['wish_debt_cap'] == cap
        assert snapshot['wishes'][0]['multiplier'] == multiplier
        assert snapshot['wishes'][0]['weight'] == 0.2 * multiplier
        assert snapshot['generation_options']['scoring']['wish_budget_points'] == staff['budget_points']
        assert snapshot['generation_options']['scoring']['wish_period'] == staff['period']
        process_one(h.app.state.engine, settings=settings, recover_owned=True)
        result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
        assert result['status'] == 'candidate_feasible' and result['validation']['valid']
        assert result['effective_options']['scoring']['wish_debt_cap'] == cap
        assert result['summary']['score']['settings']['weights'] == weights
        assert result['summary']['score_before']['settings']['weights'] == weights
        with transaction(h.app.state.engine) as db:
            completed_snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
        saved.append((job['id'], completed_snapshot, result))
    assert h.get('staff', endpoint) == staff
    assert h.get('coordinator', '/api/publications') == publications
    assert h.get('admin', '/api/me')['department']['generation_defaults'] == department
    assert h.snapshot(nxt)['wishes'][0]['multiplier'] == 1.35
    for job_id, snapshot, result in saved:
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, job_id).input_snapshot == snapshot
        assert h.get('coordinator', '/api/jobs/' + job_id)['job']['result'] == result

    # A run cap cannot enable department-disabled wishes, or erase the recorded wishes.
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'wish_budget_points': 0}}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    nxt = h.refresh_draft_baseline(nxt)
    disabled = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'),
        start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[])['workspace']
    for draft in (nxt, disabled):
        off = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation',
            'generation_options': {'scoring': {'wish_debt_cap': 3}}}, revision=draft['revision'])['job']
        with transaction(h.app.state.engine, write=True) as db:
            claim = claim_job(db)
        assert claim and claim['id'] == off['id']
        prepare_deferred_generation(h.app.state.engine, claim, h.settings)
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Job, off['id']).input_snapshot['wishes'] == []
        assert process_one(h.app.state.engine, settings=h.settings, recover_owned=True)
        assert h.get('staff', endpoint)['wishes'] == staff['wishes']


def test_published_reasons_are_private_on_every_staff_wish_projection(h, monkeypatch):
    import json
    from apps.api import models as m
    from apps.api.db import transaction

    person, other = h.seed['people'][:2]
    own = wish(h, '2026-10-04', points=2)['wish']
    peer = wish(h, '2026-10-05', role='coordinator', person=other)['wish']
    w = h.shift(h.workspace(), people=[person])['workspace']
    own_session = h.detail(w)['shifts'][0]['id']
    secret = 'PRIVATE COMPETITOR AND RULE DETAILS'
    reasons = [
        {'day': '2026-10-04', 'kind': 'rule', 'message': secret, 'rule_id': 'private-rule', 'rule_name': secret,
         'code': 'REST_GAP'},
        {'day': '2026-10-04', 'kind': 'competing_wishes', 'message': secret, 'shift_id': 'private-session',
         'session_label': secret, 'competing_wishes': [{'wish_id': peer['id'], 'person_id': other,
             'person_name': secret, 'kind': 'prefer_off', 'weight': 0.9, 'daily_weight': 0.9}]},
        {'day': '2026-10-04', 'kind': 'cover', 'message': 'This duty needed you', 'shift_id': own_session,
         'session_label': 'Your published duty', 'start': '2026-10-04T08:00:00+03:00',
         'end': '2026-10-04T12:00:00+03:00'},
    ]
    monkeypatch.setattr('packages.domain.wish_reasons.declined_wishes', lambda data, validation: [
        {'wish_id': own['id'], 'reasons': reasons},
        {'wish_id': peer['id'], 'reasons': [{'day': '2026-10-05', 'kind': 'tradeoff', 'message': secret}]}])
    publication_id = h.publish(w)['publication_id']
    endpoints = [
        '/api/wishes?start=2026-10-01&end=2026-11-01',
        '/api/requests/all?type=wish', '/api/requests/all?id=' + own['id'],
        f'/api/fairness/sessions?person_id={person}&metric=wishes_declined&starts_on=2026-10-01&ends_on=2026-11-01',
    ]
    for endpoint in endpoints:
        staff = h.get('staff', endpoint)
        encoded = json.dumps(staff)
        assert secret not in encoded and other not in encoded and peer['id'] not in encoded
        assert 'private-rule' not in encoded and 'private-session' not in encoded
        assert "A colleague's higher-priority preference was honoured." in encoded
        assert own_session in encoded and 'Your published duty' in encoded
        assert secret in json.dumps(h.get('coordinator', endpoint))
    h.get('staff', '/api/requests/all?id=' + peer['id'], status=404)
    h.get('staff', '/api/wishes?person_id=' + other, status=403)
    assert all('outcome_reasons' not in row for row in h.snapshot(w)['wishes'])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.StaffWish, own['id']).outcome_reasons = []
    snapshot = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    assert next(row for row in snapshot['wishes'] if row['id'] == own['id'])['outcome_reasons'] == reasons


def test_publication_reuses_full_applied_receipt_and_rejects_partial_application(h):
    from copy import deepcopy
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services.wishes import _applied_reasons
    from packages.domain.time import aware

    person = h.seed['people'][0]
    own = wish(h, '2026-10-04', points=2)['wish']
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(w)
    first, extra = data['shifts'][:2]
    # Freeze a result that adds one person to each session; the current draft has applied only the second addition.
    participant = deepcopy(extra['participants'][0])
    participant.update(start=first['start'], end=first['end'])
    reasons = [{'day': '2026-10-04', 'kind': 'cover', 'message': 'Frozen run explanation',
                'shift_id': first['id'], 'session_label': 'Frozen duty'}]
    receipt = [{'wish_id': own['id'], 'person_id': person, 'kind': 'prefer_off', 'weight': data['wishes'][0]['weight'],
                'honoured': 0.0, 'reasons': reasons}]
    original = deepcopy(data)
    for shift in original['shifts']:
        shift['participants'] = []
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Job(department_id=h.seed['department_id'], workspace_id=w['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='applied',
            input_fingerprint=original['fingerprint'], input_snapshot=original,
            result={'status': 'candidate_feasible', 'applied_content_revision': w['content_revision'],
                'additions': [{'shift_id': first['id'], 'participant': participant},
                              {'shift_id': extra['id'], 'participant': extra['participants'][0]}],
                'removals': [], 'declined_wishes': receipt}))
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, w['id'])
        assert _applied_reasons(db, workspace, data, data['wishes']) is None
        complete = deepcopy(data)
        complete['shifts'][0]['participants'] = [participant]
        assert _applied_reasons(db, workspace, complete, data['wishes']) == receipt
        for key in ('points', 'weight', 'multiplier'):
            changed_wishes = deepcopy(data['wishes'])
            changed_wishes[0][key] += 1
            assert _applied_reasons(db, workspace, complete, changed_wishes) is None
        for invalid in (float('nan'), float('inf'), -float('inf')):
            changed_wishes = deepcopy(data['wishes'])
            changed_wishes[0]['weight'] = invalid
            assert _applied_reasons(db, workspace, complete, changed_wishes) is None
        stale = deepcopy(complete)
        stale['organisation']['input_revision'] += 1
        assert _applied_reasons(db, workspace, stale, data['wishes']) == receipt
        stale = deepcopy(complete)
        stale['organisation']['availability_revision'] += 1
        assert _applied_reasons(db, workspace, stale, data['wishes']) == receipt
        stale = deepcopy(complete)
        stale['shifts'].append({**deepcopy(stale['shifts'][0]), 'id': 'new-external-duty'})
        assert _applied_reasons(db, workspace, stale, data['wishes']) is None
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Assignment(shift_id=first['id'], person_id=person, start=aware(first['start']), end=aware(first['end'])))
    h.publish(w)
    [published] = h.get('coordinator', '/api/wishes?start=2026-10-01&end=2026-11-01')['wishes']
    assert published['outcome'] == 'declined' and published['outcome_reasons'] == reasons


@pytest.mark.parametrize('kind,settings', [
    ('max_days_in_row', {'limit': 60}),
    ('min_days_off_together', {'limit': 60}),
    ('max_weekends_in_row', {'limit': 60, 'weekend_start': 6, 'weekend_days': 3}),
    ('whole_weekends', {'weekend_start': 6, 'weekend_days': 3}),
])
def test_pattern_wish_contract_accepts_months_and_large_limits(kind, settings):
    from packages.contracts.schemas import StaffWishInput
    value = StaffWishInput(person_id='00000000-0000-4000-8000-000000000001', first_day='2026-10-01',
                           last_day='2026-10-31', kind=kind, **settings)
    assert value.kind == kind and all(getattr(value, key) == setting for key, setting in settings.items())


@pytest.mark.parametrize('kind,settings', [
    ('max_days_in_row', {}), ('min_days_off_together', {}), ('max_weekends_in_row', {}),
    ('max_days_in_row', {'limit': 0}), ('max_days_in_row', {'limit': True}), ('max_days_in_row', {'limit': 2.0}),
    ('max_days_in_row', {'limit': 2_147_483_648}),
    ('whole_weekends', {'limit': 2}), ('prefer_off', {'limit': 2}),
    ('max_days_in_row', {'limit': 2, 'weekend_start': 4}),
    ('min_days_off_together', {'limit': 2, 'weekend_days': 2}),
    ('whole_weekends', {'weekend_days': 1}), ('whole_weekends', {'weekend_days': 8}),
    ('whole_weekends', {'weekend_start': 7}), ('whole_weekends', {'weekend_start': False}),
    ('whole_weekends', {'assignment_type_id': '00000000-0000-4000-8000-000000000002'}),
    ('prefer_on', {'last_day': '2026-10-15'}), ('max_days_in_row', {'limit': 2, 'last_day': '2026-09-30'}),
])
def test_pattern_wish_contract_rejects_incoherent_fields(kind, settings):
    from packages.contracts.schemas import StaffWishInput
    with pytest.raises(ValidationError):
        StaffWishInput.model_validate({'person_id': '00000000-0000-4000-8000-000000000001',
                                      'first_day': '2026-10-01', 'last_day': '2026-10-01', 'kind': kind, **settings})


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_pattern_wishes_persist_additively_with_shared_budget_snapshot_and_undo(database, request):
    h = request.getfixturevalue(database)
    me = h.seed['people'][0]
    saved = wish(h, '2026-10-01', last='2026-10-31', kind='max_days_in_row', limit=2_147_483_647, points=2)['wish']
    wish(h, '2026-10-05')  # A date wish and different patterns add to the same days.
    wish(h, '2026-10-01', last='2026-10-31', kind='min_days_off_together', limit=2, points=3)
    wish(h, '2026-10-01', last='2026-10-31', kind='max_weekends_in_row', limit=2, weekend_start=6, weekend_days=2)
    wish(h, '2026-10-01', last='2026-10-31', kind='whole_weekends', points=2)
    assert wish(h, '2026-10-15', last='2026-10-31', kind='max_days_in_row', limit=4, status=409)['error']['code'] == 'WISH_OVERLAP'
    assert wish(h, '2026-10-07', points=2, status=409)['error']['code'] == 'WISH_BUDGET'
    listed = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert len(listed['wishes']) == 5 and listed['usage'][0]['used'] == 9
    assert next(w for w in listed['wishes'] if w['id'] == saved['id'])['limit'] == 2_147_483_647
    snapshot = h.snapshot(h.workspace())
    assert next(w for w in snapshot['wishes'] if w['id'] == saved['id'])['limit'] == 2_147_483_647
    assert {w['kind']: w['weight'] for w in snapshot['wishes']} == {
        'max_days_in_row': 0.2, 'prefer_off': 0.1, 'min_days_off_together': 0.3,
        'max_weekends_in_row': 0.1, 'whole_weekends': 0.2}
    payload = {key: saved[key] for key in ('person_id', 'first_day', 'last_day', 'kind', 'assignment_type_id',
                                          'limit', 'weekend_start', 'weekend_days', 'points', 'note', 'repeat')}
    changed = h.command('staff', 'wish.update', {**payload, 'limit': 4, 'points': 1}, saved['id'], saved['revision'])
    assert changed['wish']['limit'] == 4 and changed['previous'] == payload
    restored = h.command('staff', 'wish.update', changed['previous'], saved['id'], changed['wish']['revision'])['wish']
    assert restored['limit'] == 2_147_483_647 and restored['points'] == 2
    h.command('staff', 'wish.withdraw', {'reason': 'Plans changed'}, saved['id'], restored['revision'])
    wish(h, '2026-10-01', last='2026-10-31', kind='max_days_in_row', limit=4, points=3)
    assert h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage'][0]['used'] == 10


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_three_month_pattern_uses_each_budget_for_create_edit_and_withdraw(database, request):
    h = request.getfixturevalue(database)
    wish(h, '2026-11-20', points=8)
    refused = wish(h, '2026-10-01', last='2026-12-31', kind='max_days_in_row', limit=2, points=3, status=409)
    assert refused['error']['code'] == 'WISH_BUDGET'
    assert refused['error']['details'] == {'used': 8, 'budget': 10, 'period_start': '2026-11-01'}
    saved = wish(h, '2026-10-01', last='2026-12-31', kind='max_days_in_row', limit=2, points=2)['wish']
    endpoint = '/api/wishes?start=2026-10-01&end=2027-01-01'
    expected = [{'start': '2026-10-01', 'end': '2026-11-01', 'used': 2},
                {'start': '2026-11-01', 'end': '2026-12-01', 'used': 10},
                {'start': '2026-12-01', 'end': '2027-01-01', 'used': 2}]
    expected = [{**p, 'standing_points': 0, 'one_off_points': p['used'],
                 'unfunded_standing_points': 0, 'unfunded_one_off_points': 0} for p in expected]
    assert h.get('staff', endpoint)['usage'] == expected
    assert h.get('staff', '/api/wishes?start=2026-11-10&end=2026-11-11')['usage'] == expected[1:2]
    refused = wish(h, '2026-12-05', points=9, status=409)
    assert refused['error']['code'] == 'WISH_BUDGET'
    assert refused['error']['details'] == {'used': 2, 'budget': 10, 'period_start': '2026-12-01'}
    # Adjacent periods remain independent, including the year boundary.
    wish(h, '2026-09-30', points=10)
    wish(h, '2027-01-01', points=10)
    payload = {'person_id': saved['person_id'], 'first_day': saved['first_day'], 'last_day': saved['last_day'],
               'kind': saved['kind'], 'limit': 2, 'points': 1}
    changed = h.command('staff', 'wish.update', payload, saved['id'], saved['revision'])['wish']
    assert [p['used'] for p in h.get('staff', endpoint)['usage']] == [1, 9, 1]
    h.command('staff', 'wish.withdraw', {'reason': 'Plans changed'}, changed['id'], changed['revision'])
    assert [p['used'] for p in h.get('staff', endpoint)['usage']] == [0, 8, 0]
    wish(h, '2026-12-05', points=10)


@pytest.mark.parametrize('period,first,last,starts', [
    ('week', '2026-10-10', '2026-10-11', ['2026-10-04', '2026-10-11']),
    ('month', '2026-10-31', '2026-11-01', ['2026-10-01', '2026-11-01']),
    ('quarter', '2026-12-31', '2027-01-01', ['2026-10-01', '2027-01-01']),
])
def test_dated_wish_charges_both_periods_including_its_last_day(h, period, first, last, starts):
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'wish_period': period}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    wish(h, first, last=last, points=7)
    next_day = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    periods = h.get('staff', f'/api/wishes?start={first}&end={next_day}')['usage']
    assert [p['start'] for p in periods] == starts
    assert [p['used'] for p in periods] == [7, 7]
    refused = wish(h, next_day, points=4, status=409)
    assert refused['error']['code'] == 'WISH_BUDGET'
    assert refused['error']['details'] == {'used': 7, 'budget': 10, 'period_start': starts[-1]}
    wish(h, next_day, points=3)


def test_pattern_wish_can_cover_leave_without_charging_a_date_wish_on_leave(h):
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-08T00:00:00+03:00', 'private_reason': 'Annual leave'})
    wish(h, '2026-10-01', last='2026-10-31', kind='min_days_off_together', limit=2)
    assert wish(h, '2026-10-06', status=409)['error']['code'] == 'WISH_ON_LEAVE'


def test_publication_records_pattern_results_and_charges_points_once(h):
    me = h.seed['people'][0]
    broken_run = wish(h, '2026-10-01', last='2026-10-31', kind='max_days_in_row', limit=1, points=2)['wish']
    broken_weekend = wish(h, '2026-10-01', last='2026-10-31', kind='whole_weekends')['wish']
    granted = wish(h, '2026-10-01', last='2026-10-31', kind='min_days_off_together', limit=2)['wish']
    w = h.workspace()
    for day in (4, 5, 9):
        w = h.shift(w, start=f'2026-10-{day:02d}T08:00:00+03:00',
                    end=f'2026-10-{day:02d}T12:00:00+03:00', people=[me])['workspace']
    h.publish(w)
    rows = {w['id']: w for w in h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['wishes']}
    assert rows[broken_run['id']]['outcome'] == rows[broken_weekend['id']]['outcome'] == 'declined'
    assert rows[granted['id']]['outcome'] == 'granted'
    # Three declined points minus half of one granted point, divided by the ten-point budget.
    assert h.get('staff', '/api/wishes?start=2026-10-12&end=2026-11-01')['multipliers'][me] == 1.25
    drill = h.get('staff', f'/api/fairness/sessions?person_id={me}&metric=wishes_declined&starts_on=2026-10-01&ends_on=2026-11-01')
    [publication] = drill['publications']
    assert publication['value'] == 3
    wishes = {w['kind']: w for w in publication['wishes']}
    assert wishes['max_days_in_row']['limit'] == 1
    assert wishes['whole_weekends']['weekend_start'] is wishes['whole_weekends']['weekend_days'] is None
