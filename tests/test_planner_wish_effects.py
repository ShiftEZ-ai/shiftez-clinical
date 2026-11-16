"""Planner wish markers and current outcomes share packages/domain/patterns.py, without per-person scoring."""
from datetime import date, timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.wishes import planner_effects
from packages.contracts.schemas import DraftWishOutcome, WishEffect
from packages.domain import patterns, score
from scripts.generate_contracts import render
from tests.test_backend_scale import measure, scale_department
from tests.test_performance_budget import python_budget
from tests.test_staff_wishes import wish


@pytest.mark.parametrize('kind,matching,effect,outcome', [
    ('prefer_on', False, 'grants', 'granted'),
    ('prefer_off', False, 'against', 'declined'),
    ('prefer_shift_type', True, 'grants', 'granted'),
    ('prefer_shift_type', False, 'against', 'declined'),
    ('avoid_shift_type', True, 'against', 'declined'),
    ('avoid_shift_type', False, None, 'granted'),
])
def test_planner_granted_against_and_unaffected_wishes(h, kind, matching, effect, outcome):
    person, other = h.seed['people'][:2]
    typ = h.seed['type'] if matching else h.configure('assignment_types', {
        'name': 'Another synthetic duty', 'starts_on': '2020-01-01', 'definition': {}})['record']['id']
    extra = {'assignment_type_id': typ} if 'shift_type' in kind else {}
    saved = wish(h, '2026-10-05', kind=kind, note='Private wish note', **extra)['wish']
    elsewhere = wish(h, '2026-10-06', kind='prefer_on')['wish']
    w = h.shift(h.workspace(), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[person, other])['workspace']
    detail = h.detail(w)
    people = {p['person_id']: p for p in detail['shifts'][0]['participants']}
    assert people[person]['wish_effects'] == ([{'wish_id': saved['id'], 'effect': effect}] if effect else [])
    assert people[other]['wish_effects'] == []
    wishes = {w['id']: w for w in detail['wishes']}
    assert wishes[saved['id']]['draft_outcome'] == {
        'outcome': outcome, 'honoured_days': int(outcome == 'granted'), 'total_days': 1}
    assert wishes[elsewhere['id']]['draft_outcome'] == {'outcome': 'declined', 'honoured_days': 0, 'total_days': 1}
    assert all('note' not in w for w in wishes.values())
    assert wishes[saved['id']]['outcome'] is None  # a read never records a publication result
    for w in wishes.values():
        DraftWishOutcome.model_validate(w['draft_outcome'])
    for p in people.values():
        for marker in p['wish_effects']:
            WishEffect.model_validate(marker)


@pytest.mark.parametrize('kind,limit,outcome', [
    ('max_days_in_row', 1, 'granted'),
    ('min_days_off_together', 2, 'granted'),
    ('max_weekends_in_row', 1, 'granted'),
    ('whole_weekends', None, 'declined'),
])
def test_planner_pattern_wishes_load_without_per_day_marks(h, kind, limit, outcome):
    saved = wish(h, '2026-10-04', last='2026-10-10', kind=kind, limit=limit)['wish']
    w = h.shift(h.workspace(), start='2026-10-09T08:00:00+03:00',
                end='2026-10-09T12:00:00+03:00')['workspace']
    detail = h.detail(w)
    assert detail['wishes'][0]['id'] == saved['id']
    assert detail['wishes'][0]['draft_outcome'] == {
        'outcome': outcome, 'honoured_days': 7 if outcome == 'granted' else 0, 'total_days': 7}
    assert detail['shifts'][0]['participants'][0]['wish_effects'] == []


def test_planner_outcomes_reload_clipping_cancellation_and_wish_etag(h, monkeypatch):
    saved = wish(h, '2026-10-03', last='2026-10-05', kind='prefer_on')['wish']
    w = h.shift(h.workspace(start='2026-10-04', end='2026-10-06'))['workspace']
    path = '/api/workspaces/' + w['id']
    client = h.client('coordinator')
    calls, real = [], patterns.wish_outcomes
    monkeypatch.setattr(patterns, 'wish_outcomes', lambda data: calls.append(1) or real(data))
    first = client.get(path)
    assert first.status_code == 200
    body = first.json()
    assert calls == [1]
    assert body['wishes'][0]['draft_outcome'] == {'outcome': 'partly', 'honoured_days': 1, 'total_days': 2}
    assert client.get(path, headers={'if-none-match': first.headers['etag']}).status_code == 304
    assert calls == [1], 'A 304 neither reads assignments nor evaluates wishes'
    # A wish changes without a workspace revision changing; its new facts invalidate the ETag.
    changed = h.command('staff', 'wish.update', {'person_id': saved['person_id'], 'first_day': '2026-10-04',
        'last_day': '2026-10-05', 'kind': 'prefer_off', 'points': 1}, saved['id'], saved['revision'])['wish']
    refreshed = client.get(path, headers={'if-none-match': first.headers['etag']})
    assert refreshed.status_code == 200 and refreshed.headers['etag'] != first.headers['etag']
    assert refreshed.json()['workspace']['revision'] == body['workspace']['revision']
    assert refreshed.json()['shifts'][0]['participants'][0]['wish_effects'] == [
        {'wish_id': saved['id'], 'effect': 'against'}]
    assert refreshed.json()['wishes'][0]['draft_outcome'] == {'outcome': 'partly', 'honoured_days': 1, 'total_days': 2}
    monkeypatch.setattr(patterns, 'wish_outcomes', real)
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [body['shifts'][0]['id']],
        'action': 'cancel', 'reason': 'Synthetic cancellation'}, revision=w['revision'])['workspace']
    cancelled = h.detail(w)
    assert cancelled['shifts'][0]['participants'][0]['wish_effects'] == []
    assert cancelled['wishes'][0]['draft_outcome'] == {'outcome': 'granted', 'honoured_days': 2, 'total_days': 2}
    h.command('staff', 'wish.withdraw', {'reason': 'Plans changed'}, changed['id'], changed['revision'])
    withdrawn = h.detail(w)
    assert withdrawn['wishes'] == [] and withdrawn['shifts'][0]['participants'][0]['wish_effects'] == []
    h.get('staff', path, status=403)


def test_directional_type_markers_and_current_outcome_are_distinct(h):
    preferred = h.configure('assignment_types', {'name': 'Preferred synthetic duty', 'starts_on': '2020-01-01',
                                               'definition': {}})['record']['id']
    saved = wish(h, '2026-10-04', last='2026-10-05', kind='prefer_shift_type', assignment_type_id=preferred)['wish']
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-04T14:00:00+03:00', end='2026-10-04T18:00:00+03:00', typ=preferred)['workspace']
    detail = h.detail(w)
    assert [s['participants'][0]['wish_effects'] for s in detail['shifts']] == [
        [{'wish_id': saved['id'], 'effect': 'against'}], [{'wish_id': saved['id'], 'effect': 'grants'}]]
    # Matching + other work honours the day, and the next day off also honours a preferred-type wish.
    assert detail['wishes'][0]['draft_outcome'] == {'outcome': 'granted', 'honoured_days': 2, 'total_days': 2}
    assert h.get('approver', '/api/workspaces/' + w['id'])['wishes'] == detail['wishes']
    assert h.get('auditor', '/api/workspaces/' + w['id'])['shifts'] == detail['shifts']
    assert all('wish_effects' not in p for s in h.snapshot(w)['shifts'] for p in s['participants'])


def test_wish_markers_use_participant_local_start_day_for_overnight_work():
    data = {'workspace': {'starts_on': '2026-10-04', 'ends_on': '2026-10-06'},
            'wishes': [{'id': 'w', 'person_id': 'p', 'kind': 'prefer_off',
                        'first_day': '2026-10-05', 'last_day': '2026-10-05'}],
            'shifts': [{'assignment_type_id': 'type', 'cancelled': False, 'start': '2026-10-04T20:00:00Z',
                        'participants': [{'person_id': 'p', 'start': '2026-10-04T21:30:00Z',
                                          'end': '2026-10-05T03:00:00Z'}]}]}
    planner_effects(data, 'Asia/Riyadh')
    assert data['shifts'][0]['participants'][0]['wish_effects'] == [{'wish_id': 'w', 'effect': 'against'}]
    assert data['wishes'][0]['draft_outcome'] == {'outcome': 'declined', 'honoured_days': 0, 'total_days': 1}


@pytest.mark.parametrize('fixed_series', [False, True])
def test_draft_wish_outcomes_count_official_work_but_exclude_amended_and_alternative_rotas(h, fixed_series):
    off = wish(h, '2026-10-04')['wish']
    on = wish(h, '2026-10-05', kind='prefer_on')['wish']
    pub = h.publish(h.shift(h.workspace())['workspace'])
    alternative = h.workspace('Alternative draft')
    if fixed_series:
        from tests.test_series_overlap_api import candidate, reviewed
        from tests.test_series_reconciliation import materialise
        alternative = materialise(h, reviewed(h, candidate(h)), alternative)
    else:
        alternative = h.shift(alternative, start='2026-10-05T08:00:00+03:00',
                              end='2026-10-05T12:00:00+03:00')['workspace']
    empty = h.workspace('Draft with published context')
    detail = h.detail(empty)
    outcomes = {w['id']: w['draft_outcome']['outcome'] for w in detail['wishes']}
    assert outcomes == {off['id']: 'declined', on['id']: 'declined'}
    assert detail['shifts'] == [], 'Published context affects outcomes without exposing other rota assignments'
    assert patterns.wish_outcomes(h.snapshot(empty))[off['id']][0] == 0
    amendment = h.workspace(base=pub['publication_id'])
    own = h.detail(amendment)['shifts'][0]
    amendment = h.command('coordinator', 'shift.action', {'workspace_id': amendment['id'], 'shift_ids': [own['id']],
        'action': 'cancel', 'reason': 'Synthetic amendment'}, revision=amendment['revision'])['workspace']
    amended = h.detail(amendment)
    wishes = {w['id']: w for w in amended['wishes']}
    assert wishes[off['id']]['draft_outcome']['outcome'] == 'granted'
    assert wishes[off['id']]['outcome'] == 'declined', 'The original publication result remains recorded'
    assert wishes[on['id']]['draft_outcome']['outcome'] == 'declined', 'Alternative drafts never count'
    assert patterns.wish_outcomes(h.snapshot(amendment))[off['id']][0] == 1
    alternative_detail = h.detail(alternative)
    assert alternative_detail['shifts'][0]['participants'][0]['wish_effects'] == [
        {'wish_id': on['id'], 'effect': 'grants'}]
    # Shared overlap context still includes published work outside the draft's own session dates.
    assert {w['id']: w['draft_outcome']['outcome'] for w in alternative_detail['wishes']} == {
        off['id']: 'declined', on['id']: 'granted'}


@pytest.fixture(scope='module')
def planner_wish_effects_scale(scale_harness):
    """163 people, 768 sessions, 2,304 assignments, 1,630 wishes; one outcome pass, bounded local work and SQL."""
    h = scale_harness('pg')
    with pytest.MonkeyPatch.context() as monkeypatch:
        w, data, _, _ = scale_department(h)
        with transaction(h.app.state.engine, write=True) as db:
            db.add_all([m.StaffWish(department_id=h.seed['department_id'], person_id=pid, first_day=day, last_day=day,
                                   kind='prefer_on', points=1, created_by=h.seed['identities']['coordinator'])
                        for pid in data['people'] for day in (date(2026, 10, 4) + timedelta(days=i) for i in range(10))])
        h.lifecycle(w, 'validate')
        # Sign in before measuring; authentication setup is outside the payload's budget.
        h.client('coordinator')
        outcome_calls, day_calls = [], []
        real_outcomes, real_day = patterns.wish_outcomes, patterns.wish_day_effect
        monkeypatch.setattr(patterns, 'wish_outcomes', lambda data: outcome_calls.append(1) or real_outcomes(data))
        monkeypatch.setattr(patterns, 'wish_day_effect', lambda *args: day_calls.append(1) or real_day(*args))
        def no_score(*args, **kwargs):
            pytest.fail('Planner wish markers must not run scoring or placement trials')
        monkeypatch.setattr(score, 'schedule_score', no_score)
        monkeypatch.setattr(score, 'placement_effects', no_score)
        detail, metrics = measure(h, 'planner-wish-effects', lambda: h.detail(w))
        return detail, metrics, outcome_calls, day_calls


@pytest.mark.postgres
def test_planner_wish_effects_department_budget(planner_wish_effects_scale):
    detail, metrics, outcome_calls, day_calls = planner_wish_effects_scale
    assert len(detail['shifts']) == 768 and len(detail['wishes']) == 1630
    assignments = [p for s in detail['shifts'] for p in s['participants']]
    assert len(assignments) == 2304 and any(p['wish_effects'] for p in assignments)
    assert outcome_calls == [1]
    assert len(day_calls) <= len(detail['wishes']) + len(assignments), 'No assignments × all wishes scan'
    # workspace detail gates Apply → authoritative read required (docs/design/scoped-job-inputs.md); batching follow-up tightens it. Measured 63 (pg).
    assert metrics['sql']['SELECT'] < 70, metrics


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_planner_wish_effects_department_budget_cpu_budget(planner_wish_effects_scale):
    _, metrics, _, _ = planner_wish_effects_scale
    assert metrics['cpu_seconds'] < python_budget(1.5), metrics


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_planner_wish_effects_department_wall_benchmark(planner_wish_effects_scale):
    _, metrics, _, _ = planner_wish_effects_scale
    assert metrics['wall_seconds'] < python_budget(1.5) * 4, metrics


def test_planner_wish_contracts_are_generated():
    text, schemas = render()
    assert 'export type WishEffect = ' in text and 'export type DraftWishOutcome = ' in text
    assert schemas['WishEffect']['properties']['effect']['enum'] == ['grants', 'against']
    assert set(schemas['DraftWishOutcome']['required']) == {'outcome', 'honoured_days', 'total_days'}
