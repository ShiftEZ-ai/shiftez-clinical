"""Versioned score settings and the complete advisory-line score review read."""


def test_score_settings_are_versioned_projected_and_undoable(h):
    initial = h.get('admin', '/api/score-settings')
    h.command('admin', 'score.settings', {'definition': initial['definition'],
        'reason': 'Persist the initial score calibration'}, h.seed['department_id'])
    current = h.get('admin', '/api/score-settings')
    assert current['definition']['strengths'] == {'low': 1, 'medium': 3, 'high': 10, 'critical': 30}
    changed = h.command('admin', 'score.settings', {
        'definition': {'weights': {'coverage': 35, 'rules': 25, 'fairness': 40, 'preferences': 10, 'stability': 5},
                       'strengths': {'low': 1, 'medium': 4, 'high': 12, 'critical': 30}},
        'reason': 'Calibrate advisory tiers for score review'},
        h.seed['department_id'], current['settings']['revision'])['settings']
    assert changed['revision'] == current['settings']['revision'] + 1
    assert h.get('admin', '/api/me')['department']['generation_defaults']['scoring']['strengths']['medium'] == 4
    history = h.get('admin', '/api/score-settings?history=true')
    assert [row['revision'] for row in history['versions'][:2]] == [changed['revision'], current['settings']['revision']]
    restored = h.command('admin', 'score.settings.rollback',
        {'version_id': current['settings']['id'], 'reason': 'Undo score calibration'},
        h.seed['department_id'], changed['revision'])['settings']
    assert restored['definition'] == current['definition']


def test_score_review_lists_zero_and_nonzero_advisory_lines(h):
    workspace = h.shift(h.workspace('Score review read'), people=[h.seed['people'][0]])['workspace']
    h.command('coordinator', 'workspace.validate', {'reason': 'Build score review evidence'},
              workspace['id'], workspace['revision'])
    result = h.get('coordinator', f"/api/workspaces/{workspace['id']}/score-review")
    assert result['score']['score'] is not None
    assert result['total_lost_points'] == round(100 - result['score']['score'], 1)
    assert all({'rule_id', 'level', 'weight', 'lost_points', 'lost_share', 'session_count', 'people_count'} <= set(row)
               for row in result['lines'])


def test_score_review_attributes_grouped_fair_share_loss_to_the_source_line(monkeypatch):
    from packages.domain import score
    from packages.domain import fairness

    monkeypatch.setattr(score, 'active_rules', lambda policies, context=None: policies)
    monkeypatch.setattr(score, 'counting_views', lambda data, rules: rules)
    monkeypatch.setattr(fairness, 'goal_people', lambda data, policy: {'who': [], 'partners': []})

    source = {
        'id': 'source-policy', 'name': 'Fair share line', 'status': 'active', 'active': True,
        'department_id': 'd', 'starts_on': '2020-01-01', 'ends_on': None,
        'template_line': {'id': 'source-line', 'line': 2, 'slot': 'apply'},
        'definition': {'severity': 'advisory', 'strength': 'high', 'scope': {},
                       'rule': {'kind': 'balance_goal', 'metric': 'duties'}},
    }
    data = {'policies': [source], 'focus_ids': [], 'shifts': [], 'people': {}, 'memberships': [],
            'workspace': {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'organisation': {'timezone': 'UTC'}, 'generation_options': {}}
    scored = {'contributions': [{'component': 'fairness', 'points': 4.0, 'rule_id': 'comparison-policy',
                                 'rule_source_id': 'source-policy', 'line_id': 'source-line', 'line': 2,
                                 'slot': 'apply', 'rule_strength': 'high', 'rule_weight': 10}]}

    result = score.score_review_lines(data, {'findings': []}, scored=scored)

    assert len(result['lines']) == 1
    assert result['lines'][0]['rule_id'] == 'source-policy'
    assert result['lines'][0]['lost_points'] == 4.0
    assert result['lines'][0]['component'] == 'fairness'


def test_score_review_counts_quota_audience_without_a_current_assignment(monkeypatch):
    from packages.domain import score

    monkeypatch.setattr(score, 'active_rules', lambda policies, context=None: policies)
    monkeypatch.setattr(score, 'counting_views', lambda data, rules: rules)

    quota = {
        'id': 'quota-policy', 'name': 'Monthly quota', 'status': 'active', 'active': True,
        'department_id': 'd', 'starts_on': '2020-01-01', 'ends_on': None,
        'template_line': {'id': 'quota-line', 'line': 0, 'slot': 'target'},
        'definition': {'severity': 'advisory', 'strength': 'medium', 'scope': {},
                       'rule': {'kind': 'block_quota'}},
    }
    data = {'policies': [quota], 'focus_ids': [], 'shifts': [], 'people': {}, 'memberships': [],
            'workspace': {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'organisation': {'timezone': 'UTC'}, 'generation_options': {}}
    validation = {'findings': [], 'quota_report': {'rows': [{'policy_id': 'quota-policy', 'line_id': 'quota-line',
                   'line': 0, 'slot': 'target', 'person_id': 'person-without-assignment'}]}}

    result = score.score_review_lines(data, validation, scored={'contributions': []})

    assert result['lines'][0]['people_count'] == 1


def test_an_admin_only_account_reads_the_score_settings_it_changes(h):
    from .governance_support import identity
    identity(h, 'score_admin_only', ['admin'])
    assert h.get('score_admin_only', '/api/score-settings')['definition']['strengths']['medium'] >= 1
    assert h.get('score_admin_only', '/api/score-settings?history=true')['versions']
