"""Pattern ranges and standing defaults use one period allocation without duplicating evaluated wishes."""
import pytest

from .test_staff_wishes import wish
from .test_standing_wishes import configure, expanded, override, standing


@pytest.mark.parametrize('standing_first', [False, True])
def test_patterns_coexist_with_standing_and_only_dated_wishes_replace_defaults(h, standing_first):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    if standing_first:
        standing(h, points=2)
    pattern = wish(h, '2026-10-01', last='2026-10-31', kind='max_days_in_row', limit=3, points=2)['wish']
    if not standing_first:
        standing(h, points=2)
    dated = wish(h, '2026-10-12', kind='prefer_on', points=2)['wish']
    rows = expanded(h, workspace)
    assert {row['id'] for row in rows if not row.get('standing_wish_id')} == {pattern['id'], dated['id']}
    assert [row['first_day'] for row in rows if row.get('standing_wish_id')] == [
        '2026-10-05', '2026-10-19', '2026-10-26']
    [period] = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')['usage']
    assert (period['used'], period['one_off_points'], period['standing_points']) == (10, 4, 6)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_multi_period_pattern_admission_usage_update_and_withdraw_share_allocation(request, database):
    h = request.getfixturevalue(database)
    workspace = h.workspace(start='2026-10-15', end='2026-12-15')
    default = standing(h, points=2, number=3)['wish']
    override(h, default, period_start='2026-11-01', number=1)
    wish(h, '2026-11-20', points=6)
    rejected = wish(h, '2026-10-01', last='2026-12-31', kind='max_days_in_row', limit=2, points=3, status=409)
    assert rejected['error']['code'] == 'WISH_BUDGET'
    assert rejected['error']['details'] == {'used': 8, 'budget': 10, 'period_start': '2026-11-01'}
    saved = wish(h, '2026-10-01', last='2026-12-31', kind='max_days_in_row', limit=2, points=2)['wish']
    endpoint = '/api/wishes?start=2026-10-01&end=2027-01-01'
    before = h.get('staff', endpoint)['usage']
    assert [row['one_off_points'] for row in before] == [2, 8, 2]
    assert [row['standing_points'] for row in before] == [6, 2, 6]
    assert [row['unfunded_standing_points'] for row in before] == [0, 0, 0]
    assert [row['used'] for row in before] == [8, 10, 8]
    assert [row['id'] for row in expanded(h, workspace)].count(saved['id']) == 1
    rejected = h.command('staff', 'wish.update', {
        'person_id': saved['person_id'], 'first_day': saved['first_day'], 'last_day': saved['last_day'],
        'kind': saved['kind'], 'limit': 2, 'points': 3}, saved['id'], saved['revision'], status=409)
    assert rejected['error']['details']['period_start'] == '2026-11-01'
    listed = h.get('staff', endpoint)
    assert listed['usage'] == before
    assert next(row for row in listed['wishes'] if row['id'] == saved['id'])['points'] == 2
    h.command('staff', 'wish.withdraw', {'reason': 'Restore the standing allocation'}, saved['id'], saved['revision'])
    after = h.get('staff', endpoint)['usage']
    assert [row['one_off_points'] for row in after] == [0, 6, 0]
    assert [row['standing_points'] for row in after] == [6, 2, 6]


def test_adjacent_publications_reserve_each_pattern_period_once_and_report_unfunded_live_periods(h):
    person = h.seed['people'][0]
    pattern = wish(h, '2026-10-01', last='2026-12-31', kind='max_days_in_row', limit=10, points=2)['wish']
    standing(h, points=2)
    publications = []
    for name, first, last, duty in [
        ('Issued first half', '2026-10-01', '2026-10-15', '2026-10-05'),
        ('Issued second half', '2026-10-15', '2026-11-01', '2026-10-19'),
    ]:
        workspace = h.workspace(name, first, last)
        workspace = h.shift(workspace, start=duty + 'T08:00:00+03:00',
                            end=duty + 'T12:00:00+03:00', people=[person])['workspace']
        publication_id = h.publish(workspace)['publication_id']
        snapshot = h.get('staff', '/api/publications/' + publication_id)['snapshot']
        assert [row['id'] for row in snapshot['wishes']].count(pattern['id']) == 1
        publications.append((publication_id, snapshot))
    preview = h.workspace('Partial periods', '2026-10-10', '2026-11-10')
    endpoint = '/api/wishes?start=2026-10-10&end=2026-11-10&workspace_id=' + preview['id']
    usage = h.get('staff', endpoint)['usage']
    assert [row['one_off_points'] for row in usage] == [2, 0]
    assert [row['standing_points'] for row in usage] == [8, 10]
    assert [row['unfunded_one_off_points'] for row in usage] == [0, 2]
    assert [row['used'] for row in usage] == [10, 10]
    assert [row['id'] for row in expanded(h, preview)].count(pattern['id']) == 1
    configure(h, wish_budget_points=1)
    [current] = [row for row in expanded(h, preview) if row['id'] == pattern['id']]
    assert current['points'] == 2 and current['weight'] == current['multiplier'] == 1
    assert [row['one_off_points'] for row in h.get('staff', endpoint)['usage']] == [2, 0]
    for publication_id, snapshot in publications:
        assert h.get('staff', '/api/publications/' + publication_id)['snapshot'] == snapshot


def test_new_pattern_cannot_spend_reserved_standing_points(h):
    person = h.seed['people'][0]
    standing(h, points=3, number=2)
    workspace = h.workspace('Issued first half', '2026-10-01', '2026-10-15')
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    h.publish(workspace)
    rejected = wish(h, '2026-10-15', last='2026-10-31', kind='whole_weekends', points=5, status=409)
    assert rejected['error']['code'] == 'WISH_BUDGET'
    assert rejected['error']['details'] == {'used': 6, 'budget': 10, 'period_start': '2026-10-01'}
