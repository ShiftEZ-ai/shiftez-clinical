"""Distribution metadata reaches shared findings, scores and API profiles in the metric's actual units."""
import pytest

from packages.domain import fairness as f
from packages.domain.score import schedule_score
from tests.test_engine_fairness_review import A, B, duty, policy, uid, world


@pytest.mark.parametrize('metric,singular', [('duties', 'duty'), ('supervision', 'trainee session supervised')])
@pytest.mark.parametrize('amount', [0, 1, 2])
def test_finding_and_why_units_agree_with_the_count(metric, singular, amount):
    from tests.test_supervision_balance import supervision_world

    data = (supervision_world([A]) if metric == 'supervision' else
            world([duty(i, 5 + i, [A]) for i in range(amount)] + [duty(10, 20, [B])]))
    data['policies'] = [policy({'metric': metric, 'aim': 'target', 'amount': 3,
                                'tolerance_units': 0, 'tolerance_percent': 0})]
    for shift in data['shifts']:
        if metric == 'supervision':
            trainee = shift['participants'].pop()
            shift['participants'].extend({**trainee, 'person_id': uid(30 + i)} for i in range(amount))
    findings = []
    [report] = f.evaluate_goals(data, lambda _code, message, **detail: findings.append((message, detail)))
    unit = singular if amount == 1 else f.UNITS[metric]
    assert report['unit'] == f.UNITS[metric]
    assert any(f'has {amount} {unit}, aim' in message for message, detail in findings if detail['person'] == A)
    score = schedule_score(data, {'goals': [report], 'findings': []})
    assert any(f'· {amount} {unit} vs aim' in row['text'] for row in score['contributions'])


def test_fractional_fair_share_aim_is_written_in_plain_words():
    data = world([duty(1, 29, [A]), duty(2, 30, [A]), duty(3, 31, [A])])
    data['memberships'][0]['starts_on'] = '2026-10-29'
    data['policies'] = [policy({'metric': 'duties', 'aim': 'equal_share',
                                'tolerance_units': 0, 'tolerance_percent': 0})]
    findings = []
    f.evaluate_goals(data, lambda _code, message, **_more: findings.append(message))
    assert any('aim less than 1' in message for message in findings)


def test_amount_units_preserve_workload_and_unrecognized_quota_units():
    assert f.unit_for_amount('duties', 1) == 'duty'
    assert f.unit_for_amount('h', 1) == 'h'
    assert f.unit_for_amount('reserved hours', 1) == 'reserved hours'


@pytest.mark.parametrize('distribution,metric,unit', [
    ('share', 'duties', 'duties'),
    ('period_spread', 'duties', 'duties'),
    ('pair_limit', 'pair_exposure', 'sessions together'),
])
def test_distribution_units_reach_report_findings_and_score_without_changing_values(distribution, metric, unit):
    data = world([duty(1, 5, [A, B]), duty(2, 6, [A, B])], '2026-10-05', '2026-10-19')
    settings = ({'within': {'conditions': [{'field': 'service_ids', 'values': [uid(20)]}]}}
                if distribution == 'share' else
                {'aim': 'target', 'amount': 1,
                 'partners': {'conditions': [{'field': 'person_ids', 'values': [B]}]}}
                if distribution == 'pair_limit' else {})
    goal = policy({'distribution': distribution, 'metric': metric, **settings})
    data['policies'] = [goal]
    report = f.goal_report(data, goal)
    assert report['unit'] == unit == f.UNITS[metric]
    findings = []
    f.evaluate_goals(data, lambda _code, message, **_more: findings.append(message), quota_report={'rows': []})
    assert findings and all(f' {unit}, aim about ' in message for message in findings)
    score = schedule_score(data, {'goals': [report], 'findings': []})
    fairness = [row for row in score['contributions'] if row['component'] == 'fairness' and row['rule_id'] == goal['id']]
    assert fairness and all(f' {unit} vs aim ' in row['text'] for row in fairness)
    old_label = schedule_score(data, {'goals': [{**report, 'unit': 'distribution'}], 'findings': []})
    assert score['score'] == old_label['score'] and score['components'] == old_label['components']
    assert sum(row['points'] for row in score['contributions']) == sum(row['points'] for row in old_label['contributions'])


@pytest.mark.parametrize('distribution,metric,unit', [
    ('period_spread', 'duties', 'duties'),
    ('pair_limit', 'pair_exposure', 'sessions together'),
])
def test_distribution_units_reach_fairness_and_person_api_profiles(h, distribution, metric, unit):
    person, partner = h.seed['people'][:2]
    settings = ({'aim': 'target', 'amount': 1,
                 'partners': {'conditions': [{'field': 'person_ids', 'values': [partner]}]}}
                if distribution == 'pair_limit' else {})
    goal = h.policy({'kind': 'balance_goal', 'metric': metric, 'distribution': distribution, **settings},
                    {'person_id': person}, severity='advisory')
    workspace = h.workspace(start='2026-10-05', end='2026-10-19')
    for day in (5, 6):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[person, partner])['workspace']
    dashboard = h.get('coordinator', '/api/fairness/goals?workspace_id=' + workspace['id'])
    profile = h.get('coordinator', f"/api/people/{person}/fairness?workspace_id={workspace['id']}")
    for response in (dashboard, profile):
        [report] = [row for row in response['goals'] if row['policy_id'] == goal['id']]
        assert (report['metric'], report['distribution'], report['unit']) == (metric, distribution, unit)
    score = h.get('coordinator', f"/api/people/{person}/score?workspace_id={workspace['id']}")
    contributions = [row for row in score['draft']['contributions']
                     if row['rule_id'] == goal['id'] and row['component'] == 'fairness']
    assert contributions and all(f' {unit} vs aim ' in row['text'] for row in contributions)
