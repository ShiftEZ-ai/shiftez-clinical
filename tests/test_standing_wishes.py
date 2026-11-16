"""Standing defaults expand once into ordinary wishes, sharing each person's period budget."""
from collections import Counter
import copy
import gc
from datetime import date
from time import process_time
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import jobs
from apps.api.services.wishes import snapshot_wishes
from apps.worker.main import process_one
from packages.contracts import schemas as s
from packages.domain.cp_sat import generate
from packages.domain.patterns import wish_days, wish_outcomes
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate


def standing(h, *, weekdays=None, starts_on='2026-10-01', ends_on=None, number=None,
             points=1, person=None, role='staff', status=200, **extra):
    return h.command(role, 'wish.create', {
        'person_id': person or h.seed['people'][0], 'kind': 'prefer_off', 'points': points,
        'repeat': {'weekdays': [0] if weekdays is None else weekdays, 'starts_on': starts_on,
                   'ends_on': ends_on, 'number': number}, **extra}, status=status)


def override(h, wish, *, role='staff', status=200, **payload):
    return h.command(role, 'wish.override', payload, wish['id'], wish['revision'], status=status)


def expanded(h, workspace, until=None, *, person_id=None):
    with transaction(h.app.state.engine) as db:
        return snapshot_wishes(db, [h.seed['department_id']], db.get(m.Workspace, workspace['id']), until,
                               person_id=person_id)


def configure(h, **scoring):
    me = h.get('admin', '/api/me')
    h.command('admin', 'department.settings', {
        'department_name': me['department']['name'], 'department_revision': me['department']['revision'],
        'organisation_name': me['organisation']['name'], 'timezone': me['organisation']['timezone'],
        'week_start': me['organisation']['week_start'], 'reason': 'Synthetic standing-preference budget',
        'generation_defaults': {'scoring': scoring}}, h.seed['department_id'], me['organisation']['revision'])


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_submission_keeps_its_check_fresh_while_invalidating_neighbour_allocations(database, request):
    h = request.getfixturevalue(database)
    standing(h, points=3)
    candidate = h.shift(h.workspace('Submit first week'))['workspace']
    neighbour = h.shift(h.workspace('Next week', '2026-10-11', '2026-10-18'),
        start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00')['workspace']
    h.lifecycle(neighbour, 'validate')
    before = h.detail(neighbour)
    assert before['workspace']['validation']['fingerprint'] == before['current_fingerprint']
    h.lifecycle(candidate, 'submit')
    for role in ('coordinator', 'approver', 'auditor'):
        detail = h.get(role, '/api/workspaces/' + candidate['id'])
        assert detail['workspace']['validation']['fingerprint'] == detail['current_fingerprint']
        assert detail['workspace']['submitted_fingerprint'] == detail['current_fingerprint']
        assert h.get(role, '/api/workspaces/' + candidate['id'] + '/rule-map')['fresh'] is True
    after = h.detail(neighbour)
    assert after['workspace']['validation']['fingerprint'] == after['current_fingerprint'] == before['current_fingerprint']


@pytest.mark.parametrize('repeat', [
    {'weekdays': []}, {'weekdays': [0, 0]}, {'weekdays': [-1]}, {'weekdays': [7]},
    {'weekdays': [True]}, {'ends_on': '2026-09-30'}, {'number': 0}, {'number': 1.5},
])
def test_invalid_repeat_is_rejected_at_the_command_boundary(h, repeat):
    payload = {'weekdays': [0], 'starts_on': '2026-10-01', **repeat}
    h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
                                      'repeat': payload}, status=422)


def test_open_ended_defaults_are_listed_and_expand_to_unique_concrete_days(h):
    wish = standing(h, starts_on='2026-09-28', note='Private synthetic explanation')['wish']
    assert wish['first_day'] == wish['last_day'] == '2026-09-28'
    assert wish['repeat']['ends_on'] is None
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    concrete = expanded(h, workspace)
    assert [w['first_day'] for w in concrete] == ['2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26']
    assert all(w['first_day'] == w['last_day'] and w['standing_wish_id'] == wish['id'] for w in concrete)
    assert len({w['id'] for w in concrete}) == 4 and wish['id'] not in {w['id'] for w in concrete}
    for w in concrete:
        assert s.WishEffect(wish_id=w['id'], effect='against').wish_id == w['id']
    with pytest.raises(ValueError):
        s.WishEffect(wish_id='standing:invalid:2026-10-05', effect='against')
    assert all('note' not in w and w['weight'] == .1 for w in concrete)
    assert [w['id'] for w in expanded(h, workspace)] == [w['id'] for w in concrete]
    assert [w['first_day'] for w in expanded(h, workspace, date(2026, 10, 15))] == ['2026-10-05', '2026-10-12']
    listed = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert [w['id'] for w in listed['wishes']] == [wish['id']]
    # The existing domain evaluator consumes concrete wishes without a repeat branch.
    data = {'workspace': workspace, 'organisation': {'timezone': 'Asia/Riyadh'}, 'shifts': [], 'wishes': concrete}
    assert [wish_days(w, data) for w in concrete] == [[date.fromisoformat(w['first_day'])] for w in concrete]
    assert wish_outcomes(data) == {w['id']: (1, 1) for w in concrete}


def test_one_point_standing_wish_keeps_positive_weight_with_large_budget(h, monkeypatch):
    from apps.api.services import wishes

    wish = standing(h, points=1)['wish']
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    scoring = wishes.scoring
    # Exercise snapshot precision beyond the current command contract's 100-point ceiling.
    monkeypatch.setattr(wishes, 'scoring', lambda db, department_id:
        scoring(db, department_id).model_copy(update={'wish_budget_points': 2_000_001}))
    concrete = expanded(h, workspace)
    assert len(concrete) == 4
    assert all(w['standing_wish_id'] == wish['id'] and w['weight'] > 0 for w in concrete)
    assert all(w['weight'] == 1 / 2_000_001 for w in concrete)


@pytest.mark.parametrize('is_standing', [False, True])
@pytest.mark.parametrize('multiplier', [1.0, 1.35, 3.0])
def test_budget_cut_caps_wish_weight_at_catch_up_multiplier(h, monkeypatch, is_standing, multiplier):
    from apps.api.services import wishes

    person = h.seed['people'][0]
    if is_standing:
        saved = standing(h, points=6, number=1)['wish']
    else:
        saved = h.command('staff', 'wish.create', {'person_id': person, 'kind': 'prefer_off',
            'first_day': '2026-10-06', 'last_day': '2026-10-06', 'points': 6})['wish']
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')

    def budget_cut(db, department_id, people, settings, before=None):
        # Inject at normalisation so this regression does not change or assert P6 allocation semantics.
        settings.wish_budget_points = 2
        return {pid: multiplier for pid in people}

    monkeypatch.setattr(wishes, 'multipliers', budget_cut)
    concrete = expanded(h, workspace)
    assert len(concrete) == 1
    assert concrete[0].get('standing_wish_id', concrete[0]['id']) == saved['id']
    assert concrete[0]['points'] == 6
    assert concrete[0]['weight'] == concrete[0]['multiplier'] == multiplier


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('budget,boost', [(10, 1.2), (1, 2)])
def test_adjacent_live_one_off_uses_current_scoring_without_changing_publication(request, database, budget, boost):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    configure(h, wish_debt_cap=1)
    saved = h.command('staff', 'wish.create', {'person_id': person, 'kind': 'prefer_off',
        'first_day': '2026-10-14', 'last_day': '2026-10-15', 'points': 2})['wish']
    first = h.shift(h.workspace('Issued first half', '2026-10-01', '2026-10-15'),
        start='2026-10-14T08:00:00+03:00', end='2026-10-14T12:00:00+03:00', people=[person])['workspace']
    publication_id = h.publish(first)['publication_id']
    original = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    assert [(w['weight'], w['multiplier']) for w in original['wishes']] == [(0.2, 1)]
    later = h.workspace('Live second half', '2026-10-15', '2026-11-01')
    configure(h, wish_budget_points=budget, wish_debt_cap=2)
    [current] = expanded(h, later)
    assert current['id'] == saved['id'] and current['points'] == 2
    assert current['multiplier'] == boost
    assert current['weight'] == pytest.approx(min(2, budget) / budget * boost)
    [usage] = h.get('staff', '/api/wishes?start=2026-10-15&end=2026-11-01&workspace_id=' + later['id'])['usage']
    assert usage['one_off_points'] == usage['used'] == 2
    assert h.get('coordinator', '/api/publications/' + publication_id)['snapshot'] == original


def test_repeat_dates_clip_inclusively_and_update_retains_undo_payload(h):
    saved = standing(h, weekdays=[0, 2], starts_on='2026-10-06', ends_on='2026-10-19')['wish']
    workspace = h.workspace(start='2026-10-01', end='2026-10-21')
    assert [w['first_day'] for w in expanded(h, workspace)] == ['2026-10-07', '2026-10-12', '2026-10-14', '2026-10-19']
    changed = h.command('staff', 'wish.update', {
        'person_id': saved['person_id'], 'kind': 'prefer_off', 'points': 2,
        'repeat': {'weekdays': [2], 'starts_on': '2026-10-07', 'ends_on': '2026-10-14'}},
        saved['id'], saved['revision'])
    assert changed['previous']['repeat'] == saved['repeat']
    assert [(w['first_day'], w['points']) for w in expanded(h, workspace)] == [('2026-10-07', 2), ('2026-10-14', 2)]


@pytest.mark.parametrize('modifier,days,points', [
    ({'skip': True}, [], 1),
    ({'points': 2}, ['2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26'], 2),
    ({'number': 2}, ['2026-10-05', '2026-10-12'], 1),
])
def test_each_period_override_and_reset(h, modifier, days, points):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    saved = standing(h)['wish']
    reply = override(h, saved, period_start='2026-10-01', **modifier)
    assert reply['wish']['revision'] == saved['revision'] + 1
    assert reply['previous'] == {'period_start': '2026-10-01', 'workspace_id': None,
                                 'skip': False, 'points': None, 'number': None}
    concrete = expanded(h, workspace)
    assert [w['first_day'] for w in concrete] == days
    assert all(w['points'] == points for w in concrete)
    listing = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert len(listing['overrides']) == 1 and listing['overrides'][0]['wish_id'] == saved['id']
    reset = override(h, reply['wish'], period_start='2026-10-01')
    assert reset['previous']['period_start'] == '2026-10-01'
    assert len(expanded(h, workspace)) == 4
    reset_row = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['overrides'][0]
    assert reset_row['skip'] is False and reset_row['points'] is None and reset_row['number'] is None


def test_rota_override_replaces_period_override_and_does_not_leak(h):
    first = h.workspace('First rota', '2026-10-01', '2026-10-15')
    second = h.workspace('Second rota', '2026-10-15', '2026-11-01')
    saved = standing(h)['wish']
    saved = override(h, saved, period_start='2026-10-01', skip=True)['wish']
    saved = override(h, saved, workspace_id=first['id'], points=2, number=2)['wish']
    assert [(w['first_day'], w['points']) for w in expanded(h, first)] == [('2026-10-05', 2), ('2026-10-12', 2)]
    assert expanded(h, second) == []
    rota_usage = h.get('staff', f"/api/wishes?start=2026-10-01&end=2026-11-01&workspace_id={first['id']}")['usage'][0]
    assert rota_usage['standing_points'] == 4
    assert h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage'][0]['standing_points'] == 4


def test_rota_skip_frees_the_shared_period_budget_in_every_context(h):
    first = h.workspace('Skipped defaults', '2026-10-01', '2026-10-15')
    second = h.workspace('Remaining defaults', '2026-10-15', '2026-11-01')
    saved = standing(h, points=3)['wish']
    override(h, saved, workspace_id=first['id'], skip=True)
    assert expanded(h, first) == []
    assert [(wish['first_day'], wish['points']) for wish in expanded(h, second)] == [
        ('2026-10-19', 3), ('2026-10-26', 3)]
    endpoint = '/api/wishes?start=2026-10-01&end=2026-11-01'
    usage = h.get('staff', endpoint)['usage']
    assert usage == [{'start': '2026-10-01', 'end': '2026-11-01', 'used': 6,
        'standing_points': 6, 'one_off_points': 0, 'unfunded_standing_points': 0, 'unfunded_one_off_points': 0}]
    for workspace in (first, second):
        assert h.get('staff', endpoint + '&workspace_id=' + workspace['id'])['usage'] == usage


def test_number_selects_earliest_matches_before_rota_clipping(h):
    workspace = h.workspace(start='2026-10-11', end='2026-10-20')
    saved = standing(h, number=1)['wish']
    assert expanded(h, workspace) == []  # October's first Monday was before this rota.
    override(h, saved, period_start='2026-10-01', number=2)
    assert [w['first_day'] for w in expanded(h, workspace)] == ['2026-10-12']


def test_rota_number_selects_earliest_match_across_budget_periods(h):
    workspace = h.workspace(start='2026-10-15', end='2026-11-15')
    saved = standing(h)['wish']
    override(h, saved, workspace_id=workspace['id'], number=1)
    assert [w['first_day'] for w in expanded(h, workspace)] == ['2026-10-19']


@pytest.mark.parametrize('start,end', [('2026-11-01', '2026-11-15'), ('2026-11-14', '2026-11-22')])
@pytest.mark.parametrize('number,days', [(1, ['2026-10-19']), (3, ['2026-10-19', '2026-10-26', '2026-11-02'])])
def test_rota_number_keeps_earlier_matches_in_suffix_views(h, start, end, number, days):
    workspace = h.workspace(start='2026-10-15', end=end)
    saved = standing(h)['wish']
    override(h, saved, workspace_id=workspace['id'], number=number)
    full = expanded(h, workspace)
    assert [w['first_day'] for w in full] == days
    later = h.workspace('Later view', start, end)
    suffix = [w for w in full if w['first_day'] >= start]
    assert expanded(h, later) == suffix
    assert expanded(h, later, person_id=saved['person_id']) == suffix


def test_rota_number_replay_excludes_leave_before_the_budget_window(h):
    workspace = h.workspace(start='2026-10-15', end='2026-11-22')
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'start': '2026-10-19T00:00:00+03:00', 'end': '2026-10-27T00:00:00+03:00',
        'private_reason': 'Synthetic annual leave'})
    saved = standing(h)['wish']
    override(h, saved, workspace_id=workspace['id'], number=3)
    full = expanded(h, workspace)
    assert [w['first_day'] for w in full] == ['2026-11-02', '2026-11-09', '2026-11-16']
    later = h.workspace('Later view after leave', '2026-11-14', '2026-11-22')
    assert expanded(h, later) == [w for w in full if w['first_day'] >= later['starts_on']]
    assert expanded(h, later, person_id=saved['person_id']) == expanded(h, later)


def test_default_number_still_selects_earliest_match_in_each_budget_period(h):
    workspace = h.workspace(start='2026-10-15', end='2026-11-15')
    standing(h, starts_on='2026-10-15', number=1)
    assert [w['first_day'] for w in expanded(h, workspace)] == ['2026-10-19', '2026-11-02']


def test_standing_charge_first_and_report_unfunded_one_offs_without_partial_points(h):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    one_off = h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
        'first_day': '2026-10-06', 'last_day': '2026-10-06', 'points': 4})['wish']
    reply = standing(h, points=3)
    saved = reply['wish']
    concrete = expanded(h, workspace)
    assert [w['first_day'] for w in concrete] == ['2026-10-05', '2026-10-12', '2026-10-19']
    assert all(w['standing_wish_id'] == saved['id'] and w['points'] == 3 for w in concrete)
    listing = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert {w['id'] for w in listing['wishes']} == {saved['id'], one_off['id']}
    assert reply['usage'] == listing['usage'] == [{'start': '2026-10-01', 'end': '2026-11-01', 'used': 9,
        'standing_points': 9, 'one_off_points': 0, 'unfunded_standing_points': 3, 'unfunded_one_off_points': 4}]


@pytest.mark.parametrize('standing_first', [False, True])
@pytest.mark.parametrize('kind', ['prefer_off', 'prefer_on', 'prefer_shift_type', 'avoid_shift_type'])
def test_one_off_replaces_standing_days_and_withdrawal_restores_defaults(h, standing_first, kind):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    saved = standing(h, weekdays=[6], points=2)['wish'] if standing_first else None
    one_off = h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': kind,
        'first_day': '2026-10-04', 'last_day': '2026-10-11', 'points': 4,
        **({'assignment_type_id': h.seed['type']} if 'shift_type' in kind else {})})['wish']
    if saved is None:
        saved = standing(h, weekdays=[6], points=2)['wish']
    concrete = expanded(h, workspace)
    assert [(w['first_day'], w['last_day'], w['kind']) for w in concrete] == [
        ('2026-10-18', '2026-10-18', 'prefer_off'), ('2026-10-25', '2026-10-25', 'prefer_off'),
        ('2026-10-04', '2026-10-11', kind)]
    usage = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage'][0]
    assert (usage['used'], usage['standing_points'], usage['one_off_points'], usage['unfunded_standing_points']) == (8, 4, 4, 0)
    h.command('staff', 'wish.withdraw', {'reason': 'Restore standing defaults'}, one_off['id'], one_off['revision'])
    assert [w['first_day'] for w in expanded(h, workspace)] == ['2026-10-04', '2026-10-11', '2026-10-18', '2026-10-25']


def test_one_off_replacement_keeps_number_selection_and_cross_period_charges(h):
    workspace = h.workspace(start='2026-11-01', end='2026-12-01')
    one_off = h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_on',
        'first_day': '2026-10-31', 'last_day': '2026-11-01', 'points': 4})['wish']
    saved = standing(h, weekdays=[6], starts_on='2026-11-01', number=1)
    assert [w['id'] for w in expanded(h, workspace)] == [one_off['id']]
    assert saved['usage'] == [{'start': '2026-11-01', 'end': '2026-12-01', 'used': 4,
        'standing_points': 0, 'one_off_points': 4, 'unfunded_standing_points': 0, 'unfunded_one_off_points': 0}]


@pytest.mark.parametrize('starts_on,ends_on,weekdays,status', [
    ('2026-10-01', None, [6], 409),
    ('2026-09-01', None, [6], 409),
    ('2026-10-25', '2026-11-01', [0, 6], 409),
    ('2026-09-01', '2026-10-01', [6], 409),
    ('2026-10-26', None, [6], 200),
    ('2026-09-01', '2026-09-30', [6], 200),
    ('2026-10-01', None, [0], 200),
])
def test_standing_overlap_uses_inclusive_repeat_ranges_and_shared_weekdays(h, starts_on, ends_on, weekdays, status):
    standing(h, weekdays=[6], starts_on='2026-10-01', ends_on='2026-10-25')
    reply = standing(h, weekdays=weekdays, starts_on=starts_on, ends_on=ends_on, kind='prefer_on', status=status)
    if status == 409:
        assert reply['error']['code'] == 'WISH_OVERLAP'
        assert reply['error']['message'] == 'There is already a standing preference on Sundays; change that one instead'


def test_standing_overlap_on_update_excludes_self_withdrawn_and_other_people(h):
    saved = standing(h, weekdays=[6])['wish']
    standing(h, weekdays=[6], person=h.seed['people'][1], role='coordinator')
    payload = {'person_id': saved['person_id'], 'kind': 'prefer_on', 'points': 2, 'repeat': saved['repeat']}
    changed = h.command('staff', 'wish.update', payload, saved['id'], saved['revision'])
    assert changed['previous']['repeat'] == saved['repeat']
    assert changed['usage'][0]['standing_points'] == 8
    monday = standing(h, weekdays=[0])['wish']
    rejected = h.command('staff', 'wish.update', payload, monday['id'], monday['revision'], status=409)
    assert rejected['error']['code'] == 'WISH_OVERLAP'
    h.command('staff', 'wish.withdraw', {'reason': 'Replace Sunday default'}, saved['id'], changed['wish']['revision'])
    h.command('staff', 'wish.update', payload, monday['id'], monday['revision'])


def test_wish_points_share_the_contract_ceiling():
    for cls in (s.StaffWishInput, s.WishOverrideInput):
        assert next(item.le for item in cls.model_fields['points'].metadata if getattr(item, 'le', None)) == s.MAX_WISH_POINTS


@pytest.mark.postgres
def test_override_database_check_has_only_a_lower_point_bound(pg):
    h = pg
    workspace = h.workspace()
    saved = standing(h)['wish']
    changed = override(h, saved, workspace_id=workspace['id'], points=1)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.WishOverride, changed['override']['id']).points = s.MAX_WISH_POINTS
    with pytest.raises(IntegrityError), transaction(h.app.state.engine, write=True) as db:
        db.get(m.WishOverride, changed['override']['id']).points = 0
        db.flush()


def test_months_have_independent_budgets_and_usage_splits(h):
    workspace = h.workspace(start='2026-10-01', end='2026-12-01')
    standing(h)
    h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
        'first_day': '2026-10-06', 'last_day': '2026-10-06', 'points': 2})
    concrete = expanded(h, workspace)
    assert Counter(w['first_day'][:7] for w in concrete) == {'2026-10': 5, '2026-11': 5}
    usage = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-12-01')['usage']
    assert [(u['used'], u['standing_points'], u['one_off_points']) for u in usage] == [(6, 4, 2), (5, 5, 0)]


def test_one_off_update_cannot_displace_an_already_funded_one_off(h):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    first_payload = {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
        'first_day': '2026-10-06', 'last_day': '2026-10-06', 'points': 6}
    first = h.command('staff', 'wish.create', first_payload)['wish']
    second = h.command('staff', 'wish.create', {**first_payload,
        'first_day': '2026-10-07', 'last_day': '2026-10-07', 'points': 4})['wish']
    rejected = h.command('staff', 'wish.update', {**first_payload, 'points': 7},
                         first['id'], first['revision'], status=409)
    assert rejected['error']['code'] == 'WISH_BUDGET'
    assert {wish['id']: wish['points'] for wish in expanded(h, workspace)} == {first['id']: 6, second['id']: 4}
    assert h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage'][0]['one_off_points'] == 10


@pytest.mark.parametrize('payload,status', [
    ({}, 422), ({'period_start': '2026-10-01', 'workspace_id': str(uuid4())}, 422),
    ({'period_start': '2026-10-02'}, 422), ({'period_start': '2026-12-01'}, 404),
    ({'period_start': '2026-10-01', 'points': 0}, 422), ({'period_start': '2026-10-01', 'number': 0}, 422),
])
def test_override_validates_period_and_modifiers(h, payload, status):
    h.workspace(start='2026-10-01', end='2026-11-01')
    saved = standing(h)['wish']
    override(h, saved, status=status, **payload)


def test_override_requires_existing_standing_source_and_rota(h):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    saved = standing(h)['wish']
    override(h, saved, workspace_id=str(uuid4()), points=2, status=404)
    override(h, {**saved, 'id': str(uuid4())}, workspace_id=workspace['id'], points=2, status=404)
    one_off = h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
        'first_day': '2026-10-06', 'last_day': '2026-10-06'})['wish']
    override(h, one_off, workspace_id=workspace['id'], skip=True, status=409)


def test_override_ownership_stale_revision_and_submitted_rota_lock(h):
    workspace = h.shift(h.workspace())['workspace']
    other = standing(h, person=h.seed['people'][1], role='coordinator')['wish']
    override(h, other, workspace_id=workspace['id'], skip=True, status=403)
    own = standing(h)['wish']
    changed = override(h, own, workspace_id=workspace['id'], points=2)['wish']
    override(h, own, workspace_id=workspace['id'], points=3, status=409)
    h.lifecycle(workspace, 'submit')
    item = next(item for item in h.get('staff', '/api/requests/all')['items'] if item['id'] == own['id'])
    assert item['locked'] is True
    assert override(h, changed, workspace_id=workspace['id'], skip=True, status=409)['error']['code'] == 'WISH_LOCKED'
    assert override(h, changed, period_start='2026-10-01', skip=True, status=409)['error']['code'] == 'WISH_LOCKED'


@pytest.mark.parametrize('modifier', [{'skip': True}, {'points': 2}, {'number': 2}, {}],
                         ids=['skip', 'points', 'number', 'reset'])
def test_draft_rota_override_cannot_change_overlapping_submitted_wishes(h, modifier):
    settled = h.shift(h.workspace('Submitted rota', '2026-10-01', '2026-10-20'),
                      start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    draft = h.workspace('Overlapping draft', '2026-10-15', '2026-11-01')
    saved = standing(h)['wish']
    if not modifier:
        saved = override(h, saved, workspace_id=draft['id'], points=2)['wish']
    h.lifecycle(settled, 'submit')
    endpoint = '/api/wishes?start=2026-10-01&end=2026-11-01'
    before = h.get('staff', endpoint)
    assert before['wishes'][0]['locked'] is True
    settled_wishes, draft_wishes = expanded(h, settled), expanded(h, draft)
    assert any(w['first_day'] == '2026-10-19' for w in settled_wishes)

    rejected = override(h, saved, workspace_id=draft['id'], status=409, **modifier)

    assert rejected['error']['code'] == 'WISH_LOCKED'
    assert rejected['error']['message'] == 'A rota for these days has been submitted; this preference can no longer change'
    assert h.get('staff', endpoint) == before
    assert expanded(h, settled) == settled_wishes
    assert expanded(h, draft) == draft_wishes


def test_draft_rota_override_works_next_to_submitted_rota(h):
    settled = h.shift(h.workspace())['workspace']
    draft = h.workspace('Next draft', '2026-10-11', '2026-10-18')
    saved = standing(h)['wish']
    h.lifecycle(settled, 'submit')
    assert [w['first_day'] for w in expanded(h, draft)] == ['2026-10-12']

    changed = override(h, saved, workspace_id=draft['id'], skip=True)

    assert changed['override']['skip'] is True
    assert expanded(h, draft) == []
    assert [(w['first_day'], w['points']) for w in expanded(h, settled)] == [('2026-10-05', 1)]
    override(h, changed['wish'], workspace_id=draft['id'])
    assert [w['first_day'] for w in expanded(h, draft)] == ['2026-10-12']


@pytest.mark.parametrize('status', ['submitted', 'approved'])
@pytest.mark.parametrize('points,funded,reopened', [
    (3, ['2026-10-19'], ['2026-10-19', '2026-10-26']), (6, [], ['2026-10-19']),
])
def test_adjacent_draft_skip_cannot_fund_an_unfunded_settled_occurrence(h, status, points, funded, reopened):
    configure(h, wish_budget_points=10)
    draft = h.workspace('Open first half', '2026-10-01', '2026-10-15')
    settled = h.shift(h.workspace('Settled second half', '2026-10-15', '2026-11-01'),
                      start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00')['workspace']
    saved = standing(h, points=points)['wish']
    before = expanded(h, settled)
    assert [(w['first_day'], w['points']) for w in before] == [(day, points) for day in funded]
    settled = h.lifecycle(settled, 'submit')['workspace']
    if status == 'approved':
        settled = h.lifecycle(settled, 'approve', 'approver')['workspace']

    changed = override(h, saved, workspace_id=draft['id'], skip=True)

    assert changed['override']['skip'] is True
    assert expanded(h, draft) == []
    assert expanded(h, settled) == before
    assert h.detail(settled)['wishes'] == [
        {**w, 'draft_outcome': {'outcome': 'declined', 'honoured_days': 0, 'total_days': 1}} for w in before]
    usage = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage'][0]
    assert usage['used'] == usage['standing_points'] == len(funded) * points
    h.lifecycle(settled, 'validate')
    assert expanded(h, settled) == before
    h.lifecycle(settled, 'reopen')
    assert [w['first_day'] for w in expanded(h, settled)] == reopened


@pytest.mark.postgres
@pytest.mark.parametrize('settled_status,reopen_role', [
    ('submitted', 'coordinator'), ('submitted', 'approver'), ('approved', 'coordinator'),
])
def test_reopening_neighbouring_rota_reconciles_generation_without_losing_evidence(pg, settled_status, reopen_role):
    h = pg
    configure(h, wish_period='month', wish_budget_points=10)
    saved = standing(h, points=3)['wish']
    settled = h.shift(h.workspace('Settled second half', '2026-10-15', '2026-11-01'),
                      start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00')['workspace']
    before_submit = h.snapshot(settled)
    settled = h.lifecycle(settled, 'submit')['workspace']
    submitted = h.snapshot(settled)
    assert submitted['organisation']['input_revision'] == before_submit['organisation']['input_revision'] + 1
    assert settled['submitted_fingerprint'] == submitted['fingerprint']
    assert [w['first_day'] for w in expanded(h, settled)] == ['2026-10-19']
    if settled_status == 'approved':
        settled = h.lifecycle(settled, 'approve', 'approver')['workspace']
        assert h.snapshot(settled)['fingerprint'] == submitted['fingerprint']
    draft = h.shift(h.workspace('Open first half', '2026-10-01', '2026-10-15'),
                    start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[])['workspace']
    # The settled 3-point occurrence leaves room for only one 4-point draft occurrence.
    override(h, saved, workspace_id=draft['id'], points=4)
    before = h.snapshot(draft)
    assert [w['first_day'] for w in before['wishes'] if w.get('standing_wish_id')] == ['2026-10-05']
    job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                    revision=draft['revision'])['job']
    process_one(h.app.state.engine)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded' and completed['result']['additions']
    with transaction(h.app.state.engine) as db:
        frozen_job = record(db.get(m.Job, job['id']))
        assert frozen_job['input_snapshot']['wishes'] == before['wishes']

    h.lifecycle(settled, 'reopen', reopen_role)
    current = h.snapshot(draft)
    assert [w['first_day'] for w in current['wishes'] if w.get('standing_wish_id')] == ['2026-10-05', '2026-10-12']
    detail = h.detail(draft)
    with transaction(h.app.state.engine) as db:
        changes = [record(row) for row in db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == draft['id']))]

    applied = h.command('coordinator', 'generation.apply', {'workspace_id': draft['id'], 'job_id': job['id'],
        'reason': 'Reconcile released neighbouring funding'}, revision=draft['revision'])

    assert applied['reconciliation']['status'] == 'applied'
    assert applied['reconciliation']['changed_facts']
    assert current['fingerprint'] != before['fingerprint']
    assert current['organisation']['input_revision'] == before['organisation']['input_revision'] + 1
    assert current['organisation']['availability_revision'] == before['organisation']['availability_revision']
    with transaction(h.app.state.engine) as db:
        stored = record(db.get(m.Job, job['id']))
        assert stored['input_snapshot'] == frozen_job['input_snapshot']
        assert all(stored['result'][key] == value for key, value in frozen_job['result'].items())
        assert len(list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == draft['id'])))) == len(changes) + 1


@pytest.mark.postgres
@pytest.mark.parametrize('mutation', ['archive', 'restore', 'resolve_overlap', 'undo_dates'])
def test_neighbouring_draft_lifecycle_and_dates_reconcile_retained_generation(pg, mutation):
    h = pg
    configure(h, wish_period='month', wish_budget_points=10)
    changing_dates = mutation in ('resolve_overlap', 'undo_dates')
    if changing_dates:
        # Closed October 5 still consumes the override's first match until the draft moves past it.
        h.publish(h.shift(h.workspace('Issued first week', '2026-10-01', '2026-10-08'))['workspace'])
    saved = standing(h, points=3)['wish']
    neighbour = h.workspace('Changing first half', '2026-10-01', '2026-10-15')
    override(h, saved, workspace_id=neighbour['id'], **(
        {'points': 6, 'number': 1} if changing_dates else {'skip': True}))
    if mutation == 'restore':
        neighbour = h.command('coordinator', 'workspace.archive', {'reason': 'Discard first half'},
                              neighbour['id'], neighbour['revision'])['workspace']
    elif mutation == 'undo_dates':
        neighbour = h.command('coordinator', 'workspace.resolve_overlap', {
            'choice': 'start_after', 'reason': 'Start after the issued week'},
            neighbour['id'], neighbour['revision'])['workspace']
    draft = h.shift(h.workspace('Neighbouring second half', '2026-10-15', '2026-11-01'),
                    start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00', people=[])['workspace']
    before = h.snapshot(draft)
    released = mutation in ('restore', 'undo_dates')
    assert [w['first_day'] for w in before['wishes']] == (
        ['2026-10-19'] if released else ['2026-10-19', '2026-10-26'])
    job = h.command('coordinator', 'job.create', {'workspace_id': draft['id'], 'kind': 'generation'},
                    revision=draft['revision'])['job']
    process_one(h.app.state.engine)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded' and completed['result']['additions']
    with transaction(h.app.state.engine) as db:
        frozen_job = record(db.get(m.Job, job['id']))
        assert frozen_job['input_snapshot']['wishes'] == before['wishes']

    if changing_dates:
        kind = 'workspace.undo' if mutation == 'undo_dates' else 'workspace.resolve_overlap'
        payload = {'reason': 'Change the neighbouring override dates'}
        if mutation == 'resolve_overlap':
            payload['choice'] = 'start_after'
    else:
        kind = 'workspace.' + mutation
        payload = {'reason': 'Change whether the neighbouring override is live'}
    h.command('coordinator', kind, payload, neighbour['id'], neighbour['revision'])
    current = h.snapshot(draft)
    assert [w['first_day'] for w in current['wishes']] == (
        ['2026-10-19', '2026-10-26'] if released else ['2026-10-19'])
    assert current['fingerprint'] != before['fingerprint']
    assert current['organisation']['input_revision'] == before['organisation']['input_revision'] + 1
    assert current['organisation']['availability_revision'] == before['organisation']['availability_revision']
    detail = h.detail(draft)
    with transaction(h.app.state.engine) as db:
        changes = [record(row) for row in db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == draft['id']))]

    payload = {'workspace_id': draft['id'], 'job_id': job['id'], 'reason': 'Reject stale neighbouring funding'}
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        with pytest.raises(Problem) as rejected:
            jobs.apply_generation(db, actor, s.Command(kind='generation.apply', payload=payload,
                                                       expected_revision=draft['revision']))
        assert rejected.value.code == 'RECONCILIATION_REQUIRED' and rejected.value.status == 409
    applied = h.command('coordinator', 'generation.apply', payload, revision=draft['revision'])
    assert applied['reconciliation']['status'] == 'applied'
    assert applied['reconciliation']['changed_facts']
    with transaction(h.app.state.engine) as db:
        stored = record(db.get(m.Job, job['id']))
        assert stored['input_snapshot'] == frozen_job['input_snapshot']
        assert all(stored['result'][key] == value for key, value in frozen_job['result'].items())
        assert len(list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == draft['id'])))) == len(changes) + 1


def test_cp_sat_and_score_use_the_same_expanded_standing_wishes(h):
    a, b = h.seed['people'][:2]
    workspace = h.workspace()
    for day in (4, 5):
        workspace = h.shift(workspace, start=f'2026-10-0{day}T08:00:00+03:00',
                            end=f'2026-10-0{day}T12:00:00+03:00', people=[])['workspace']
    standing(h, weekdays=[6], number=1, person=a, points=3, role='coordinator')
    standing(h, weekdays=[0], number=1, person=b, kind='prefer_on', role='coordinator')
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (a, b)}
    # Isolate expanded wish preferences in this compiler/scorer parity fixture.
    data['generation_options'] = {'scoring': {'weights': {
        'coverage': 0, 'rules': 0, 'fairness': 0, 'preferences': 1, 'stability': 0}}}
    assert {w['kind']: w['weight'] for w in data['wishes']} == {'prefer_off': .3, 'prefer_on': .1}
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible'
    assert {x['participant']['start'][:10]: x['participant']['person_id'] for x in result['additions']} == {
        '2026-10-04': b, '2026-10-05': b}
    assert result['summary']['wishes'] == {'met': 2, 'total': 2}
    placed = copy.deepcopy(data)
    shifts = {shift['id']: shift for shift in placed['shifts']}
    for addition in result['additions']:
        shifts[addition['shift_id']]['participants'].append(addition['participant'])
    assert set(wish_outcomes(placed).values()) == {(1, 1)}
    bad = copy.deepcopy(placed)
    for shift in bad['shifts']:
        for participant in shift['participants']:
            participant['person_id'] = a
    before = schedule_score(bad, evaluate(bad))
    after = schedule_score(placed, evaluate(placed))
    assert after['components']['preferences']['lost'] == 0 < before['components']['preferences']['lost']
    assert result['compiled_component_values']['preferences'] == after['components']['preferences']['value'] == 1


@pytest.mark.parametrize('mutation', ['update', 'withdraw'])
def test_published_standing_occurrences_keep_evidence_notifications_and_privacy(h, mutation):
    a, b = h.seed['people'][:2]
    saved = standing(h, weekdays=[6, 1], points=2)['wish']  # Sun 4 declined, Tue 6 granted.
    other = standing(h, person=b, role='coordinator')['wish']
    workspace = h.shift(h.workspace(), people=[a])['workspace']
    publication_id = h.publish(workspace)['publication_id']
    issued = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    mine = [wish for wish in issued['wishes'] if wish['person_id'] == a]
    assert [(wish['first_day'], wish['points'], wish['outcome']) for wish in mine] == [
        ('2026-10-04', 2, 'declined'), ('2026-10-06', 2, 'granted')]
    assert all(wish['standing_wish_id'] == saved['id'] for wish in mine)
    assert any(wish['standing_wish_id'] == other['id'] for wish in issued['wishes'])
    projected = h.get('staff', '/api/publications/' + publication_id)['snapshot']
    [reason] = mine[0]['outcome_reasons']
    assert reason['kind'] == 'cover'
    staff_reason = {key: reason[key] for key in ('day', 'kind', 'shift_id', 'session_label', 'start', 'end')}
    staff_reason['message'] = 'Your duty was needed to cover this session.'
    assert projected['wishes'] == [{**mine[0], 'outcome_reasons': [staff_reason]},
                                   {**mine[1], 'outcome_reasons': []}]
    assert 'wish_worked' not in projected
    assert all(wish['person_id'] == a for wish in projected['wishes'])
    with transaction(h.app.state.engine) as db:
        parent = record(db.get(m.StaffWish, saved['id']))
        ledger = [record(row) for row in db.scalars(select(m.WishLedger).where(
            m.WishLedger.publication_id == publication_id).order_by(m.WishLedger.person_id, m.WishLedger.metric))]
    assert parent['status'] == 'open' and parent['outcome'] is None and parent['publication_id'] is None
    assert parent['outcome_reasons'] is None
    assert {row['metric']: row['value'] for row in ledger if row['person_id'] == a} == {
        'wishes_submitted': 4, 'wishes_granted': 2, 'wishes_declined': 2}
    process_one(h.app.state.engine)
    notifications = h.get('staff', '/api/notifications')['notifications']
    assert len(notifications) == 1
    assert notifications[0]['message'].endswith('Your preferences: 1 granted, 1 not granted. Open Requests for each one.')
    drill_path = f'/api/fairness/sessions?person_id={a}&metric=wishes_declined&starts_on=2026-10-01&ends_on=2026-11-01'
    drill = h.get('staff', drill_path)
    published = drill['publications'][0]
    assert published['publication_id'] == publication_id and published['value'] == 2 and published['sessions'] == []
    assert published['wishes'] == [{key: wish[key] for key in ('id', 'kind', 'first_day', 'last_day', 'points', 'outcome', 'limit', 'weekend_start', 'weekend_days', 'outcome_reasons')}
                                    for wish in projected['wishes']]
    if mutation == 'update':
        h.command('staff', 'wish.update', {'person_id': a, 'kind': 'prefer_on', 'points': 1,
            'repeat': {'weekdays': [0], 'starts_on': '2026-10-01'}}, saved['id'], parent['revision'])
    else:
        h.command('staff', 'wish.withdraw', {'reason': 'Change future defaults'}, saved['id'], parent['revision'])
    assert h.get('coordinator', '/api/publications/' + publication_id)['snapshot'] == issued
    assert h.get('staff', '/api/publications/' + publication_id)['snapshot'] == projected
    assert h.get('staff', drill_path) == drill
    assert h.snapshot(workspace)['wishes'] == [{key: value for key, value in wish.items() if key != 'outcome_reasons'}
                                              for wish in issued['wishes']]
    with transaction(h.app.state.engine) as db:
        assert [record(row) for row in db.scalars(select(m.WishLedger).where(
            m.WishLedger.publication_id == publication_id).order_by(m.WishLedger.person_id, m.WishLedger.metric))] == ledger


def test_replacement_trims_prior_published_occurrences_without_rewriting_snapshot(h):
    person = h.seed['people'][0]
    standing(h)
    first = h.workspace('Original month', '2026-10-01', '2026-11-01')
    first = h.shift(first, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    first_id = h.publish(first)['publication_id']
    with transaction(h.app.state.engine) as db:
        original = copy.deepcopy(db.get(m.Publication, first_id).snapshot)
    assert [wish['first_day'] for wish in original['wishes']] == ['2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26']
    later = h.command('coordinator', 'workspace.create', {'name': 'Replacement second half',
        'starts_on': '2026-10-15', 'ends_on': '2026-11-01', 'replaces_publication_id': first_id})['workspace']
    later = h.shift(later, start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00', people=[person])['workspace']
    h.publish(later)
    for role in ('staff', 'coordinator'):
        prior = h.get(role, '/api/publications/' + first_id)
        assert [wish['first_day'] for wish in prior['snapshot']['wishes']] == ['2026-10-05', '2026-10-12']
    drill = h.get('staff', f'/api/fairness/sessions?person_id={person}&metric=wishes_submitted'
                          '&starts_on=2026-10-01&ends_on=2026-11-01')
    prior_drill = next(publication for publication in drill['publications'] if publication['publication_id'] == first_id)
    assert prior_drill['value'] == 2
    assert [wish['first_day'] for wish in prior_drill['wishes']] == ['2026-10-05', '2026-10-12']
    with transaction(h.app.state.engine) as db:
        prior_publication = db.get(m.Publication, first_id)
        assert prior_publication.effective_until == date(2026, 10, 15)
        assert prior_publication.snapshot == original


def test_published_standing_charges_stay_reserved_after_defaults_change_and_withdraw(h):
    person = h.seed['people'][0]
    saved = standing(h, points=3)['wish']
    first = h.workspace('Issued first half', '2026-10-01', '2026-10-15')
    later = h.workspace('Open second half', '2026-10-15', '2026-11-01')
    first = h.shift(first, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    publication_id = h.publish(first)['publication_id']
    issued = h.get('staff', '/api/publications/' + publication_id)['snapshot']
    issued_raw = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']['wishes']
    assert [(wish['first_day'], wish['points']) for wish in issued['wishes']] == [('2026-10-05', 3), ('2026-10-12', 3)]
    changed = h.command('staff', 'wish.update', {'person_id': person, 'kind': 'prefer_off', 'points': 3,
        'repeat': {'weekdays': [2], 'starts_on': '2026-10-01'}}, saved['id'], saved['revision'])['wish']
    endpoint = '/api/wishes?start=2026-10-15&end=2026-11-01&workspace_id=' + later['id']
    assert [(wish['first_day'], wish['points']) for wish in expanded(h, later)] == [('2026-10-21', 3)]
    assert h.get('staff', endpoint)['usage'] == [{'start': '2026-10-01', 'end': '2026-11-01', 'used': 9,
        'standing_points': 9, 'one_off_points': 0, 'unfunded_standing_points': 3, 'unfunded_one_off_points': 0}]
    assert expanded(h, first) == issued_raw  # No new Wednesday is inserted into the issued half.
    h.command('staff', 'wish.withdraw', {'reason': 'Remove future defaults'}, changed['id'], changed['revision'])
    assert expanded(h, later) == []
    assert h.get('staff', endpoint)['usage'] == [{'start': '2026-10-01', 'end': '2026-11-01', 'used': 6,
        'standing_points': 6, 'one_off_points': 0, 'unfunded_standing_points': 0, 'unfunded_one_off_points': 0}]
    assert h.get('staff', '/api/publications/' + publication_id)['snapshot'] == issued


def test_legacy_publication_replacement_keeps_one_offs_without_adding_backdated_defaults(h, monkeypatch):
    from apps.api.services import planning

    person = h.seed['people'][0]
    original_wish = h.command('staff', 'wish.create', {'person_id': person, 'kind': 'prefer_off',
        'first_day': '2026-10-05', 'last_day': '2026-10-05', 'points': 2})['wish']
    first = h.workspace('Legacy month', '2026-10-01', '2026-11-01')
    first = h.shift(first, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    current_snapshot = planning.publication_snapshot

    def legacy_snapshot(*args, **kwargs):
        snapshot = current_snapshot(*args, **kwargs)
        snapshot.pop('wishes', None)
        return snapshot

    with monkeypatch.context() as old_image:
        old_image.setattr(planning, 'publication_snapshot', legacy_snapshot)
        publication_id = h.publish(first)['publication_id']
    with transaction(h.app.state.engine) as db:
        original_snapshot = copy.deepcopy(db.get(m.Publication, publication_id).snapshot)
        wish = db.get(m.StaffWish, original_wish['id'])
        assert wish.publication_id == publication_id and wish.outcome == 'declined'
        original_ledger = {row.metric: row.value for row in db.scalars(select(m.WishLedger).where(
            m.WishLedger.publication_id == publication_id, m.WishLedger.person_id == person))}
    assert 'wishes' not in original_snapshot
    assert original_ledger == {'wishes_submitted': 2, 'wishes_granted': 0, 'wishes_declined': 2}
    standing(h)  # A backdated default must not become historical one-off evidence.
    later = h.command('coordinator', 'workspace.create', {'name': 'Modern second half',
        'starts_on': '2026-10-15', 'ends_on': '2026-11-01', 'replaces_publication_id': publication_id})['workspace']
    later = h.shift(later, start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00', people=[person])['workspace']
    h.publish(later)
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, publication_id)
        assert publication.effective_until == date(2026, 10, 15)
        assert publication.snapshot == original_snapshot and 'wishes' not in publication.snapshot
        assert {row.metric: row.value for row in db.scalars(select(m.WishLedger).where(
            m.WishLedger.publication_id == publication_id, m.WishLedger.person_id == person))} == original_ledger
        wish = db.get(m.StaffWish, original_wish['id'])
        assert wish.publication_id == publication_id and wish.outcome == 'declined'


def test_amendment_releases_base_standing_charges_and_publishes_exact_checked_wishes(h):
    person = h.seed['people'][0]
    saved = standing(h)['wish']
    first = h.workspace('Original standing month', '2026-10-01', '2026-11-01')
    first = h.shift(first, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    first_id = h.publish(first)['publication_id']
    with transaction(h.app.state.engine) as db:
        original = copy.deepcopy(db.get(m.Publication, first_id).snapshot)
    assert len(original['wishes']) == 4
    h.command('staff', 'wish.withdraw', {'reason': 'Remove future defaults'}, saved['id'], saved['revision'])
    amendment = h.command('coordinator', 'workspace.create', {'name': 'Amend withdrawn defaults',
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'base_publication_id': first_id})['workspace']
    checked_wishes = h.snapshot(amendment)['wishes']
    assert checked_wishes == []
    amendment_id = h.publish(amendment)['publication_id']
    assert h.get('coordinator', '/api/publications/' + amendment_id)['snapshot']['wishes'] == checked_wishes
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, amendment_id).snapshot['wishes'] == checked_wishes
        assert db.get(m.Publication, first_id).snapshot == original


def test_amendment_skip_override_preserves_published_standing_occurrences(h):
    saved = standing(h)['wish']
    first = h.shift(h.workspace())['workspace']
    publication_id = h.publish(first)['publication_id']
    issued = h.get('staff', '/api/publications/' + publication_id)['snapshot']
    assert [wish['first_day'] for wish in issued['wishes']] == ['2026-10-05']
    amendment = h.workspace(base=publication_id)
    assert [wish['first_day'] for wish in h.snapshot(amendment)['wishes']] == ['2026-10-05']

    changed = override(h, saved, workspace_id=amendment['id'], skip=True)

    assert changed['override']['skip'] is True
    assert h.snapshot(amendment)['wishes'] == []
    assert h.snapshot(first)['wishes'] == [{key: value for key, value in wish.items() if key != 'outcome_reasons'}
                                         for wish in issued['wishes']]
    assert h.get('staff', '/api/publications/' + publication_id)['snapshot'] == issued


def test_planned_replacement_applies_new_override_and_preserves_published_skip(h):
    person = h.seed['people'][0]
    saved = standing(h)['wish']
    first = h.workspace('Skipped original month', '2026-10-01', '2026-11-01')
    skipped = override(h, saved, workspace_id=first['id'], skip=True)
    first = h.shift(first, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    first_id = h.publish(first)['publication_id']
    later = h.command('coordinator', 'workspace.create', {'name': 'Repriced replacement',
        'starts_on': '2026-10-15', 'ends_on': '2026-11-01', 'replaces_publication_id': first_id})['workspace']
    changed = override(h, skipped['wish'], workspace_id=later['id'], points=2)
    assert changed['override']['points'] == 2
    later = h.shift(later, start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00', people=[person])['workspace']
    assert [(wish['first_day'], wish['points']) for wish in h.snapshot(later)['wishes']] == [
        ('2026-10-19', 2), ('2026-10-26', 2)]
    later_id = h.publish(later)['publication_id']
    assert [(wish['first_day'], wish['points']) for wish in h.get('staff', '/api/publications/' + later_id)['snapshot']['wishes']] == [
        ('2026-10-19', 2), ('2026-10-26', 2)]
    usage = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01&workspace_id=' + later['id'])['usage'][0]
    assert usage['standing_points'] == usage['used'] == 4
    assert usage['unfunded_standing_points'] == 0
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, first_id).effective_until == date(2026, 10, 15)
        old_override = db.get(m.WishOverride, skipped['override']['id'])
        assert old_override.workspace_id == first['id'] and old_override.skip is True


def test_overlapping_draft_overrides_require_reset_before_moving_modifier(h):
    first = h.workspace('Overlapping first draft', '2026-10-01', '2026-10-20')
    second = h.workspace('Overlapping second draft', '2026-10-15', '2026-11-01')
    saved = standing(h)['wish']
    changed = override(h, saved, workspace_id=first['id'], points=2)['wish']
    rejected = override(h, changed, workspace_id=second['id'], points=3, status=409)
    assert rejected['error']['code'] == 'WISH_OVERRIDE_OVERLAP'
    reset = override(h, changed, workspace_id=first['id'])['wish']
    moved = override(h, reset, workspace_id=second['id'], points=3)
    assert moved['override']['points'] == 3 and moved['override']['workspace_id'] == second['id']


@pytest.mark.parametrize('mutation', ['restore', 'undo_dates'])
def test_restoring_override_dates_refuses_overlap_without_changing_data(h, mutation):
    if mutation == 'undo_dates':
        h.publish(h.shift(h.workspace('Issued first week', '2026-10-01', '2026-10-08'))['workspace'])
    saved = standing(h)['wish']
    first = h.workspace('First modifier', '2026-10-01', '2026-10-20')
    saved = override(h, saved, workspace_id=first['id'], skip=True)['wish']
    if mutation == 'restore':
        first = h.command('coordinator', 'workspace.archive', {'reason': 'Keep discarded modifier'},
                          first['id'], first['revision'])['workspace']
    else:
        first = h.command('coordinator', 'workspace.resolve_overlap', {
            'choice': 'start_after', 'reason': 'Start after the issued week'},
            first['id'], first['revision'])['workspace']
        assert first['starts_on'] == '2026-10-08'
    second = h.workspace('Conflicting modifier', '2026-10-01', '2026-10-08')
    override(h, saved, workspace_id=second['id'], points=6)
    snapshots = [h.snapshot(workspace) for workspace in (first, second)]
    tables = (m.Workspace, m.StaffWish, m.WishOverride, m.ChangeSet, m.Organisation, m.Shift, m.Assignment)
    with transaction(h.app.state.engine) as db:
        before = {table: [record(row) for row in db.scalars(select(table).order_by(table.id))] for table in tables}

    rejected = h.command('coordinator', 'workspace.restore' if mutation == 'restore' else 'workspace.undo',
        {'reason': 'Restore the earlier modifier dates'}, first['id'], first['revision'], status=409)

    assert rejected['error']['code'] == 'WISH_OVERRIDE_OVERLAP'
    assert rejected['error']['message'] == (
        'Another open rota already changes this preference on these days; change or remove that one first')
    assert [h.snapshot(workspace) for workspace in (first, second)] == snapshots
    with transaction(h.app.state.engine) as db:
        assert {table: [record(row) for row in db.scalars(select(table).order_by(table.id))] for table in tables} == before


def test_standing_dates_on_leave_are_not_charged(h):
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'start': '2026-10-12T00:00:00+03:00', 'end': '2026-10-13T00:00:00+03:00',
        'private_reason': 'Synthetic annual leave'})
    standing(h, points=3)
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    assert [(wish['first_day'], wish['points']) for wish in expanded(h, workspace)] == [
        ('2026-10-05', 3), ('2026-10-19', 3), ('2026-10-26', 3)]
    usage = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage'][0]
    assert usage['standing_points'] == usage['used'] == 9 and usage['unfunded_standing_points'] == 0


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_recurring_creates_read_current_standing_wishes(database, request, monkeypatch):
    h = request.getfixturevalue(database)
    from apps.api.services import planning
    saved = standing(h)['wish']
    series = h.configure('recurring_series', {'name': 'Synthetic preference template',
        'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0], 'starts_on': '2026-10-01',
        'definition': {'anchor': '2026-10-01', 'service_ids': [h.seed['services'][0]],
                       'days': [{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}],
                       'default_person_ids': [h.seed['people'][0]]}})['record']
    checked = []
    real_evaluate = planning.evaluate
    def capture(data, **options):
        checked.append(copy.deepcopy(data))
        return real_evaluate(data, **options)
    monkeypatch.setattr(planning, 'evaluate', capture)
    for points in (1, 2):
        if points == 2:
            saved = h.command('staff', 'wish.update', {'person_id': saved['person_id'], 'kind': saved['kind'],
                'points': points, 'repeat': saved['repeat']}, saved['id'], saved['revision'])['wish']
        checked.clear()
        created = h.command('coordinator', 'workspace.create', {'name': f'Synthetic preferences {points}',
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'series_ids': [series['id']]})
        assert created['setup']['created'] == 4 and created['validation']['valid']
        assert len(checked) == 1 and len(checked[0]['focus_ids']) == 4
        assert all(len(shift['participants']) == 1 for shift in checked[0]['shifts'])
        wishes = checked[0]['wishes']
        assert len(wishes) == 4 and {wish['standing_wish_id'] for wish in wishes} == {saved['id']}
        assert {wish['points'] for wish in wishes} == {points}


@pytest.mark.parametrize('standing_count', [0, 3])
def test_planner_reuses_manifest_wishes_and_batches_standing_reads(h, standing_count):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    one_off = h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_on',
        'first_day': '2026-10-06', 'last_day': '2026-10-06'})['wish']
    for person in h.seed['people'][:standing_count]:
        saved = standing(h, person=person, role='coordinator')['wish']
        override(h, saved, role='coordinator', period_start='2026-10-01', points=2)
    if standing_count:
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
            'start': '2026-10-12T00:00:00+03:00', 'end': '2026-10-13T00:00:00+03:00',
            'private_reason': 'Synthetic annual leave'})
    h.client('coordinator')
    reads = Counter()

    def collect(_connection, _cursor, statement, _parameters, _context, _many):
        for table in ('staff_wishes', 'wish_overrides', 'availability'):
            if 'FROM ' + table in statement:
                reads[table] += 1

    event.listen(h.app.state.engine, 'before_cursor_execute', collect)
    try:
        detail = h.detail(workspace)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', collect)
    assert reads['staff_wishes'] == 1
    assert reads['wish_overrides'] == bool(standing_count)
    assert reads['availability'] <= 3  # Canonical currency, standing expansion and overlap, independent of people count.
    assert next(w for w in detail['wishes'] if w['id'] == one_off['id'])['draft_outcome']['outcome'] == 'declined'
    occurrences = [w for w in detail['wishes'] if w.get('standing_wish_id')]
    assert len(occurrences) == standing_count * 4 - bool(standing_count)
    assert all(w['points'] == 2 and w['draft_outcome']['outcome'] == 'granted' for w in occurrences)
    assert not any(w['person_id'] == h.seed['people'][0] and w['first_day'] == '2026-10-12' for w in occurrences)


@pytest.fixture(scope='module')
def standing_quarter_expansion(scale_harness):
    h = scale_harness('h')
    configure(h, wish_period='quarter', wish_budget_points=100)
    workspace = h.workspace(start='2026-10-01', end='2027-01-01')
    engine = h.app.state.engine
    measurements, outcomes = [], []
    for count in (75, 150):
        with transaction(engine, write=True) as db:
            people = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic standing {i}')
                      for i in range(count - (75 if measurements else 0))]
            db.add_all(people)
            db.flush()
            db.add_all([m.StaffWish(department_id=h.seed['department_id'], person_id=person.id,
                first_day=date(2026, 10, 1), last_day=date(2026, 10, 1), kind='prefer_off', points=1,
                created_by=h.seed['identities']['coordinator'],
                repeat={'weekdays': list(range(7)), 'starts_on': '2026-10-01', 'ends_on': None, 'number': None})
                for person in people])
        statements = []

        def collect(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        # Exclude earlier fixture garbage, as in the backend scale measurements; GC stays enabled during the call.
        gc.collect()
        event.listen(engine, 'before_cursor_execute', collect)
        try:
            started = process_time()
            wishes = expanded(h, workspace)
            elapsed = process_time() - started
        finally:
            event.remove(engine, 'before_cursor_execute', collect)
        outcomes.append((count, wishes))
        measurements.append({'people': count, 'queries': len(statements), 'cpu_seconds': elapsed})
    print('standing quarter expansion', measurements)
    return measurements, outcomes


def test_quarter_expansion_for_150_people_has_bounded_queries_and_linear_growth(standing_quarter_expansion):
    measurements, outcomes = standing_quarter_expansion
    for count, wishes in outcomes:
        assert len(wishes) == count * 92
        counts = Counter(w['person_id'] for w in wishes)
        assert len(counts) == count and set(counts.values()) == {92}
    assert measurements[1]['queries'] <= measurements[0]['queries'] + 1
    assert measurements[1]['queries'] < 15  # Batch reads, independent of the number of people.


@pytest.mark.cpu_budget
def test_standing_quarter_expansion_cpu_budget(standing_quarter_expansion):
    measurements = standing_quarter_expansion[0]
    assert measurements[1]['cpu_seconds'] <= 3 * measurements[0]['cpu_seconds'] + .1


def test_usage_rejects_excessive_ranges_before_expanding_open_ended_standing_wishes(h, monkeypatch):
    from datetime import timedelta
    from apps.api.services import wishes
    saved = standing(h)['wish']
    first = date(2026, 10, 1)
    last = first + timedelta(days=s.WISH_RANGE_MAX_DAYS)
    result = h.get('staff', f'/api/wishes?start={first}&end={last}')
    assert result['wishes'][0]['id'] == saved['id']
    def unexpected_expansion(*args, **kwargs):
        raise AssertionError('Oversized previews must fail before expanding standing wishes')
    monkeypatch.setattr(wishes, 'snapshot_wishes', unexpected_expansion)
    for end in (last + timedelta(days=1), date(9998, 12, 31)):
        result = h.get('staff', f'/api/wishes?start={first}&end={end}', status=422)
        assert result['error']['code'] == 'WISH_RANGE_LIMIT'
