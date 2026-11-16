from .test_alpha_calendar import programme
import pytest


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_academic_dates_do_not_invent_membership_or_absence(request, database):
    h = request.getfixturevalue(database)
    p, _, _ = programme(h)
    current = h.configure('programme_calendar_entries', {'programme_id': p['id'], 'name': 'October block',
        'kind': 'academic_block', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')['record']
    h.configure('programme_calendar_entries', {'programme_id': p['id'], 'name': 'Later break',
        'kind': 'break', 'starts_on': '2026-11-01', 'ends_on': '2026-11-03'}, role='coordinator')
    url = '/api/calendar?start=2026-10-04&end=2026-10-11'
    team = h.get('coordinator', url)
    assert team['programme_calendar'][0]['confirmed_members'] == 1
    own = h.get('staff', url)
    assert [r['id'] for r in own['programme_calendar']] == [current['id']]
    assert 'confirmed_members' not in own['programme_calendar'][0]
    assert not own['events']  # An academic date is not an assigned duty or absence.
    later = '/api/calendar?start=2026-11-01&end=2026-11-08'
    assert h.get('coordinator', later)['programme_calendar'][0]['confirmed_members'] == 0
    assert h.get('staff', later)['programme_calendar'] == []
    h.configure('programme_calendar_entries', {'programme_id': p['id'], 'name': 'Forbidden edit',
        'kind': 'notice', 'starts_on': '2026-10-01', 'ends_on': '2026-10-02'}, role='staff', status=403)
    assert len(h.get('coordinator', '/api/catalogues')['programme_memberships']) == 1


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('second_start,covered', [('2026-10-15', 1), ('2026-10-16', 0)])
def test_academic_calendar_counts_continuous_membership_over_the_whole_entry(request, database, second_start, covered):
    h = request.getfixturevalue(database)
    p = h.configure('programmes', {'name': 'Continuous membership', 'starts_on': '2026-01-01'})['record']
    person = h.seed['people'][0]
    for first, last in [('2026-10-01', '2026-10-15'), (second_start, '2026-11-01')]:
        h.configure('programme_memberships', {'person_id': person, 'programme_id': p['id'],
            'starts_on': first, 'ends_on': last}, role='coordinator')
    entry = h.configure('programme_calendar_entries', {'programme_id': p['id'], 'name': 'Whole month',
        'kind': 'academic_block', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})['record']
    before = h.get('coordinator', '/api/catalogues')['programme_memberships']
    for start, end in [('2026-10-01', '2026-11-01'), ('2026-10-20', '2026-10-25')]:
        url = f'/api/calendar?start={start}&end={end}&programme_id={p["id"]}'
        team = h.get('coordinator', url)
        item = next(r for r in team['programme_calendar'] if r['id'] == entry['id'])
        assert item['confirmed_members'] == covered
        own = h.get('staff', url)['programme_calendar']
        assert len(own) == 1 and 'confirmed_members' not in own[0]
        assert not team['events'] and not h.get('coordinator', '/api/availability')['availability']
    assert h.get('coordinator', '/api/catalogues')['programme_memberships'] == before


@pytest.mark.parametrize('spans,covered', [
    ([(1, 15), (15, 31)], True), ([(1, 15), (16, 31)], False),
    ([(1, 20), (10, 31), (1, 20)], True), ([(2, 31)], False), ([], False)])
def test_shared_membership_union_tolerates_overlap_duplicates_but_never_a_gap(spans, covered):
    from datetime import date
    from apps.api.services.catalogue import membership_covers
    assert membership_covers([(date(2026, 10, a), date(2026, 10, b)) for a, b in spans],
        date(2026, 10, 1), date(2026, 10, 31)) is covered
