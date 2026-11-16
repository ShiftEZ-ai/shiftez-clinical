"""Offered sharing is additive, overtime-only and suggested through the governed catalogue."""
import uuid
from typing import get_args

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import CATALOGUE_BY_ID, V2_CATALOGUE_BY_ID


@pytest.mark.parametrize('metric', get_args(s.BalanceGoal.model_fields['metric'].annotation))
def test_offered_sharing_is_only_valid_for_overtime_metrics(metric):
    values = {'kind': 'balance_goal', 'metric': metric, 'share_by': 'offered'}
    if metric.startswith('overtime_'):
        assert s.BalanceGoal.model_validate(values).share_by == 'offered'
    else:
        with pytest.raises(ValidationError) as error:
            s.BalanceGoal.model_validate(values)
        assert error.value.errors()[0]['type'] == s.BALANCE_SHARE_OFFERED_METRIC


@pytest.mark.parametrize('registry', [CATALOGUE_BY_ID, V2_CATALOGUE_BY_ID])
def test_catalogue_suggests_new_overtime_lines_without_changing_ordinary_defaults(registry):
    entry = registry['fair_share']
    assert entry['default_settings'] == {'balance_goal': {'metric': 'duties'}}
    ordinary = s.BalanceGoal(kind='balance_goal', **entry['default_settings']['balance_goal'])
    assert (ordinary.period, ordinary.share_by) == ('rota', 'available_days')
    for metric, suggestion in entry['suggested_lines'].items():
        goal = s.BalanceGoal(kind='balance_goal', **suggestion['default_settings']['balance_goal'])
        assert (goal.metric, goal.period, goal.share_by) == (metric, 'quarter', 'offered')
        assert suggestion['default_line']['numbers'] == {'apply': {'value': None, 'level': 'medium'}}
        assert all(slot['advisory'] for slot in entry['numbers']['apply'])


def test_catalogue_rejects_an_invalid_effective_line_metric():
    with pytest.raises(ValidationError) as error:
        s.CatalogueRule.model_validate({'kind': 'catalogue', 'id': 'fair_share', 'catalogue_version': 2,
            'settings': {'balance_goal': {'metric': 'overtime_hours', 'share_by': 'offered'}},
            'lines': [{'id': str(uuid.uuid4()), 'numbers': {'apply': {'level': 'medium'}},
                       'settings': {'balance_goal': {'metric': 'hours'}}}]})
    assert error.value.errors()[0]['type'] == s.BALANCE_SHARE_OFFERED_METRIC


def test_legacy_policy_admission_returns_a_clear_400(h):
    result = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic invalid sharing', 'owner': 'Test owner', 'source': 'Synthetic contract regression',
        'starts_on': '2026-10-01', 'definition': {'severity': 'advisory',
            'rule': {'kind': 'balance_goal', 'metric': 'duties', 'share_by': 'offered'}}}, status=400)
    assert result['error']['code'] == s.BALANCE_SHARE_OFFERED_METRIC
    assert 'overtime duties and overtime hours' in result['error']['message']


def test_catalogue_checks_and_proposals_preserve_the_400_error(h):
    current = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'fair_share')['rule']
    body = {'settings': {'balance_goal': {'metric': 'hours', 'share_by': 'offered'}},
            'lines': [{'id': str(uuid.uuid4()), 'numbers': {'apply': {'level': 'medium'}}}]}
    response = h.client('coordinator').post('/api/rules/catalogue/fair_share/check', json=body)
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == s.BALANCE_SHARE_OFFERED_METRIC
    result = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'fair_share', 'reason': 'Synthetic invalid sharing',
        'expected_policy_id': current['id'], 'expected_revision': current['revision'], **body}, status=400)
    assert result['error']['code'] == s.BALANCE_SHARE_OFFERED_METRIC


def test_catalogue_exposes_suggestions_without_creating_a_goal(h):
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'fair_share')
    current = entry['rule']
    h.command('approver', 'policy.retire', {'reason': 'Synthetic empty catalogue state'}, current['id'], current['revision'])
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'fair_share')
    assert entry['rule'] is None and entry['proposed'] is None
    assert entry['suggested_lines'] == CATALOGUE_BY_ID['fair_share']['suggested_lines']
