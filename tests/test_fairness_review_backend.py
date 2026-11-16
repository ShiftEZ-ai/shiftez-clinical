"""Regression proofs for the independent Fair share backend review (F1/F2/F3/F4/F6/F14)."""
from datetime import date

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import BalanceGoal, PolicyDefinition
from packages.domain import fairness as f
from packages.domain.rule_lines import outranking_by_number
from packages.domain.solver_diagnostics import explain_unmet_requirements
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world


def test_lead_turn_history_reads_flagged_leaders_across_published_snapshots():
    from copy import deepcopy
    from apps.api.services.fairness import publication_facts

    first, second = duty(1, 5, [A, B]), duty(2, 12, [A, B])
    first['participants'][0]['team_lead'] = True
    second['participants'][1]['team_lead'] = True
    snapshots = [('first', {'timezone': 'UTC', 'shifts': [first]}),
                 ('second', {'timezone': 'UTC', 'shifts': [second]})]
    frozen = deepcopy(snapshots)
    goal = policy({'metric': 'lead_turns', 'period': 'month'})
    facts = publication_facts(None, goal['department_id'], ['lead_turns'], date(2026, 10, 1),
        date(2026, 11, 1), [A, B], snapshots=snapshots, policy=goal, data=world())
    assert facts == [('first', A, None, date(2026, 10, 5), 1.0),
                     ('second', B, None, date(2026, 10, 12), 1.0)]
    assert snapshots == frozen


def catalogue(lines, *, pooled=False):
    definition = PolicyDefinition.model_validate({
        'balance_between_people': pooled,
        'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': lines,
                 'settings': {'balance_goal': {'metric': 'duties', 'comparison': 'pooled' if pooled else 'within_group'}}},
    }).model_dump(mode='json')
    return {'id': uid(500), 'name': 'Fair share', 'department_id': uid(4), 'status': 'active', 'active': True,
            'starts_on': '2020-01-01', 'ends_on': None, 'definition': definition}


def line(number, people=None, *, service_ids=(), settings=None):
    value = {'id': uid(500 + number), 'numbers': {'apply': {'value': None, 'level': 'medium'}}}
    if people:
        value['who'] = {'conditions': [{'field': 'person_ids', 'values': list(people)}]}
    if service_ids:
        value['settings'] = {'balance_goal': {'service_ids': list(service_ids)}}
    if settings:
        value.setdefault('settings', {}).setdefault('balance_goal', {}).update(settings)
    return value


def test_line_precedence_switch_separates_who_and_where_but_folds_person_dials(monkeypatch):
    shifts = [duty(i, i, [pid]) for i, pid in enumerate((A, A, A, A, B, B, B, B), 1)]
    data = world(shifts)
    data['people'][uid(7)] = {'display_name': 'C', 'tags': []}
    data['people'][uid(8)] = {'display_name': 'D', 'tags': []}
    data['memberships'] += [{'person_id': pid, 'department_id': uid(4), 'starts_on': '2020-01-01'} for pid in (uid(7), uid(8))]
    data['policies'] = [catalogue([line(1, [A, B]), line(2, [uid(7), uid(8)])])]
    assert len(f.goal_reports(data)) == 2

    # An all-people line and a person line compare together; the person's adjustment survives.
    data['policies'] = [catalogue([line(3), line(4, [A], settings={'share_adjustment': {'mode': 'percent', 'value': 50}})])]
    [report] = f.goal_reports(data)
    rows = {row['person_id']: row for row in report['people']}
    assert {A, B} <= rows.keys()
    assert rows[A]['expected'] != rows[B]['expected']

    service = uid(99)
    data['policies'] = [catalogue([line(5, [A, B]), line(6, [A, B], service_ids=[service])])]
    assert len(f.goal_reports(data)) == 2


def test_calendar_period_switch_covers_each_overlapping_month(monkeypatch):
    data = world([duty(1, '2026-12-29', [A]), duty(2, '2027-01-05', [B])], '2026-12-29', '2027-01-26')
    data['policies'] = [policy({'metric': 'duties', 'period': 'month', 'share_by': 'equal'})]
    reports = f.goal_reports(data)
    assert [(r['period_start'], r['period_end']) for r in reports] == [
        ('2026-12-01', '2027-01-01'), ('2027-01-01', '2027-02-01')]
    assert [r['people'][0]['actual'] for r in reports] == [1.0, 0.0]


@pytest.mark.parametrize('grouping', ['pooled', 'line_precedence'])
def test_catalogue_conversion_preserves_grouping_metadata_and_reports(monkeypatch, grouping):
    from packages.domain.conditions import active_rules, with_tags
    from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2

    data = world([duty(1, 1, [A]), duty(2, 2, [A]), duty(3, 3, [B])])
    source = {**catalogue([line(1), line(2, [A], settings={
        'share_adjustment': {'mode': 'percent', 'value': 50}})]), 'kind': 'catalogue', 'revision': 1}
    source['definition']['rule']['settings']['balance_goal']['comparison'] = 'pooled' if grouping == 'pooled' else 'within_group'
    data['policies'] = [source]
    proposal = plan_v2(data['policies'], department_id=source['department_id'])
    assert not proposal['blocked']
    after = after_snapshot(data, proposal)
    # Production readers materialize converted source policies before resolving scoped report rows.
    with_tags(data)
    with_tags(after)

    def copies(snapshot):
        return [{k: v for k, v in row.items() if k != '_v2_source'}
                for row in active_rules(snapshot['policies'], context=snapshot)]

    assert copies(data) == copies(after)
    assert f.goal_reports(data) == f.goal_reports(after)


def test_zero_count_tolerance_uses_floor_ceil_band():
    rule = {'metric': 'supervision', 'tolerance_percent': 0, 'tolerance_units': 0}
    assert f.band('supervision', 7 / 3, rule) == (2, 3, 0)
    assert f.status('supervision', 2, 7 / 3, rule=rule) == 'within'
    assert f.status('supervision', 1, 7 / 3, rule=rule) == 'below'
    assert f.outside('supervision', 3, 7 / 3, rule=rule) == 0


def test_weight_and_greedy_are_precedence_dials():
    policy = {'id': 'p', 'department_id': 'd', 'kind': 'catalogue', 'status': 'active',
              'definition': {'severity': 'advisory', 'scope': {}, 'rule': {
                  'kind': 'catalogue', 'id': 'fair_share', 'catalogue_version': 2, 'on': True,
                  'settings': {'balance_goal': {'metric': 'duties'}},
                  'lines': [line(7), line(8, [A], settings={'weight': 2, 'optimize_greedy': True})]}}}
    assert outranking_by_number(policy) == {(0, 'apply'): [1]}


def test_empty_audience_is_explicit_in_report_and_findings():
    data = world([duty(1, 5, [A])])
    data['policies'] = [policy({'metric': 'supervision'})]
    findings = []
    [report] = f.evaluate_goals(data, lambda code, message, **detail: findings.append((code, message, detail)))
    assert report['applies_to_nobody'] is True
    assert any(detail['extra']['applies_to_nobody'] for _, _, detail in findings)


def test_stalled_fairness_findings_are_search_honest():
    validation = {'findings': [{'code': 'BALANCE_GOAL', 'rule_id': 'r', 'message': 'Fair share: A is below',
                                'severity': 'warning', 'detail': {'actual': 0, 'expected': 2}}]}
    objectives = [{'name': 'weighted', 'proven_optimal': False, 'stopped_without_improvement': True}]
    rows = explain_unmet_requirements({'policies': [{'id': 'r', 'name': 'Fair share'}]}, validation, objectives)
    assert rows[0]['code'] == 'SEARCH_STOPPED_WITHOUT_IMPROVEMENT'
    assert 'Search stopped' in rows[0]['message']


@pytest.mark.parametrize('finding_code', ['BALANCE_GOAL', 'BALANCE_PAIR'])
@pytest.mark.parametrize('optimal,stalled,code', [
    (False, False, 'SEARCH_INCOMPLETE'),
    (False, True, 'SEARCH_STOPPED_WITHOUT_IMPROVEMENT'),
    (True, False, 'OBJECTIVE_TRADEOFF'),
])
def test_fairness_search_notices_group_people_but_preserve_goal_identity(finding_code, optimal, stalled, code):
    def finding(goal, person):
        return {'code': finding_code, 'rule_id': 'r', 'person_id': person, 'severity': 'warning',
                'message': 'Fair share shortfall', 'detail': {'goal_id': goal}}
    # A repeated observation must not inflate the affected count; other groups/windows stay separate.
    findings = [finding('group-a-june', A), finding('group-a-june', B), finding('group-a-june', A),
                finding('group-b-june', A), finding('group-a-july', A)]
    rows = explain_unmet_requirements({'policies': [{'id': 'r', 'name': 'Fair share'}]},
        {'findings': findings}, [{'name': 'weighted', 'proven_optimal': optimal,
                                 'stopped_without_improvement': stalled}])
    assert [r['goal_id'] for r in rows] == ['group-a-june', 'group-b-june', 'group-a-july']
    assert [r['affected_people'] for r in rows] == [2, 1, 1]
    assert rows[0]['person_ids'] == sorted([A, B])
    assert all(r['severity'] == 'warning' and r['code'] == code and not r['proven'] for r in rows)
    assert all(r['person_id'] is None and r['tier'] == 'weighted' for r in rows)


def test_fair_share_operator_notice_distinguishes_plateau_limit_and_optimum():
    from packages.domain.solver_diagnostics import fair_share_search_notice
    receipt, message = fair_share_search_notice({'stopped_without_improvement': True, 'search': {'fairness_penalty': 5}}, 15, 'FEASIBLE')
    assert receipt['status'] == 'FEASIBLE' and receipt['stop_reason'] == 'plateau'
    assert 'ordinary plateau' in message and '(FEASIBLE)' not in message and '15-second window' in message
    _, message = fair_share_search_notice({'stopped_without_improvement': True, 'search': {'fairness_penalty': 0}}, 15, 'FEASIBLE')
    assert 'fairness was fully satisfied' in message
    _, message = fair_share_search_notice({'stopped_without_improvement': True}, 15, 'FEASIBLE')
    assert 'ordinary plateau' in message and '15-second' not in message
    # A newer full-candidate time limit takes precedence over a retained shortlist plateau flag.
    row = {'stopped_without_improvement': True, 'search': {'stop_reason': 'time_limit', 'fairness_improving': True}}
    receipt, message = fair_share_search_notice(row, 15, 'FEASIBLE')
    assert 'Search stopped while fairness was still improving' in message
    assert '(FEASIBLE)' not in message
    assert receipt['stop_reason'] == 'time_limit'
    row['search']['fairness_improving'] = False
    assert 'time allowance' in fair_share_search_notice(row, 15, 'FEASIBLE')[1]
    row['search']['fairness_improving'] = True
    receipt, message = fair_share_search_notice(row, 15, 'OPTIMAL')
    assert receipt['stop_reason'] == 'optimal' and not receipt['fairness_improving'] and message is None
    receipt, message = fair_share_search_notice(None, 15, 'FEASIBLE')
    assert receipt['stop_reason'] == 'not_reached' and 'not reached' in message and '(FEASIBLE)' not in message


def test_person_override_does_not_merge_rank_groups_or_metrics(monkeypatch):
    from tests.test_weighted_goals import assert_losses, report_losses, solver_losses
    from packages.domain.fairness_distribution import greedy_goals
    data = world([duty(i, i, [A] if i < 4 else [B]) for i in range(1, 6)])
    consultants, residents = uid(90), uid(91)
    data['categories'] = {consultants: {}, residents: {}}
    for row in data['memberships']:
        row['category_id'] = consultants if row['person_id'] == A else residents
    def rank(n, category):
        value = line(n, settings={'optimize_greedy': True})
        value['who'] = {'conditions': [{'field': 'category_ids', 'values': [category]}]}
        return value
    data['policies'] = [catalogue([rank(1, consultants), rank(2, residents),
        line(3, [A], settings={'weight': 2}),
        line(4, settings={'metric': 'hours', 'optimize_greedy': True})])]
    reports = f.goal_reports(data)
    duty_reports = [r for r in reports if r['metric'] == 'duties' and r['people']]
    assert [{r['person_id'] for r in report['people']} for report in duty_reports] == [{A}, {B}]
    assert f.report_weights(duty_reports[0], {'medium': 3}) == {A: 6}
    assert len([r for r in reports if r['metric'] == 'hours']) == 1
    assert {f.goal_identity(g) for g in greedy_goals(data)} == {r['goal_id'] for r in reports}
    assert_losses(solver_losses(data), report_losses(data))


def test_person_line_keeps_two_overlapping_rank_tag_comparisons_separate(monkeypatch):
    data = world([duty(1, 1, [A]), duty(2, 2, [B])])
    data['people'][A]['tags'] = ['group1', 'group2']
    data['people'][B]['tags'] = ['group1', 'group2']
    lines = []
    for i, tag in enumerate(('group1', 'group2'), 1):
        item = line(i)
        item['who'] = {'conditions': [{'field': 'tags', 'values': [tag]}]}
        lines.append(item)
    lines.append(line(3, [A], settings={'share_adjustment': {'mode': 'percent', 'value': 50}}))
    data['policies'] = [catalogue(lines)]
    reports = f.goal_reports(data)
    assert len(reports) == 2
    assert all({r['person_id'] for r in report['people']} == {A, B} for report in reports)
    assert all(next(r['expected'] for r in report['people'] if r['person_id'] == A) == 0.67 for report in reports)
    assert len({r['goal_id'] for r in reports}) == 2


@pytest.mark.parametrize(('aim', 'adjustment'), [(aim, adjustment)
    for aim in ('equal_share', 'target', 'max_min')
    for adjustment in (None, {'mode': 'amount', 'value': 1.25}) if aim != 'target' or adjustment is None])
def test_zero_count_band_compiler_report_parity(aim, adjustment):
    from tests.test_weighted_goals import assert_losses, report_losses, solver_losses
    data = world([duty(i, i, [A] if i < 3 else [B]) for i in range(1, 6)])
    rule = {'metric': 'duties', 'aim': aim, 'share_by': 'equal', 'tolerance_percent': 0, 'tolerance_units': 0}
    if aim == 'target':
        rule['amount'] = 2.5
    data['policies'] = [policy(rule)]
    if adjustment:
        data['policies'] = [catalogue([line(1), line(2, [A], settings={'share_adjustment': adjustment})], pooled=True)]
        data['policies'][0]['definition']['rule']['settings']['balance_goal'].update(rule)
    assert_losses(solver_losses(data), report_losses(data))
    if not adjustment:
        assert f.goal_reports(data)[0]['score'] == 100
        assert f.status('duties', 4, 2.5, aim='max_min', rule=rule) == 'within'


def test_fractional_session_units_do_not_get_an_integer_band():
    rule = {'metric': 'session_units', 'tolerance_percent': 0, 'tolerance_units': 0}
    assert f.band('session_units', 2.5, rule) == (2.5, 2.5, 0)


def test_zero_count_band_is_recorded_for_fast_score_completion():
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_score import begin_score_program, finish_score_program
    from packages.domain.cp_sat_goals import add_score_goals
    from tests.test_weighted_goals import STRENGTHS
    data = world([duty(i, i, [A, B]) for i in range(1, 6)])
    data['policies'] = [policy({'metric': 'duties', 'share_by': 'equal', 'tolerance_percent': 0, 'tolerance_units': 0})]
    model = cp_model.CpModel()
    participants = {(s['id'], p['person_id']): p for s in data['shifts'] for p in s['participants']}
    choices = {key: model.new_bool_var('attendance') for key in participants}
    begin_score_program(model)
    [(_, loss)] = add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS)
    assert finish_score_program(model, {'fairness': (loss, 1)}, 1) is not None
    assert model.validate() == ''


@pytest.mark.parametrize(('period', 'first', 'last', 'bounds'), [
    ('quarter', '2026-12-15', '2027-01-12', [('2026-10-01', '2027-01-01'), ('2027-01-01', '2027-04-01')]),
    ('year', '2026-12-15', '2027-01-12', [('2026-01-01', '2027-01-01'), ('2027-01-01', '2028-01-01')]),
])
def test_calendar_period_identities_and_compiler_parity(monkeypatch, period, first, last, bounds):
    from tests.test_weighted_goals import assert_losses, report_losses, solver_losses
    data = world([duty(1, '2026-12-20', [A]), duty(2, '2027-01-05', [B])], first, last)
    data['policies'] = [catalogue([line(1, [A]), line(2, [B])], pooled=True)]
    data['policies'][0]['definition']['rule']['settings']['balance_goal']['period'] = period
    reports = f.goal_reports(data)
    assert [(r['period_start'], r['period_end']) for r in reports] == bounds
    assert len({r['goal_id'] for r in reports}) == 2
    assert all(len(r['goal_id']) <= 96 for r in reports)
    assert_losses(solver_losses(data), report_losses(data))


@pytest.mark.parametrize(('last', 'months', 'expected'), [
    ('2027-01-01', 12, '2026-01-01'), ('2027-01-17', 12, '2026-01-17'),
    ('2027-03-31', 1, '2027-02-28'), ('2028-03-31', 1, '2028-02-29'),
    ('2028-02-29', 12, '2027-02-28'),
])
def test_exact_rolling_calendar_months_end_at_rota_end(last, months, expected):
    rule = BalanceGoal(kind='balance_goal', metric='lead_turns', period='rolling', period_months=months)
    assert rule.period_days is None
    assert f.period_bounds(rule.model_dump(), date(2026, 12, 1), date.fromisoformat(last)) == (
        date.fromisoformat(expected), date.fromisoformat(last))


@pytest.mark.parametrize('fields', [{}, {'period_days': 30, 'period_months': 12}, {'period_months': 0},
                                    {'period_months': 13}, {'period_months': True}])
def test_rolling_period_requires_exactly_one_valid_window(fields):
    with pytest.raises(ValidationError):
        BalanceGoal(kind='balance_goal', metric='lead_turns', period='rolling', **fields)


def test_rolling_months_use_existing_report_and_compiler_pipeline():
    from tests.test_weighted_goals import assert_losses, report_losses, solver_losses
    data = world([duty(1, '2026-12-05', [A]), duty(2, '2026-12-15', [B])], '2026-12-01', '2027-01-01')
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['lead'] = {'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': []}
        for p in shift['participants']:
            p['team_lead'] = True
    data['policies'] = [policy({'metric': 'lead_turns', 'period': 'rolling', 'period_months': 12})]
    goal = data['policies'][0]
    data['fairness_history'] = {f.goal_identity(goal): {'people': {A: {'value': 7, 'available': 334}, B: {'value': 1, 'available': 334}}}}
    [report] = f.goal_reports(data)
    assert report['period_start'] == '2026-01-01' and report['period_end'] == '2027-01-01'
    assert sorted(r['actual'] for r in report['people']) == [2, 8]
    assert_losses(solver_losses(data, lead=True), report_losses(data))


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_calendar_month_contract_survives_catalogue_save_and_publication_history(h):
    from tests.test_rule_catalogue_api import approve, propose, line as api_line
    from packages.domain.validation import evaluate
    person = h.seed['people'][1]
    settings = {'metric': 'lead_turns', 'period': 'rolling', 'period_months': 12, 'period_days': None}
    row = api_line(who={'conditions': [{'field': 'person_ids', 'values': [person]}]},
                   numbers={'apply': {'value': None, 'level': 'medium'}}, settings={'balance_goal': settings})
    saved = approve(h, propose(h, 'fair_share', [row])['record'])
    properties = h.get('coordinator', '/api/policies/capabilities')['definition_schema']['$defs']['BalanceGoal']['properties']
    assert properties['period_months']['default'] is None
    assert properties['period_months']['x-applies-to'] == {'period': ['rolling']}
    from tests.test_governed_leadership import activate, proposal, line as lead_line
    typ = h.configure('assignment_types', {'name': 'Rolling calendar lead clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    activate(h, proposal(h, [lead_line(typ['id'], [person])]))
    w = h.workspace()
    start, end = '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'
    w = h.shift(w, typ=typ['id'], start=start, end=end, people=[{'person_id': person,
        'start': start, 'end': end, 'participation': 'supervisor', 'team_lead': True}])['workspace']
    h.publish(w)
    nxt = h.workspace(start='2026-12-17', end='2027-01-17')
    nxt = h.shift(nxt, typ=typ['id'], start='2026-12-18T08:00:00+03:00',
        end='2026-12-18T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(nxt)
    [report] = [r for r in evaluate(data)['goals'] if r['policy_id'] == saved['id']]
    assert report['period_start'] == '2026-01-17' and report['period_end'] == '2027-01-17'
    assert report['people'][0]['prior'] == 1


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_each_calendar_period_reads_its_own_publication_history(h, monkeypatch):
    from tests.test_rule_catalogue_api import approve, propose, line as api_line
    from packages.domain.validation import evaluate
    person = h.seed['people'][1]
    row = api_line(who={'conditions': [{'field': 'person_ids', 'values': [person]}]},
        numbers={'apply': {'value': None, 'level': 'medium'}},
        settings={'balance_goal': {'metric': 'duties', 'period': 'month'}})
    saved = approve(h, propose(h, 'fair_share', [row])['record'])
    w = h.workspace(start='2026-12-01', end='2026-12-08')
    w = h.shift(w, start='2026-12-05T08:00:00+03:00', end='2026-12-05T12:00:00+03:00', people=[person])['workspace']
    h.publish(w)
    nxt = h.workspace(start='2026-12-29', end='2027-01-26')
    for month, day in ((12, 30), (1, 5)):
        year = 2026 if month == 12 else 2027
        nxt = h.shift(nxt, start=f'{year}-{month:02}-{day:02}T08:00:00+03:00',
            end=f'{year}-{month:02}-{day:02}T12:00:00+03:00', people=[person])['workspace']
    reports = [r for r in evaluate(h.snapshot(nxt))['goals'] if r['policy_id'] == saved['id']]
    assert [(r['period_start'], r['people'][0]['prior'], r['people'][0]['planned']) for r in reports] == [
        ('2026-12-01', 1, 1), ('2027-01-01', 0, 1)]


@pytest.mark.parametrize('legacy', [False, True])
def test_rolling_history_separates_activation_from_measurement_unless_frozen(legacy):
    data = world([duty(1, '2026-12-05', [A])], '2026-12-01', '2027-01-01')
    data['policies'] = [catalogue([line(1, [A])])]
    if legacy:
        data['fairness_semantics'] = 1
    parent = data['policies'][0]
    parent['starts_on'] = '2026-11-06'
    parent['definition']['rule']['settings']['balance_goal'].update(period='rolling', period_months=12)
    [goal] = f.balance_goals(data)
    assert f.goal_period(data, goal, A) == (date(2026, 1, 1), date(2027, 1, 1))
    window = f.history_windows(data, goal, preload=True)[A]
    assert f.goal_intervals(data, goal, A, *window) == [(date(2026, 11, 6) if legacy else date(2026, 1, 1), date(2026, 12, 1))]
    parent['definition']['rule']['lines'][0]['starts_on'] = '2026-11-20'
    [goal] = f.balance_goals(data)
    assert f.goal_intervals(data, goal, A, *window) == [(date(2026, 11, 20) if legacy else date(2026, 1, 1), date(2026, 12, 1))]


def test_calendar_expansion_keeps_person_override_starting_in_later_month(monkeypatch):
    data = world([duty(1, '2026-12-30', [A]), duty(2, '2027-01-05', [B])], '2026-12-29', '2027-01-26')
    mine = line(2, [A], settings={'weight': 2})
    mine['starts_on'] = '2027-01-01'
    data['policies'] = [catalogue([line(1), mine])]
    data['policies'][0]['definition']['rule']['settings']['balance_goal']['period'] = 'month'
    december, january = f.goal_reports(data)
    assert f.report_weights(december, {'medium': 3}) == {A: 6, B: 3}
    assert f.report_weights(january, {'medium': 3}) == {A: 6, B: 3}
