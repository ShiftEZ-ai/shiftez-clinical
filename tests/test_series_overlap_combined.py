"""Combined recurrence previews and atomic acknowledgement through both bulk writers."""
import json
import uuid

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from tests.test_leave_disclosure import setting
from tests.test_series_overlap_api import (
    MONDAYS, PRIVATE, REASON, absence, candidate, preview, reviewed, save, state, value_of,
)


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgresql'])
def series_harness(request):
    return request.getfixturevalue(request.param)


def pair(h, active=True, same_person=False):
    rows = []
    for index in range(2):
        value = candidate(h, 'Synthetic combined clinic ' + str(index), service=index)
        value['active'] = active
        value['definition']['default_person_ids'] = [h.seed['people'][0 if same_person else index]]
        rows.append(reviewed(h, value))
    return rows


def person_values(rows, person_id):
    values = [value_of(row) for row in rows]
    for value in values:
        value['definition']['default_person_ids'] = [person_id]
    return values


def pending(rows, values):
    return [{'series_id': row['id'], 'expected_revision': row['revision'], 'value': value}
            for row, value in zip(rows, values, strict=True)]


def combined(h, rows, values, role='admin', status=200):
    response = h.client(role).post('/api/series/preview', json={'items': pending(rows, values)})
    assert response.status_code == status, response.text
    return response.json()


def bulk_payload(rows, values, token=None):
    return {'item_kind': 'catalogue.update', 'reason': REASON,
        'items': [{'target_id': row['id'], 'expected_revision': row['revision'],
                   'payload': {'catalogue': 'recurring_series', 'value': value}}
                  for row, value in zip(rows, values, strict=True)],
        **({'overlap_acknowledgement': token} if token is not None else {})}


def stored(h, rows):
    ids = {row['id'] for row in rows}
    return {row['id']: row for row in h.get('admin', '/api/catalogues')['recurring_series'] if row['id'] in ids}


def expect_acknowledged(h, rows, token, count):
    with transaction(h.app.state.engine) as db:
        for row in rows:
            versions = list(db.scalars(select(m.DefinitionHistory).where(
                m.DefinitionHistory.object_id == row['id']).order_by(m.DefinitionHistory.object_revision)))
            evidence = versions[-1].snapshot
            assert evidence['overlap_acknowledged']
            assert evidence['overlap_conflict_token'] == token
            assert len(evidence['overlap_conflicts']) == count
            assert all(conflict['person_id'] == h.seed['people'][2]
                       for conflict in evidence['overlap_conflicts'])


def test_combined_assignee_change_is_previewed_acknowledged_audited_and_undoable(series_harness):
    h = series_harness
    rows = pair(h)
    values = person_values(rows, h.seed['people'][2])
    assert all(preview(h, value, row)['conflicts'] == []
               for row, value in zip(rows, values, strict=True))
    before, original = state(h), stored(h, rows)
    checked = combined(h, rows, values)
    assert state(h) == before and stored(h, rows) == original
    assert [(item['index'], item['series_id']) for item in checked['items']] == [
        (index, row['id']) for index, row in enumerate(rows)]
    for index, item in enumerate(checked['items']):
        assert len(item['occurrences']) == 4 and item['timezone']
        assert {conflict['date'] for conflict in item['conflicts']} == MONDAYS
        assert {conflict['other_series_id'] for conflict in item['conflicts']} == {rows[1 - index]['id']}
        assert all(conflict['person_id'] == h.seed['people'][2] for conflict in item['conflicts'])
        assert all({action['side']: action['target_id'] for action in conflict['cancel_actions']} == {
            'this': rows[index]['id'], 'other': rows[1 - index]['id']} for conflict in item['conflicts'])
    for token in (None, '0' * 64):
        refused = h.command('admin', 'bulk.apply', bulk_payload(rows, values, token), status=409)['error']
        assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
        assert refused['details'] == checked
        assert state(h) == before and stored(h, rows) == original
    tokenless = h.client('admin').post('/api/commands/preview', json={
        'kind': 'bulk.apply', 'payload': bulk_payload(rows, values)})
    assert tokenless.status_code == 200, tokenless.text
    tokenless = tokenless.json()
    for index, item in enumerate(tokenless['items']):
        assert item['results'][0]['overlap_conflicts'] == checked['items'][index]['conflicts']
    acknowledged = bulk_payload(rows, values, checked['conflict_token'])
    confirmed = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': acknowledged})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['source_fingerprint'] == tokenless['source_fingerprint']
    assert state(h) == before and stored(h, rows) == original
    response = h.client('admin').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
        'kind': 'bulk.apply', 'payload': acknowledged, 'expected_fingerprint': tokenless['source_fingerprint']})
    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied['count'] == 2 and applied['counts']['changed'] == 2
    assert all(row['definition']['default_person_ids'] == [h.seed['people'][2]]
               for row in stored(h, rows).values())
    expect_acknowledged(h, rows, checked['conflict_token'], 4)
    h.command('admin', 'bulk.apply', applied['undo']['payload'])
    assert {key: value_of(row) for key, row in stored(h, rows).items()} == {
        key: value_of(row) for key, row in original.items()}


def test_combined_preview_replaces_old_peers_when_people_swap(series_harness):
    h = series_harness
    rows = pair(h)
    values = [value_of(rows[0]), value_of(rows[1])]
    values[0]['definition']['default_person_ids'] = [h.seed['people'][1]]
    values[1]['definition']['default_person_ids'] = [h.seed['people'][0]]
    assert all(preview(h, value, row)['conflicts'] for row, value in zip(rows, values, strict=True))
    checked = combined(h, rows, values)
    assert all(item['conflicts'] == [] for item in checked['items'])
    applied = h.command('admin', 'bulk.apply', bulk_payload(rows, values))
    assert applied['counts']['changed'] == 2
    after = stored(h, rows)
    assert [after[row['id']]['definition']['default_person_ids'] for row in rows] == [
        value['definition']['default_person_ids'] for value in values]


def test_combined_undo_restores_an_acknowledged_original_overlap(series_harness):
    h = series_harness
    rows = pair(h, same_person=True)
    original = {row['id']: value_of(row) for row in rows}
    values = [value_of(row) for row in rows]
    for index, value in enumerate(values):
        value['definition']['default_person_ids'] = [h.seed['people'][index + 1]]
    assert all(item['conflicts'] == [] for item in combined(h, rows, values)['items'])
    applied = h.command('admin', 'bulk.apply', bulk_payload(rows, values))
    assert applied['undo']['payload']['overlap_acknowledgement']
    h.command('admin', 'bulk.apply', applied['undo']['payload'])
    restored = stored(h, rows)
    assert {key: value_of(row) for key, row in restored.items()} == original
    checked = combined(h, list(restored.values()), [value_of(row) for row in restored.values()])
    assert all(len(item['conflicts']) == 4 for item in checked['items'])
    with transaction(h.app.state.engine) as db:
        for row in rows:
            version = db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id'])
                                 .order_by(m.DefinitionHistory.object_revision.desc())).first()
            assert version.snapshot['overlap_acknowledged']
            assert len(version.snapshot['overlap_conflicts']) == 4


@pytest.mark.parametrize('change', ['definition', 'revision', 'subset', 'only', 'skip'])
def test_combined_acknowledgement_cannot_replay_for_another_pending_set(series_harness, change):
    h = series_harness
    rows = pair(h)
    peer = candidate(h, 'Synthetic persistent P3 peer')
    peer['definition']['default_person_ids'] = [h.seed['people'][2]]
    reviewed(h, peer)
    values = person_values(rows, h.seed['people'][2])
    checked = combined(h, rows, values)
    if change == 'definition':
        values[0]['definition']['days'][0]['start_time'] = '09:00'
    elif change == 'revision':
        changed = value_of(rows[0])
        changed['name'] += ' renamed'
        rows[0] = save(h, changed, rows[0])['record']
    elif change == 'subset':
        rows, values = rows[:1], values[:1]
    payload = bulk_payload(rows, values, checked['conflict_token'])
    expected_rows, expected_values = rows, values
    if change == 'only':
        payload['only'] = [0]
        expected_rows, expected_values = rows[:1], values[:1]
    elif change == 'skip':
        payload['resolutions'] = {'1': 'skip'}
        expected_rows, expected_values = rows[:1], values[:1]
    before, original = state(h), stored(h, rows)
    refused = h.command('admin', 'bulk.apply', payload, status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
    assert refused['details']['conflict_token'] != checked['conflict_token']
    assert refused['details'] == combined(h, expected_rows, expected_values)
    assert state(h) == before and stored(h, rows) == original


def test_catalogue_bulk_activation_requires_combined_acknowledgement_and_can_undo(series_harness):
    h = series_harness
    rows = pair(h, active=False, same_person=True)
    values = person_values(rows, h.seed['people'][2])
    # Persist the shared person while inactive; activation is the only proposed change.
    rows = [save(h, value, row)['record'] for row, value in zip(rows, values, strict=True)]
    values = [value_of(row) for row in rows]
    for value in values:
        value['active'] = True
    checked = combined(h, rows, values)
    assert all(len(item['conflicts']) == 4 for item in checked['items'])
    payload = {'catalogue': 'recurring_series', 'items': [
        {'id': row['id'], 'revision': row['revision']} for row in rows], 'active': True, 'reason': REASON}
    before, original = state(h), stored(h, rows)
    refused = h.command('admin', 'catalogue.bulk_update', payload, status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT' and refused['details'] == checked
    assert state(h) == before and stored(h, rows) == original
    applied = h.command('admin', 'catalogue.bulk_update', {
        **payload, 'overlap_acknowledgement': checked['conflict_token']})
    assert all(row['active'] for row in applied['records'])
    expect_acknowledged(h, rows, checked['conflict_token'], 4)
    h.command('admin', **applied['undo'])
    assert all(not row['active'] for row in stored(h, rows).values())


def test_legacy_single_preview_and_update_keep_the_original_contract(series_harness):
    h = series_harness
    peer = reviewed(h, candidate(h, 'Synthetic legacy peer'))
    value = candidate(h, 'Synthetic legacy pending', service=1)
    before = state(h)
    checked = preview(h, value)
    assert set(checked) == {'occurrences', 'conflicts', 'conflict_token', 'timezone'}
    assert {conflict['other_series_id'] for conflict in checked['conflicts']} == {peer['id']}
    assert state(h) == before
    created = save(h, value, token=checked['conflict_token'])['record']
    current = preview(h, value_of(created), created)
    single = combined(h, [created], [value_of(created)])
    assert single['items'] == [{key: value for key, value in current.items() if key != 'conflict_token'}
                               | {'index': 0, 'series_id': created['id']}]
    assert len(single['conflict_token']) == len(current['conflict_token']) == 64
    changed = value_of(created)
    changed['name'] += ' edited'
    checked = preview(h, changed, created)
    assert save(h, changed, created, checked['conflict_token'])['record']['name'] == changed['name']


@pytest.mark.parametrize('fixed', [None, False, True])
def test_single_saved_preview_token_matches_omitted_or_explicit_fixed_choice(series_harness, fixed):
    h = series_harness
    rows = pair(h, same_person=True)
    with transaction(h.app.state.engine, write=True) as db:
        legacy = db.get(m.RecurringSeries, rows[0]['id'])
        legacy.definition = {key: value for key, value in legacy.definition.items() if key != 'usual_assignees_fixed'}
    value = value_of(rows[0])
    value['definition'].pop('usual_assignees_fixed')
    if fixed is not None:
        value['definition']['usual_assignees_fixed'] = fixed
    value['name'] += ' timing-neutral edit'
    before = state(h)
    checked = preview(h, value, rows[0])
    assert set(checked) == {'occurrences', 'timezone', 'conflicts', 'conflict_token'}
    assert len(checked['conflicts']) == 4 and state(h) == before
    explicit = {**value, 'definition': {**value['definition'], 'usual_assignees_fixed': bool(fixed)}}
    assert preview(h, explicit, rows[0]) == checked
    saved = save(h, value, rows[0], checked['conflict_token'])['record']
    assert saved['name'] == value['name'] and saved['definition']['usual_assignees_fixed'] is bool(fixed)
    with transaction(h.app.state.engine) as db:
        version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == saved['id'],
            m.DefinitionHistory.object_revision == saved['revision']))
        assert version.snapshot['overlap_acknowledged']
        assert version.snapshot['overlap_conflict_token'] == checked['conflict_token']
        assert len(version.snapshot['overlap_conflicts']) == 4


def test_combined_preview_preserves_permissions_and_hidden_leave_wording(series_harness):
    h = series_harness
    rows = pair(h)
    absence(h)
    setting(h, 'none')
    h.client('coordinator')
    h.client('staff')
    values = person_values(rows, h.seed['people'][0])
    before = state(h)
    checked = combined(h, rows, values, role='coordinator')
    flags = [conflict for item in checked['items'] for conflict in item['conflicts']
             if conflict['code'] == 'UNAVAILABLE']
    assert len(flags) == 2 and all(flag['label'] == 'Unavailable' for flag in flags)
    assert PRIVATE not in json.dumps(checked) and state(h) == before
    combined(h, rows, values, role='staff', status=403)
    refused = h.command('coordinator', 'bulk.apply', bulk_payload(rows, values, checked['conflict_token']), status=409)
    assert refused['error']['code'] == 'FORBIDDEN'
    assert state(h) == before


@pytest.mark.parametrize('invalid', ['definition', 'attributes'])
def test_bulk_ready_only_keeps_malformed_series_diagnostics_and_saves_valid_rows(series_harness, invalid):
    h = series_harness
    rows = pair(h)
    values = [value_of(row) for row in rows]
    values[0]['name'] += ' valid edit'
    if invalid == 'definition':
        values[1]['definition'] = None
    else:
        values[1]['definition']['attributes'] = {'synthetic_unknown_attribute': True}
    payload = bulk_payload(rows, values)
    before, original = state(h), stored(h, rows)
    response = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert response.status_code == 200, response.text
    checked = response.json()
    assert [row['result'] for row in checked['items']] == ['ready', 'problem']
    assert checked['items'][1]['issues'][0]['code'] == (
        'SCHEMA_INVALID' if invalid == 'definition' else 'VALIDATION_ERROR')
    assert state(h) == before and stored(h, rows) == original
    applied = h.command('admin', 'bulk.apply', {**payload, 'apply_ready_only': True})
    assert applied['counts']['changed'] == 1 and applied['counts']['staged'] == 1
    after = stored(h, rows)
    assert after[rows[0]['id']]['name'] == values[0]['name']
    assert after[rows[1]['id']] == original[rows[1]['id']]


def test_combined_live_preview_batches_catalogue_reference_queries(series_harness):
    h = series_harness
    client = h.client('admin')

    def body(count):
        values = [candidate(h, 'Synthetic unsaved batch clinic ' + str(index)) for index in range(count)]
        for value in values:
            value['active'] = False
        return {'items': [{'value': value} for value in values]}

    def measure(count):
        queries = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith('SELECT'):
                queries.append(statement)

        event.listen(h.app.state.engine, 'before_cursor_execute', capture)
        try:
            response = client.post('/api/series/preview', json=body(count))
            assert response.status_code == 200, response.text
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', capture)
        checked = response.json()
        assert len(checked['items']) == count and all(not item['conflicts'] for item in checked['items'])
        return len(queries)

    warm = client.post('/api/series/preview', json=body(2))
    assert warm.status_code == 200, warm.text
    before = state(h)
    small, large = measure(2), measure(32)
    assert small > 0 and large <= small + 4, (small, large)
    assert state(h) == before


def test_combined_root_acknowledgement_supersedes_stale_per_item_tokens(series_harness):
    h = series_harness
    rows = pair(h)
    values = person_values(rows, h.seed['people'][2])
    checked = combined(h, rows, values)
    payload = bulk_payload(rows, values, checked['conflict_token'])
    for item, row, value in zip(payload['items'], rows, values, strict=True):
        legacy = preview(h, value, row)
        assert not legacy['conflicts']
        item['payload']['overlap_acknowledgement'] = legacy['conflict_token']
    applied = h.command('admin', 'bulk.apply', payload)
    assert applied['counts']['changed'] == 2
    expect_acknowledged(h, rows, checked['conflict_token'], 4)


def test_combined_preview_preserves_legacy_omitted_fixed_assignees_choice(series_harness):
    h = series_harness
    rows = pair(h)
    with transaction(h.app.state.engine, write=True) as db:
        legacy = db.get(m.RecurringSeries, rows[0]['id'])
        legacy.definition = {key: value for key, value in legacy.definition.items()
                             if key != 'usual_assignees_fixed'}
    values = person_values(rows, h.seed['people'][2])
    del values[0]['definition']['usual_assignees_fixed']
    peer = candidate(h, 'Synthetic fixed peer for legacy omission')
    peer['definition']['default_person_ids'] = [h.seed['people'][2]]
    reviewed(h, peer)
    before = state(h)
    checked = combined(h, rows, values)
    assert all(len(item['conflicts']) == 8 for item in checked['items'])
    explicit = [{**value, 'definition': dict(value['definition'])} for value in values]
    explicit[0]['definition']['usual_assignees_fixed'] = False
    assert combined(h, rows, explicit) == checked
    assert state(h) == before
    applied = h.command('admin', 'bulk.apply', bulk_payload(rows, values, checked['conflict_token']))
    assert applied['counts']['changed'] == 2
    after = stored(h, rows)
    assert after[rows[0]['id']]['definition']['usual_assignees_fixed'] is False
    assert after[rows[1]['id']]['definition']['usual_assignees_fixed'] is True
    with transaction(h.app.state.engine) as db:
        for row in rows:
            version = db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id'])
                                 .order_by(m.DefinitionHistory.object_revision.desc())).first()
            assert version.snapshot['overlap_conflict_token'] == checked['conflict_token']
            assert len(version.snapshot['overlap_conflicts']) == 8


def test_identical_pending_creates_keep_distinct_conflict_bindings_and_fixed_default(series_harness):
    h = series_harness
    h.client('admin')
    value = candidate(h, 'Synthetic identical pending clinics')
    del value['definition']['usual_assignees_fixed']
    before = state(h)
    response = h.client('admin').post('/api/series/preview', json={'items': [{'value': value}, {'value': value}]})
    assert response.status_code == 200, response.text
    checked = response.json()
    assert state(h) == before
    for index, item in enumerate(checked['items']):
        assert len(item['conflicts']) == 4
        assert {flag['other_shift_id'] for flag in item['conflicts']} == {
            f'series:preview-{1 - index}:{day}' for day in MONDAYS}
    applied = h.command('admin', 'bulk.apply', {'item_kind': 'catalogue.create', 'reason': REASON,
        'overlap_acknowledgement': checked['conflict_token'], 'items': [
            {'payload': {'catalogue': 'recurring_series', 'value': value}},
            {'payload': {'catalogue': 'recurring_series', 'value': value}}]})
    assert applied['counts']['changed'] == 2
    created = [item['results'][0] for item in applied['items']]
    assert created[0]['record']['id'] != created[1]['record']['id']
    for index, result in enumerate(created):
        assert result['record']['definition']['usual_assignees_fixed'] is True
        expected_peers = {flag['other_shift_id'] for flag in checked['items'][index]['conflicts']}
        assert {flag['other_shift_id'] for flag in result['overlap_conflicts']} == expected_peers
        with transaction(h.app.state.engine) as db:
            version = db.scalar(select(m.DefinitionHistory).where(
                m.DefinitionHistory.object_id == result['record']['id']))
            assert version.snapshot['overlap_conflict_token'] == checked['conflict_token']
            assert {flag['other_shift_id'] for flag in version.snapshot['overlap_conflicts']} == expected_peers


def test_mixed_batch_keeps_live_leave_checks_for_pending_series(series_harness):
    h = series_harness
    rows = pair(h)
    values = person_values(rows[:1], h.seed['people'][2])
    checked = combined(h, rows[:1], values)
    assert checked['items'][0]['conflicts'] == []
    update = bulk_payload(rows[:1], values)['items'][0]
    payload = {'reason': REASON, 'items': [
        {'item_kind': 'availability.record', 'payload': {'person_id': h.seed['people'][2],
            'kind': 'external_commitment', 'start': '2026-10-12T08:00:00+03:00',
            'end': '2026-10-12T12:00:00+03:00', 'private_reason': PRIVATE}},
        {'item_kind': 'catalogue.update', **update}]}
    before, original = state(h), stored(h, rows)
    refused = h.command('admin', 'bulk.apply', payload, status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
    assert state(h) == before and stored(h, rows) == original
    refused = h.command('admin', 'bulk.apply', {**payload,
        'overlap_acknowledgement': checked['conflict_token']}, status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_BATCH'
    assert state(h) == before and stored(h, rows) == original
