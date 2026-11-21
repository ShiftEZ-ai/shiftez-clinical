"""A prepared session edit must match the live inputs at its serialized writer."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import commands, models as m
from apps.api.db import transaction
from apps.api.services import planning
from packages.contracts.schemas import ParticipantInput, ShiftInput
from tests.clock_support import freeze_clock
from tests.governance_support import identity
from tests.test_events import event, event_type, repeat
from tests.test_leave_types import absence


START = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)


def edit_payload(workspace, shift, **changes):
    return {**{key: shift[key] for key in ShiftInput.model_fields if key in shift},
            'workspace_id': workspace['id'], 'shift_id': shift['id'],
            'participants': [{key: person[key] for key in ParticipantInput.model_fields if key in person}
                             for person in shift['participants']], **changes}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('case', ['C1', 'C2', 'C3', 'C4'])
def test_shift_update_checks_writer_currency(h, monkeypatch, record_property, case):
    engine = h.app.state.engine
    person = h.seed['people'][0]
    before_start = START - timedelta(seconds=1)
    freeze_clock(monkeypatch, START - timedelta(days=30) if case == 'C3' else before_start)
    duty_type = h.seed['type']
    if case == 'C3':
        duty_type = h.configure('assignment_types', {'name': 'Duty with editable detail', 'starts_on': '2020-01-01',
            'definition': {'fields': [{'key': 'caption', 'label': 'Caption', 'kind': 'text'}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=duty_type)['workspace']
    event_id = None
    if case == 'C3':
        with transaction(engine, write=True) as db:
            db.get(m.Department, h.seed['department_id']).event_horizon_days = 7
        typ = event_type(h, blocks_duties=True, default_audience={'person_ids': [person]})
        event_id = event(h, typ, repeat=repeat())['id']
        h.clients.pop('coordinator').close()
        freeze_clock(monkeypatch, before_start)
    coordinator = h.client('coordinator')
    if case == 'C1':
        h.client('approver')
    other = 'other-coordinator'
    if case == 'C4':
        identity(h, other, ['coordinator'])
        h.client(other)

    detail = h.detail(workspace)
    workspace, shift = detail['workspace'], detail['shifts'][0]
    changes = {'attributes': {'caption': 'Edited'}} if case == 'C3' else {'location_id': h.seed['locations'][1]}
    payload = edit_payload(workspace, shift, **changes)
    key = str(uuid4())

    def current():
        with transaction(engine) as db:
            row = db.get(m.Workspace, workspace['id'])
            session = db.get(m.Shift, shift['id'])
            if event_id is not None:
                assert db.scalar(select(m.EventOccurrence.id).where(m.EventOccurrence.event_id == event_id)) is None
            stored = row.validation
            fresh = planning.revalidate(db, row, store=False)
            revision = row.revision, session.revision, session.location_id, session.attributes
            return revision, stored, fresh

    retry_write = commands.retry_write
    before_writer = None
    injected = False

    def at_writer(engine, operation, *args, **kwargs):
        nonlocal before_writer, injected
        if not injected and (kwargs.get('context') or {}).get('command_kind') == 'shift.update':
            injected = True
            if case == 'C1':
                assert h.policy({'kind': 'restriction'}, {'person_id': person})['status'] == 'active'
            elif case == 'C2':
                assert absence(h, person_id=person)['kind'] == 'recorded_leave'
            elif case == 'C3':
                assert any(row['code'] == 'RESTRICTION' for row in current()[2]['findings'])
                freeze_clock(monkeypatch, START + timedelta(seconds=1))
            else:
                h.command(other, 'shift.update', edit_payload(workspace, shift, service_ids=[h.seed['services'][1]]),
                          revision=workspace['revision'])
            before_writer = current()
        return retry_write(engine, operation, *args, **kwargs)

    monkeypatch.setattr(commands, 'retry_write', at_writer)
    reply = coordinator.post('/api/commands', headers={'idempotency-key': key}, json={
        'kind': 'shift.update', 'payload': payload, 'expected_revision': workspace['revision']})
    assert before_writer is not None
    revision, stored, fresh = current()
    error = reply.json().get('error', {}).get('code')
    saved = reply.status_code == 200
    record_property('currency', f'{case},{engine.dialect.name},{reply.status_code},{error or "saved"}')
    with transaction(engine) as db:
        receipt = db.scalar(select(m.CommandReceipt.id).where(m.CommandReceipt.key == key))
        assert bool(receipt) == saved

    if case == 'C3':
        assert all(row['code'] != 'RESTRICTION' for row in before_writer[2]['findings'])
        if saved:
            assert stored['findings'] == fresh['findings']
            assert stored['fingerprint'] == fresh['fingerprint']
            return
    assert reply.status_code == 409
    assert error == ('STALE_REVISION' if case == 'C4' else 'STALE_INPUTS')
    assert revision == before_writer[0]
    assert stored == before_writer[1]
