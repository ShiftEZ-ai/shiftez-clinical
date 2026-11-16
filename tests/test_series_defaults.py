"""New usual assignees are fixed without reinterpreting stored series or event timing."""
from datetime import date

import pytest
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.recurrence import expand, stored_series_definition
from .test_events import event, occurrences, repeat
from .test_resources_calendars import update
from .test_series_reconciliation import materialise, template


def definition(**changes):
    return {'anchor': '2026-10-04',
        'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}], **changes}


def test_fixed_default_applies_only_when_usual_assignees_exist():
    person_id = m.uid()
    fields = s.SeriesDefinition.model_json_schema()['properties']
    assert fields['usual_assignees_fixed']['default'] is True and fields['usual_assignees_fixed']['bulk'] is True
    assert fields['close_when_all_usual_unavailable']['default'] is False and 'bulk' not in fields['close_when_all_usual_unavailable']
    assert 'close_when_all_usual_unavailable' not in s._bulk_fields(s.SeriesInput)  # retired switch: never offered in Bulk
    assert s.SeriesDefinition(**definition(default_person_ids=[person_id])).usual_assignees_fixed
    assert not s.SeriesDefinition(**definition()).usual_assignees_fixed
    assert not s.SeriesDefinition(**definition(default_person_ids=[person_id], usual_assignees_fixed=False)).usual_assignees_fixed
    with pytest.raises(ValidationError, match='Choose usual assignees'):
        s.SeriesDefinition(**definition(usual_assignees_fixed=True))


def test_legacy_definition_reader_preserves_source_and_expansion():
    raw = definition(default_person_ids=[m.uid()])
    parsed = stored_series_definition(raw)
    assert not parsed.usual_assignees_fixed and 'usual_assignees_fixed' not in raw
    assert len(expand(parsed, date(2026, 10, 4), date(2026, 10, 11))) == 1


def test_new_series_fixes_its_usual_people_and_allows_explicit_opt_out(h):
    for fixed in (True, False):
        changes = {} if fixed else {'usual_assignees_fixed': False}
        # Both cases deliberately book the same person/time; acknowledge that shared fixture clash.
        source = template(h, acknowledge_overlaps=True, definition=definition(default_person_ids=[h.seed['people'][0]],
            service_ids=[h.seed['services'][0]], **changes))
        assert source['definition']['usual_assignees_fixed'] is fixed
        row = h.detail(materialise(h, source))['shifts'][0]
        assert row['participants'][0]['fixed'] is fixed


def test_series_without_usual_assignees_still_materialises_an_open_session(h):
    source = template(h)
    row = h.detail(materialise(h, source))['shifts'][0]
    assert source['definition']['usual_assignees_fixed'] is False
    assert row['participants'] == []


@pytest.mark.parametrize('fixed', [None, False, True])
def test_stored_series_previews_and_materialises_without_changing_its_fixed_choice(h, fixed):
    source = template(h, definition=definition(default_person_ids=[h.seed['people'][0]], service_ids=[h.seed['services'][0]]))
    raw = {key: value for key, value in source['definition'].items() if key != 'usual_assignees_fixed'}
    if fixed is not None:
        raw['usual_assignees_fixed'] = fixed
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RecurringSeries, source['id']).definition = raw
    assert len(h.get('coordinator', f"/api/series/{source['id']}/preview")['occurrences']) == 4
    row = h.detail(materialise(h, source))['shifts'][0]
    assert row['participants'][0]['fixed'] is bool(fixed)
    assert row['series_snapshot']['definition'] == raw
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, source['id']).definition == raw


@pytest.mark.parametrize('fixed', [None, False, True])
def test_occurrence_cancellation_preserves_stored_fixed_choice_and_undo(h, fixed):
    source = template(h, definition=definition(default_person_ids=[h.seed['people'][0]],
        service_ids=[h.seed['services'][0]]))
    raw = {key: value for key, value in source['definition'].items() if key != 'usual_assignees_fixed'}
    if fixed is not None:
        raw['usual_assignees_fixed'] = fixed
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RecurringSeries, source['id']).definition = raw

    cancelled = h.command('admin', 'series.occurrence.cancel', {
        'occurrence_on': '2026-10-05', 'reason': 'Cancel one occurrence without changing usual assignees'},
        source['id'], source['revision'])
    preserved = {**raw, 'usual_assignees_fixed': bool(fixed)}
    assert cancelled['record']['definition'] == {**preserved, 'excluded_dates': ['2026-10-05']}
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    remaining = h.detail(materialise(h, cancelled['record'], workspace))['shifts']
    assert {row['occurrence_on'] for row in remaining} == {'2026-10-12', '2026-10-19', '2026-10-26'}
    assert all(row['participants'][0]['fixed'] is bool(fixed) for row in remaining)

    undo = cancelled['undo_command']
    restored = h.command('admin', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])
    assert restored['record']['definition'] == preserved
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, source['id']).definition == preserved


@pytest.mark.parametrize('fixed', [None, False, True])
def test_peer_preview_keeps_stored_fixed_choice(h, monkeypatch, fixed):
    from apps.api.services import planning
    from .test_series_overlap_api import candidate, preview

    source = template(h, definition=definition(default_person_ids=[h.seed['people'][0]],
        service_ids=[h.seed['services'][0]]))
    raw = {key: value for key, value in source['definition'].items() if key != 'usual_assignees_fixed'}
    if fixed is not None:
        raw['usual_assignees_fixed'] = fixed
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RecurringSeries, source['id']).definition = raw
    observed, expand = [], planning.series_occurrences

    def expanded(snapshot, series, parsed, *args):
        if series.id == source['id']:
            observed.append(parsed.usual_assignees_fixed)
        return expand(snapshot, series, parsed, *args)

    monkeypatch.setattr(planning, 'series_occurrences', expanded)
    checked = preview(h, candidate(h, service=1))
    assert {row['other_series_id'] for row in checked['conflicts']} == {source['id']}
    assert observed and all(value is bool(fixed) for value in observed)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, source['id']).definition == raw


@pytest.mark.parametrize('method', ['single', 'catalogue.bulk_update', 'bulk.apply'])
@pytest.mark.parametrize('fixed', [False, True])
def test_existing_series_updates_keep_omitted_fixed_choice(h, fixed, method):
    source = template(h, definition=definition(default_person_ids=[h.seed['people'][0]],
        service_ids=[h.seed['services'][0]], usual_assignees_fixed=fixed))
    raw = {key: value for key, value in source['definition'].items() if key != 'usual_assignees_fixed'}
    if not fixed:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.RecurringSeries, source['id']).definition = raw
    if method == 'bulk.apply':
        from .test_bulk import REASON, preview
        from .test_bulk_series_counting import apply

        body = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'recurring_series',
            'value': {'definition': raw}}, 'items': [{'target_id': source['id'],
                'expected_revision': source['revision']}],
            'changes': [{'field': 'description', 'value': 'Synthetic bulk note'}], 'reason': REASON}
        checked = preview(h, 'admin', body)
        assert checked['counts']['ready'] == 1, checked
        with transaction(h.app.state.engine) as db:
            assert db.get(m.RecurringSeries, source['id']).definition == (source['definition'] if fixed else raw)
        result = apply(h, 'admin', body, checked)
        saved = result['items'][0]['results'][0]['record']
        assert saved['description'] == 'Synthetic bulk note'
        h.command('admin', 'bulk.apply', result['undo']['payload'])
        with transaction(h.app.state.engine) as db:
            assert db.get(m.RecurringSeries, source['id']).definition['usual_assignees_fixed'] is fixed
    elif method == 'catalogue.bulk_update':
        reply = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'recurring_series',
            'items': [{'id': source['id'], 'revision': source['revision']}],
            'set': {'definition': raw}, 'reason': 'Synthetic recurrence timing edit'})
        saved = reply['records'][0]
    else:
        saved = update(h, 'recurring_series', source, definition=raw)
    assert saved['definition']['usual_assignees_fixed'] is fixed


@pytest.mark.parametrize('bulk', [False, True])
def test_enabling_fixed_usual_assignees_on_a_legacy_series_is_undoable(h, bulk):
    source = template(h, definition=definition(default_person_ids=[h.seed['people'][0]]))
    raw = {key: value for key, value in source['definition'].items() if key != 'usual_assignees_fixed'}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.RecurringSeries, source['id']).definition = raw
    if bulk:
        changed = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'recurring_series',
            'items': [{'id': source['id'], 'revision': source['revision']}],
            'set': {'definition': {**raw, 'usual_assignees_fixed': True}}, 'reason': 'Fix synthetic usual assignees'})
        h.command('admin', **changed['undo'])
    else:
        value = {key: value for key, value in source.items() if key in s.SeriesInput.model_fields}
        changed = h.configure('recurring_series', value, source['id'], source['revision'])
        assert changed['previous_value']['definition']['usual_assignees_fixed'] is False
        h.configure('recurring_series', changed['previous_value'], source['id'], changed['record']['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RecurringSeries, source['id']).definition['usual_assignees_fixed'] is False


def test_recurring_events_without_usual_assignees_remain_valid(h):
    row = event(h, repeat=repeat(), repeat_until='2026-10-19')
    found = [item for item in occurrences(h) if item['event_id'] == row['id']]
    assert len(found) == 3
    assert row['repeat']['default_person_ids'] == []
    assert row['repeat']['usual_assignees_fixed'] is False
