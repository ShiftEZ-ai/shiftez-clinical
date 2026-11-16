"""Why replacements retain the selected seat's obligations and its disclosure boundary."""
from tests.legacy_lead import historical_type

import json

import pytest

from .test_leave_disclosure import setting
from .test_planner_why import why


START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


@pytest.mark.parametrize('assigned', [True, False])
@pytest.mark.parametrize('aggregation', [None, 'service', 'service_group'])
def test_incremental_why_trials_match_full_validation_and_score(h, monkeypatch, assigned, aggregation):
    from packages.domain import validation
    from packages.domain.score import schedule_score
    a, b, c = h.seed['people']
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'target', 'amount': 2}, severity='advisory')
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'rota'},
             severity='advisory')
    if aggregation:
        rule = ({'kind': 'maximum_participants', 'maximum': 1, 'aggregation': aggregation}
                if aggregation == 'service' else {'kind': 'minimum_cover', 'minimum': 2, 'aggregation': aggregation})
        h.policy(rule, {'service_ids': h.seed['services']})
    w = h.shift(h.workspace(), people=[a] if assigned else [])['workspace']
    w = h.shift(w, start='2026-10-04T10:00:00+03:00', end='2026-10-04T14:00:00+03:00',
                people=[b], location=1)['workspace']
    for day, people in ((5, [a, b]), (6, [c]), (7, [])):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00',
                    end=f'2026-10-{day:02}T12:00:00+03:00', people=people)['workspace']
    source = h.snapshot(w)
    target = min(source['shifts'], key=lambda s: s['start'])
    context = next(s for s in source['shifts'] if s['participants'] and s['participants'][0]['person_id'] == c)
    source['focus_ids'].remove(context['id'])  # Wishes also read duties outside the scored rota.
    next(s for s in source['shifts'] if not s['participants'] and s is not target)['cancelled'] = True
    source['availability'].append({'id': 'synthetic-leave', 'person_id': c, 'start': '2026-10-04T10:00:00+03:00',
                                   'end': '2026-10-04T11:00:00+03:00', 'kind': 'recorded_leave'})
    source['wishes'] = [{'id': 'off', 'person_id': c, 'kind': 'prefer_off', 'first_day': '2026-10-04',
                         'last_day': '2026-10-06', 'weight': 2}]
    checked, original, cache = [], validation.validate_session_setup, {}

    def counted(data, shift, *args):
        checked.append(shift['id'])
        return original(data, shift, *args)

    monkeypatch.setattr(validation, 'validate_session_setup', counted)
    live_ids = {s['id'] for s in source['shifts'] if not s['cancelled'] and s['id'] in source['focus_ids']}
    for i, pid in enumerate((b, c, a, b)):
        seat = {**(target['participants'][0] if assigned else {'start': target['start'], 'end': target['end'],
                 'participation': 'staff', 'overtime': False}), 'id': 'why-' + pid, 'shift_id': target['id'], 'person_id': pid}
        trial = {**source, 'shifts': [dict(s, participants=[seat]) if s is target else s for s in source['shifts']]}
        checked.clear()
        incremental = validation.evaluate(trial, session_cache=cache)
        assert set(checked) == (live_ids if not i or aggregation else {target['id']})
        full = validation.evaluate(trial)
        assert incremental == full
        for options in (None, {'balance_measure': 'duties', 'scoring': {'weights': {
                'coverage': 0, 'rules': 1, 'fairness': 3, 'preferences': 2, 'stability': 0}}}):
            assert schedule_score(trial, incremental, options) == schedule_score(trial, full, options)


def test_session_cache_replays_duplicates_and_checks_changed_sessions(h, monkeypatch):
    from packages.domain import validation
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    source = h.snapshot(w)
    unchanged, changed = source['shifts']
    unchanged['participants'].append({**unchanged['participants'][0], 'id': 'duplicate-unchanged'})
    cache = {}
    initial = validation.evaluate(source, session_cache=cache)
    assert {f['shift_id'] for f in initial['findings'] if f['code'] == 'DUPLICATE_PARTICIPATION'} == {unchanged['id']}
    checked, original = [], validation.validate_session_setup

    def counted(data, shift, *args):
        checked.append(shift['id'])
        return original(data, shift, *args)

    monkeypatch.setattr(validation, 'validate_session_setup', counted)
    replacement = {**changed, 'participants': [*changed['participants'],
                   {**changed['participants'][0], 'id': 'duplicate-changed'}]}
    trial = {**source, 'shifts': [unchanged, replacement]}
    incremental = validation.evaluate(trial, session_cache=cache)
    assert checked == [changed['id']]
    assert {f['shift_id'] for f in incremental['findings'] if f['code'] == 'DUPLICATE_PARTICIPATION'} == {
        unchanged['id'], changed['id']}
    assert incremental == validation.evaluate(trial)


def test_runner_up_keeps_the_paired_trainees_named_supervisor(h):
    trainee, supervisor, alternative = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Why paired replacement', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']['id']
    result = h.shift(h.workspace(), typ=typ, people=[
        {'person_id': trainee, 'start': START, 'end': END, 'participation': 'trainee',
         'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': START, 'end': END, 'participation': 'supervisor'}])
    assert result['validation']['valid']
    w = result['workspace']
    shift = h.detail(w)['shifts'][0]

    session = why(h, w, [shift['id']], person_id=trainee, runner_up=True)['sessions'][0]

    assert session['score_status'] == 'complete'
    assert session['runner_up']['person_id'] == alternative
    assert h.detail(w)['shifts'][0]['participants'] == shift['participants']


def test_runner_up_cannot_inherit_a_lead_role_without_its_qualification(h):
    selected, alternative, unqualified = h.seed['people']
    rank = h.configure('categories', {'name': 'Synthetic lead qualification', 'starts_on': '2020-01-01'})['record']['id']
    for person in (selected, alternative):
        h.command('coordinator', 'membership.create', {'person_id': person, 'category_id': rank,
                                                      'starts_on': '2020-01-01'})
    typ = historical_type(h, {'name': 'Why qualified lead replacement', 'starts_on': '2020-01-01',
        'definition': {'lead': {'minimum': 1, 'maximum': 1, 'category_ids': [rank]}}})['id']
    result = h.shift(h.workspace(), typ=typ, people=[
        {'person_id': selected, 'start': START, 'end': END, 'team_lead': True}])
    assert result['validation']['valid']
    w = result['workspace']
    shift = h.detail(w)['shifts'][0]
    rows = {row['person_id']: row for row in why(h, w, [shift['id']])['sessions'][0]['rows']}
    assert rows[unqualified]['group'] == 'free' and not rows[unqualified]['can_lead']

    session = why(h, w, [shift['id']], person_id=selected, runner_up=True)['sessions'][0]

    assert session['score_status'] == 'complete'
    assert session['runner_up']['person_id'] == alternative


def test_runner_up_cannot_trade_an_existing_capability_gap_for_a_new_one(h, monkeypatch):
    from apps.api.services import staffing
    from packages.domain.validation import evaluate, finding_observations
    selected, unsafe, alternative = h.seed['people']
    first, second = h.seed['capability'], h.seed['supervisor_capability']
    typ = h.configure('assignment_types', {'name': 'Why distinct capability gaps', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'capabilities': [
            {'capability_id': first, 'minimum': 1}, {'capability_id': second, 'minimum': 2}]}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=[selected])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    source['grants'] = [grant for grant in source['grants']
                        if grant['capability_id'] == (second if grant['person_id'] == unsafe else first)]
    source['wishes'] = [{'id': 'prefer-unsafe-alternative', 'person_id': unsafe, 'kind': 'prefer_on',
                         'first_day': '2026-10-04', 'last_day': '2026-10-04', 'weight': 1}]
    baseline = [finding['detail'] for finding in finding_observations(evaluate(source))
                if finding['code'] == 'CAPABILITY_GAP']
    assert [(gap['capability_id'], gap['required'], gap['available']) for gap in baseline] == [(second, 2, 0)]
    unsafe_trial = {**source, 'shifts': [{**shift, 'participants': [
        {**shift['participants'][0], 'person_id': unsafe}]}]}
    unsafe_gaps = {finding['detail']['capability_id']: finding['detail']
                   for finding in finding_observations(evaluate(unsafe_trial)) if finding['code'] == 'CAPABILITY_GAP'}
    assert unsafe_gaps[first]['available'] == 0 and unsafe_gaps[second]['available'] == 1
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: source)

    session = why(h, w, [shift['id']], person_id=selected, runner_up=True)['sessions'][0]

    assert session['score_status'] == 'complete'
    assert session['runner_up']['person_id'] == alternative


def test_runner_up_can_improve_cover_when_partial_leave_boundaries_disappear(h):
    from packages.domain.validation import evaluate
    selected, *alternatives = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Why partially absent replacement', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    w = h.shift(h.workspace(), typ=typ, people=[selected])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': selected, 'kind': 'recorded_leave',
        'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00',
        'private_reason': 'Synthetic partial absence'})
    w = h.detail(w)['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    before = evaluate(source)['coverage']
    assert len(before) == 3 and [row['available_eligible'] for row in before] == [1, 0, 1]

    session = why(h, w, [shift['id']], person_id=selected, runner_up=True)['sessions'][0]

    assert session['score_status'] == 'complete'
    assert session['runner_up']['person_id'] in alternatives
    shift['participants'][0]['person_id'] = session['runner_up']['person_id']
    after = evaluate(source)['coverage']
    assert len(after) == 1 and after[0]['required'] == 2 and after[0]['available_eligible'] == 1


@pytest.mark.parametrize('mode', ['type', 'details', 'none'])
def test_why_unassigned_leave_uses_the_typed_disclosure_contract(h, mode):
    away = h.seed['people'][0]
    note = 'Private synthetic leave note'
    w = h.shift(h.workspace('Why leave projection'), people=[])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': away, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-03T00:00:00+03:00',
        'end': '2026-10-06T00:00:00+03:00', 'private_reason': note})
    assert h.get('admin', '/api/me')['department']['leave_reason_display'] == 'type'
    if mode != 'type':
        setting(h, mode)
    w = h.detail(w)['workspace']
    shift = h.detail(w)['shifts'][0]

    result = why(h, w, [shift['id']])

    row = next(row for row in result['sessions'][0]['rows'] if row['person_id'] == away)
    assert row['group'] == 'unavailable'
    findings = [finding for finding in row['findings'] if finding['code'] == 'UNAVAILABLE']
    assert findings
    if mode == 'none':
        assert all('leave' not in finding for finding in findings)
        assert 'Annual' not in json.dumps(result)
    else:
        expected = {'type': 'Annual', 'starts_on': '2026-10-03', 'ends_on': '2026-10-05'}
        if mode == 'details':
            expected['note'] = note
        assert all(finding['leave'] == expected for finding in findings)
    assert (note in json.dumps(result)) == (mode == 'details')
