"""Fairness dashboard reads: schedule score and trend, distribution bands, groups and wishes, the sessions
drill-down, compare-drafts detail, saved fairness views, and a big-department speed guard."""
import json
import random
import time
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import insert, select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import fairness as fairness_service
from packages.domain import fairness as f
from packages.domain.score import COMPONENTS
from packages.domain.quotas import METRIC_VERSION
from packages.domain.time import aware

PERIOD = '&starts_on=2026-10-01&ends_on=2026-11-01'


def at(day, hour=8, length=4):
    return f'2026-10-{day:02}T{hour:02}:00:00+03:00', f'2026-10-{day:02}T{hour + length:02}:00:00+03:00'


def session(h, w, day, people, overtime=(), typ=None):
    start, end = at(day)
    return h.shift(w, start=start, end=end, typ=typ,
                   people=[{'person_id': p, 'start': start, 'end': end, 'overtime': p in overtime} for p in people])['workspace']


def test_band_and_status_are_the_goal_band():
    assert f.band('duties', 5) == (4, 6, 1.0) and f.band('hours', 100) == (90, 110, 10.0) and f.band('hours', 10)[2] == 4
    assert [f.status('duties', v, 5) for v in (3, 5, 6, 7)] == ['below', 'within', 'within', 'above']
    assert f.status('duties', 9, 5, 'max_min') == 'within'


def test_schedule_score_and_trend_for_planners_only(h):
    a, b, _ = h.seed['people']
    empty = h.get('coordinator', '/api/fairness/goals')
    assert empty == {'workspace': None, 'goals': [], 'schedule': None, 'score_trend': []}
    w = session(h, h.workspace(), 4, [a, b])
    published = h.publish(w)
    goals = h.get('coordinator', '/api/fairness/goals')
    schedule = goals['schedule']
    assert 0 <= schedule['score'] <= 100 and isinstance(schedule['publishable'], bool) and isinstance(schedule['fresh'], bool)
    assert set(schedule['components']) == set(COMPONENTS) and {'value', 'weight', 'lost'} <= set(schedule['components']['coverage'])
    assert isinstance(schedule['why'], list) and 'gate' in schedule
    [trend] = goals['score_trend']
    assert trend['publication_id'] == published['publication_id'] and trend['workspace_id'] == w['id'] and trend['name'] == w['name']
    assert trend['starts_on'] == '2026-10-04' and trend['score'] == schedule['score'] and set(trend['components']) == set(COMPONENTS)
    assert h.get('coordinator', '/api/fairness/goals?starts_on=2026-11-01&ends_on=2027-01-01')['score_trend'] == []
    draft = session(h, h.workspace(start='2026-10-11', end='2026-10-18'), 11, [a])
    assert h.get('coordinator', '/api/fairness/goals?workspace_id=' + draft['id'])['schedule']['fresh'] is True
    mine = h.get('staff', '/api/fairness/goals')
    assert mine['schedule'] is None and mine['score_trend'] == []


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('changed_input', ['session', 'scoring', 'grant'])
def test_draft_schedule_currency_matches_the_check_gate(h, changed_input):
    from apps.api.services.planning import complete_check
    from apps.api.snapshots import domain_snapshot

    created = h.shift(h.workspace('Draft check currency'))
    workspace = created['workspace']
    h.command('coordinator', 'workspace.validate', {'reason': 'Check draft inputs'},
              workspace['id'], workspace['revision'])
    url = '/api/fairness/goals?workspace_id=' + workspace['id']
    assert h.get('coordinator', url)['schedule']['fresh'] is True
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        stored = row.validation
        department = db.get(m.Department, row.department_id)
        source = domain_snapshot(db, row)
        assert complete_check(stored, source, source['department']['generation_defaults']) is True
        if changed_input == 'session':
            shift = db.get(m.Shift, created['shift_id'])
            shift.end += timedelta(hours=1)
            shift.revision += 1
            row.content_revision += 1
            row.revision += 1
        elif changed_input == 'scoring':
            department.generation_defaults = {**(department.generation_defaults or {}),
                'scoring': {'weights': {'coverage': 36, 'rules': 25, 'fairness': 25,
                                        'preferences': 10, 'stability': 5}}}
            department.revision += 1
        else:
            grant = db.scalar(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == h.seed['people'][0]))
            grant.revoked = True
            grant.revision += 1
            db.get(m.Organisation, department.organisation_id).input_revision += 1
    # A later scoring change leaves the draft's captured settings and check current.
    expected_fresh = changed_input == 'scoring'
    assert h.get('coordinator', url)['schedule']['fresh'] is expected_fresh
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        source = domain_snapshot(db, row)
        assert complete_check(row.validation, source, source['department']['generation_defaults']) is expected_fresh
        assert row.validation == stored
    if changed_input == 'scoring':
        h.refresh_draft_baseline(workspace)
        assert h.get('coordinator', url)['schedule'] is None


def test_draft_schedule_currency_discards_legacy_cached_verdict(h, monkeypatch):
    workspace = h.shift(h.workspace('Cached draft check'))['workspace']
    client = h.client('coordinator')
    url = '/api/fairness/goals?workspace_id=' + workspace['id']
    current_schedule = fairness_service.schedule

    def legacy_etag(db, actor, workspace_id=None, who=None, starts_on=None, ends_on=None):
        return fairness_service._dashboard_etag(db, actor, 'fairness-goals', workspace_id, who, starts_on, ends_on,
                                                workspace_id=workspace_id)

    with monkeypatch.context() as legacy:
        legacy.setattr(fairness_service, 'goals_etag', legacy_etag)
        legacy.setattr(fairness_service, 'schedule',
                       lambda db, row: {**current_schedule(db, row), 'fresh': False})
        cached = client.get(url)
        assert cached.status_code == 200 and cached.json()['schedule']['fresh'] is False

    current = client.get(url, headers={'if-none-match': cached.headers['etag']})
    assert current.status_code == 200 and current.headers['etag'] != cached.headers['etag']
    assert current.json()['schedule']['fresh'] is True


def test_distribution_and_exposure_support_conditional_reads(h):
    person = h.seed['people'][0]
    published = session(h, h.workspace('Conditional fairness'), 4, [person])
    h.publish(published)
    client = h.client('coordinator')
    paths = [('/api/fairness/distribution?metric=duties' + PERIOD),
             ('/api/fairness/exposure?' + PERIOD[1:])]
    for path in paths:
        first = client.get(path)
        assert first.status_code == 200 and first.headers['etag']
        repeat = client.get(path, headers={'if-none-match': first.headers['etag']})
        assert repeat.status_code == 304 and repeat.content == b''
        assert repeat.headers['etag'] == first.headers['etag']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_default_fairness_goals_use_the_current_rota_and_name_the_audience(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, {}, severity='advisory')
    current = h.shift(h.workspace('Current fairness rota', '2026-10-01', '2026-10-11'),
                       start='2026-10-04T08:00:00+03:00', end='2026-10-04T12:00:00+03:00', people=[person])['workspace']
    h.publish(current)
    future = h.shift(h.workspace('Far future fairness rota', '2031-06-01', '2031-06-11'),
                     start='2031-06-02T08:00:00+03:00', end='2031-06-02T12:00:00+03:00', people=[person])['workspace']
    h.publish(future)
    result = h.get('coordinator', '/api/fairness/goals')
    assert result['workspace']['id'] == current['id']
    assert result['goals'] and all(goal['audience_label'] for goal in result['goals'])


def test_score_card_uses_latest_publication_inside_requested_period(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, {}, severity='advisory')
    current = h.shift(h.workspace('Current fairness rota', '2026-10-01', '2026-10-11'),
                      start='2026-10-04T08:00:00+03:00', end='2026-10-04T12:00:00+03:00', people=[person])['workspace']
    h.publish(current)
    requested = h.shift(h.workspace('May fairness rota', '2027-05-01', '2027-05-11'),
                        start='2027-05-02T08:00:00+03:00', end='2027-05-02T12:00:00+03:00', people=[person])['workspace']
    h.publish(requested)

    result = h.get('coordinator', '/api/fairness/goals?starts_on=2027-05-01&ends_on=2027-06-01')

    assert result['workspace']['id'] == requested['id']
    assert result['workspace']['name'] == requested['name']
    assert result['schedule'] is not None


@pytest.mark.parametrize('cancelled', [False, True])
def test_historical_empty_score_is_not_applicable_in_insights_without_rewriting_evidence(h, monkeypatch, cancelled):
    from packages.domain import score
    current_score = score.schedule_score
    def historical_score(*args, **kwargs):
        issued = current_score(*args, **kwargs)
        return {**issued, 'score': 100, 'raw': 100, 'uncapped_score': 100, 'publishable': True, 'gate': None} if issued['score'] is None else issued
    monkeypatch.setattr(score, 'schedule_score', historical_score)
    w = h.workspace('Historical empty score')
    publication = None
    if cancelled:
        created = h.shift(w)
        w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'],
            'shift_ids': [created['shift_id']], 'action': 'cancel', 'reason': 'Clinic closed'},
            revision=created['workspace']['revision'])['workspace']
        publication = h.publish(w)['publication_id']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Workspace, w['id']).validation
        issued = db.get(m.Publication, publication).validation if publication else None
    assert stored['score']['score'] == 100
    body = h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'] + PERIOD)
    assert body['schedule'] is None and body['score_trend'] == []
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).validation == stored
        if publication:
            assert db.get(m.Publication, publication).validation == issued
            assert issued['score']['score'] == 100


def test_legacy_score_without_coverage_metadata_keeps_its_issued_value(h, monkeypatch):
    from apps.api.services import planning
    current_evaluate = planning.evaluate
    monkeypatch.setattr(planning, 'evaluate', lambda data, **options: {
        key: value for key, value in current_evaluate(data, **options).items() if key != 'coverage'})
    w = session(h, h.workspace(), 4, [h.seed['people'][0]])
    publication = h.publish(w)['publication_id']
    with transaction(h.app.state.engine) as db:
        issued = db.get(m.Publication, publication).validation
    assert 'coverage' not in issued and issued['score']['score'] is not None
    body = h.get('coordinator', '/api/fairness/goals?' + PERIOD[1:])
    assert body['schedule']['score'] == issued['score']['score']
    trend, = body['score_trend']
    assert trend['score'] == issued['score']['score'] and trend['publication_id'] == publication
    person = h.get('coordinator', '/api/people/' + h.seed['people'][0] + '/score')['published']
    assert person['score'] == issued['score']['score']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_distribution_excludes_people_without_solver_eligibility(h):
    a, b, c = h.seed['people']
    h.publish(session(h, h.workspace(), 4, [a, b]))
    with transaction(h.app.state.engine, write=True) as db:
        for row in db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == c)):
            row.ends_on = date(2026, 10, 1)
    rows = h.get('coordinator', '/api/fairness/distribution?metric=duties' + PERIOD)['people']
    assert {row['person_id'] for row in rows} == {a, b}
    assert all(row['expected'] == 1 for row in rows)


def test_prepublication_distribution_uses_draft_eligible_audience(h):
    person, wrong_rank, other_wrong_rank = h.seed['people']
    with transaction(h.app.state.engine, write=True) as db:
        for pid in (wrong_rank, other_wrong_rank):
            for membership in db.scalars(select(m.Membership).where(m.Membership.person_id == pid,
                                                                     m.Membership.department_id == h.seed['department_id'])):
                membership.category_id = h.seed['trainee_category']
    typ = h.configure('assignment_types', {'name': 'Prepublication qualified duty', 'starts_on': '2020-01-01',
        'definition': {'allowed_category_ids': [h.seed['category']], 'requirements': [{'minimum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(start='2026-10-04', end='2026-10-11'), typ=typ,
                        people=[person])['workspace']
    first, last = date(2026, 10, 1), date(2026, 11, 1)
    rule = fairness_service.metric_rule('duties', [], [])
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        draft = fairness_service._eligible_distribution_people(
            db, actor, h.snapshot(workspace), rule, first, last, list(h.seed['people']))
    assert draft == {person}

    published = h.publish(workspace)['workspace']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        issued = fairness_service._eligible_distribution_people(
            db, actor, h.snapshot(published), rule, first, last, list(h.seed['people']))
    assert issued == {person}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('kind', ['leave', 'pattern', 'recorded'])
def test_reference_distribution_retains_issued_work_after_absence(h, kind):
    from .test_published_operational import absence, frozen

    a, b, c = h.seed['people']
    with transaction(h.app.state.engine, write=True) as db:
        for row in db.scalars(select(m.Eligibility).where(m.Eligibility.person_id == c)):
            row.ends_on = date(2026, 10, 1)
    publication = h.publish(session(h, h.workspace(), 6, [a]))['publication_id']
    issued = frozen(h, publication)
    url = '/api/fairness/distribution?metric=hours&group_by=category_ids' + PERIOD
    before = h.get('coordinator', url)
    # The unassigned eligible colleague remains in the reference; the unqualified person does not.
    assert {r['person_id']: (r['value'], r['expected']) for r in before['people']} == {a: (4, 2), b: (0, 2)}
    absence(h, kind)
    assert h.get('coordinator', url) == before
    own = h.get('staff', url)['people']
    assert own == [r for r in before['people'] if r['person_id'] == a]
    drill = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric=hours' + PERIOD)
    assert drill['basis'] == 'published_plan' and drill['metric_value'] == 4 and drill['total'] == 1
    assert frozen(h, publication) == issued


def test_distribution_band_months_groups_and_wishes(h):
    a, b, c = h.seed['people']
    h.command('staff', 'wish.create', {'person_id': a, 'first_day': '2026-10-06', 'last_day': '2026-10-06', 'kind': 'prefer_off', 'points': 2})
    w = session(h, h.workspace(), 4, [a, b])
    for day in (5, 6):
        w = session(h, w, day, [a])
    h.publish(w)
    with transaction(h.app.state.engine, write=True) as db:  # c is a learner, a and b practitioners
        db.scalar(select(m.Membership).where(m.Membership.person_id == c)).category_id = h.seed['trainee_category']
    spread = h.get('coordinator', '/api/fairness/distribution?metric=duties' + PERIOD)
    people = {r['person_id']: r for r in spread['people']}
    assert people[a]['expected'] == 1.33 and (people[a]['low'], people[a]['high']) == (0.33, 2.33)
    assert [people[p]['status'] for p in (a, b, c)] == ['above', 'within', 'below']
    assert spread['months'] == {'2026-10': 4} and spread['groups'] == []
    grouped = h.get('coordinator', '/api/fairness/distribution?metric=duties&group_by=category_ids' + PERIOD)['groups']
    assert [(g['key'], g['label'], g['person_ids'], g['people'], g['value']) for g in grouped] == [
        (h.seed['trainee_category'], 'Demo learners', [c], 1, 0), (h.seed['category'], 'Demo practitioners', [a, b], 2, 4)]
    assert grouped[1]['mean'] == 2 and grouped[1]['expected'] == 2.66 and grouped[1]['expected_mean'] == 1.33
    [none] = h.get('coordinator', '/api/fairness/distribution?group_by=tags' + PERIOD)['groups']
    assert none['key'] == '' and none['label'] == 'None' and none['people'] == 3
    assert h.get('staff', '/api/fairness/distribution?group_by=category_ids' + PERIOD)['groups'] == []
    h.get('coordinator', '/api/fairness/distribution?group_by=person_ids', status=422)
    wishes = h.get('coordinator', '/api/fairness/distribution?metric=wishes_declined' + PERIOD)
    assert wishes['unit'] == 'preference points' and {r['person_id']: r['value'] for r in wishes['people']} == {a: 2, b: 0, c: 0}
    assert wishes['months'] == {'2026-10': 2}
    assert {r['person_id']: r['value'] for r in h.get('coordinator', '/api/fairness/distribution?metric=wishes_granted' + PERIOD)['people']}[a] == 0
    filtered = h.get('coordinator', f"/api/fairness/distribution?metric=wishes_declined&assignment_type_ids={h.seed['type']}", status=422)
    assert filtered['error']['message'] == 'Preferences are not counted per duty type, clinic or place'
    drill = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric=wishes_declined' + PERIOD)
    [pub] = drill['publications']
    assert pub['value'] == 2 and pub['sessions'] == [] and [(x['kind'], x['points'], x['outcome']) for x in pub['wishes']] == [('prefer_off', 2, 'declined')]


def test_places_count_with_what_is_inside_and_work_groups(h):
    """A place counts its own sessions, or with `within` every place inside it; grouping by clinic, duty type or place
    sums what each person did there (the ledger's filtered keys) with no expected share."""
    a, b, c = h.seed['people']
    north, south = h.seed['locations']
    room = h.configure('locations', {'name': 'North annex', 'starts_on': '2020-01-01', 'location_type_id': h.seed['location_type'],
                                     'parent_id': north})['record']['id']
    h.seed['locations'].append(room)
    evening = h.configure('assignment_types', {'name': 'Evening clinic', 'starts_on': '2020-01-01', 'definition': {}})['record']['id']
    w = h.workspace()
    w = h.shift(w, *at(4), people=[a])['workspace']                                    # North, clinic 0
    w = h.shift(w, *at(5), people=[a, b], location=2, service=1, typ=evening)['workspace']  # the annex (inside North), clinic 1
    w = h.shift(w, *at(6), people=[c], location=1)['workspace']                        # South, clinic 0
    h.publish(w)
    url = '/api/fairness/distribution?metric=duties' + PERIOD
    value = lambda path: {r['person_id']: r['value'] for r in h.get('coordinator', url + path)['people']}
    assert value(f'&location_ids={north}') == {a: 2, b: 1, c: 0}
    assert value(f'&location_ids={north}&within=false') == {a: 1, b: 0, c: 0}
    assert value(f'&location_ids={room}&metric=hours') == {a: 4, b: 4, c: 0}
    assert value('&location_ids=' + str(uuid.uuid4())) == {}   # an unknown place counts nothing
    groups = lambda path: [(g['label'], g['person_ids'], g['value'], g['mean'], g['expected']) for g in h.get('coordinator', url + path)['groups']]
    assert groups('&group_by=location_ids') == [('North annex', [a, b], 2, 1, None), ('North consulting room', [a], 1, 1, None),
                                                ('South consulting room', [c], 1, 1, None)]
    assert groups(f'&group_by=location_ids&location_ids={north}') == [('North annex', [a, b], 2, 1, None), ('North consulting room', [a], 1, 1, None)]
    assert groups('&group_by=service_ids') == [('Continuity clinic', [a, c], 2, 1, None), ('Review clinic', [a, b], 2, 1, None)]
    by_type = h.get('coordinator', url + '&group_by=assignment_type_ids')
    assert by_type['group_kind'] == 'work' and {g['label']: g['value'] for g in by_type['groups']} == {'Clinical review session': 2, 'Evening clinic': 2}
    assert h.get('coordinator', url + '&group_by=category_ids')['group_kind'] == 'people'
    weekends = h.get('coordinator', '/api/fairness/distribution?metric=weekends&group_by=location_ids' + PERIOD)   # Sun 4 - Tue 6: none
    assert weekends['groups'] == []
    h.get('coordinator', url + f'&group_by=service_ids&location_ids={north}', status=422)   # a clinic group can't be cut by place
    h.get('coordinator', url + f'&location_ids={north}&service_ids={h.seed["services"][0]}', status=422)
    h.get('coordinator', '/api/fairness/distribution?metric=wishes_declined&group_by=service_ids' + PERIOD, status=422)
    h.get('coordinator', f'/api/fairness/distribution?metric=wishes_declined&location_ids={north}' + PERIOD, status=422)
    drill = h.get('coordinator', f'/api/fairness/sessions?person_id={a}&location_ids={north}' + PERIOD)
    assert drill['total'] == 2 and [s['location'] for s in drill['publications'][0]['sessions']] == ['North consulting room', 'North annex']
    assert h.get('coordinator', f'/api/fairness/sessions?person_id={a}&location_ids={north}&within=false' + PERIOD)['total'] == 1
    together = h.get('coordinator', f'/api/fairness/exposure?location_ids={room}' + PERIOD)
    cell = lambda x, p, q: x['cells'][[r['person_id'] for r in x['rows']].index(p)][[r['person_id'] for r in x['cols']].index(q)]
    assert cell(together, a, b) == 1 and cell(h.get('coordinator', f'/api/fairness/exposure?location_ids={south}' + PERIOD), a, b) == 0


def test_sessions_drill_down(h):
    a, b, _ = h.seed['people']
    other = h.configure('assignment_types', {'name': 'Evening clinic', 'starts_on': '2020-01-01', 'definition': {}})['record']['id']
    w = session(h, h.workspace(), 4, [a, b])
    w = session(h, w, 5, [a], overtime=[a])
    w = session(h, w, 6, [a], typ=other)
    published = h.publish(w)
    url = f'/api/fairness/sessions?person_id={a}'
    duties = h.get('coordinator', url + PERIOD)
    assert duties['person']['id'] == a and duties['other'] is None and duties['unit'] == 'duties' and duties['total'] == 3
    [pub] = duties['publications']
    assert pub['publication_id'] == published['publication_id'] and pub['name'] == w['name'] and pub['value'] == 3 and pub['wishes'] == []
    assert [s['day'] for s in pub['sessions']] == ['2026-10-04', '2026-10-05', '2026-10-06']
    first = pub['sessions'][0]
    assert first['services'] == ['Continuity clinic'] and first['location'] == 'North consulting room' and first['value'] == 1
    assert first['type_name'] == 'Clinical review session' and pub['sessions'][1]['overtime'] and not first['team_lead']
    hours = h.get('coordinator', url + '&metric=hours' + PERIOD)['publications'][0]
    assert hours['value'] == 12 and {s['value'] for s in hours['sessions']} == {4}
    typed = h.get('coordinator', url + f"&assignment_type_ids={h.seed['type']}" + PERIOD)
    assert typed['total'] == 2 and typed['publications'][0]['value'] == 2
    assert h.get('coordinator', url + '&month=2026-10' + PERIOD)['total'] == 3
    assert h.get('coordinator', url + '&month=2026-11&starts_on=2026-10-01&ends_on=2027-01-01')['publications'] == []
    h.get('coordinator', url + '&month=2027-13', status=422)
    pair = h.get('coordinator', url + f'&metric=pair_exposure&other_person_id={b}' + PERIOD)
    assert pair['other']['id'] == b and pair['total'] == 1 and pair['publications'][0]['sessions'][0]['day'] == '2026-10-04'
    weekends = h.get('coordinator', url + '&metric=weekends' + PERIOD)  # Sun 4 - Tue 6 October: no Friday or Saturday
    assert weekends['publications'][0]['value'] == 0
    assert h.get('staff', url + PERIOD)['total'] == 3
    h.get('staff', f'/api/fairness/sessions?person_id={b}', status=403)
    h.get('coordinator', url + '&metric=nonsense', status=422)


@pytest.mark.parametrize('measure,metric,unit,amount,parts', [
    ('duties', 'duties', 'duties', 4, [1, 1, 1, 1]), ('reserved_hours', 'hours', 'h', 14, [4, 4, 2, 4]),
])
def test_workload_goal_drill_counts_its_rota_measure_and_dated_scope(h, measure, metric, unit, amount, parts):
    a, b, _ = h.seed['people']
    me = h.get('admin', '/api/me')
    dept, org = me['department'], me['organisation']
    h.command('admin', 'department.settings', {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'generation_defaults': {**(dept['generation_defaults'] or {}), 'balance_measure': measure},
        'reason': 'Choose the workload measure'}, dept['id'], org['revision'])
    proposed = h.command('coordinator', 'policy.create', {'name': 'Dated clinic workload', 'owner': 'Test policy owner',
        'source': 'Synthetic workload drill fixture', 'starts_on': '2026-10-13', 'ends_on': '2026-10-16',
        'definition': {'severity': 'advisory', 'scope': {'person_id': a}, 'rule': {'kind': 'balance_goal',
            'metric': 'workload', 'aim': 'workload_spread', 'period': 'rota', 'share_by': 'equal',
            'service_ids': [h.seed['services'][0]]}}})['record']
    policy = h.command('approver', 'policy.activate', {'reason': 'Approve synthetic workload scope'},
                       proposed['id'], proposed['revision'])['record']
    h.publish(session(h, h.workspace(), 6, [a]))
    w = h.workspace('Current workload rota', '2026-10-11', '2026-10-18')
    for day, service, length in ((11, 0, 4), (13, 0, 4), (14, 1, 4), (15, 0, 2), (16, 0, 4)):
        w = h.shift(w, *at(day, length=length), people=[a], service=service)['workspace']
    published = h.publish(w)
    report = next(goal for goal in h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'])['goals'] if goal['policy_id'] == policy['id'])
    [row] = report['people']
    # Effective dates activate the goal; its rota window measures all matching work, including before activation.
    assert row['actual'] == amount and report['unit'] == unit
    url = f"/api/fairness/sessions?person_id={a}&metric=workload&workspace_id={w['id']}&goal_id={report['goal_id']}" + PERIOD
    # A dashboard filter cannot replace the clicked goal's clinic scope.
    drill = h.get('coordinator', url + '&service_ids=' + h.seed['services'][1])
    assert drill['metric'] == metric and drill['unit'] == unit and drill['total'] == 4
    assert (drill['starts_on'], drill['ends_on']) == (w['starts_on'], w['ends_on'])
    [pub] = drill['publications']
    assert pub['publication_id'] == published['publication_id'] and pub['workspace_id'] == w['id']
    assert pub['value'] == row['actual'] == sum(s['value'] for s in pub['sessions'])
    assert [s['day'] for s in pub['sessions']] == ['2026-10-11', '2026-10-13', '2026-10-15', '2026-10-16']
    assert [s['value'] for s in pub['sessions']] == parts
    assert h.get('staff', url)['publications'] == drill['publications']
    h.get('staff', url.replace(f'person_id={a}', f'person_id={b}'), status=403)
    h.get('coordinator', url.replace(report['goal_id'], 'missing'), status=404)
    h.get('coordinator', f'/api/fairness/sessions?person_id={a}&metric=workload', status=422)
    draft = h.workspace('Unpublished workload rota', '2026-10-18', '2026-10-25')
    h.get('staff', url.replace(w['id'], draft['id']), status=403)
    h.get('coordinator', url.replace(w['id'], draft['id']), status=422)


def test_compare_detail_per_component_goal_and_person(h):
    a, b, _ = h.seed['people']
    g = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'rota'}, {}, severity='advisory')
    one = session(h, h.workspace(), 4, [a, b])
    two = session(h, h.workspace('Other draft'), 4, [a])
    two = session(h, two, 5, [a])
    detail = h.get('coordinator', f"/api/workspaces/{one['id']}/compare?other_id={two['id']}")['scores']['detail']
    assert [c['component'] for c in detail['components']] == list(COMPONENTS)
    fairness = next(c for c in detail['components'] if c['component'] == 'fairness')
    assert fairness['delta'] == round(fairness['after'] - fairness['before'], 4) and fairness['delta'] < 0
    [goal] = [row for row in detail['goals'] if row['policy_id'] == g['id']]
    assert goal['policy_id'] == g['id'] and goal['unit'] == 'duties' and goal['after'] < goal['before']
    assert detail['people'] and all(r['name'] and (r['delta'] is None or abs(r['delta']) >= 0.1) for r in detail['people'])
    # A published rota's score stays current after later changes elsewhere (it is frozen); a draft's goes stale.
    h.publish(one)
    h.configure('people', {'display_name': 'Someone new'})
    scores = h.get('coordinator', f"/api/workspaces/{two['id']}/compare?other_id={one['id']}")['scores']
    assert scores['after']['fresh'] is True and scores['before']['fresh'] is False
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, two['id']).validation = None
    assert h.get('coordinator', f"/api/workspaces/{one['id']}/compare?other_id={two['id']}")['scores']['detail'] is None


def test_saved_fairness_view_round_trip(h):
    a = h.seed['people'][0]
    who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [a]}]}
    fairness = {'period': 'custom', 'starts_on': '2026-01-01', 'ends_on': '2026-07-01', 'who': who, 'metric': 'wishes_granted',
                'group_by': 'category_ids', 'pairing': 'supervised', 'sort': 'value', 'hidden': ['exposure'], 'density': 'compact'}
    saved = h.command('coordinator', 'table_view.create', {'table_key': 'fairness', 'name': 'Learners', 'definition': {'fairness': fairness}})['view']
    [view] = h.get('coordinator', '/api/table-views?table=fairness')['views']
    assert view['id'] == saved['id'] and view['definition']['fairness'] == {**fairness, 'assignment_type_ids': [], 'service_ids': [],
                                                                            'location_ids': [], 'within': True, 'rows': None, 'cols': None,
                                                                            'trend_who': None}
    # Locations, work groups and the trend lines' people are kept too.
    more = {**fairness, 'metric': 'hours', 'location_ids': [h.seed['locations'][0]], 'within': False, 'group_by': 'location_ids',
            'trend_who': who, 'hidden': ['people']}
    kept = h.command('coordinator', 'table_view.create', {'table_key': 'fairness', 'name': 'Places', 'definition': {'fairness': more}})['view']
    assert {k: kept['definition']['fairness'][k] for k in more} == more
    for bad in ({'period': 'custom', 'starts_on': '2026-01-01'}, {'period': 'custom', 'starts_on': '2026-07-01', 'ends_on': '2026-01-01'},
                {'hidden': ['nonsense']}, {'metric': 'pair_exposure'}, {'group_by': 'person_ids'}):
        h.command('coordinator', 'table_view.create', {'table_key': 'fairness', 'name': 'Bad', 'definition': {'fairness': bad}}, status=422)


def big_department(h):
    """150 people, 12 monthly publications (Oct 2025 - Sep 2026), a year of ledger rows and one monthly duties goal."""
    seed, rng = h.seed, random.Random(8)
    coordinator = seed['identities']['coordinator']
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share', 'period': 'month'}, {}, severity='advisory')
    people = [str(uuid.uuid4()) for _ in range(150)]
    months = [date(2025 + (9 + k) // 12, (9 + k) % 12 + 1, 1) for k in range(13)]
    score = {'score': 80.0, 'publishable': True, 'components': {c: {'value': 0.8, 'weight': 20, 'lost': 4.0} for c in COMPONENTS}}
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': p, 'organisation_id': seed['organisation_id'], 'display_name': f'Person {k:03}',
                                       'tags': ['night'] if k % 3 else []} for k, p in enumerate(people)])
        db.execute(insert(m.Membership), [{'person_id': p, 'department_id': seed['department_id'], 'starts_on': date(2020, 1, 1),
                                           'category_id': seed['category'] if k % 4 else seed['trainee_category']} for k, p in enumerate(people)])
        db.execute(insert(m.Eligibility), [{'person_id': p, 'department_id': seed['department_id'],
            'service_id': seed['services'][0], 'starts_on': date(2020, 1, 1)} for p in people])
        ledger = []
        for k in range(12):
            first, last = months[k], months[k + 1]
            w = m.Workspace(department_id=seed['department_id'], name=f'Rota {first:%b %Y}', starts_on=first, ends_on=last, status='draft',
                            created_by=coordinator, last_edited_by=coordinator)
            db.add(w)
            db.flush()
            shifts = []
            pub_id = str(uuid.uuid4())
            day = first
            while day < last:
                for n, p in enumerate(people):
                    ledger.append({'publication_id': pub_id, 'person_id': p, 'metric_key': f.AVAILABLE, 'bucket': day, 'value': 1.0})
                    if rng.random() < 0.4:
                        start = f'{day.isoformat()}T05:00:00+00:00'
                        end = f'{day.isoformat()}T13:00:00+00:00'
                        place = seed['locations'][n % 2]
                        shifts.append({'id': str(uuid.uuid4()), 'cancelled': False, 'assignment_type_id': seed['type'], 'service_ids': [seed['services'][0]],
                                       'location_id': place, 'type_snapshot': {'name': 'Clinical review session', 'definition': {}},
                                       'start': start, 'end': end, 'participants': [{'id': str(uuid.uuid4()), 'person_id': p, 'start': start, 'end': end, 'overtime': False}]})
                        # As the ledger writes them: each measure plain and per duty type, clinic and place.
                        for key, value in (('duties', 1.0), ('hours', 8.0), ('days_worked', 1.0)):
                            for suffix in ('', '|type:' + seed['type'], '|service:' + seed['services'][0], '|location:' + place):
                                ledger.append({'publication_id': pub_id, 'person_id': p, 'metric_key': key + suffix, 'bucket': day, 'value': value})
                        if n < 30:
                            ledger.append({'publication_id': pub_id, 'person_id': p, 'other_person_id': people[n + 1], 'metric_key': 'pair',
                                           'bucket': day, 'value': 1.0})
                day += timedelta(days=1)
            db.add(m.Publication(id=pub_id, department_id=seed['department_id'], workspace_id=w.id, sequence=k + 1, starts_on=first, ends_on=last,
                                 current=True, published_by=coordinator, fingerprint='0' * 64, validation={'score': score, 'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}},
                                 snapshot={'timezone': 'Asia/Riyadh', 'shifts': shifts, 'services': {seed['services'][0]: {'name': 'Continuity clinic'}},
                                           'locations': {seed['locations'][0]: {'name': 'North consulting room'}, seed['locations'][1]: {'name': 'South consulting room'}}}))
            if k == 11:
                # Current goal audiences need the latest published rota's actual sessions and qualification facts.
                db.execute(insert(m.Shift), [{'id': shift['id'], 'workspace_id': w.id,
                    'assignment_type_id': seed['type'], 'type_revision': 1, 'type_snapshot': shift['type_snapshot'],
                    'location_id': shift['location_id'], 'start': aware(shift['start']), 'end': aware(shift['end'])}
                    for shift in shifts])
                db.execute(insert(m.ShiftService), [{'shift_id': shift['id'], 'service_id': seed['services'][0]}
                    for shift in shifts])
                db.execute(insert(m.Assignment), [{'id': p['id'], 'shift_id': shift['id'], 'person_id': p['person_id'],
                    'start': aware(p['start']), 'end': aware(p['end'])} for shift in shifts for p in shift['participants']])
            w.status = 'published'
            db.flush()
        for chunk in (ledger[k:k + 20000] for k in range(0, len(ledger), 20000)):
            db.execute(insert(m.FairnessLedger), [{'department_id': seed['department_id'], 'other_person_id': None, 'expected': None, **r} for r in chunk])
    return people, len(ledger)


def reads_stay_fast(h):
    people, rows = big_department(h)
    period = '&starts_on=2025-10-01&ends_on=2026-10-01'
    north = h.seed['locations'][0]
    chosen = json.dumps({'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': people[:8]}]})
    timings, wall_timings, results = {}, {}, {}
    for name, path in (('distribution', '/api/fairness/distribution?metric=duties&group_by=category_ids' + period),
                       ('distribution_tags', '/api/fairness/distribution?metric=hours&group_by=tags' + period),
                       ('by_clinic', '/api/fairness/distribution?metric=hours&group_by=service_ids' + period),
                       ('by_place', '/api/fairness/distribution?metric=duties&group_by=location_ids' + period),
                       ('at_place', f'/api/fairness/distribution?metric=duties&location_ids={north}' + period),
                       ('weekends_by_type', '/api/fairness/distribution?metric=weekends&group_by=assignment_type_ids' + period),
                       ('person_trends', '/api/fairness/distribution?metric=duties&who=' + chosen + period),
                       ('exposure', '/api/fairness/exposure?' + period[1:]),
                       ('goals', '/api/fairness/goals?' + period[1:]),
                       ('sessions', f'/api/fairness/sessions?person_id={people[0]}' + period)):
        started, wall_started = time.process_time(), time.perf_counter()
        result = h.get('coordinator', path)
        timings[name] = round(time.process_time() - started, 2)
        wall_timings[name] = round(time.perf_counter() - wall_started, 2)
        results[name] = result
    print('fairness dashboard timings', timings, {'wall_seconds': wall_timings, 'ledger_rows': rows})
    return people, results, timings, wall_timings


@pytest.fixture(scope="module")
def sqlite_fairness_reads(scale_harness):
    return reads_stay_fast(scale_harness())


@pytest.fixture(scope="module")
def postgres_fairness_reads(scale_harness):
    return reads_stay_fast(scale_harness('pg'))


def assert_department_reads(measurement):
    people, results, _, _ = measurement
    for name, result in results.items():
        if name == 'distribution':
            assert len(result['people']) == 153 and sum(g['people'] for g in result['groups']) == 153 and len(result['months']) == 12
        if name == 'by_clinic':
            [clinic] = result['groups']
            assert clinic['value'] == sum(r['value'] for r in result['people']) and result['group_kind'] == 'work'
        if name == 'by_place':
            assert [g['label'] for g in result['groups']] == ['North consulting room', 'South consulting room']
            assert sum(g['value'] for g in result['groups']) == sum(r['value'] for r in result['people'])
        if name == 'at_place':
            assert {r['person_id'] for r in result['people'] if r['value']} == set(people[::2])
        if name == 'person_trends':
            assert [r['person_id'] for r in result['people']] == sorted(people[:8], key=lambda p: people.index(p)) and len(result['months']) == 12
        if name == 'exposure':
            assert len(result['rows']) == len(result['cols']) == 153 and sum(map(sum, result['cells'])) > 0
        if name == 'goals':
            [duties] = [goal for goal in result['goals'] if goal['metric'] == 'duties']
            assert len(result['score_trend']) == 12 and len(duties['people']) == 153
        if name == 'sessions':
            assert len(result['publications']) == 12 and result['total'] == sum(p['value'] for p in result['publications'])


def test_big_department_reads_stay_fast(sqlite_fairness_reads):
    assert_department_reads(sqlite_fairness_reads)


@pytest.mark.cpu_budget
def test_big_department_reads_stay_fast_cpu_budget(sqlite_fairness_reads):
    _, _, timings, _ = sqlite_fairness_reads
    for name, elapsed in timings.items():
        assert elapsed < 5, (name, timings)


@pytest.mark.wall_benchmark
def test_big_department_reads_wall_benchmark(sqlite_fairness_reads):
    _, _, _, timings = sqlite_fairness_reads
    for name, elapsed in timings.items():
        assert elapsed < 5, (name, timings)


@pytest.mark.postgres
def test_big_department_reads_stay_fast_on_postgres(postgres_fairness_reads):
    assert_department_reads(postgres_fairness_reads)


@pytest.mark.postgres
@pytest.mark.cpu_budget
def test_big_department_reads_stay_fast_on_postgres_cpu_budget(postgres_fairness_reads):
    _, _, timings, _ = postgres_fairness_reads
    for name, elapsed in timings.items():
        assert elapsed < 5, (name, timings)


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_big_department_reads_postgres_wall_benchmark(postgres_fairness_reads):
    _, _, _, timings = postgres_fairness_reads
    for name, elapsed in timings.items():
        assert elapsed < 5, (name, timings)


def test_goal_audience_label_uses_the_configured_condition_group():
    from packages.domain.fairness import _goal_audience_label
    data = {'categories': {'consultant': {'name': 'Consultants'}}}
    policy = {'definition': {'scope': {'conditions': {'match': 'all', 'conditions': [
        {'field': 'category_ids', 'op': 'any', 'values': ['consultant']}]}}}}
    assert _goal_audience_label(data, policy) == 'Consultants'
