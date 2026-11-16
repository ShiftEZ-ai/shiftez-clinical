"""Materialisation bounds do not remove meetings; archived types do not prevent cancellation."""
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.time import aware, local_midnight
from .test_event_horizon import horizon
from .test_events import definition, event, event_type, input_of, occurrences, repeat, value


BACKENDS = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('change', ['metadata', 'series_end', 'excluded_date', 'weekday', 'time'])
def test_shorter_horizon_cancels_only_actual_recurrence_removals(request, backend, change):
    h = request.getfixturevalue(backend)
    horizon(h, 28)
    row = event(h, repeat=repeat())
    original = {item['start'][:10]: item for item in occurrences(h)}
    assert set(original) == {'2026-10-04', '2026-10-11', '2026-10-18', '2026-10-25'}
    changes = {'heading': 'Updated retained meeting'}
    if change == 'series_end':
        changes['repeat_until'] = '2026-10-19'
    elif change == 'excluded_date':
        changes['repeat'] = repeat(excluded_dates=['2026-10-18'])
    elif change == 'weekday':
        changes.update(start='2026-10-05T09:00:00+03:00', end='2026-10-05T10:00:00+03:00',
            repeat=repeat(anchor='2026-10-05', days=[{'weekday': 0, 'start_time': '09:00', 'end_time': '10:00'}]))
    elif change == 'time':
        changes.update(start='2026-10-04T10:00:00+03:00', end='2026-10-04T11:00:00+03:00',
            repeat=repeat(days=[{'weekday': 6, 'start_time': '10:00', 'end_time': '11:00'}]))
    horizon(h, 7)
    current = definition(h, row)
    h.configure('events', input_of(current, **changes), row['id'], current['revision'], role='coordinator')
    after = {item['id']: item for item in occurrences(h)}
    assert {item['id'] for item in original.values()} <= set(after)
    for day in ('2026-10-18', '2026-10-25'):
        saved = after[original[day]['id']]
        removed = (change in {'weekday', 'time'} or change == 'series_end' and day == '2026-10-25'
            or change == 'excluded_date' and day == '2026-10-18')
        assert saved['cancelled'] is removed
        if not removed:
            assert saved['data']['heading'] == changes['heading']
            assert saved['original_start'] == original[day]['original_start']
    old_ids = {item['id'] for item in original.values()}
    assert all(aware(item['start']) < local_midnight(date(2026, 10, 10), 'Asia/Riyadh')
        for oid, item in after.items() if oid not in old_ids)
    assert len(after) == len(original) + int(change in {'weekday', 'time'})


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('scope', ['series', 'occurrence'])
@pytest.mark.parametrize('retirement', ['archived', 'effective_dates'])
def test_existing_event_cancels_with_saved_type_but_create_and_reopen_need_live_type(request, backend, scope, retirement):
    h = request.getfixturevalue(backend)
    typ = event_type(h, blocks_duties=True, rsvp=True, attendance='must_acknowledge')
    row = event(h, typ, repeat=repeat(), repeat_until='2026-10-19')
    original = occurrences(h)
    target = original[1]
    receipt = h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Keep attendance evidence'},
        target['id'], target['revision'])['receipt']
    live = {key: typ[key] for key in s.EventTypeInput.model_fields}
    live.update(name='Changed live type', attendance='optional', blocks_duties=False)
    if retirement == 'archived':
        live['active'] = False
    else:
        live['starts_on'] = '2026-10-20'
    h.configure('event_types', live, typ['id'], typ['revision'])
    # New definitions cannot smuggle an inactive/out-of-window type through a cancelled payload.
    rejected = h.configure('events', value(typ, cancelled=True), role='coordinator', status=422)
    assert rejected['error']['code'] == 'EVENT_TYPE_WINDOW'
    if scope == 'series':
        current = definition(h, row)
        rejected = h.configure('events', input_of(current, cancelled=True), row['id'],
            current['revision'] + 1, role='coordinator', status=409)
        assert rejected['error']['code'] == 'STALE_REVISION'
        h.configure('events', input_of(current, cancelled=True), row['id'], current['revision'], role='coordinator')
        changed = occurrences(h)
        assert all(item['cancelled'] for item in changed)
        for before, after in zip(original, changed):
            assert after['id'] == before['id'] and after['data']['type'] == before['data']['type']
        cancelled = definition(h, row)
        rejected = h.configure('events', input_of(cancelled, cancelled=False), row['id'],
            cancelled['revision'], role='coordinator', status=422)
        assert rejected['error']['code'] == 'EVENT_TYPE_WINDOW'
    else:
        payload = {'value': input_of(target['data'], cancelled=True), 'reason': 'Cancel retained occurrence'}
        rejected = h.command('coordinator', 'event.occurrence.update', payload,
            target['id'], target['revision'] + 1, status=409)
        assert rejected['error']['code'] == 'STALE_REVISION'
        cancelled = h.command('coordinator', 'event.occurrence.update', payload,
            target['id'], target['revision'])['occurrence']
        assert cancelled['cancelled'] and cancelled['data']['type'] == target['data']['type']
        rejected = h.command('coordinator', 'event.occurrence.update',
            {'value': input_of(cancelled['data'], cancelled=False), 'reason': 'Restore needs an active type'},
            target['id'], cancelled['revision'], status=422)
        assert rejected['error']['code'] == 'EVENT_TYPE_WINDOW'
    with transaction(h.app.state.engine) as db:
        retained = db.scalar(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.id == receipt['id']))
        assert retained.response == 'going' and retained.event_occurrence_id == target['id']
    inactive = event_type(h, active=False)
    current = definition(h, row)
    rejected = h.configure('events', input_of(current, event_type_id=inactive['id'], cancelled=True),
        row['id'], current['revision'], role='coordinator', status=422)
    assert rejected['error']['code'] == 'EVENT_TYPE_WINDOW'
