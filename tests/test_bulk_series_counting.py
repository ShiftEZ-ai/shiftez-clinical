"""Recurring switches and quota windows use the ordinary audited, reversible bulk commands."""
from copy import deepcopy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_rule_catalogue_api import approve, line, person, propose


SWITCHES = ('usual_assignees_fixed',)  # close_when_all_usual_unavailable is retired (D38/DL-009): not bulk-editable


def apply(h, role, body, checked):
    response = h.client(role).post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={'kind': 'bulk.apply', 'payload': body, 'expected_fingerprint': checked['source_fingerprint']})
    assert response.status_code == 200, response.text
    return response.json()


def series(h, starts_on='2026-10-01', ends_on='2026-11-01', **definition):
    return h.configure('recurring_series', {'name': 'Synthetic usual assignees', 'starts_on': starts_on, 'ends_on': ends_on,
        'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0],
        'definition': {'anchor': '2026-10-01', 'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
            'default_person_ids': [h.seed['people'][0]], 'service_ids': [h.seed['services'][0]], **definition}})['record']


@pytest.mark.parametrize('enabled', [True, False])
def test_series_bulk_switches_preview_apply_audit_and_undo_preserve_definition(h, enabled):
    rows = [series(h, **dict.fromkeys(SWITCHES, not enabled)), series(h, starts_on='2026-11-01', ends_on='2026-12-01', interval_weeks=2,
        **dict.fromkeys(SWITCHES, not enabled))]
    original = {row['id']: deepcopy(row['definition']) for row in rows}
    body = {'item_kind': 'catalogue.update', 'selection': {'collection': 'recurring_series',
        'include_ids': list(original)}, 'changes': [{'field': key, 'value': enabled} for key in SWITCHES], 'reason': REASON}
    checked = preview(h, 'admin', body)
    assert checked['counts']['ready'] == 2, checked
    with transaction(h.app.state.engine) as db:
        assert {key: db.get(m.RecurringSeries, key).definition for key in original} == original
    result = apply(h, 'admin', body, checked)
    with transaction(h.app.state.engine) as db:
        for key, definition in original.items():
            assert db.get(m.RecurringSeries, key).definition == {**definition, **dict.fromkeys(SWITCHES, enabled)}
        assert len(list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'catalogue.update',
            m.AuditEvent.detail['batch_id'].as_string() == result['batch_id'])))) == 2
    h.command('admin', 'bulk.apply', result['undo']['payload'])
    with transaction(h.app.state.engine) as db:
        assert {key: db.get(m.RecurringSeries, key).definition for key in original} == original


def test_series_bulk_switches_keep_the_usual_assignee_validation(h):
    row = series(h, default_person_ids=[])
    body = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'recurring_series'},
        'items': [{'target_id': row['id'], 'expected_revision': row['revision']}],
        'changes': [{'field': SWITCHES[0], 'value': True}], 'reason': REASON}
    checked = preview(h, 'admin', body)
    assert checked['counts']['problem'] == 1, checked
    assert checked['items'][0]['issues'][0]['code'] == 'SCHEMA_INVALID'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, row['id']).definition == row['definition']


def test_series_item_switch_does_not_mutate_another_items_common_definition(h):
    first, second = series(h, usual_assignees_fixed=True), series(h, starts_on='2026-11-01', ends_on='2026-12-01')
    shared = {**first['definition'], 'usual_assignees_fixed': True}
    body = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'recurring_series',
        'value': {'definition': shared}}, 'items': [
            {'target_id': first['id'], 'expected_revision': first['revision'],
                'changes': [{'field': 'usual_assignees_fixed', 'value': False}]},
            {'target_id': second['id'], 'expected_revision': second['revision'],
                'changes': [{'field': 'name', 'value': 'Synthetic second series'}]}], 'reason': REASON}
    checked = preview(h, 'admin', body)
    assert checked['counts']['ready'] == 2, checked
    result = apply(h, 'admin', body, checked)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, first['id']).definition == {**shared, 'usual_assignees_fixed': False}
        assert db.get(m.RecurringSeries, second['id']).definition == shared
    h.command('admin', 'bulk.apply', result['undo']['payload'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, first['id']).definition == first['definition']
        assert db.get(m.RecurringSeries, second['id']).definition == second['definition']


@pytest.mark.parametrize('definition', [None, [], 'invalid'])
def test_series_bulk_switches_reject_malformed_nested_definitions(h, definition):
    row = series(h)
    body = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'recurring_series',
        'value': {'definition': definition}}, 'items': [{'target_id': row['id'],
            'expected_revision': row['revision']}],
        'changes': [{'field': SWITCHES[0], 'value': True}], 'reason': REASON}
    checked = preview(h, 'admin', body)
    assert checked['counts']['problem'] == 1, checked
    assert checked['items'][0]['issues'][0]['code'] == 'SCHEMA_INVALID'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, row['id']).definition == row['definition']


def test_quota_bulk_counting_merges_each_line_preserves_settings_and_waits_for_approval(h):
    authored = [line(who=person(who), numbers={'min': {'value': 2}, 'max': {'value': 5}},
        note=f'Keep line {index}', settings={'block_quota': {'starts_on': '2026-10-01', 'ends_on': '2027-01-01',
            'window_alignment': 'fixed', 'window_days': 7, 'overtime': 'exclude' if index else 'include'}})
        for index, who in enumerate(h.seed['people'][:3])]
    active = approve(h, propose(h, 'duties_per_period', authored)['record'])
    original = deepcopy(active['definition']['rule']['lines'])
    patch = {'starts_on': '2026-11-01', 'ends_on': '2027-02-01', 'repeat': True, 'window_anchor_on': '2026-10-25'}
    body = {'item_kind': 'rule.propose', 'selection': {'collection': 'policy_lines',
        'include_ids': [row['id'] for row in authored[:2]]},
        'changes': [{'field': 'counting', 'value': patch}], 'reason': REASON}
    checked = preview(h, 'coordinator', body)
    assert checked['counts']['waiting'] == 1, checked
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Policy.id).where(m.Policy.status == 'proposed')) is None
        assert db.get(m.Policy, active['id']).definition['rule']['lines'] == original
    result = apply(h, 'coordinator', body, checked)
    pending = result['items'][0]['results'][0]['record']
    assert pending['status'] == 'proposed'
    assert pending['id'] and pending['definition']['rule']['id'] == 'duties_per_period'
    for index, actual in enumerate(pending['definition']['rule']['lines']):
        expected = deepcopy(original[index])
        if index < 2:
            expected['settings']['block_quota'].update(patch)
        assert actual == expected
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active['id']).definition['rule']['lines'] == original
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'rule.propose',
            m.AuditEvent.detail['batch_id'].as_string() == result['batch_id'])) is not None
    h.command('coordinator', 'policy.activate', {'reason': REASON}, pending['id'], pending['revision'], status=403)
    undo = h.command('coordinator', 'bulk.apply', result['undo']['payload'])
    assert undo['items'][0]['results'][0]['record']['id'] == pending['id']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active['id']).status == 'active'
        assert db.get(m.Policy, pending['id']).status == 'retired'


@pytest.mark.parametrize('patch,status', [({'ends_on': '2026-09-01'}, 200), ({'repeat': 'invalid'}, 422),
    ({'starts_on': None}, 422), ({'unexpected': 1}, 422), ({}, 422)])
def test_quota_bulk_counting_keeps_the_primitive_validation(h, patch, status):
    authored = line(numbers={'min': {'value': 2}}, settings={'block_quota': {
        'starts_on': '2026-10-01', 'ends_on': '2027-01-01'}})
    active = approve(h, propose(h, 'duties_per_month', [authored])['record'])
    body = {'item_kind': 'rule.propose', 'selection': {'collection': 'policy_lines',
        'include_ids': [authored['id']]}, 'changes': [{'field': 'counting', 'value': patch}], 'reason': REASON}
    response = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': body})
    assert response.status_code == status, response.text
    checked = response.json()
    if status == 200:
        assert checked['counts']['problem'] == 1, checked
    else:
        assert checked['error']['code'] == ('BULK_CHANGE_UNSUPPORTED' if patch == {} or 'unexpected' in patch else 'SCHEMA_INVALID')
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active['id']).status == 'active'
        assert db.scalar(select(m.Policy.id).where(m.Policy.status == 'proposed')) is None


def test_bulk_counting_rejects_lines_without_a_quota_number(h):
    authored = line(numbers={'max': {'value': 5}})
    active = approve(h, propose(h, 'duties_per_week', [authored])['record'])
    body = {'item_kind': 'rule.propose', 'selection': {'collection': 'policy_lines',
        'include_ids': [authored['id']]}, 'changes': [{'field': 'counting', 'value': {'repeat': True}}], 'reason': REASON}
    response = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': body})
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'BULK_CHANGE_UNSUPPORTED'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active['id']).status == 'active'
        assert db.scalar(select(m.Policy.id).where(m.Policy.status == 'proposed')) is None


def test_bulk_series_and_counting_metadata_reuses_the_underlying_contracts():
    fields = s.BULK_COLLECTIONS['recurring_series']['fields']
    assert 'close_when_all_usual_unavailable' not in fields
    for key in SWITCHES:
        assert fields[key] == {**s._bulk_fields(s.SeriesDefinition)[key], 'path': 'definition'}
        assert fields[key]['schema']['type'] == 'boolean'
        assert fields[key]['how'] == ['set']
    counting = s.BULK_COLLECTIONS['policy_lines']['fields']['counting']
    assert counting['how'] == ['set']
    assert counting['schema']['properties'] == {key: s.BlockQuota.model_json_schema()['properties'][key]
        for key in ('starts_on', 'ends_on', 'repeat', 'window_anchor_on')}
