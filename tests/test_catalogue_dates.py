"""Dated catalogue edits preserve existing session, series and relationship meaning."""
import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


KINDS = ('assignment_types', 'locations', 'location_types')


def current(h, kind):
    rows = h.get('admin', '/api/catalogues')
    location = next(row for row in rows['locations'] if row['id'] == h.seed['locations'][0])
    key = {'assignment_types': h.seed['type'], 'locations': location['id'],
           'location_types': location['location_type_id']}[kind]
    return next(row for row in rows[kind] if row['id'] == key)


def change(h, kind, row, status=200, **values):
    value = {key: row[key] for key in s.CATALOGUE_SCHEMAS[kind].model_fields if key in row}
    return h.configure(kind, {**value, **values}, row['id'], row['revision'], status=status)


def series(h, **values):
    return h.configure('recurring_series', {'name': 'Synthetic guarded series',
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0],
        'definition': {'anchor': '2026-10-04', 'days': [
            {'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}]}, **values})['record']


@pytest.mark.parametrize('kind', KINDS)
def test_later_start_refuses_session_and_series_with_counts_without_writes(h, kind):
    row = current(h, kind)
    workspace = h.shift(h.workspace())['workspace']
    series(h)
    before = h.detail(workspace)
    history = h.get('admin', f'/api/catalogues/{kind}/{row["id"]}/history')
    reply = change(h, kind, row, starts_on='2026-12-01', status=409)['error']
    assert reply['code'] == 'CATALOGUE_DATES_EXCLUDE_USE'
    assert reply['details']['counts']['sessions'] == 1
    assert reply['details']['counts']['series'] == 1
    assert '1 session and 1 series' in reply['message'] and '01 Dec 2026' in reply['message']
    assert current(h, kind) == row
    assert h.detail(workspace) == before
    assert h.get('admin', f'/api/catalogues/{kind}/{row["id"]}/history') == history


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('ends_on', ['2026-11-15', None])
def test_earlier_end_refuses_unmaterialised_series_by_its_own_range(h, kind, ends_on):
    row = current(h, kind)
    series(h, starts_on='2026-11-01', ends_on=ends_on)
    reply = change(h, kind, row, ends_on='2026-11-08', status=409)['error']
    assert reply['code'] == 'CATALOGUE_DATES_EXCLUDE_USE'
    assert reply['details']['counts']['sessions'] == 0
    assert reply['details']['counts']['series'] == 1
    assert '1 series' in reply['message']


@pytest.mark.parametrize('kind', ('assignment_types', 'locations'))
def test_contained_uses_and_local_midnight_boundaries_allow_edit_and_undo(h, kind):
    row = current(h, kind)
    series(h)
    h.shift(h.workspace(), start='2026-10-04T00:00:00+03:00', end='2026-10-04T04:00:00+03:00', people=[])
    saved = change(h, kind, row, starts_on='2026-10-01', ends_on='2026-11-01')
    assert saved['record']['starts_on'] == '2026-10-01'
    assert saved['record']['ends_on'] == '2026-11-01'
    restored = h.configure(kind, saved['previous_value'], row['id'], saved['record']['revision'])
    assert restored['record']['starts_on'] == row['starts_on']
    assert restored['record']['ends_on'] == row['ends_on']


@pytest.mark.parametrize('kind', KINDS)
def test_unused_record_can_narrow_both_dates(h, kind):
    source = current(h, kind)
    value = {key: source[key] for key in s.CATALOGUE_SCHEMAS[kind].model_fields if key in source}
    row = h.configure(kind, {**value, 'name': 'Synthetic unused record', 'starts_on': '2026-01-01',
                             'ends_on': None, **({'short_code': ''} if kind == 'assignment_types' else {})})['record']
    saved = change(h, kind, row, starts_on='2026-10-01', ends_on='2026-11-01')['record']
    assert saved['starts_on'] == '2026-10-01' and saved['ends_on'] == '2026-11-01'


@pytest.mark.parametrize('kind', ('assignment_types', 'locations'))
def test_undo_rechecks_uses_created_after_date_extension(h, kind):
    row = current(h, kind)
    narrowed = change(h, kind, row, starts_on='2026-10-04')['record']
    extended = change(h, kind, narrowed, starts_on='2026-10-01')
    series(h, ends_on='2026-10-11')
    reply = h.configure(kind, extended['previous_value'], row['id'], extended['record']['revision'], status=409)
    assert reply['error']['code'] == 'CATALOGUE_DATES_EXCLUDE_USE'
    assert reply['error']['details']['counts']['series'] == 1
    assert current(h, kind) == extended['record']


@pytest.mark.parametrize('kind', ('assignment_types', 'locations'))
@pytest.mark.parametrize('archived', [False, True])
def test_published_and_archived_sessions_still_count(h, kind, archived):
    row = current(h, kind)
    workspace = h.shift(h.workspace())['workspace']
    publication = h.publish(workspace)
    snapshot = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']
    if archived:
        with transaction(h.app.state.engine) as db:
            db.get(m.Workspace, workspace['id']).archived_at = m.now()
    reply = change(h, kind, row, starts_on='2026-12-01', status=409)['error']
    assert reply['details']['counts']['sessions'] == 1
    assert h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot'] == snapshot


def test_location_ancestor_dates_protect_descendant_sessions_and_series(h):
    row = current(h, 'locations')
    parent = h.configure('locations', {'name': 'Synthetic parent', 'location_type_id': row['location_type_id'],
                                      'starts_on': row['starts_on']})['record']
    change(h, 'locations', row, parent_id=parent['id'])
    h.shift(h.workspace())
    series(h)
    reply = change(h, 'locations', parent, starts_on='2026-12-01', status=409)['error']
    assert reply['details']['counts']['sessions'] == 1 and reply['details']['counts']['series'] == 1
    assert reply['details']['counts']['locations'] == 1


def test_location_type_dates_allow_all_contained_location_ranges(h):
    row = current(h, 'location_types')
    for location in h.get('admin', '/api/catalogues')['locations']:
        if location['location_type_id'] == row['id']:
            change(h, 'locations', location, starts_on='2026-10-01', ends_on='2026-11-01')
    h.shift(h.workspace())
    saved = change(h, 'location_types', row, starts_on='2026-10-01', ends_on='2026-11-01')['record']
    assert saved['ends_on'] == '2026-11-01'


@pytest.mark.parametrize('edge', ['start', 'end'])
def test_sessions_crossing_local_boundaries_are_refused(h, edge):
    row = current(h, 'assignment_types')
    h.shift(h.workspace(), start='2026-10-04T23:00:00+03:00', end='2026-10-05T01:00:00+03:00', people=[])
    values = {'starts_on': '2026-10-05'} if edge == 'start' else {'ends_on': '2026-10-05'}
    reply = change(h, 'assignment_types', row, status=409, **values)['error']
    assert reply['details']['counts']['sessions'] == 1


def test_other_department_uses_are_not_counted_and_queries_are_aggregates(h):
    row = current(h, 'assignment_types')
    workspace = h.shift(h.workspace())['workspace']
    recurring = series(h)
    with transaction(h.app.state.engine) as db:
        department = db.get(m.Department, db.get(m.Workspace, workspace['id']).department_id)
        other = m.Department(organisation_id=department.organisation_id, name='Synthetic other department')
        db.add(other)
        db.flush()
        db.get(m.Workspace, workspace['id']).department_id = other.id
        db.get(m.RecurringSeries, recurring['id']).department_id = other.id
    queries = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if 'AS sessions' in statement:
            queries.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        saved = change(h, 'assignment_types', row, starts_on='2026-12-01')['record']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert saved['starts_on'] == '2026-12-01'
    assert queries and all('count(' in query and 'workspaces.department_id' in query
                           and 'recurring_series.department_id' in query for query in queries)


@pytest.mark.parametrize('repeat_end', ['2026-11-01', None])
def test_duty_type_dates_protect_dated_and_standing_wishes(h, repeat_end):
    row = current(h, 'assignment_types')
    h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_shift_type',
        'assignment_type_id': row['id'], 'repeat': {'weekdays': [6], 'starts_on': '2026-10-04', 'ends_on': repeat_end}})
    reply = change(h, 'assignment_types', row, ends_on='2026-11-01', status=409)['error']
    assert reply['details']['counts']['wishes'] == 1 and '1 wish' in reply['message']


def test_duty_type_dates_protect_mentorship_ranges(h):
    row = current(h, 'assignment_types')
    h.configure('mentorships', {'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][1],
        'assignment_type_ids': [row['id']], 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})
    reply = change(h, 'assignment_types', row, starts_on='2026-10-02', status=409)['error']
    assert reply['details']['counts']['mentorships'] == 1


@pytest.mark.parametrize('kind', ('assignment_types', 'locations'))
@pytest.mark.parametrize('start,end', [('2026-10-04T00:00:00+03:00', '2026-10-04T04:00:00+03:00'),
                                     ('2026-10-04T20:00:00+03:00', '2026-10-05T00:00:00+03:00')])
def test_session_exactly_within_local_day_is_allowed(h, kind, start, end):
    row = current(h, kind)
    h.shift(h.workspace(), start=start, end=end, people=[])
    saved = change(h, kind, row, starts_on='2026-10-04', ends_on='2026-10-05')['record']
    assert saved['ends_on'] == '2026-10-05'


def test_location_dates_protect_resource_pool_and_closure(h):
    row = current(h, 'locations')
    h.configure('resource_pools', {'name': 'Synthetic dated equipment', 'capacity': 1, 'location_id': row['id'],
                                 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})
    with transaction(h.app.state.engine) as db:
        from packages.domain.time import aware
        location = db.get(m.Location, row['id'])
        db.add(m.LocationClosure(department_id=location.department_id, location_id=row['id'],
            start=aware('2026-10-04T08:00:00+03:00'), end=aware('2026-10-04T12:00:00+03:00'),
            reason='Synthetic dated closure'))
    reply = change(h, 'locations', row, starts_on='2026-10-05', status=409)['error']
    assert reply['details']['counts']['resource pools'] == 1
    assert reply['details']['counts']['closures'] == 1


@pytest.mark.parametrize('repeating', [False, True])
def test_location_dates_protect_events_and_allow_contained_events(h, repeating):
    from .test_events import event as create_event, repeat

    row = current(h, 'locations')
    create_event(h, location_id=row['id'], **({'repeat': repeat(), 'repeat_until': '2026-11-01'} if repeating else {}))
    if not repeating:
        saved = change(h, 'locations', row, starts_on='2026-10-01', ends_on='2026-10-05')['record']
        assert saved['ends_on'] == '2026-10-05'
        row = saved
    reply = change(h, 'locations', row, ends_on='2026-10-04' if not repeating else '2026-10-11', status=409)['error']
    assert reply['details']['counts']['events'] == 1
    if not repeating:
        assert reply['details']['counts']['event occurrences'] == 1


def test_rotation_window_guard_is_unchanged(h):
    from .test_rotation_leave_policy import resident

    typ, value, _ = resident(h, create=False)
    h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
                                  'starts_on': '2026-10-02'}, typ['id'], typ['revision'])
    reply = h.configure('rotations', value, status=422)
    assert reply['error']['code'] == 'ROTATION_TYPE_WINDOW'


def test_catalogue_preview_uses_the_same_guard_without_writes(h):
    row = current(h, 'assignment_types')
    h.shift(h.workspace())
    value = {key: row[key] for key in s.AssignmentTypeInput.model_fields}
    response = h.client('admin').post('/api/catalogue/preview', json={
        'kind': 'catalogue.update', 'target_id': row['id'], 'expected_revision': row['revision'],
        'payload': {'catalogue': 'assignment_types', 'reason': 'Synthetic guarded preview',
                    'value': {**value, 'starts_on': '2026-12-01'}}})
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'CATALOGUE_DATES_EXCLUDE_USE'
    assert current(h, 'assignment_types') == row


def test_bulk_date_edit_rolls_back_earlier_items_when_any_use_is_excluded(h):
    row = current(h, 'assignment_types')
    value = {key: row[key] for key in s.AssignmentTypeInput.model_fields}
    unused = h.configure('assignment_types', {**value, 'name': 'Synthetic unused bulk duty', 'short_code': ''})['record']
    h.shift(h.workspace())
    response = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'assignment_types',
        'items': [{'id': item['id'], 'revision': item['revision']} for item in (unused, row)],
        'set': {'starts_on': '2026-12-01'}, 'reason': 'Synthetic all-or-nothing date edit'}, status=409)
    assert response['error']['code'] == 'CATALOGUE_DATES_EXCLUDE_USE'
    records = {item['id']: item for item in h.get('admin', '/api/catalogues')['assignment_types']}
    assert records[unused['id']] == unused and records[row['id']] == row
