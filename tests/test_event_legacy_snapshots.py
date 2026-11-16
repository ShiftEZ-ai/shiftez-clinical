"""Historical event types remain readable even when current creation rules are stricter."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate
from .test_event_horizon import horizon
from .test_events import definition, event, event_type, input_of, occurrences, repeat


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('cancel_scope', ['series', 'occurrence'])
def test_legacy_optional_snapshot_survives_validation_scheduling_and_cancellation(request, backend, cancel_scope):
    h = request.getfixturevalue(backend)
    blocked, available = h.seed['people'][:2]
    horizon(h, 7)
    typ = event_type(h, blocks_duties=True, default_audience={'person_ids': [blocked]})
    row = event(h, typ, repeat=repeat())
    [original] = occurrences(h, end='2026-10-10')
    with transaction(h.app.state.engine, write=True) as db:
        master = db.get(m.Event, row['id'])
        legacy = {**master.occurrence_data['type'], 'attendance': 'optional', 'blocks_duties': True, 'rsvp': False}
        master.occurrence_data = {**master.occurrence_data, 'type': legacy}
        for occurrence in db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == row['id'])):
            occurrence.data = {**occurrence.data, 'type': legacy}
    workspaces = []
    for day, end in [('2026-10-04', '2026-10-05'), ('2026-10-18', '2026-10-19')]:
        workspace = h.shift(h.workspace(start=day, end=end), start=day+'T08:00:00+03:00',
            end=day+'T12:00:00+03:00', people=[blocked], seed_restricted=True)['workspace']
        assert any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked for f in workspace['validation']['findings'])
        snapshot = h.snapshot(workspace)
        assert any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked for f in evaluate(snapshot)['findings'])
        snapshot['shifts'][0]['participants'] = []
        snapshot['people'] = {pid: snapshot['people'][pid] for pid in (blocked, available)}
        result = generate(snapshot, 5, deterministic=True)
        assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
        assert result['additions'] and {item['participant']['person_id'] for item in result['additions']} == {available}
        workspaces.append(workspace)
    h.configure('event_types', {key: typ[key] for key in s.EventTypeInput.model_fields} | {'active': False},
        typ['id'], typ['revision'])
    if cancel_scope == 'series':
        current = definition(h, row)
        h.configure('events', input_of(current, cancelled=True), row['id'], current['revision'], role='coordinator')
        cleared = workspaces
    else:
        [current] = occurrences(h, end='2026-10-10')
        h.command('coordinator', 'event.occurrence.update',
            {'value': input_of(current['data'], cancelled=True), 'reason': 'Cancel historical type snapshot'},
            current['id'], current['revision'])
        cleared = workspaces[:1]
    for workspace in cleared:
        assert not any(f['code'] == 'RESTRICTION' and f['person_id'] == blocked
            for f in evaluate(h.snapshot(workspace))['findings'])
    [cancelled] = occurrences(h, end='2026-10-10')
    assert cancelled['id'] == original['id'] and cancelled['cancelled']
    assert cancelled['data']['type'] == legacy
