"""Empty effective quota ranges cannot crash expansion or displace an applicable budget."""
import pytest

from packages.domain.conditions import active_rules
from packages.domain.rule_lines import outranking, source_key, source_lines
from packages.domain.solver_rules import definition_issues
from packages.domain.validation import evaluate
from .test_rule_lines import line, person, rule
from .test_rule_lines_inherit import n


@pytest.mark.parametrize('first,last', [
    ('2026-11-01', '2026-12-01'), ('2026-09-01', '2026-10-04'),
    ('2026-11-02', '2026-12-01'), ('2026-09-01', '2026-10-03'),
])
def test_valid_disjoint_quota_ranges_make_no_numeric_copy(first, last):
    own = line(numbers={'min': n(2)}, starts_on=first, ends_on=last)
    row = rule('duties_per_month', own, settings={'block_quota': {
        'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}})
    assert definition_issues(row['definition']) == []
    assert active_rules([row]) == []
    source = source_lines(row)[source_key(row, own)]
    assert source['starts_on'] >= source['ends_on']


@pytest.mark.parametrize('cause', ['block', 'policy'])
@pytest.mark.parametrize('own_minimum', [0, None], ids=['number', 'explicit-none'])
def test_empty_person_line_does_not_remove_the_general_minimum(h, cause, own_minimum):
    pid = h.seed['people'][0]
    w = h.shift(h.workspace('Empty quota line', '2026-10-01', '2026-11-01'), people=[])['workspace']
    data = h.snapshot(w)
    data['memberships'] = [m for m in data['memberships'] if m['person_id'] == pid]
    general = line(numbers={'min': n(2)}, starts_on='2026-10-01', ends_on='2026-11-01')
    own = line(who=person(pid), numbers={'min': n(own_minimum)}, starts_on='2026-10-15', ends_on='2026-11-01',
               settings={'block_quota': {'starts_on': '2026-10-01', 'ends_on': '2026-10-15'}} if cause == 'block' else {})
    row = rule('duties_per_month', general, own, department_id=data['workspace']['department_id'],
               ends_on='2026-10-15' if cause == 'policy' else None)
    data['policies'] = [row]
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+03:00', 'end': '2026-11-01T00:00:00+03:00'}
    assert outranking(row) == {}
    copies = active_rules([row])
    assert [(c['template_line']['id'], c['definition']['rule']['amount']) for c in copies] == [(general['id'], 2)]
    validation = evaluate(data)
    assert validation['quota_report']['complete']
    [quota] = validation['quota_report']['rows']
    # The surviving configured minimum stays two; partial policy validity scales only its checked amount.
    assert (quota['person_id'], quota['amount'], quota['actual']) == (pid, 1 if cause == 'policy' else 2, 0)
    if cause == 'policy':
        assert quota['proration'] == {'configured_amount': 2, 'amount': 1, 'working_days': 10,
            'full_working_days': 21, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
            'calendar_id': data['department']['operating_calendar_id'], 'rounding': 'up'}
    else:
        assert not quota.get('proration')
    assert [f['code'] for f in validation['findings'] if f['code'].startswith('QUOTA_')] == ['QUOTA_BELOW_MINIMUM']


@pytest.mark.parametrize('block,amount', [
    ({'starts_on': '2026-10-04', 'ends_on': '2026-10-04'}, 2),
    ({'starts_on': '2026-11-01', 'ends_on': '2026-10-04'}, 2),
    ({'starts_on': '2025-01-01', 'ends_on': '2027-01-01'}, 2),
    ({'starts_on': '2026-10-04', 'ends_on': '2026-11-01'}, -1),
])
def test_empty_intersection_does_not_hide_an_invalid_original_quota(block, amount):
    own = line(numbers={'min': n(amount)}, starts_on='2027-02-01', ends_on='2027-03-01')
    row = rule('duties_per_month', own, settings={'block_quota': block})
    assert definition_issues(row['definition'])
    with pytest.raises(ValueError):
        active_rules([row])


@pytest.mark.parametrize('last,valid', [('2026-10-16', False), ('2026-10-22', True)],
                         ids=['clipped-shorter-than-window', 'clipped-complete-window'])
def test_clipped_rolling_override_keeps_the_complete_window_contract(h, last, valid):
    pid = h.seed['people'][0]
    w = h.shift(h.workspace('Clipped rolling quota', '2026-10-15', '2026-10-16'),
                start='2026-10-15T08:00:00+03:00', end='2026-10-15T12:00:00+03:00', people=[pid])['workspace']
    data = h.snapshot(w)
    data['memberships'] = [m for m in data['memberships'] if m['person_id'] == pid]
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+03:00', 'end': '2026-11-01T00:00:00+03:00'}
    general = line(numbers={'min': n(2)})
    settings = {'block_quota': {'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                               'window_days': 7, 'window_alignment': 'rolling'}}
    row = rule('duties_per_month', general, settings=settings, department_id=data['workspace']['department_id'])
    data['policies'] = [row]
    report = evaluate(data)['quota_report']
    assert report['complete']
    before = report['rows']
    assert len(before) == 7 and all((q['amount'], q['actual']) == (1, 1) for q in before)
    for quota in before:
        assert quota['proration'] == {'configured_amount': 2, 'amount': 1, 'working_days': 1,
            'full_working_days': 5, 'starts_on': quota['starts_on'], 'ends_on': quota['ends_on'],
            'calendar_id': data['department']['operating_calendar_id'], 'rounding': 'up'}
    own = line(who=person(pid), numbers={'min': n(0)}, starts_on='2026-10-15', ends_on=last)
    row['definition']['rule']['lines'].append(own)
    if not valid:
        issues = definition_issues(row['definition'])
        assert len(issues) == 1 and issues[0]['field'] == 'definition.rule.lines.1'
        assert 'complete rolling window' in issues[0]['message']
        # An invalid one-day override cannot erase the seven complete rolling windows.
        with pytest.raises(ValueError, match='complete rolling window'):
            evaluate(data)
    else:
        assert definition_issues(row['definition']) == []
        own_copy = next(c for c in active_rules([row]) if c['template_line']['id'] == own['id'])
        block = own_copy['definition']['rule']
        assert (block['starts_on'], block['ends_on'], block['window_days']) == ('2026-10-15', last, 7)
        report = evaluate(data)['quota_report']
        assert report['complete']
        rows = report['rows']
        assert [(q['person_id'], q['amount'], q['actual']) for q in rows] == [(pid, 0, 1)]
        assert rows[0]['proration'] == {'configured_amount': 0, 'amount': 0, 'working_days': 1,
            'full_working_days': 5, 'starts_on': '2026-10-15', 'ends_on': '2026-10-22',
            'calendar_id': data['department']['operating_calendar_id'], 'rounding': 'up'}
