"""Fresh rota checks protect the remaining time of a recurring event."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import aware
from .clock_support import freeze_clock
from .test_assignment_override_reason import payload
from .test_event_horizon import horizon
from .test_events import definition, event, event_type, input_of, occurrences, repeat


BACKENDS = ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)]
REGULAR_DAY = {'weekday': 6, 'start_time': '09:00', 'end_time': '10:00'}
ADDED_DAY = {'weekday': 0, 'start_time': '09:00', 'end_time': '14:00'}


def local(day, time):
    return f'{day}T{time}:00+03:00'


def rota(h, day, start, end, people):
    next_day = date.fromisoformat(day) + timedelta(days=1)
    workspace = h.workspace(start=day, end=next_day.isoformat())
    return h.shift(workspace, local(day, start), local(day, end), people=people)['workspace']


def freeze_for_check(h, monkeypatch, now):
    h.clients.pop('coordinator').close()
    freeze_clock(monkeypatch, aware(now))


def event_findings(findings, person):
    return [row for row in findings if row['code'] == 'RESTRICTION' and row.get('person_id') == person
            and (row.get('rule_id') or '').startswith('event:')]


def prepare_series(h, monkeypatch, case, person):
    typ = event_type(h, blocks_duties=True, default_audience={'person_ids': [person]})
    if case in ('missing-overnight', 'ended'):
        horizon(h, 7)
        original_start = local('2026-10-18', '23:00')
        series = event(h, typ, start=local('2026-10-04', '23:00'), end=local('2026-10-05', '04:00'),
                       repeat=repeat(days=[{'weekday': 6, 'start_time': '23:00', 'end_time': '04:00'}]))
        now = local('2026-10-19', '05:00' if case == 'ended' else '00:00')
        freeze_for_check(h, monkeypatch, now)
    else:
        day = '2026-10-05'
        original_start = local(day, '09:00')
        schedule = repeat(days=[REGULAR_DAY, ADDED_DAY])
        series = event(h, typ, repeat=repeat() if case == 'edited' else schedule)
        if case in ('cancelled', 'moved'):
            [original] = occurrences(h, start=day, end='2026-10-06')
            changes = ({'cancelled': True} if case == 'cancelled' else
                       {'start': local(day, '16:00'), 'end': local(day, '17:00')})
            h.command('coordinator', 'event.occurrence.update',
                      {'value': input_of(original['data'], **changes), 'reason': 'Adjust one synthetic occurrence'},
                      original['id'], original['revision'])
        freeze_for_check(h, monkeypatch, local(day, '10:00'))
        if case == 'edited':
            current = definition(h, series)
            h.configure('events', input_of(current, repeat=schedule), series['id'], current['revision'],
                        role='coordinator')

    with transaction(h.app.state.engine) as db:
        rows = db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == series['id']))
        original = next((row for row in rows if aware(row.original_start) == aware(original_start)), None)
        if case in ('edited', 'missing-overnight', 'ended'):
            assert original is None
        else:
            assert original is not None
            assert original.cancelled is (case == 'cancelled')
            assert original.overridden is (case in ('cancelled', 'moved'))
            assert (aware(original.start) != aware(original_start)) is (case == 'moved')


@pytest.mark.parametrize('h', BACKENDS, indirect=True)
@pytest.mark.parametrize('case,day,start,end,blocked', [
    pytest.param('edited', '2026-10-05', '11:00', '12:00', True, id='edited'),
    pytest.param('missing-overnight', '2026-10-19', '01:00', '02:00', True, id='missing-overnight'),
    pytest.param('stored', '2026-10-05', '11:00', '12:00', True, id='stored'),
    pytest.param('ended', '2026-10-19', '05:00', '06:00', False, id='ended'),
    pytest.param('cancelled', '2026-10-05', '11:00', '12:00', False, id='cancelled'),
    pytest.param('moved', '2026-10-05', '11:00', '12:00', False, id='moved'),
])
def test_fresh_draft_check_for_ongoing_occurrence(h, monkeypatch, case, day, start, end, blocked):
    freeze_clock(monkeypatch, aware(local('2026-10-03', '09:00')))
    person = h.seed['people'][0]
    workspace = rota(h, day, start, end, [person])
    if case == 'ended':
        # A fresh check of the elapsed part of this draft must also preserve unrecorded history.
        workspace = h.shift(workspace, local(day, '01:00'), local(day, '02:00'), people=[person])['workspace']
    prepare_series(h, monkeypatch, case, person)

    checked = h.lifecycle(workspace, 'validate')['validation']

    assert bool(event_findings(checked['findings'], person)) is blocked


@pytest.mark.parametrize('h', BACKENDS, indirect=True)
@pytest.mark.parametrize('case', ['edited', 'stored'])
def test_manual_placement_refuses_ongoing_occurrence(h, monkeypatch, case):
    freeze_clock(monkeypatch, aware(local('2026-10-03', '09:00')))
    day = '2026-10-05'
    workspace = rota(h, day, '11:00', '12:00', [])
    person = h.seed['people'][0]
    prepare_series(h, monkeypatch, case, person)
    [shift] = h.detail(workspace)['shifts']
    update = payload(h, workspace, shift, start=shift['start'], end=shift['end'], people=[person])

    refusal = h.command('coordinator', 'shift.update', update, revision=workspace['revision'], status=422)

    assert refusal['error']['code'] == 'EDIT_INVALID'
    assert event_findings(refusal['error']['details']['findings'], person)
