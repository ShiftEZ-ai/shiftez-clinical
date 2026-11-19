"""Selected Distribution is the goal API's canonical report, including its permissions."""
from urllib.parse import urlencode

import pytest

from packages.contracts import schemas as s
from tests.test_overtime_offered_api import offer
from tests.test_rule_catalogue_api import line


BACKENDS = ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)]


@pytest.mark.parametrize('h', BACKENDS, indirect=True)
def test_catalogue_owns_group_and_lead_option_words_and_names_missing_measure(h):
    expected = {
        'fair_share': ('comparison', s.BalanceGoal, 'Groups', {
            'within_group': 'Each group on its own', 'pooled': 'Pool these groups together'}),
        'team_leads': ('count_scope', s.TeamLeadRequirement, 'Count leaders', {
            'duty': 'Per duty', 'concurrent_duties': 'Across these concurrent duties together'}),
    }
    for version in (1, 2):
        page = h.get('coordinator', f'/api/rules/catalogue?version={version}&include_hidden=true')
        for ident, (field, model, label, options) in expected.items():
            entry = next(e for e in page['entries'] if e['id'] == ident)
            assert entry['setting_labels'][field] == label
            assert entry['setting_option_labels'][field] == options
            assert set(options) == set(model.model_json_schema()['properties'][field]['enum'])
    first = line(numbers={'apply': {'value': None, 'level': 'medium'}},
                 settings={'balance_goal': {'metric': 'duties'}})
    second = line(numbers={'apply': {'value': None, 'level': 'medium'}})
    response = h.client('coordinator').post('/api/rules/catalogue/fair_share/check',
        json={'catalogue_version': 1, 'lines': [first, second]})
    assert response.status_code == 200, response.text
    assert response.json()['issues'] == [{
        'field': 'definition.rule.lines.1.settings.balance_goal.metric',
        'code': 'INVALID_RULE_DEFINITION', 'message': 'Line 2: choose what to count'}]
    inherited = h.client('coordinator').post('/api/rules/catalogue/fair_share/check',
        json={'catalogue_version': 2, 'lines': [second]})
    assert inherited.status_code == 200 and inherited.json()['issues'] == []


@pytest.mark.parametrize('h', BACKENDS, indirect=True)
@pytest.mark.parametrize('metric,share_by', [
    ('supervision', 'supervisable_sessions'), ('overtime_hours', 'offered'), ('duties', 'available_days')])
def test_distribution_selected_goal_equals_goals_for_draft_and_publication(h, metric, share_by):
    trainee, supervisor, colleague = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Goal distribution clinic', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']
    if share_by == 'offered':
        offer(h, supervisor, '2026-10-04', '2026-10-05', '08:00', '12:00')
        offer(h, colleague, '2026-10-04', '2026-10-05', '08:00', '16:00')
    goal = h.policy({'kind': 'balance_goal', 'metric': metric, 'period': 'rota', 'share_by': share_by,
                     'tolerance_units': 0, 'tolerance_percent': 0},
                    scope={'person_ids': [supervisor, colleague]}, severity='advisory')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    participants = [{'person_id': pid, 'start': start, 'end': end, 'overtime': share_by == 'offered',
        'participation': 'trainee' if pid == trainee else 'supervisor',
        **({'supervisor_person_id': supervisor} if pid == trainee else {})}
        for pid in (trainee, supervisor, colleague)]
    workspace = h.shift(h.workspace(), typ=typ['id'], start=start, end=end, people=participants)['workspace']
    for published in (False, True):
        if published:
            h.publish(workspace)
        query = {} if published else {'workspace_id': workspace['id']}
        canonical = h.get('coordinator', '/api/fairness/goals?' + urlencode(query))
        [report] = [r for r in canonical['goals'] if r['policy_id'] == goal['id']]
        selected = {**query, 'goal_id': report['goal_id']}
        url = '/api/fairness/distribution?' + urlencode(selected)
        distribution = h.get('coordinator', url)
        assert distribution == {**report, 'basis': 'goal', 'workspace': canonical['workspace']}
        assert distribution['metric'] == metric and distribution['share_by'] == share_by
        assert distribution['period_start'] == '2026-10-04' and distribution['period_end'] == '2026-10-11'
        rows = {r['person_id']: r for r in distribution['people']}
        assert set(rows) == {supervisor, colleague}
        assert rows[supervisor]['expected'] > 0
        if share_by == 'offered':
            assert rows[colleague]['expected'] == pytest.approx(2 * rows[supervisor]['expected'], abs=0.01)
        else:
            assert rows[supervisor]['expected'] == rows[colleague]['expected']
        h.get('coordinator', '/api/fairness/distribution?' + urlencode({**selected, 'goal_id': 'absent'}), status=404)
        for field, value in [('group_by', 'category_ids'), ('assignment_type_ids', typ['id']),
                             ('service_ids', h.seed['services'][0]), ('location_ids', h.seed['locations'][0])]:
            h.get('coordinator', url + '&' + urlencode({field: value}), status=422)
        if published:
            personal = h.get('staff', '/api/fairness/goals')
            [own] = [r for r in personal['goals'] if r['goal_id'] == report['goal_id']]
            assert h.get('staff', url) == {**own, 'basis': 'goal', 'workspace': personal['workspace']}
            assert own['people'] == []  # The staff identity is the trainee, outside this goal's audience.
        else:
            h.get('staff', url, status=403)
    reference = h.get('coordinator', '/api/fairness/distribution?' + urlencode({
        'metric': metric, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}))
    assert reference['basis'] == ('reference_offered' if share_by == 'offered' else 'reference_available_days')
    h.get('coordinator', '/api/fairness/distribution?workspace_id=' + workspace['id'], status=422)
# Include this file in hosted supervision and team-lead qualification.
