"""Empty-rota targets use the same clinic-aware reach as their quota subjects."""
import copy

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain import quotas
from packages.domain.cp_sat import generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.validation import evaluate
from .test_quota_skill_subjects import _quota


def _prorated_target(h, variant, exclusion='none'):
    data, policy, person, skill = _quota(h)
    data['shifts'], data['focus_ids'] = [], []
    for line in policy['definition']['rule']['lines']:
        line['settings']['block_quota'].update(prorate_department_days=True, reference_days=28)
        line['numbers'] = {'target': {'value': 28 if line is policy['definition']['rule']['lines'][0] else 0,
                                      'level': 'medium'}}
    default_id = policy['definition']['rule']['lines'][0]['id']
    if variant == 'negative_skill':
        policy['definition'] = PolicyDefinition.model_validate({
            'severity': 'advisory', 'scope': {'conditions': {'match': 'all', 'conditions': [
                {'field': 'capability_ids', 'op': 'none', 'values': [skill]}]}},
            'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 28,
                     'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                     'prorate_department_days': True, 'reference_days': 28},
        }).model_dump(mode='json')
        policy['kind'] = 'block_quota'
    if exclusion == 'membership':
        for membership in data['memberships']:
            membership['starts_on'] = '2026-10-06'
    return data, person, default_id


@pytest.mark.parametrize('variant', ['negative_skill', 'catalogue_default'])
@pytest.mark.parametrize('exclusion,expected', [('none', 7), ('membership', 5)])
def test_empty_target_prorates_the_clinics_where_its_scope_applies(h, variant, exclusion, expected):
    data, person, default_id = _prorated_target(h, variant, exclusion)
    frozen = copy.deepcopy(data)

    report = evaluate(data)['quota_report']

    assert data == frozen
    assert report['complete'], report
    rows = [row for row in report['rows'] if row['person_id'] == person
            and (variant == 'negative_skill' or row['line_id'] == default_id)]
    assert len(rows) == 1, report
    assert rows[0]['amount'] == expected and rows[0]['actual'] == 0, rows[0]
    assert rows[0]['status'] == 'below_target', rows[0]
    assert rows[0]['target_calculation']['department_days'] == 7
    assert rows[0]['target_calculation']['calculated_amount'] == 7
    if exclusion == 'membership':
        assert rows[0]['proration']['working_days'] == 3
        assert rows[0]['proration']['full_working_days'] == 5
    else:
        assert not rows[0].get('proration')


def test_proration_shares_the_report_context_budget_and_both_schedulers_fail_closed(h, monkeypatch):
    data, person, _ = _prorated_target(h, 'negative_skill')
    # Even one clinic witness cannot fit a zero report-wide context budget.
    monkeypatch.setattr(quotas, 'MAX_SUBJECT_CONTEXTS', 0)
    check = evaluate(data)
    assert not check['quota_report']['complete'], check
    assert any(f['code'] == 'QUOTA_SCOPE_LIMIT' for f in check['findings'])
    assert next(f for f in check['findings'] if f['code'] == 'QUOTA_SCOPE_LIMIT')['person_id'] is None
    assert not any(r['person_id'] == person and r['status'] == 'met' for r in check['quota_report']['rows'])
    for engine in (generate, greedy_generate):
        result = engine(data, time_budget_seconds=2)
        assert result['status'] == 'invalid_inputs' and not result.get('additions'), result
