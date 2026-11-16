"""Event picker names stay private, current and bounded; ordinary inbox reads stay distinct from acknowledgement."""
import pytest
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from packages.contracts.schemas import ROLE_NAMES
from packages.domain.time import aware
from .governance_support import identity
from .test_event_delivery import notes, own_receipt, scheduled
from .test_events import definition, event, event_type, input_of


BACKENDS = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('role', ROLE_NAMES)
def test_event_tag_choices_expose_only_editor_picker_names(request, backend, role):
    h = request.getfixturevalue(backend)
    row = event(h, heading='Private audience meeting', description='Private synthetic agenda',
        audience={'roles': ['coordinator']}, format='online', place='', url='https://example.test/private-meeting')
    if role not in h.seed['identities']:
        identity(h, role, [role])
    if role in {'staff', 'operator'}:
        for path in ('/api/catalogues', '/api/catalogues?boot=true', '/api/catalogues?known='):
            assert h.get(role, path, status=403)['error']['code'] == 'FORBIDDEN'
        return
    full = h.get(role, '/api/catalogues')
    assert full['events'] == ([{'id': row['id'], 'name': row['heading'], 'active': True}]
        if role in {'admin', 'coordinator'} else [])
    boot = h.get(role, '/api/catalogues?boot=true')
    assert 'events' not in boot and 'events' not in boot['_versions']


@pytest.mark.parametrize('backend', BACKENDS)
def test_event_tag_choice_delta_refreshes_renames_and_keeps_cancelled_member_names(request, backend):
    h = request.getfixturevalue(backend)
    tag = h.configure('tags', {'name': 'Meeting picker label', 'labels': ['events']})['record']
    row = event(h, tag_ids=[tag['id']])
    initial = h.get('coordinator', '/api/catalogues')
    unchanged = h.get('coordinator', '/api/catalogues?known=' + ','.join(initial['_versions'].values()))
    assert 'events' not in unchanged
    current = definition(h, row)
    row = h.configure('events', input_of(current, heading='Renamed meeting picker label'),
        row['id'], current['revision'], role='coordinator')['record']
    renamed = h.get('coordinator', '/api/catalogues?known=' + ','.join(initial['_versions'].values()))
    assert renamed['events'] == [{'id': row['id'], 'name': row['heading'], 'active': True}]
    assert renamed['_versions']['events'] != initial['_versions']['events']
    current = definition(h, row)
    h.configure('events', input_of(current, cancelled=True), row['id'], current['revision'], role='coordinator')
    cancelled = h.get('coordinator', '/api/catalogues?known=' + ','.join(renamed['_versions'].values()))
    assert cancelled['events'] == [{'id': row['id'], 'name': row['heading'], 'active': False}]
    assert cancelled['_versions']['events'] != renamed['_versions']['events']
    assert definition(h, row)['tag_ids'] == [tag['id']]
    assert any(member['tag_id'] == tag['id'] and member['kind'] == 'events' and member['member_id'] == row['id']
        for member in h.get('coordinator', '/api/catalogues')['tag_members'])


@pytest.mark.parametrize('backend', BACKENDS)
def test_event_tag_choices_refuse_an_over_limit_catalogue_instead_of_silently_truncating(request, backend):
    h = request.getfixturevalue(backend)
    typ = event_type(h, notify_inbox=False)
    common = {'department_id': h.seed['department_id'], 'event_type_id': typ['id'],
        'start': aware('2026-10-04T09:00:00+03:00'), 'end': aware('2026-10-04T10:00:00+03:00')}
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Event), [{**common, 'heading': f'Synthetic event choice {index:04}'} for index in range(2000)])
    choices = h.get('coordinator', '/api/catalogues')['events']
    assert len(choices) == 2000 and all(set(row) == {'id', 'name', 'active'} for row in choices)
    # Cross the cached boundary through the real writer, which advances the catalogue revision.
    event(h, typ, heading='Synthetic event choice over the limit')
    assert h.get('coordinator', '/api/catalogues', status=409)['error']['code'] == 'CATALOGUE_LIMIT'
    assert 'events' not in h.get('coordinator', '/api/catalogues?boot=true')


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('kind', ['notification.ack', 'notification.ack_all'])
def test_default_event_reads_are_idempotent_and_acknowledgement_still_requires_a_reason(request, backend, kind):
    h = request.getfixturevalue(backend)
    _, occurrences = scheduled(h, attendance='must_acknowledge')
    process_one(h.app.state.engine)
    original = notes(h)[0]
    target = original['id'] if kind == 'notification.ack' else None
    result = h.command('staff', kind, target=target, revision=original['revision'] if target else None)
    if not target:
        assert result['read'] == result['acknowledged'] == 1
    saved, receipt = notes(h)[0], own_receipt(h, occurrences[0])
    assert saved['read_at'] and receipt['read_at']
    assert saved['acknowledged_at'] is None and receipt['acknowledged_at'] is None
    repeated = h.command('staff', kind, {}, target=target, revision=saved['revision'] if target else None)
    if not target:
        assert repeated['read'] == repeated['acknowledged'] == 0
    assert notes(h)[0] == saved and own_receipt(h, occurrences[0]) == receipt
    invalid = h.command('staff', 'notification.acknowledge', target=saved['id'], revision=saved['revision'], status=422)
    assert invalid['error']['code'] == 'SCHEMA_INVALID'
    assert own_receipt(h, occurrences[0]) == receipt
    h.command('staff', 'notification.acknowledge', {'reason': 'Explicit synthetic event acknowledgement'},
        saved['id'], saved['revision'])
    acknowledged = own_receipt(h, occurrences[0])
    assert acknowledged['acknowledged_at'] and notes(h)[0]['acknowledged_at']
    assert acknowledged['read_at'] == receipt['read_at']
