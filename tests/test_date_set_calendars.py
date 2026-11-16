"""Date sets share opening evaluation and appear on staff schedules and calendar feeds."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.services.planning import series_occurrences
from packages.contracts.schemas import SeriesDefinition
from packages.domain.resources import closed_intervals, date_set_closes, check_calendar
from packages.domain.reports import calendar, calendar_feed
from packages.domain.time import aware
from .test_calendar_feed import events
from .test_resources_calendars import update, pool, typ
from .test_series_reconciliation import template, materialise, reconcile


def date_set(h, first='2026-10-04', last='2026-10-04'):
    return h.configure('date_sets', {'name': 'Public holidays', 'entries': [
        {'name': 'Synthetic Eid', 'first_day': first, 'last_day': last}]})['record']


def opening(h, holiday):
    calendar = h.configure('operating_calendars', {'name': 'Clinic holiday opening', 'starts_on': '2020-01-01',
        'definition': {'timezone': 'Asia/Riyadh', 'week': [
            {'weekday': day, 'windows': [{'start_time': '08:00', 'end_time': '12:00'}]} for day in range(7)],
            'date_set_rules': [{'date_set_id': holiday['id'], 'windows': []}]}})['record']
    service = next(row for row in h.get('admin', '/api/catalogues')['services'] if row['id'] == h.seed['services'][0])
    update(h, 'services', service, calendar_id=calendar['id'])
    return calendar


def test_legacy_weekly_and_dated_openings_do_not_change():
    calendar = {'active': True, 'starts_on': '2020-01-01', 'definition': {'timezone': 'Asia/Riyadh',
        'week': [{'weekday': 6, 'windows': [{'start_time': '08:00', 'end_time': '12:00'}]}],
        'overrides': [{'on': '2026-10-04', 'windows': [{'start_time': '09:00', 'end_time': '11:00'}]}]}}
    before = deepcopy(calendar)
    start, end = aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00')
    expected = [(start, aware('2026-10-04T09:00:00+03:00')), (aware('2026-10-04T11:00:00+03:00'), end)]
    assert closed_intervals(calendar, start, end) == expected
    assert closed_intervals(calendar, start, end, {}) == expected
    calendar['definition']['date_set_rules'] = []
    assert closed_intervals(calendar, start, end, {}) == expected
    assert calendar['definition']['overrides'] == before['definition']['overrides']


@pytest.mark.parametrize('closed', [False, True])
def test_explicit_dated_override_wins_without_out_of_range_hijri_conversion(closed):
    start, end = aware('2080-01-04T08:00:00+03:00'), aware('2080-01-04T12:00:00+03:00')
    windows = [] if closed else [{'start_time': '08:00', 'end_time': '12:00'}]
    calendar = {'starts_on': '2020-01-01', 'definition': {'timezone': 'Asia/Riyadh', 'week': [],
        'overrides': [{'on': '2080-01-04', 'windows': windows}],
        'date_set_rules': [{'date_set_id': 'holidays', 'windows': []}]}}
    sets = {'holidays': {'name': 'Public holidays', 'entries': [{'id': 'eid', 'name': 'Eid',
        'repeat': 'yearly_hijri', 'first_day': '1447-10-01', 'last_day': '1447-10-04'}]}}
    assert closed_intervals(calendar, start, end, sets) == ([(start, end)] if closed else [])
    assert date_set_closes(calendar, start, end, sets) is closed
    findings = []
    calendar['definition']['overrides'] = []
    check_calendar({'date_sets': sets, 'operating_calendars': {'clinic': calendar}}, 'clinic', start, end,
                   'session', lambda code, *args, **kwargs: findings.append(code), {'service_id': 'service'})
    assert findings == ['CALENDAR_DATE_SET_UNRESOLVED']


def test_calendar_entry_selection_precedes_hijri_resolution_and_later_rules():
    start, end = aware('2080-09-23T08:00:00+03:00'), aware('2080-09-23T12:00:00+03:00')
    calendar = {'starts_on': '2020-01-01', 'definition': {'timezone': 'Asia/Riyadh', 'week': [],
        'overrides': [], 'date_set_rules': [
            {'date_set_id': 'holidays', 'entry_names': ['National Day'],
             'windows': [{'start_time': '08:00', 'end_time': '12:00'}]},
            {'date_set_id': 'holidays', 'entry_names': ['Eid'], 'windows': []}]}}
    sets = {'holidays': {'name': 'Public holidays', 'entries': [
        {'id': 'eid', 'name': 'Eid', 'repeat': 'yearly_hijri',
         'first_day': '1447-10-01', 'last_day': '1447-10-04'},
        {'id': 'national-day', 'name': 'National Day', 'repeat': 'yearly_gregorian',
         'first_day': '2026-09-23', 'last_day': '2026-09-23'}]}}
    assert closed_intervals(calendar, start, end, sets) == []
    assert date_set_closes(calendar, start, end, sets) is False


def test_calendar_set_entry_filter_first_match_and_explicit_override_precedence(h):
    holiday = date_set(h)
    calendar = opening(h, holiday)
    data = {holiday['id']: holiday}
    start, end = aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00')
    assert closed_intervals(calendar, start, end, data) == [(start, end)]
    rule = calendar['definition']['date_set_rules'][0]
    rule['entry_names'] = ['Different entry']
    assert closed_intervals(calendar, start, end, data) == []
    rule['entry_names'] = ['Synthetic Eid']
    rule['windows'] = [{'start_time': '09:00', 'end_time': '11:00'}]
    calendar['definition']['date_set_rules'].append({'date_set_id': holiday['id'], 'windows': []})
    assert closed_intervals(calendar, start, end, data) == [
        (start, aware('2026-10-04T09:00:00+03:00')), (aware('2026-10-04T11:00:00+03:00'), end)]
    calendar['definition']['overrides'] = [{'on': '2026-10-04', 'windows': [
        {'start_time': '08:00', 'end_time': '12:00'}]}]
    assert closed_intervals(calendar, start, end, data) == []


@pytest.mark.parametrize('method', ['create', 'expand', 'reconcile'])
def test_closed_clinic_set_skips_sessions_in_every_template_generation_path(h, method):
    series = template(h, days=(6, 0))
    original = materialise(h, series) if method == 'reconcile' else None
    opening(h, date_set(h))
    if method == 'create':
        result = h.command('coordinator', 'workspace.create', {'name': 'Holiday-filtered template',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [series['id']]})
    elif method == 'expand':
        result = {'workspace': materialise(h, series)}
    else:
        result = reconcile(h, original, series, from_on='2026-10-04')
        assert result['removed_count'] == 1
    assert [row['occurrence_on'] for row in h.detail(result['workspace'])['shifts']] == ['2026-10-05']


@pytest.mark.parametrize('method', ['create', 'expand', 'reconcile'])
def test_short_set_opening_keeps_template_sessions_for_calendar_validation(h, method):
    series = template(h, days=(6, 0))
    original = materialise(h, series) if method == 'reconcile' else None
    calendar = opening(h, date_set(h))
    definition = deepcopy(calendar['definition'])
    definition['date_set_rules'][0]['windows'] = [{'start_time': '09:00', 'end_time': '11:00'}]
    update(h, 'operating_calendars', calendar, definition=definition)
    if method == 'create':
        result = h.command('coordinator', 'workspace.create', {'name': 'Keep partial opening conflicts visible',
            'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'series_ids': [series['id']]})
    elif method == 'expand':
        workspace = h.workspace()
        result = h.command('coordinator', 'series.expand',
            {'workspace_id': workspace['id'], 'series_id': series['id']}, revision=workspace['revision'])
    else:
        result = reconcile(h, original, series, from_on='2026-10-04')
        assert result['removed_count'] == 0
    shifts = h.detail(result['workspace'])['shifts']
    assert [row['occurrence_on'] for row in shifts] == ['2026-10-04', '2026-10-05']
    assert (aware(shifts[0]['start']), aware(shifts[0]['end'])) == (
        aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00'))
    assert 'CALENDAR_CLOSED' in {finding['code'] for finding in result['validation']['findings']}


@pytest.mark.parametrize('explicit_override', [False, True])
def test_nonoverlapping_opening_is_a_validation_conflict_not_a_closed_day(explicit_override):
    opening = {'windows': [{'start_time': '13:00', 'end_time': '14:00'}]}
    calendar = {'starts_on': '2020-01-01', 'definition': {'timezone': 'Asia/Riyadh', 'week': [],
        'overrides': [{'on': '2026-10-04', **opening}] if explicit_override else [],
        'date_set_rules': [{'date_set_id': 'holiday', **({'windows': []} if explicit_override else opening)}]}}
    sets = {'holiday': {'name': 'Public holiday', 'entries': [{'id': 'eid', 'name': 'Eid',
        'first_day': '2026-10-04', 'last_day': '2026-10-04'}]}}
    start, end = aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00')
    assert date_set_closes(calendar, start, end, sets) is False
    assert closed_intervals(calendar, start, end, sets) == [(start, end)]


@pytest.mark.parametrize('method', ['create', 'expand', 'reconcile'])
@pytest.mark.parametrize('failure', ['hijri_range', 'ambiguous_time'])
def test_template_calendar_and_wall_time_resolution_errors_return_422(h, method, failure):
    if failure == 'hijri_range':
        holiday = h.configure('date_sets', {'name': 'Umm al-Qura opening', 'entries': [
            {'name': 'Eid', 'repeat': 'yearly_hijri',
             'first_day': '1447-10-01', 'last_day': '1447-10-04'}]})['record']
        opening(h, holiday)
        first, last = '2080-01-04', '2080-01-05'
        series = template(h, starts_on=first, ends_on=last, definition={'anchor': first,
            'days': [{'weekday': date.fromisoformat(first).weekday(), 'start_time': '08:00', 'end_time': '12:00'}],
            'service_ids': [h.seed['services'][0]]})
        code, message = 'DATE_SET_RANGE', 'Umm al-Qura'
    else:
        # The saved one-year preview ends before the following year's DST fold.
        first, last = '2027-11-07', '2027-11-08'
        series = template(h, starts_on='2026-11-02', ends_on=None,
            definition={'anchor': '2026-11-02', 'timezone': 'America/New_York',
                'days': [{'weekday': 6, 'start_time': '01:30', 'end_time': '03:30'}],
                'service_ids': [h.seed['services'][0]]})
        code, message = 'SERIES_TIME_UNRESOLVED', 'Ambiguous or nonexistent local wall time'
    if method == 'create':
        result = h.command('coordinator', 'workspace.create', {'name': 'Unresolvable template',
            'starts_on': first, 'ends_on': last, 'series_ids': [series['id']]}, status=422)
    else:
        workspace = h.workspace(start=first, end=last)
        before = h.detail(workspace)
        if method == 'expand':
            result = h.command('coordinator', 'series.expand',
                {'workspace_id': workspace['id'], 'series_id': series['id']},
                revision=workspace['revision'], status=422)
        else:
            result = reconcile(h, workspace, series, from_on=first, status=422)
        assert h.detail(workspace) == before
    assert result['error']['code'] == code
    assert message in result['error']['message']


def test_legacy_template_snapshot_without_operating_calendars_keeps_occurrences():
    clinic = '00000000-0000-0000-0000-000000000001'
    definition = SeriesDefinition(anchor='2026-10-04',
        days=[{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}], service_ids=[clinic])
    snapshot = {'services': {clinic: {'calendar_id': 'missing'}}, 'locations': {'site': {}},
        'types': {'duty': {'definition': {'resources': [{'resource_pool_id': 'pool', 'from_minute': 0}]}}},
        'resource_pools': {'pool': {'calendar_id': 'missing'}},
        'workspace': {'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}}
    before = deepcopy(snapshot)
    series = SimpleNamespace(location_id='site', assignment_type_id='duty')
    assert series_occurrences(snapshot, series, definition, date(2026, 10, 4), date(2026, 10, 5)) == [
        {'start': aware('2026-10-04T08:00:00+03:00'), 'end': aware('2026-10-04T12:00:00+03:00')}]
    assert snapshot == before


def test_calendar_validator_uses_date_sets_and_explicit_dated_override(h):
    calendar = opening(h, date_set(h))
    result = h.shift(h.workspace())
    assert 'CALENDAR_CLOSED' in {finding['code'] for finding in result['validation']['findings']}
    definition = deepcopy(calendar['definition'])
    definition['overrides'] = [{'on': '2026-10-04', 'label': 'Approved replacement opening',
        'windows': [{'start_time': '08:00', 'end_time': '12:00'}]}]
    update(h, 'operating_calendars', calendar, definition=definition)
    assert h.lifecycle(result['workspace'], 'validate')['validation']['valid']


def test_dated_overrides_win_during_template_generation(h):
    opening_calendar = opening(h, date_set(h))
    definition = deepcopy(opening_calendar['definition'])
    definition['overrides'] = [{'on': '2026-10-04', 'label': 'Explicitly open',
                              'windows': [{'start_time': '08:00', 'end_time': '12:00'}]}]
    opening_calendar = update(h, 'operating_calendars', opening_calendar, definition=definition)
    series = template(h, days=(6, 0))
    assert len(h.detail(materialise(h, series))['shifts']) == 2
    definition['overrides'][0]['windows'] = []
    definition['date_set_rules'][0]['windows'] = [{'start_time': '08:00', 'end_time': '12:00'}]
    update(h, 'operating_calendars', opening_calendar, definition=definition)
    assert [row['occurrence_on'] for row in h.detail(materialise(h, series))['shifts']] == ['2026-10-05']


def test_legacy_calendar_without_set_rules_keeps_existing_template_behavior(h):
    opening_calendar = opening(h, date_set(h))
    definition = deepcopy(opening_calendar['definition'])
    definition.pop('date_set_rules')
    definition['week'] = []
    update(h, 'operating_calendars', opening_calendar, definition=definition)
    assert len(h.detail(materialise(h, template(h, days=(6, 0))))['shifts']) == 2


@pytest.mark.parametrize('partial_reservation', [False, True])
def test_required_resource_pool_date_set_closure_skips_only_the_required_interval(h, partial_reservation):
    calendar = opening(h, date_set(h))
    service = next(row for row in h.get('admin', '/api/catalogues')['services'] if row['id'] == h.seed['services'][0])
    update(h, 'services', service, calendar_id=None)
    resource = pool(h, calendar_id=calendar['id'])
    if partial_reservation:
        definition = deepcopy(calendar['definition'])
        definition['date_set_rules'][0]['windows'] = [{'start_time': '09:00', 'end_time': '11:00'}]
        update(h, 'operating_calendars', calendar, definition=definition)
    duty_type = typ(h, resource, **({'resources': [{'resource_pool_id': resource['id'], 'units': 1,
                                                 'from_minute': 60, 'to_minute': 120}]} if partial_reservation else {}))
    series = template(h, days=(6, 0), assignment_type_id=duty_type['id'])
    workspace = materialise(h, series)
    expected = ['2026-10-04', '2026-10-05'] if partial_reservation else ['2026-10-05']
    assert [row['occurrence_on'] for row in h.detail(workspace)['shifts']] == expected


def test_staff_schedule_markers_include_non_duty_days_and_invalidate_etags(h):
    me = h.get('staff', '/api/me')['person_id']
    h.publish(h.shift(h.workspace(), people=[me])['workspace'])
    paths = ['/api/schedule/days?start=2026-10-04&end=2026-10-07',
             '/api/schedule/sessions?start=2026-10-04&end=2026-10-07']
    client = h.client('staff')
    before = {path: client.get(path).headers['etag'] for path in paths}
    holiday = date_set(h, last='2026-10-05')
    marker = {'set_id': holiday['id'], 'set_name': holiday['name'],
              'entry_id': holiday['entries'][0]['id'], 'name': 'Synthetic Eid'}
    expected = {'2026-10-04': [marker], '2026-10-05': [marker]}
    for path, tag in before.items():
        changed = client.get(path, headers={'if-none-match': tag})
        assert changed.status_code == 200 and changed.headers['etag'] != tag
        assert changed.json()['date_set_markers'] == expected
        assert client.get(path, headers={'if-none-match': changed.headers['etag']}).status_code == 304
    assert h.get('staff', '/api/calendar?start=2026-10-04&end=2026-10-07')['date_set_markers'] == expected


def test_calendar_feed_keeps_published_names_and_named_markers_without_duties(h):
    holiday = date_set(h, last='2026-10-05')
    me = h.get('staff', '/api/me')['person_id']
    publication = h.publish(h.shift(h.workspace(), people=[me])['workspace'])
    path = h.command('staff', 'calendar_feed.create', {})['path']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.get(path)
        assert response.status_code == 200
        rows = events(response)
        duty, = [row for row in rows if 'DTSTART' in row]
        assert duty['X-FMUC-DATE-SETS'] == 'Public holidays: Synthetic Eid'
        assert 'Public holidays: Synthetic Eid' in duty['DESCRIPTION']
        markers = [row for row in rows if 'DTSTART;VALUE=DATE' in row]
        assert [(row['DTSTART;VALUE=DATE'], row['DTEND;VALUE=DATE']) for row in markers] == [
            ('20261004', '20261005'), ('20261005', '20261006')]
        assert all(row['SUMMARY'] == 'Synthetic Eid' and row['CATEGORIES'] == 'Public holidays'
                   and row['TRANSP'] == 'TRANSPARENT' for row in markers)
        update(h, 'date_sets', holiday, name='Renamed after publication')
        changed = client.get(path, headers={'If-None-Match': response.headers['etag']})
        assert changed.status_code == 304
        preserved = events(client.get(path))
        assert preserved == rows
    snapshot = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']
    static = events(calendar(snapshot, publication['publication_id'], publication['sequence'], me))
    assert len([row for row in static if 'DTSTART;VALUE=DATE' in row]) == 2
    assert next(row for row in static if 'DTSTART' in row)['X-FMUC-DATE-SETS'] == 'Public holidays: Synthetic Eid'
    duplicate = calendar_feed([(snapshot, 1, '2026-10-03T06:00:00Z'),
                               (snapshot, 2, '2026-10-03T06:00:00Z')], me, 'My duties')
    assert len([row for row in events(duplicate) if 'DTSTART;VALUE=DATE' in row]) == 2


def test_calendar_marker_uses_department_local_day(h):
    holiday = date_set(h, first='2026-10-04', last='2026-10-04')
    from packages.domain.reports import date_set_label
    snapshot = {'timezone': 'Asia/Riyadh', 'date_sets': {holiday['id']: holiday}}
    assert date_set_label(snapshot, '2026-10-03T21:30:00Z') == 'Public holidays: Synthetic Eid'
    assert date_set_label(snapshot, '2026-10-03T20:30:00Z') == ''
