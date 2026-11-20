"""Run the old and current greedy trial paths on the same database-backed world."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from packages.domain import generation
from packages.domain.score import schedule_score
from packages.domain.validation import _workload_windows, workload_windows
from tests import greedy_reference_trials
from tests.scheduler_departments import START, apply, department
from tests.test_governed_leadership import activate, line, proposal
from tests.test_series_reconciliation import materialise, template


def dated_leave_quota(h, monkeypatch):
    # Leave is drawn in person order. Give the seed people the same order on both databases.
    snapshot = h.snapshot

    def ordered_snapshot(workspace):
        data = snapshot(workspace)
        data['people'] = {pid: data['people'][pid] for pid in h.seed['people']}
        return data

    monkeypatch.setattr(h, 'snapshot', ordered_snapshot)
    data, info = department(h, 3, days=4, people=11, clinics=2, leave_rate=0.12)
    start = (START + timedelta(days=1)).isoformat()
    end = (START + timedelta(days=4)).isoformat()
    zero = h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'},
                    {'person_id': h.seed['people'][1], 'assignment_type_id': info['types']['am']})
    data['policies'].append({**zero, 'starts_on': start, 'ends_on': end})
    data['policies'].append(h.policy({'kind': 'block_quota', 'starts_on': start, 'ends_on': end, 'metric': 'duties',
                                     'comparison': 'minimum', 'amount': 1},
                                    {'person_id': h.seed['people'][0], 'assignment_type_id': info['types']['night']}))
    assert data['availability'] and info['trainees']
    return data


def duty_type(h, name, minimum=1, **definition):
    return h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': minimum, 'maximum': max(1, minimum)}], **definition}})['record']['id']


def editable_displacement(h, _monkeypatch):
    person = h.seed['people'][0]
    optional = duty_type(h, 'Optional duty', minimum=0)
    required = duty_type(h, 'Required duty')
    w = h.shift(h.workspace(), typ=optional, people=[person])['workspace']
    w = h.shift(w, '2026-10-04T09:00:00+03:00', '2026-10-04T13:00:00+03:00',
                typ=required, people=[], service=1, location=1)['workspace']
    data = h.snapshot(w)
    data['generation_options']['mode'] = 'rebalance'
    destination = next(s for s in data['shifts'] if s['assignment_type_id'] == required)
    data['planning_population'] = {destination['id']: [person]}
    return data


def team_lead(h, _monkeypatch):
    leader = h.seed['people'][0]
    typ = duty_type(h, 'Duty with governed leadership', minimum=2)
    activate(h, proposal(h, [line(typ, [leader])]))
    w = h.shift(h.workspace(), typ=typ, people=[leader])['workspace']
    return h.snapshot(w)


def repeating_sessions(h, _monkeypatch):
    typ = duty_type(h, 'Repeating staffed duty', minimum=2)
    series = template(h, assignment_type_id=typ, definition={
        'anchor': '2026-10-04',
        'days': [{'weekday': day, 'start_time': '08:00', 'end_time': '12:00'} for day in (6, 0)],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [h.seed['people'][0]],
        'usual_assignees_fixed': False, 'close_when_all_usual_unavailable': True,
    })
    data = h.snapshot(materialise(h, series))
    assert len(data['shifts']) >= 2 and all(s['series_id'] == series['id'] and s['participants'] for s in data['shifts'])
    return data


def pooled_cover(h, _monkeypatch):
    services = h.seed['services'][:2]
    h.policy({'kind': 'minimum_cover', 'minimum': 1, 'aggregation': 'service_group'}, {'service_ids': services})
    h.policy({'kind': 'maximum_participants', 'maximum': 1, 'aggregation': 'service'}, {'service_ids': services[:1]})
    typ = duty_type(h, 'Pooled duty', minimum=0)
    w = h.workspace()
    for start, end, service in ((8, 10, 0), (9, 12, 1), (11, 13, 0)):
        w = h.shift(w, f'2026-10-04T{start:02}:00:00+03:00', f'2026-10-04T{end:02}:00:00+03:00',
                    typ=typ, people=[], service=service, location=service)['workspace']
    # An overlapping duty in the same clinic makes the service-wide cap relevant to a trial.
    w = h.shift(w, '2026-10-04T09:00:00+03:00', '2026-10-04T12:00:00+03:00',
                typ=typ, people=[], location=1)['workspace']
    return h.snapshot(w)


def named_supervision(h, _monkeypatch):
    supervisor, _, trainee = h.seed['people']
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2026-10-04'})
    typ = duty_type(h, 'Supervised duty', minimum=2, supervision={
        'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
        'trainees_per_supervisor': 1, 'require_pairing': True,
    })
    w = h.shift(h.workspace(), typ=typ, people=[supervisor])['workspace']
    data = h.snapshot(w)
    data['planning_population'] = {data['shifts'][0]['id']: [supervisor, trainee]}
    return data


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('world', [dated_leave_quota, editable_displacement, team_lead, repeating_sessions,
                                  pooled_cover, named_supervision], ids=lambda world: world.__name__)
def test_greedy_matches_reference(h, monkeypatch, world):
    data = world(h, monkeypatch)
    original = copy.deepcopy(data)
    monkeypatch.setattr(generation, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    with monkeypatch.context() as reference:
        reference.setattr(generation, 'Recheck', greedy_reference_trials.Recheck)
        reference.setattr(generation, 'own_duty_blocked', greedy_reference_trials.own_duty_blocked)
        old = generation.generate(data)
    actual = generation.generate(data)

    assert data == original
    assert actual['additions']
    assert actual['status'] == old['status']
    assert actual['additions'] == old['additions']
    assert actual['removals'] == old['removals']
    assert actual['validation']['findings'] == old['validation']['findings']
    assert schedule_score(apply(data, actual), actual['validation']) == schedule_score(apply(data, old), old['validation'])

    if world is editable_displacement:
        assert [r['reason'] for r in actual['removals']] == ['Moved to reduce uncovered duties']
    elif world is team_lead:
        assert [r['participant']['person_id'] for r in actual['additions'] if r['participant'].get('team_lead')] == [h.seed['people'][0]]
    elif world is named_supervision:
        assert [r['participant'].get('supervisor_person_id') for r in actual['additions']] == [h.seed['people'][0]]
    elif world is pooled_cover:
        assert actual['validation']['valid']


def test_workload_windows_uses_utc_cache_keys():
    zone = ZoneInfo('America/New_York')
    a = datetime(2026, 3, 7, 12, tzinfo=zone)
    b = a + timedelta(hours=4)
    utc_a, utc_b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    day = timedelta(days=1)
    rule = {'window': 'rolling', 'window_days': 1}

    _workload_windows.cache_clear()
    try:
        windows = workload_windows(a, b, rule, zone.key, 0)
        assert windows == {(utc_a, utc_a + day), (utc_b - day, utc_b)}
        assert workload_windows(utc_a, utc_b, rule, zone.key, 0) is windows
        assert workload_windows(utc_a + day, utc_b + day, rule, zone.key, 0) != windows
        info = _workload_windows.cache_info()
        assert (info.hits, info.misses, info.currsize) == (1, 2, 2)
    finally:
        _workload_windows.cache_clear()
