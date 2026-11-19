"""Recorded teams have one order in print, CSV, downloads and calendar subscriptions."""
import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from packages.domain.reports import assignments_csv, calendar, calendar_feed, schedule_print
from .governance_support import identity
from .test_home_team_payload import supervision_type


def snapshot():
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    rows = [{'id': pid, 'person_id': pid, 'start': start, 'end': end, 'overtime': False,
             'supervisor_person_id': sup, 'rank': rank}
            for pid, sup, rank in [('a', 's', 'R2'), ('s', None, ''), ('b', 's', 'R1'), ('other', None, '')]]
    return {'workspace': {'name': 'Synthetic rota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'status': 'published'},
            'timezone': 'Asia/Riyadh', 'people': {pid: {'display_name': name} for pid, name in
                [('s', 'Dr X'), ('a', 'A'), ('b', 'B'), ('other', 'Other synthetic')]},
            'locations': {'l': {'name': 'Synthetic room'}}, 'services': {'c': {'name': 'Synthetic clinic'}},
            'shifts': [{'id': 'duty', 'assignment_type_id': 't', 'location_id': 'l', 'service_ids': ['c'],
                        'start': start, 'end': end, 'attributes': {}, 'cancelled': False, 'participants': rows,
                        'type_snapshot': {'name': 'Synthetic duty', 'definition': {'fields': []}}}]}


def exports(data, person='s'):
    return [assignments_csv(data, 'p', 1), schedule_print(data, 1, True, stylesheet_url='/print.css'),
            calendar(data, 'p', 1, person, calendar_name='Synthetic department rota').replace('\r\n ', ''),
            calendar_feed([(data, 1, datetime(2026, 10, 1, tzinfo=timezone.utc))], person, 'Synthetic feed').replace('\r\n ', '')]


def test_team_summary_in_all_reports_retains_supervisor_column_and_attendance():
    data = snapshot()
    csv_text, printed, static, feed = exports(data)
    team = 'Supervisor: Dr X — R2 A, R1 B'
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert [r['team'] for r in rows] == [team, team, team, '']
    assert rows[0]['supervisor'] == 'Dr X'
    assert team in printed and '04 Oct 08:00' in printed
    assert printed.count(team) == 1
    for text in (static, feed):
        assert team.replace(',', '\\,') not in text
        assert all(name not in text for name in ('Dr X', 'Other synthetic', 'R1', 'R2', 'Supervisor:'))
        assert 'Synthetic duty' in text and 'Synthetic room' in text
        assert text.count('BEGIN:VEVENT') == 1


def test_unknown_supervisor_uses_honest_fallback_and_reports_escape_names():
    data = snapshot()
    data['shifts'][0]['participants'] = [data['shifts'][0]['participants'][0]]
    data['people'].pop('s')
    for text in exports(data, 'a')[:2]:
        assert 'not in this rota' in text and 'Assigned' not in text
    for text in exports(data, 'a')[2:]:
        assert 'not in this rota' not in text and 'Assigned' not in text
        assert 'R2' not in text and 'Dr X' not in text
    data['people']['a']['display_name'] = '<script>synthetic</script>,Comma\nNewline'
    csv_text, printed, static, feed = exports(data, 'a')
    assert '<script>' not in printed and '&lt;script&gt;' in printed
    assert list(csv.DictReader(io.StringIO(csv_text)))[0]['person'] == data['people']['a']['display_name']
    assert 'Comma' in printed and 'Newline' in printed
    assert '\\,Comma\\nNewline' not in static and '\\,Comma\\nNewline' not in feed


@pytest.mark.postgres
def test_supervisor_personal_exports_include_only_their_issued_trainees(pg):
    h = pg
    trainee, supervisor, unrelated = h.seed['people']
    identity(h, 'synthetic_supervisor', ['staff'], supervisor)
    identity(h, 'synthetic_trainee', ['staff'], trainee)
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace(), typ=supervision_type(h), people=[
        {'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'},
        {'person_id': unrelated, 'start': start, 'end': end}])['workspace']
    pub = h.publish(w)['publication_id']
    snapshot = h.get('coordinator', f'/api/publications/{pub}')['snapshot']
    people = snapshot['people']
    duty = snapshot['shifts'][0]
    ics_values = (duty['type_snapshot']['name'], snapshot['locations'][duty['location_id']]['name'], 'DTSTART:', 'DTEND:')
    url = f'/api/reports/{pub}'
    for format in ('assignments', 'print', 'ics'):
        response = h.client('synthetic_supervisor').get(url + '?format=' + format + ('&person_id=' + supervisor if format == 'ics' else ''))
        assert response.status_code == 200
        text = response.text.replace('\r\n ', '')
        if format in ('assignments', 'print'):
            assert people[trainee]['display_name'] in text
        else:
            assert all(name not in text for name in (people[trainee]['display_name'], people[unrelated]['display_name'],
                                                     people[supervisor]['display_name'], 'R1', 'R2'))
            assert all(value in text for value in ics_values)
        assert people[unrelated]['display_name'] not in text
    department_name = h.get('synthetic_supervisor', '/api/me')['department']['name']
    for role, own, other in (('synthetic_supervisor', supervisor, trainee), ('synthetic_trainee', trainee, supervisor)):
        path = h.command(role, 'calendar_feed.create')['path']
        with TestClient(h.app, base_url=h.settings.public_origin) as client:
            feed = client.get(path)
        assert feed.status_code == 200
        text = feed.text.replace('\r\n ', '')
        assert f'X-WR-CALNAME:{department_name} rota' in text
        assert all(name not in text for name in (people[other]['display_name'], people[unrelated]['display_name'],
                                                 people[own]['display_name'], 'R1', 'R2'))
        assert all(value in text for value in ics_values)


def test_personal_calendar_does_not_disclose_other_trainees_or_other_sessions():
    data = snapshot()
    for text in exports(data, 'a')[2:]:
        assert all(value not in text for value in ('R2 A', 'Dr X', 'R1 B', 'Other synthetic', 'R1', 'R2'))
    printed = schedule_print(data, 1, True, person_id='s', stylesheet_url='/print.css')
    assert 'Supervisor: Dr X — R2 A, R1 B' in printed


def test_legacy_attendance_without_ids_and_repeated_supervisor_rows_remain_exportable():
    data = snapshot()
    for p in data['shifts'][0]['participants']:
        p.pop('id')
    assert all(exports(data))
    data = snapshot()
    supervisor = data['shifts'][0]['participants'][1]
    data['shifts'][0]['participants'].append({**supervisor, 'id': 's-later',
        'start': '2026-10-04T14:00:00+03:00', 'end': '2026-10-04T16:00:00+03:00'})
    rows = list(csv.DictReader(io.StringIO(exports(data)[0])))
    assert rows[1]['team'] == rows[-1]['team'] == 'Supervisor: Dr X — R2 A, R1 B'
