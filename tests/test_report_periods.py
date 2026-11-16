import copy
import json
import pytest
from apps.api import models as m
from apps.api.db import transaction
from packages.domain.reports import METRIC_VERSION, report_csv, workload, workload_breakdown
from packages.domain.time import aware


@pytest.mark.parametrize('version', ['scheduled-v1', 'scheduled-v2', METRIC_VERSION])
@pytest.mark.parametrize('grouping', ['week', 'month'])
def test_offer_only_rows_respect_visible_people_effective_period_and_metric_version(monkeypatch, version, grouping):
    monkeypatch.setattr('packages.domain.reports.clock_now', lambda: aware('2026-10-03T06:00:00+00:00'))
    snapshot = {'metric_version': version, 'timezone': 'Asia/Riyadh', 'week_start': 6,
        'workspace': {'starts_on': '2026-10-01', 'ends_on': '2026-12-01', 'status': 'published'},
        'effective_until': '2026-11-01', 'input_revision': 1, 'availability_revision': 1,
        'people': {'visible': {'display_name': 'Visible person'}, 'later': {'display_name': 'Later person'},
                   'empty': {'display_name': 'No offers'}},
        'overtime_offered_intervals': {
            'visible': [('2026-10-20T08:00:00+03:00', '2026-10-20T16:00:00+03:00')],
            'hidden': [('2026-10-20T08:00:00+03:00', '2026-10-20T16:00:00+03:00')],
            'later': [('2026-10-31T23:00:00+03:00', '2026-11-02T01:00:00+03:00'),
                      ('2026-11-04T08:00:00+03:00', '2026-11-04T16:00:00+03:00')],
            'empty': []}, 'shifts': []}
    before = copy.deepcopy(snapshot)
    for report in (workload(snapshot), workload_breakdown(snapshot, grouping=grouping)):
        if version == METRIC_VERSION:
            assert {r['person_id']: r['overtime_offered_hours'] for r in report['rows']} == {'visible': 8, 'later': 1}
            assert all(r.get('period_start', '2026-10-01') < '2026-11-01' for r in report['rows'])
        else:
            legacy = {k: v for k, v in snapshot.items() if k != 'overtime_offered_intervals'}
            expected = workload_breakdown(legacy, grouping=grouping) if 'grouping' in report else workload(legacy)
            assert report['rows'] == []
            assert json.dumps(report) == json.dumps(expected)
            assert report_csv(report, 'synthetic-publication', 1) == report_csv(expected, 'synthetic-publication', 1)
    own = workload_breakdown(snapshot, person_id='visible', grouping=grouping)
    assert {r['person_id'] for r in own['rows']} == ({'visible'} if version == METRIC_VERSION else set())
    assert workload_breakdown(snapshot, person_id='empty', grouping=grouping)['rows'] == []
    assert snapshot == before


@pytest.mark.parametrize('offers,expected', [
    ([('2026-10-04T08:00:00+03:00', '2026-10-04T16:00:00+03:00'),
      ('2026-10-20T08:00:00+03:00', '2026-10-20T16:00:00+03:00')], 8),
    ([('2026-10-14T23:00:00+03:00', '2026-10-15T03:00:00+03:00')], 1),
])
def test_offered_hours_stop_at_publication_effective_local_midnight(offers, expected):
    person = '00000000-0000-0000-0000-000000000001'
    snapshot = {'metric_version': 'scheduled-v3', 'timezone': 'Asia/Riyadh',
        'workspace': {'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'status': 'published'},
        'effective_until': '2026-10-15', 'input_revision': 1, 'availability_revision': 1,
        'people': {person: {'display_name': 'Synthetic person'}},
        'overtime_offered_intervals': {person: offers},
        'shifts': [{'id': day, 'cancelled': False, 'type_snapshot': {'definition': {}},
                    'participants': [{'person_id': person, 'start': day + 'T08:00:00+03:00',
                                      'end': day + 'T12:00:00+03:00'}]}
                   for day in ('2026-10-04', '2026-10-14')]}
    before = copy.deepcopy(snapshot)
    assert workload(snapshot)['rows'][0]['overtime_offered_hours'] == expected
    for grouping in ('week', 'month'):
        report = workload_breakdown(snapshot, grouping=grouping)
        assert sum(row['overtime_offered_hours'] for row in report['rows']) == expected
    assert snapshot == before


def test_month_boundary_hours_counts_csv_and_personal_access(h):
    w = h.shift(h.workspace(start='2026-10-31', end='2026-11-02'),
        '2026-10-31T22:00:00+03:00', '2026-11-01T02:00:00+03:00')['workspace']
    pub = h.publish(w)
    url = '/api/reports/' + pub['publication_id']
    total = h.get('staff', url)
    report = h.get('staff', url + '?grouping=month')
    assert [r['scheduled_hours'] for r in report['rows']] == [2, 2]
    assert [r['assignments'] for r in report['rows']] == [1, 0]
    assert sum(r['scheduled_hours'] for r in report['rows']) == total['rows'][0]['scheduled_hours']
    assert all(r['person_id'] == h.seed['people'][0] for r in report['rows'])
    h.get('staff', url + '?grouping=month&person_id=' + h.seed['people'][1], status=403)
    response = h.client('staff').get(url + '?grouping=month&format=csv')
    assert response.status_code == 200 and 'period_end_exclusive' in response.text
    with transaction(h.app.state.engine) as db:
        snapshot = copy.deepcopy(db.get(m.Publication, pub['publication_id']).snapshot)
    p = snapshot['shifts'][0]['participants'][0]
    p['segments'] = [{'start': p['start'], 'end': '2026-10-31T23:00:00+03:00'},
                     {'start': '2026-11-01T01:00:00+03:00', 'end': p['end']}]
    before = copy.deepcopy(snapshot)
    result = workload_breakdown(snapshot, grouping='month')
    assert [r['scheduled_hours'] for r in result['rows']] == [1, 1]
    assert [r['off_cover_hours'] for r in result['rows']] == [1, 1]
    assert sum(r['reserved_hours'] for r in result['rows']) == workload(snapshot)['rows'][0]['reserved_hours']
    assert snapshot == before


def test_print_layouts_are_escaped_filtered_and_personal(h):
    person = next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][1])
    h.configure('people', {'display_name': '<script>Example</script>'}, person['id'], person['revision'])
    pub = h.publish(h.shift(h.workspace(), people=h.seed['people'][:2])['workspace'])
    url = '/api/reports/' + pub['publication_id'] + '?format=print'
    for layout in ('day', 'week', 'month'):
        team = h.client('coordinator').get(url + '&layout=' + layout)
        assert team.status_code == 200
        assert '&lt;script&gt;Example&lt;/script&gt;' in team.text
        assert '<script>Example' not in team.text
        atomic = '<span class="print-time">04 Oct 08:00–04 Oct 12:00</span>'
        assert team.text.count(atomic) == 3  # whole duty and both people's assigned times
        own = h.client('staff').get(url + '&layout=' + layout)
        assert own.status_code == 200 and 'Example' not in own.text and 'Alex Morgan' in own.text
        assert own.text.count(atomic) == 2  # only the authorised person's assigned time
    assert h.client('staff').get(url + '&person_id=' + person['id']).status_code == 403
    assert h.client('coordinator').get(url + '&layout=year').status_code == 422
    assert h.client('coordinator').get(url + '&service_ids=' + h.seed['services'][0]).status_code == 200
    assert h.client('coordinator').get(url + '&service_ids=missing').status_code == 404
    assert 'No published duties match' in h.client('coordinator').get(url + '&layout=day&on=2027-01-01').text
