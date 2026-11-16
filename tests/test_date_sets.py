"""Date-set arithmetic is independent of persistence and scheduling callers."""
import copy
from datetime import date

import pytest

from packages.domain.date_sets import (
    copy_set_to_next_year, date_set_markers, on_date_set, parse_import,
    date_set_days, prepare_date_sets, resolve_date_sets, validate_entry,
)


def entry(**changes):
    return {'id': 'eid', 'name': 'Eid al-Fitr', 'repeat': 'yearly_hijri',
            'first_day': '1446-10-01', 'last_day': '1446-10-04',
            'last_day_of_month': False, 'overrides': [], **changes}


def data(*entries, **changes):
    return {'date_sets': {'holidays': {'id': 'holidays', 'name': 'Public holidays',
                                     'active': True, 'entries': list(entries), **changes}}}


# Published primary references identify the actual Umm al-Qura date, independently
# of the conversion package. See docs/design/date-sets.md for source links.
@pytest.mark.parametrize(('year', 'gregorian'), [
    (1444, '2023-03-23'), (1445, '2024-03-11'), (1446, '2025-03-01'),
])
def test_ramadan_matches_published_umm_al_qura_dates_for_three_years(year, gregorian):
    rows = resolve_date_sets(data(entry(first_day='1446-09-01', last_day='1446-09-01')),
                             date.fromisoformat(gregorian), date.fromisoformat(gregorian))
    assert [(row['year'], row['first_day'], row['last_day']) for row in rows] == [(year, gregorian, gregorian)]


def test_multi_day_eid_span_crosses_gregorian_month_boundary_inclusively():
    value = data(entry())
    rows = resolve_date_sets(value, '2025-03-01', '2025-04-30')
    assert [(row['first_day'], row['last_day']) for row in rows] == [('2025-03-30', '2025-04-02')]
    for day in ('2025-03-30', '2025-03-31', '2025-04-01', '2025-04-02'):
        assert on_date_set(value, 'holidays', day, ['Eid al-Fitr'])
    assert not on_date_set(value, 'holidays', '2025-04-03')
    assert not on_date_set(value, 'holidays', '2025-03-30', ['Different entry'])


def test_yearly_gregorian_repeat_and_cross_year_span():
    value = data(entry(id='national', name='National Day', repeat='yearly_gregorian',
                       first_day='2025-09-23', last_day='2025-09-23'),
                 entry(id='break', name='Year break', repeat='yearly_gregorian',
                       first_day='2025-12-30', last_day='2026-01-02'))
    rows = resolve_date_sets(value, '2026-01-01', '2027-12-31')
    assert [(row['name'], row['first_day'], row['last_day']) for row in rows] == [
        ('Year break', '2025-12-30', '2026-01-02'),
        ('National Day', '2026-09-23', '2026-09-23'),
        ('Year break', '2026-12-30', '2027-01-02'),
        ('National Day', '2027-09-23', '2027-09-23'),
        ('Year break', '2027-12-30', '2028-01-02')]


def test_hijri_override_replaces_one_native_year_and_cache_tracks_mutation():
    value = data(entry())
    assert on_date_set(value, 'holidays', '2025-03-30')
    value['date_sets']['holidays']['entries'][0]['overrides'] = [
        {'year': 1446, 'first_day': '2025-03-31', 'last_day': '2025-04-03'}]
    assert not on_date_set(value, 'holidays', '2025-03-30')
    rows = resolve_date_sets(value, '2024-01-01', '2026-12-31')
    assert [(row['year'], row['first_day'], row['adjusted']) for row in rows] == [
        (1445, '2024-04-10', False), (1446, '2025-03-31', True), (1447, '2026-03-20', False)]


def test_ramadan_end_uses_actual_month_length_and_two_native_years_in_one_gregorian_year():
    value = data(entry(name='Ramadan', first_day='1446-09-01', last_day='1446-09-01', last_day_of_month=True))
    rows = resolve_date_sets(value, '2024-01-01', '2025-12-31')
    assert [(row['first_day'], row['last_day']) for row in rows] == [
        ('2024-03-11', '2024-04-09'), ('2025-03-01', '2025-03-29')]
    value['date_sets']['holidays']['entries'][0]['overrides'] = [
        {'year': 1452, 'first_day': '2030-12-27', 'last_day': '2031-01-24'}]
    rows = resolve_date_sets(value, '2030-01-01', '2030-12-31')
    assert [(row['year'], row['first_day']) for row in rows] == [(1451, '2030-01-05'), (1452, '2030-12-27')]


def test_markers_are_named_scoped_and_no_configuration_remains_empty():
    assert date_set_markers({}, date(2025, 3, 30)) == []
    assert date_set_markers(data(entry(), active=False), date(2025, 3, 30)) == []
    assert date_set_markers(data(entry()), date(2025, 3, 30)) == [
        {'set_id': 'holidays', 'set_name': 'Public holidays', 'entry_id': 'eid', 'name': 'Eid al-Fitr'}]
    assert resolve_date_sets(data(entry()), '2025-03-30', '2025-03-30', set_ids=[]) == []


def test_native_leap_dates_skip_nonexistent_years_and_allow_explicit_override():
    value = data(entry(repeat='yearly_gregorian', first_day='2024-02-29', last_day='2024-02-29'))
    assert resolve_date_sets(value, '2025-01-01', '2025-12-31') == []
    assert len(resolve_date_sets(value, '2024-01-01', '2024-12-31')) == 1
    value['date_sets']['holidays']['entries'][0]['overrides'] = [
        {'year': 2025, 'first_day': '2025-02-28', 'last_day': '2025-02-28'}]
    assert on_date_set(value, 'holidays', '2025-02-28')


@pytest.mark.parametrize('changes', [
    {'first_day': '1446-13-01'}, {'first_day': '1446-10-31'},
    {'first_day': '1501-01-01', 'last_day': '1501-01-02'},
    {'first_day': '1342-01-01', 'last_day': '1342-01-02'},
    {'last_day': '1446-09-01'}, {'last_day': '1447-10-01'},
    {'repeat': 'weekly'}, {'first_day': '1446-1-01'},
    {'last_day': '1446-10-99', 'last_day_of_month': True},
    {'overrides': [{'year': 1446, 'first_day': '2025-03-30', 'last_day': '2026-04-01'}]},
    {'overrides': [{'year': 1446, 'first_day': '2025-03-30', 'last_day': '2025-04-02'}] * 2},
])
def test_invalid_native_entries_and_ambiguous_overrides_are_rejected(changes):
    with pytest.raises(ValueError):
        validate_entry(entry(**changes))


def test_once_entry_cannot_override_other_year_and_reversed_ranges_fail():
    with pytest.raises(ValueError, match='original year'):
        validate_entry(entry(repeat='once', first_day='2025-03-30', last_day='2025-04-02',
                             overrides=[{'year': 2026, 'first_day': '2026-03-30', 'last_day': '2026-04-02'}]))
    with pytest.raises(ValueError, match='range last day'):
        resolve_date_sets({}, '2025-04-02', '2025-03-30')


@pytest.mark.parametrize(('first', 'last'), [('1924-07-31', '1924-08-01'), ('2077-11-16', '2077-11-17')])
def test_hijri_resolution_outside_published_table_fails_explicitly(first, last):
    with pytest.raises(ValueError, match='1924-08-01 to 2077-11-16'):
        resolve_date_sets(data(entry()), first, last)


def test_cross_native_year_span_at_table_end_is_unresolved_instead_of_silently_absent():
    value = data(entry(first_day='1446-12-25', last_day='1447-01-05'))
    with pytest.raises(ValueError, match='1343-1500'):
        resolve_date_sets(value, '2077-11-11', '2077-11-16')
    assert resolve_date_sets(value, '2077-10-01', '2077-10-02') == []


def test_cross_native_year_tail_at_table_start_is_unresolved_instead_of_silently_absent():
    value = data(entry(first_day='1446-12-28', last_day='1447-01-03'))
    with pytest.raises(ValueError, match='starts before supported 1343'):
        resolve_date_sets(value, '1924-08-01', '1924-08-03')
    assert resolve_date_sets(value, '1924-08-04', '1924-08-05') == []
    value['date_sets']['holidays']['entries'][0]['last_day_of_month'] = True
    with pytest.raises(ValueError, match='starts before supported 1343'):
        resolve_date_sets(value, '1924-08-04', '1924-08-05')


def ical(*lines):
    return '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', *lines,
                         'END:VEVENT', 'END:VCALENDAR'])


def test_csv_import_keeps_native_spans_and_validates_every_row():
    result = parse_import('name,first_day,last_day,repeat,last_day_of_month\n'
                          'Ramadan,1446-09-01,1446-09-01,yearly_hijri,true\n', 'csv')
    assert result[0]['name'] == 'Ramadan' and result[0]['last_day_of_month'] is True
    assert result[0]['repeat'] == 'yearly_hijri' and result[0]['id']
    with pytest.raises(ValueError):
        parse_import('name,first_day,last_day\nValid,2025-01-01,2025-01-02\nBad,invalid,invalid', 'csv')


def test_ical_import_unfolds_escaped_names_and_exclusive_end_without_losing_recurrence():
    result = parse_import(ical('SUMMARY:Eid\\, public', ' holiday',
                               'DTSTART;VALUE=DATE:20250330', 'DTEND;VALUE=DATE:20250403'), 'ical')
    assert (result[0]['name'], result[0]['first_day'], result[0]['last_day']) == (
        'Eid, publicholiday', '2025-03-30', '2025-04-02')
    result = parse_import(ical('SUMMARY:National Day', 'DTSTART;VALUE=DATE:20250923',
                               'RRULE:FREQ=YEARLY;INTERVAL=1'), 'ical')
    assert result[0]['repeat'] == 'yearly_gregorian'
    assert result[0]['last_day'] == '2025-09-23'


def test_ical_gregorian_boundaries_do_not_overflow_the_import_trust_boundary():
    rows = parse_import(ical('SUMMARY:Final day', 'DTSTART;VALUE=DATE:99991231'), 'ical')
    assert rows[0]['first_day'] == rows[0]['last_day'] == '9999-12-31'
    with pytest.raises(ValueError, match='exclusive end'):
        parse_import(ical('SUMMARY:Invalid end', 'DTSTART;VALUE=DATE:00010101',
                          'DTEND;VALUE=DATE:00010101'), 'ical')
    annual = parse_import(ical('SUMMARY:Year end', 'DTSTART;VALUE=DATE:99981231',
                                'DTEND;VALUE=DATE:99990102', 'RRULE:FREQ=YEARLY'), 'ical')[0]
    assert len(resolve_date_sets(data(annual), '9998-12-31', '9999-01-01')) == 1
    with pytest.raises(ValueError, match='fixed-duration occurrence ends outside'):
        resolve_date_sets(data(annual), '9999-12-31', '9999-12-31')


@pytest.mark.parametrize(('start', 'end', 'expected'), [
    ('20250228', '20250302', [('2025-02-28', '2025-03-01'), ('2026-02-28', '2026-03-01'),
                            ('2027-02-28', '2027-03-01'), ('2028-02-28', '2028-02-29')]),
    ('20240228', '20240301', [('2024-02-28', '2024-02-29'), ('2025-02-28', '2025-03-01'),
                            ('2026-02-28', '2026-03-01'), ('2027-02-28', '2027-03-01'),
                            ('2028-02-28', '2028-02-29')]),
    ('20240229', '20240302', [('2024-02-29', '2024-03-01'), ('2028-02-29', '2028-03-01')]),
    ('20251231', '20260102', [('2025-12-31', '2026-01-01'), ('2026-12-31', '2027-01-01'),
                            ('2027-12-31', '2028-01-01'), ('2028-12-31', '2029-01-01')]),
])
def test_ical_yearly_repeat_keeps_onset_and_duration_across_leap_years(start, end, expected):
    imported = parse_import(ical('SUMMARY:Two days', f'DTSTART;VALUE=DATE:{start}',
                                 f'DTEND;VALUE=DATE:{end}', 'RRULE:FREQ=YEARLY'), 'ical')[0]
    assert imported['start_year'] == int(start[:4]) and imported['fixed_duration'] is True
    rows = resolve_date_sets(data(imported), '2023-01-01', '2028-12-31')
    assert [(row['first_day'], row['last_day']) for row in rows] == expected
    assert all((date.fromisoformat(row['last_day']) - date.fromisoformat(row['first_day'])).days == 1
               for row in rows)


def test_ical_yearly_onset_applies_to_overrides_and_single_day_imports():
    imported = parse_import(ical('SUMMARY:Annual day', 'DTSTART;VALUE=DATE:20250228',
                                 'RRULE:FREQ=YEARLY;INTERVAL=1'), 'ical')[0]
    imported['overrides'] = [{'year': 2024, 'first_day': '2024-02-28', 'last_day': '2024-02-28'}]
    assert resolve_date_sets(data(imported), '2024-01-01', '2024-12-31') == []
    assert [(row['first_day'], row['last_day']) for row in
            resolve_date_sets(data(imported), '2028-01-01', '2028-12-31')] == [('2028-02-28', '2028-02-28')]


def test_manual_gregorian_repeats_keep_calendar_endpoints_and_no_onset():
    value = entry(repeat='yearly_gregorian', first_day='2025-02-28', last_day='2025-03-01')
    assert [(row['first_day'], row['last_day']) for row in
            resolve_date_sets(data(value), '2024-01-01', '2024-12-31')] == [('2024-02-28', '2024-03-01')]


@pytest.mark.parametrize('unsupported', [
    'RRULE:FREQ=YEARLY;COUNT=3', 'RRULE:FREQ=YEARLY;UNTIL=20300228',
    'RRULE:FREQ=YEARLY;INTERVAL=2', 'RRULE:FREQ=YEARLY;BYMONTH=2',
    'RRULE:FREQ=YEARLY;BYMONTHDAY=28', 'RRULE:FREQ=YEARLY;BYDAY=MO',
    'RRULE:FREQ=YEARLY;BYSETPOS=1', 'RRULE:FREQ=YEARLY;FREQ=YEARLY',
    'EXDATE;VALUE=DATE:20260228', 'RDATE;VALUE=DATE:20260301',
    'DTEND;VALUE=DATE:20270228',
])
def test_ical_rejects_unrepresentable_events_with_event_context_and_no_partial_import(unsupported):
    content = ical('SUMMARY:Valid', 'DTSTART;VALUE=DATE:20250101').replace(
        'END:VCALENDAR', '\r\n'.join(['BEGIN:VEVENT', 'SUMMARY:Unsupported',
                                     'DTSTART;VALUE=DATE:20250228', unsupported, 'END:VEVENT', 'END:VCALENDAR']))
    if unsupported.startswith('DTEND'):
        content = content.replace(unsupported, unsupported + '\r\nRRULE:FREQ=YEARLY')
    with pytest.raises(ValueError, match='iCal event 2:'):
        parse_import(content, 'ical')


@pytest.mark.parametrize('lines', [
    ['SUMMARY:Timed', 'DTSTART:20250330T080000Z'],
    ['SUMMARY:Cancelled', 'DTSTART;VALUE=DATE:20250330', 'STATUS:CANCELLED'],
    ['SUMMARY:Alarm', 'DTSTART;VALUE=DATE:20250330', 'BEGIN:VALARM', 'SUMMARY:Notify', 'END:VALARM'],
    ['SUMMARY:Limited', 'DTSTART;VALUE=DATE:20250330', 'RRULE:FREQ=YEARLY;COUNT=3'],
    ['SUMMARY:Weekly', 'DTSTART;VALUE=DATE:20250330', 'RRULE:FREQ=WEEKLY'],
    ['SUMMARY:Exception', 'DTSTART;VALUE=DATE:20250330', 'EXDATE;VALUE=DATE:20260330'],
    ['SUMMARY:Reversed', 'DTSTART;VALUE=DATE:20250330', 'DTEND;VALUE=DATE:20250330'],
    ['SUMMARY:Duplicate', 'SUMMARY:Again', 'DTSTART;VALUE=DATE:20250330'],
    ['DTSTART;VALUE=DATE:20250330'],
])
def test_ical_import_rejects_unsupported_or_malformed_semantics(lines):
    with pytest.raises(ValueError):
        parse_import(ical(*lines), 'ical')


@pytest.mark.parametrize(('content', 'format'), [
    ('', 'csv'), ('name,first_day,last_day\n', 'csv'), ('name,first_day,last_day,unknown', 'csv'),
    ('name,name,first_day,last_day\nX,Y,2025-01-01,2025-01-01', 'csv'),
    ('name,first_day,last_day\nX,2025-01-01', 'csv'), ('BEGIN:VEVENT', 'ical'), ('x', 'unknown'),
    ('"unclosed header', 'csv'),
])
def test_import_errors_are_explicit(content, format):
    with pytest.raises(ValueError):
        parse_import(content, format)


def test_copy_moves_only_once_entries_and_preserves_source_recurrences():
    value = data(entry(), entry(id='national', name='National Day', repeat='yearly_gregorian',
                               first_day='2025-09-23', last_day='2025-09-23'),
                 entry(id='once', name='Local break', repeat='once',
                               first_day='2025-08-01', last_day='2025-08-03'))['date_sets']['holidays']
    original = copy.deepcopy(value)
    result = copy_set_to_next_year(value, 2025)
    assert result['name'] == 'Public holidays 2026'
    assert {row['name']: (row['first_day'], row['last_day'], row['repeat']) for row in result['entries']} == {
        'Local break': ('2026-08-01', '2026-08-03', 'once')}
    assert all(row['id'] not in {source['id'] for source in value['entries']} for row in result['entries'])
    assert value == original
    combined = {'date_sets': {'holidays': value, 'copy': {'id': 'copy', **result}}}
    rows = resolve_date_sets(combined, '2026-01-01', '2026-12-31')
    assert [row['name'] for row in rows].count('Eid al-Fitr') == 1
    assert [row['name'] for row in rows].count('National Day') == 1


@pytest.mark.parametrize('last_day', ['1445-09-30', '1446-09-30'])
def test_hijri_day_thirty_end_requires_explicit_month_end_in_long_and_short_years(last_day):
    value = entry(first_day=last_day[:4] + '-09-01', last_day=last_day)
    with pytest.raises(ValueError, match='day-30 end requires last_day_of_month'):
        validate_entry(value)
    value['last_day_of_month'] = True
    validate_entry(value)
    rows = resolve_date_sets(data(value), '2024-01-01', '2025-12-31')
    assert [(row['first_day'], row['last_day']) for row in rows] == [
        ('2024-03-11', '2024-04-09'), ('2025-03-01', '2025-03-29')]


def test_invalid_hijri_occurrence_is_reported_instead_of_silently_skipped():
    with pytest.raises(ValueError, match='Invalid Umm al-Qura date'):
        resolve_date_sets(data(entry(first_day='1445-09-01', last_day='1445-09-30')),
                          '2025-01-01', '2025-12-31')


@pytest.mark.parametrize('month_end', [False, True])
def test_hijri_start_rejects_day_thirty_even_when_it_exists_in_the_stored_year(month_end):
    with pytest.raises(ValueError, match='Not every Hijri month has a 30th; start on the 29th or earlier'):
        validate_entry(entry(first_day='1448-08-30', last_day='1448-09-01', last_day_of_month=month_end))
    validate_entry(entry(first_day='1448-08-29', last_day='1448-09-01', last_day_of_month=month_end))


def test_stored_bad_entry_does_not_break_other_entries_sets_markers_or_exports():
    from packages.domain.reports import date_set_events, date_set_label

    bad = entry(id='bad', name='Old Shaban end', first_day='1448-08-30', last_day='1448-09-01')
    good = entry(id='good', name='Local holiday', repeat='once', first_day='2025-02-28', last_day='2025-02-28')
    value = data(bad, good)
    value['date_sets']['other'] = {'id': 'other', 'name': 'Other dates', 'entries': [{**good, 'id': 'other-good'}]}
    value['workspace'] = {'starts_on': '2025-02-28', 'ends_on': '2025-03-01'}
    errors = {}
    rows = resolve_date_sets(value, '2025-02-28', '2025-02-28', errors=errors)
    assert {row['entry_id'] for row in rows} == {'good', 'other-good'}
    assert list(errors) == [('holidays', 'Old Shaban end')]
    assert on_date_set(value, 'holidays', '2025-02-28', ['Local holiday'])
    assert on_date_set(value, 'other', '2025-02-28')
    with pytest.raises(ValueError, match='Invalid Umm al-Qura date'):
        on_date_set(value, 'holidays', '2025-02-28', ['Old Shaban end'])
    assert {marker['entry_id'] for marker in date_set_markers(value, '2025-02-28')} == {'good', 'other-good'}
    assert date_set_label(value, '2025-02-28T08:00:00Z') == 'Other dates: Local holiday; Public holidays: Local holiday'
    assert len(date_set_events(value, 1, '20250228T000000Z')) == 2


def test_preparing_date_sets_leaves_the_source_snapshot_unchanged():
    value = data(entry())
    before = copy.deepcopy(value)
    first, last = date(2025, 3, 30), date(2025, 4, 2)
    cached = prepare_date_sets(value, first, last)
    assert cached['days'][first] == frozenset({('holidays', 'Eid al-Fitr')})
    assert value == before

    prepared = {**value, '_date_set_cache': cached}
    before = copy.deepcopy(prepared)
    extended = prepare_date_sets(prepared, '2025-03-29', '2025-04-03')
    assert (extended['first'], extended['last']) == (date(2025, 3, 29), date(2025, 4, 3))
    assert prepared == before


def test_day_index_is_content_keyed_and_keeps_markers_and_memberships_current():
    value = data(entry())
    first, last = date(2025, 1, 1), date(2025, 12, 31)
    cached = value['_date_set_cache'] = prepare_date_sets(value, first, last)
    assert prepare_date_sets(value, first, last) is cached
    assert date_set_days(value, date(2025, 3, 30)) == frozenset({('holidays', 'Eid al-Fitr')})
    value['date_sets']['holidays']['entries'][0]['overrides'] = [
        {'year': 1446, 'first_day': '2025-03-31', 'last_day': '2025-04-03'}]
    value['_date_set_cache'] = prepare_date_sets(value, first, last)
    assert value['_date_set_cache'] is not cached
    assert date_set_days(value, date(2025, 3, 30)) == frozenset()
    assert not date_set_markers(value, date(2025, 3, 30))
    assert date_set_markers(value, date(2025, 3, 31))[0]['name'] == 'Eid al-Fitr'
    cached = value['_date_set_cache']
    value['date_sets']['holidays']['revision'] = 2
    assert prepare_date_sets(value, first, last) is not cached


@pytest.mark.parametrize('span', [('2077-01-01', '2077-12-31'), ('2077-11-15', '2077-11-17')])
def test_indexed_hijri_range_errors_do_not_contaminate_supported_days(span):
    value = data(entry(first_day='1446-12-25', last_day='1447-01-05') if span[0] == '2077-01-01' else entry())
    value['_date_set_cache'] = prepare_date_sets(value, *span)
    supported = '2077-01-01' if span[0] == '2077-01-01' else '2077-11-15'
    expected = resolve_date_sets(value, supported, supported)
    assert on_date_set(value, 'holidays', supported) is bool(expected)
    assert date_set_days(value, supported) == frozenset((row['set_id'], row['name']) for row in expected)
    assert date_set_markers(value, supported) == [
        {key: row[key] for key in ('set_id', 'set_name', 'entry_id', 'name')} for row in expected]
    with pytest.raises(ValueError, match='Umm al-Qura'):
        date_set_days(value, '2077-11-17')


def test_day_index_keeps_extended_bounds_for_calendar_timezones(monkeypatch):
    from packages.domain import date_sets
    value = data(entry())
    first = date(2025, 3, 29)
    last = date(2025, 4, 3)
    value['_date_set_cache'] = prepare_date_sets(value, first, first)
    extended = value['_date_set_cache'] = prepare_date_sets(value, last, last)
    assert extended['first'] == first and extended['last'] == last

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError('Extending one edge must retain the other cached edge')

    monkeypatch.setattr(date_sets, 'resolve_date_sets', unexpected_resolution)
    assert not date_set_days(value, first)
    assert not date_set_days(value, last)
    assert prepare_date_sets(value, first, last) is extended


def test_range_error_recovery_keeps_same_name_markers_unique_and_stably_ordered():
    value = data(entry(id='a-hijri', name='Holiday'),
                 entry(id='z-once', name='Holiday', repeat='once',
                       first_day='2077-01-01', last_day='2077-01-01'))
    value['_date_set_cache'] = prepare_date_sets(value, '2077-01-01', '2077-12-31')
    expected = [{'set_id': 'holidays', 'set_name': 'Public holidays', 'entry_id': 'z-once', 'name': 'Holiday'}]
    assert date_set_markers(value, '2077-01-01') == expected
    assert date_set_markers(value, '2077-01-01') == expected


def test_range_error_recovery_isolates_invalid_entries_with_the_same_name():
    value = data(entry(id='bad', name='Holiday', first_day='1448-08-30', last_day='1448-09-01'),
                 entry(id='good', name='Holiday'))
    value['_date_set_cache'] = prepare_date_sets(value, '1900-01-01', '2025-12-31')
    expected = [{'set_id': 'holidays', 'set_name': 'Public holidays', 'entry_id': 'good', 'name': 'Holiday'}]
    assert date_set_markers(value, '2025-03-30') == expected
    assert date_set_markers(value, '2025-03-30') == expected
    with pytest.raises(ValueError, match='Invalid Umm al-Qura date'):
        date_set_days(value, '2025-03-30', ['holidays'], ['Holiday'])


def test_copy_rejects_nonexistent_leap_day_and_uses_explicit_target_name():
    value = data(entry(repeat='once', first_day='2024-02-29', last_day='2024-02-29'))['date_sets']['holidays']
    with pytest.raises(ValueError, match='no date next year'):
        copy_set_to_next_year(value, 2024)
    value['name'] = 'X' * 160
    with pytest.raises(ValueError, match='160 characters'):
        copy_set_to_next_year(value, 2025)
    assert copy_set_to_next_year(value, 2025, name='Next year')['name'] == 'Next year'
