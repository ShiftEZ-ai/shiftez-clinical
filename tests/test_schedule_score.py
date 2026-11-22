"""The schedule score (packages/domain/score.py): weights, the 59 cap, exact "why" points, per-person scores."""
import pytest
from pydantic import ValidationError

from packages.contracts.schemas import MAX_SCORE_INPUT, MAX_WISH_POINTS, ScoringSettings
from packages.domain.score import COMPONENTS, apportion, schedule_score, settings_of


def shift(sid, day, people, typ='t'):
    return {'id': sid, 'cancelled': False, 'assignment_type_id': typ, 'service_ids': [], 'location_id': 'l',
            'start': f'2026-10-{day:02}T08:00:00+00:00', 'end': f'2026-10-{day:02}T16:00:00+00:00',
            'type_snapshot': {'name': 'Clinic', 'definition': {'requirements': [{'minimum': 1}]}},
            'participants': [{'person_id': p, 'start': f'2026-10-{day:02}T08:00:00+00:00',
                              'end': f'2026-10-{day:02}T16:00:00+00:00', 'overtime': False} for p in people]}


def policy(pid, severity='advisory', strength='medium', name='Rest'):
    return {'id': pid, 'name': name, 'status': 'active', 'department_id': 'd', 'starts_on': '2020-01-01', 'ends_on': None,
            'definition': {'severity': severity, 'strength': strength, 'scope': {}, 'rule': {'kind': 'max_duties', 'maximum': 1, 'window': 'day'}}}


def data(shifts, policies=()):
    from apps.api.services.scoring_defaults import workload_line
    workload = {'id': 'workload', 'name': 'Fair share', 'kind': 'catalogue', 'status': 'active',
                'active': True, 'department_id': 'd', 'starts_on': '2020-01-01', 'ends_on': None,
                'definition': {'rule': {'kind': 'catalogue', 'id': 'fair_share', 'catalogue_version': 2,
                                        'lines': [workload_line('d')]}}}
    return {'organisation': {'timezone': 'UTC'}, 'focus_ids': [s['id'] for s in shifts], 'shifts': shifts,
            'workspace': {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'people': {p: {'display_name': p.upper(), 'active': True} for p in 'abcd'}, 'policies': [workload, *policies],
            'types': {'t': {'department_id': 'd'}},
            'memberships': [{'person_id': p, 'department_id': 'd', 'category_id': 'rank', 'starts_on': '2020-01-01'} for p in 'abcd'],
            'categories': {'rank': {}}}


def coverage(sid, day, required, available):
    return {'shift_id': sid, 'start': f'2026-10-{day:02}T08:00:00+00:00', 'end': f'2026-10-{day:02}T16:00:00+00:00',
            'required': required, 'available_eligible': available, 'status': 'gap' if available < required else 'covered'}


def warning(rule, person, code='MIN_REST', severity='warning'):
    return {'code': code, 'severity': severity, 'rule_id': rule, 'person_id': person, 'shift_id': None,
            'message': 'Rest', 'start': None, 'end': None, 'detail': None}


def total_why(result):
    return round(sum(w['points'] for w in result['why']), 1)


def test_perfect_rota_scores_100_and_absent_components_drop_out():
    d = data([shift('s1', 1, ['a']), shift('s2', 2, ['b'])])
    d['people'] = {pid: d['people'][pid] for pid in ('a', 'b')}
    result = schedule_score(d, {'coverage': [coverage('s1', 1, 1, 1), coverage('s2', 2, 1, 1)], 'findings': []})
    assert result['score'] == 100 and result['publishable'] and result['why'] == []
    assert result['uncapped_score'] == 100 and result['blocking_count'] == 0 and result['capped'] is False
    assert result['components']['stability']['value'] is None  # no baseline: weight redistributed


def test_lost_points_add_up_exactly_and_follow_the_weights():
    d = data([shift('s1', 1, ['a']), shift('s2', 2, ['a', 'b']), shift('s3', 3, ['c'])], [policy('rest')])
    validation = {'coverage': [coverage('s1', 1, 2, 1), coverage('s2', 2, 2, 2), coverage('s3', 3, 2, 1)],
                  'findings': [warning('rest', 'a'), warning('rest', 'a')]}
    result = schedule_score(d, validation)
    assert result['score'] < 100
    assert total_why(result) == round(100 - result['score'], 1)
    lost = {c: v['lost'] for c, v in result['components'].items()}
    assert round(sum(lost.values()), 1) == round(100 - result['score'], 1)
    # Coverage: 2 of 6 seat-days open -> value 2/3; with only coverage weighted the score is 66.7.
    only = schedule_score(d, validation, {'scoring': {'weights': {'coverage': 1, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 0}}})
    assert only['score'] == 66.7 and total_why(only) == 33.3
    assert any('(2×)' in w['text'] for w in result['why'])


def test_a_mandatory_breach_caps_at_59_and_blocks_publishing():
    d = data([shift('s1', 1, ['a'])])
    d['people'] = {'a': d['people']['a']}
    result = schedule_score(d, {'coverage': [coverage('s1', 1, 1, 1)], 'findings': [warning(None, 'a', 'TIME_OVERLAP', 'error')]})
    assert result['score'] == 59 and not result['publishable'] and result['gate']['code'] == 'TIME_OVERLAP'
    assert result['capped'] is True and result['capped_points'] == 41 and total_why(result) == 41
    assert result['uncapped_score'] == result['raw'] == 100 and result['blocking_count'] == 1
    # Any error caps it (an overlap is not a rule): the line says what it is, not "a mandatory rule is broken".
    gate = next(w for w in result['why'] if w['component'] == 'gate')
    assert gate['text'].startswith('Capped at 59: not publishable until the rota check errors are fixed')


def test_strength_weights_rules_and_per_person_scores():
    d = data([shift('s1', 1, ['a']), shift('s2', 2, ['b'])], [policy('soft', strength='low', name='Soft'),
                                                              policy('hard', strength='critical', name='Hard')])
    weights = {'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}}
    low = schedule_score(d, {'coverage': [], 'findings': [warning('soft', 'a')]}, weights)
    high = schedule_score(d, {'coverage': [], 'findings': [warning('hard', 'a')]}, weights)
    # Eight fixed opportunities (2 rules x 4 people), including unassigned c/d; the critical breach costs 30x more.
    assert low['score'] == round(100 * (1 - 1 / 124), 1) and high['score'] == round(100 * (1 - 30 / 124), 1)
    assert high['people']['a']['rules'] == round(1 - 30 / 31, 3) and high['people']['b']['score'] == 100
    assert high['people']['a']['issues'][0]['text'].startswith('A · Hard')


def test_fairness_and_stability_components():
    d = data([shift('s1', 1, ['a']), shift('s2', 2, ['a']), shift('s3', 3, ['a']), shift('s4', 4, ['b'])])
    d['people'] = {pid: d['people'][pid] for pid in ('a', 'b')}
    options = {'balance_measure': 'duties', 'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 1, 'preferences': 0, 'stability': 1}}}
    baseline = {('s1', 'a'), ('s2', 'a'), ('s3', 'b'), ('s4', 'b')}
    result = schedule_score(d, {'coverage': [], 'findings': []}, options, baseline=baseline)
    # Four required duties / two people: a is one above their fixed aim of 2; overload share 1/4, worst 1/2.
    assert result['components']['fairness']['value'] == 0.5625
    assert result['components']['stability']['value'] == 0.75  # 1 of 4 assignments changed (b's s3 went to a)
    assert total_why(result) == round(100 - result['score'], 1)
    assert result['people']['a']['fairness'] == 0.5


def test_filling_open_places_is_not_a_change():
    """Only removing, moving or swapping an existing assignment, or changing its lead/overtime flag, counts."""
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 1}}}
    empty = {'coverage': [], 'findings': []}
    d = data([shift('s1', 1, ['a', 'b']), shift('s2', 2, ['c'])])
    # An empty rota filled: full stability points.
    filled = schedule_score(d, empty, options, baseline={})
    assert filled['components']['stability']['value'] == 1 and filled['score'] == 100
    # Existing assignments kept and an open place filled beside them: still no change.
    kept = schedule_score(d, empty, options, baseline={('s1', 'a'): (False, False)})
    assert kept['components']['stability']['value'] == 1
    # A move (c from s1 to s2) and a swap (b and a trade places) each count once per assignment given up.
    moved = schedule_score(d, empty, options, baseline={('s1', 'c'): (False, False), ('s1', 'a'): (False, False)})
    assert moved['components']['stability']['value'] == 0.5
    swapped = schedule_score(data([shift('s1', 1, ['b']), shift('s2', 2, ['a'])]), empty, options,
                             baseline={('s1', 'a'): (False, False), ('s2', 'b'): (False, False)})
    assert swapped['components']['stability']['value'] == 0
    assert total_why(swapped) == round(100 - swapped['score'], 1)
    # A kept assignment whose lead or overtime flag changed is a change.
    d['shifts'][0]['participants'][0]['team_lead'] = True
    d['shifts'][1]['participants'][0]['overtime'] = True
    flagged = schedule_score(d, empty, options, baseline={('s1', 'a'): (False, False), ('s1', 'b'): (False, False),
                                                          ('s2', 'c'): (False, False)})
    assert flagged['components']['stability']['value'] == round(1 - 2 / 3, 4)
    assert flagged['people']['b']['stability'] == 1 and flagged['people']['a']['stability'] == 0


def test_placement_effects_explain_fair_share_and_wishes(h):
    from packages.domain.score import placement_effects
    from packages.domain.validation import evaluate
    a, b, c = h.seed['people'][:3]
    w = h.workspace(start='2026-10-01', end='2026-10-08')
    for day, people in ((1, [a]), (2, [a]), (3, [a]), (4, [b]), (5, [])):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T16:00:00+03:00', people=people)['workspace']
    d = h.snapshot(w)
    sid = next(s['id'] for s in d['shifts'] if not s['participants'])
    d['wishes'] = [{'id': 'w', 'person_id': c, 'kind': 'prefer_off', 'first_day': '2026-10-05', 'last_day': '2026-10-05', 'weight': 1}]
    options = {'balance_measure': 'duties'}
    effects = placement_effects(d, evaluate(d), sid, [a, b, c], options)
    # Five required duties / three people fixes the aim at two. Filling b/c cannot erase a's overload.
    assert effects[a]['fairness'] < 0 and effects[b]['fairness'] == effects[c]['fairness'] == 0
    assert effects[c]['preferences'] < 0 and effects[c]['wishes'] == [{'text': 'off Mon 5 Oct', 'granted': False}]
    assert effects[a]['wishes'] == []
    for effect in effects.values():
        assert effect['points'] == round(sum(effect[k] for k in (*COMPONENTS, 'gate', 'other')), 1)
    # Someone already on a session: what the score gains or loses by having them there (a's third duty costs).
    sid = next(s['id'] for s in d['shifts'] if s['start'].startswith('2026-10-01'))
    assert placement_effects(d, evaluate(d), sid, [a], options)[a]['fairness'] < 0


@pytest.mark.parametrize('limit', [0, 3])
@pytest.mark.parametrize('weights', [None, {'coverage': 1, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 0}])
def test_score_effect_explains_first_and_last_optional_assignment(limit, weights):
    from copy import deepcopy
    from packages.domain.score import score_effect

    empty = data([shift('optional', 1, [])])
    empty['shifts'][0]['type_snapshot']['definition']['requirements'][0]['minimum'] = 0
    filled = deepcopy(empty)
    filled['shifts'][0]['participants'] = shift('optional', 1, ['a'])['participants']
    options = {'scoring': {'weights': weights}} if weights else None
    without_score = schedule_score(empty, {'coverage': [coverage('optional', 1, 0, 0)], 'findings': []},
                                   options, limit=limit)
    with_score = schedule_score(filled, {'coverage': [coverage('optional', 1, 0, 1)], 'findings': []},
                                options, limit=limit)
    assert without_score['score'] == 0 and with_score['score'] == 100
    for chosen, alternative, direction in ((with_score, without_score, 1), (without_score, with_score, -1)):
        effect = score_effect(chosen, alternative)
        canonical_other = lambda result: sum(row['points'] for row in result['contributions'] if row['component'] == 'other')
        assert effect['other'] == round(canonical_other(alternative) - canonical_other(chosen), 1) == direction * 100
        assert all(effect[key] == 0 for key in (*COMPONENTS, 'gate'))
        assert effect['points'] == direction * 100
        assert round(sum(effect[key] for key in (*COMPONENTS, 'gate', 'other')), 1) == effect['points']


@pytest.mark.parametrize('weights', [None, {'coverage': 1, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 0}])
def test_placement_effects_use_fresh_validation_and_shared_component_points(h, weights):
    import copy
    from packages.domain.score import COMPONENTS, flags, placement_effects
    from packages.domain.validation import evaluate
    a, b = h.seed['people'][:2]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = h.shift(h.workspace(), people=[a, b])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T16:00:00+03:00', people=[a])['workspace']
    d = h.snapshot(w)
    target = next(s for s in d['shifts'] if s['start'].startswith('2026-10-05'))
    baseline = {(s['id'], p['person_id']): flags(p) for s in d['shifts'] for p in s['participants']}
    d['wishes'] = [{'id': 'wish', 'person_id': a, 'kind': 'prefer_off', 'first_day': '2026-10-05',
                    'last_day': '2026-10-05', 'weight': 1}]
    options = {'scoring': {'weights': weights}} if weights else None
    original = copy.deepcopy(d)
    without = {**d, 'shifts': [dict(s, participants=[]) if s['id'] == target['id'] else s for s in d['shifts']]}
    with_score = schedule_score(d, evaluate(d), options, baseline=baseline, limit=0)
    without_score = schedule_score(without, evaluate(without), options, baseline=baseline, limit=0)
    effect = placement_effects(d, evaluate(d), target['id'], [a], options, baseline=baseline)[a]
    for component in COMPONENTS:
        assert effect[component] == round(without_score['components'][component]['lost'] - with_score['components'][component]['lost'], 1)
    assert effect['points'] == round(with_score['score'] - without_score['score'], 1)
    assert round(sum(effect[k] for k in (*COMPONENTS, 'gate', 'other')), 1) == effect['points']
    assert effect['other'] == 0
    assert not [row for result in (with_score, without_score) for row in result['contributions'] if row['component'] == 'other']
    assert effect['score_with'] == with_score['score'] and effect['score_without'] == without_score['score']
    if weights is None:
        assert all(effect[k] != 0 for k in ('coverage', 'rules', 'preferences', 'stability'))
        assert effect['gate'] != 0
        assert with_score['why'][0]['component'] == 'other'  # Truncated display rows are not the canonical adjustment.
    else:
        assert all(effect[k] == 0 for k in ('rules', 'fairness', 'preferences', 'stability'))
    assert d == original


def test_settings_validate_and_apportion_is_exact():
    with pytest.raises(ValidationError):
        ScoringSettings.model_validate({'weights': {'coverage': 0, 'rules': 0, 'fairness': 0, 'preferences': 0, 'stability': 0}})
    with pytest.raises(ValidationError):
        ScoringSettings.model_validate({'strengths': {'low': 5, 'medium': 3}})
    assert sum(apportion([1 / 3] * 3, 10)) == 10
    assert sum(apportion([0.05, 0.05, 0.05], 1)) == 1


DEFAULTS = {'weights': {'coverage': 35, 'rules': 25, 'fairness': 20, 'preferences': 15, 'stability': 5},
            'strengths': {'low': 1, 'medium': 3, 'high': 10, 'critical': 30},
            'wish_budget_points': 10, 'wish_history_periods': 6,
            'wish_priority_decay': 0.5, 'wish_granted_credit': 0.5,
            'wish_period': 'month', 'wish_debt_cap': 3, 'lead_balance': 'medium',
            'wish_priority_decay': 0.5, 'wish_granted_credit': 0.5, 'wish_history_periods': 6,
            'fairness_worst_off_emphasis': 0.5,
            'desired_cover_factor': 0.25, 'preferences_mean_weight': 7, 'preferences_worst_weight': 3}


def test_unset_scoring_resolves_to_the_agreed_defaults():
    # A fresh department ({}), settings saved before scoring existed (no `scoring`), a run without options.
    for options in ({}, None, {'mode': 'rebalance', 'balance_measure': 'duties'}, {'scoring': None}):
        assert settings_of(options) == DEFAULTS
    assert ScoringSettings().model_dump() == DEFAULTS


def test_wish_budget_preserves_its_storage_bound_when_score_limits_change():
    assert ScoringSettings(wish_budget_points=MAX_WISH_POINTS).wish_budget_points == MAX_WISH_POINTS
    with pytest.raises(ValidationError):
        ScoringSettings(wish_budget_points=MAX_WISH_POINTS + 1)


def test_department_without_scoring_offers_and_scores_with_the_defaults(h):
    assert h.get('admin', '/api/policies/capabilities')['generation_defaults']['scoring'] == DEFAULTS
    assert 'scoring' not in (h.get('admin', '/api/me')['department']['generation_defaults'] or {})
    score = h.detail(h.shift(h.workspace())['workspace'])['workspace']['validation']['score']
    # One indivisible required duty is a fair share even with three eligible people.
    assert score['components']['fairness']['value'] == 1
    assert score['score'] == 100
