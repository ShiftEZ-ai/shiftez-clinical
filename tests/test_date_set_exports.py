"""Date-entry labels retain the shared CSV formula-injection protection."""
import copy
import csv
import io

import pytest

from packages.contracts.schemas import DateSetEntry
from packages.domain.reports import calendar, calendar_feed, safe_cell


@pytest.mark.parametrize('name', ['=SUM(A1:A2)', '+holiday', '-holiday', '@holiday'])
def test_date_entry_names_are_safe_in_csv_cells(name):
    entry = DateSetEntry(name=name, first_day='2026-10-04', last_day='2026-10-04')
    output = io.StringIO(newline='')
    csv.writer(output).writerow([safe_cell(entry.name)])
    assert next(csv.reader(io.StringIO(output.getvalue()))) == ["'" + name]


@pytest.mark.parametrize('version,expected_duties', [('scheduled-v1', 1), ('scheduled-v2', 2), ('scheduled-v3', 2)])
def test_date_markers_preserve_versioned_presence_in_static_and_subscribed_calendars(h, version, expected_duties):
    h.configure('date_sets', {'name': 'Public holidays', 'entries': [{
        'name': 'Synthetic holiday', 'first_day': '2026-10-04', 'last_day': '2026-10-04'}]})
    publication = h.publish(h.shift(h.workspace())['workspace'])
    snapshot = h.get('coordinator', '/api/publications/' + publication['publication_id'])['snapshot']
    snapshot['metric_version'] = version
    participant, = snapshot['shifts'][0]['participants']
    participant['segments'] = [
        {'start': participant['start'], 'end': '2026-10-04T10:00:00+03:00'},
        {'start': '2026-10-04T10:30:00+03:00', 'end': participant['end']}]
    before = copy.deepcopy(snapshot)
    for exported in (calendar(snapshot, publication['publication_id'], 1, participant['person_id']),
                     calendar_feed([(snapshot, 1, '2026-10-03T06:00:00Z')], participant['person_id'], 'My duties')):
        assert snapshot == before
        text = exported.replace('\r\n ', '')
        assert text.count('BEGIN:VEVENT') == expected_duties + 1
        assert text.count('DTSTART;VALUE=DATE:20261004') == 1
        assert text.count('X-FMUC-DATE-SETS:Public holidays: Synthetic holiday') == expected_duties
        if expected_duties == 2:
            assert 'DTEND:20261004T070000Z' in text and 'DTSTART:20261004T073000Z' in text
        else:
            assert 'DTSTART:20261004T050000Z' in text and 'DTEND:20261004T090000Z' in text
