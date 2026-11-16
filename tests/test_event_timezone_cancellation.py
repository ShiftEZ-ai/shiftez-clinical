"""Cancellation retains recorded event timing after the organisation timezone changes."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.cp_sat import generate
from packages.domain.time import aware
from packages.domain.validation import evaluate
from .test_event_horizon import horizon, projected
from .test_events import definition, event, event_type, input_of, input_revisions, occurrences, repeat, value


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('scope', ['series', 'occurrence'])
@pytest.mark.parametrize('start,end', [('09:00', '10:00'), ('00:30', '01:30')])
def test_cancel_after_timezone_change_releases_protection_without_reinterpreting_occurrences(
        request, backend, scope, start, end):
    h = request.getfixturevalue(backend)
    horizon(h, 14)
    blocked, available = h.seed['people'][:2]
    typ = event_type(h, starts_on='2026-10-04', blocks_duties=True, rsvp=True,
                     default_audience={'person_ids': [blocked]})
    row = event(h, typ, start=f'2026-10-04T{start}:00+03:00', end=f'2026-10-04T{end}:00+03:00',
        repeat=repeat(days=[{'weekday': 6, 'start_time': start, 'end_time': end}]), repeat_until='2026-10-26')
    original = occurrences(h)
    assert len(original) == 2
    target = original[0]
    receipt = h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Retain attendance evidence'},
                        target['id'], target['revision'])['receipt']
    shift_start, shift_end = ('08:00', '12:00') if start == '09:00' else ('00:00', '04:00')
    w = h.shift(h.workspace(start='2026-10-03', end='2026-10-19'),
        start=f'2026-10-04T{shift_start}:00+03:00', end=f'2026-10-04T{shift_end}:00+03:00',
        people=[blocked], seed_restricted=True)['workspace']
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    h.command('admin', 'department.settings', {'department_name': dept['name'],
        'department_revision': dept['revision'], 'organisation_name': org['name'],
        'timezone': 'UTC', 'week_start': org['week_start'], 'reason': 'Change the organisation timezone'},
        dept['id'], org['revision'])

    def check(protected):
        data = h.snapshot(w)
        assert (target['id'] in {fact.get('event_occurrence_id') for fact in data['scheduling_facts']}) == protected
        assert any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked
                   for f in evaluate(data)['findings']) == protected
        data['shifts'][0]['participants'] = []
        data['people'] = {pid: data['people'][pid] for pid in ((blocked, available) if protected else (blocked,))}
        result = generate(data, 5, deterministic=True)
        assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
        assert result['additions'] and {item['participant']['person_id'] for item in result['additions']} == {
            available if protected else blocked}

    check(True)
    assert len(projected(h, '2026-10-18T00:00:00+03:00', '2026-10-19T00:00:00+03:00')) == 1
    before_revision = input_revisions(h)
    current = definition(h, row)
    if scope == 'series':
        h.configure('events', input_of(current, cancelled=True), row['id'], current['revision'], role='coordinator')
    else:
        h.command('coordinator', 'event.occurrence.update', {'value': input_of(target['data'], cancelled=True),
            'reason': 'Cancel the stored occurrence'}, target['id'], target['revision'])
    assert input_revisions(h) == tuple(revision + 1 for revision in before_revision)
    check(False)
    after = {item['id']: item for item in occurrences(h)}
    assert set(after) == {item['id'] for item in original}
    for previous in original:
        cancelled = after[previous['id']]
        assert cancelled['cancelled'] == (scope == 'series' or previous['id'] == target['id'])
        assert cancelled['data']['timezone'] == previous['data']['timezone'] == 'Asia/Riyadh'
        assert cancelled['data']['type'] == previous['data']['type']
        assert all(aware(cancelled[key]) == aware(previous[key]) for key in ('original_start', 'start', 'end'))
    remaining = projected(h, '2026-10-18T00:00:00+03:00', '2026-10-19T00:00:00+03:00')
    assert len(remaining) == int(scope == 'occurrence'), 'Series cancellation also releases unmaterialised protection'
    with transaction(h.app.state.engine) as db:
        retained = db.scalar(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.id == receipt['id']))
        assert retained.response == 'going' and retained.event_occurrence_id == target['id']

    if scope == 'series' and start == '09:00':
        current = definition(h, row)
        # Cancellation cannot reinterpret an old recurrence in a newly supplied timezone.
        rejected = h.configure('events', input_of(current, repeat=repeat(timezone='UTC')), row['id'],
            current['revision'], role='coordinator', status=422)
        assert rejected['error']['code'] == 'EVENT_TIMEZONE'
        rejected = h.configure('events', input_of(current, cancelled=False,
            start='2026-10-04T10:00:00+03:00', end='2026-10-04T11:00:00+03:00'), row['id'],
            current['revision'], role='coordinator', status=422)
        assert rejected['error']['code'] == 'EVENT_TIMEZONE'
        rejected = h.configure('events', value(typ, cancelled=True, repeat=repeat()),
            role='coordinator', status=422)
        assert rejected['error']['code'] == 'EVENT_TIMEZONE'
