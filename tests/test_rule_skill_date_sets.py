"""Clinic-specific quota subjects retain their date-set conditions across the complete block."""
import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.time import aware
from packages.domain.validation import evaluate, policy_applies
from .test_date_set_conditions import date_sets, selected
from .test_quota_skill_subjects import _assigned, _quota


def scoped_holiday_quota(h, *, comparison='max', op='any', skill_interval=None, counted=False):
    data, policy, person, skill = _quota(h, service=0, comparison=comparison)
    skill_line = policy['definition']['rule']['lines'][1]
    skill_line['numbers'][comparison]['value'] = 0 if comparison == 'max' else 1
    skill_line['where'] = selected('Eid', op=op, count='each' if counted else 'together')
    if op == 'none':
        skill_line['where']['conditions'][0].pop('count')
    policy['definition']['rule']['lines'] = [skill_line]
    policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    data['date_sets'] = date_sets()  # Eid starts Oct 5, later within the block starting Oct 4.
    if op == 'none':
        next(iter(data['date_sets'].values()))['entries'][0]['first_day'] = '2026-10-04'
    if skill_interval:
        for grant in data['grants']:
            if grant['person_id'] == person and grant['capability_id'] == skill:
                grant.update(skill_interval)
    return data, policy, person


def test_actual_holiday_work_retains_clinic_skill_maximum(h):
    data, policy, person = scoped_holiday_quota(h)
    for shift in data['shifts']:
        shift['start'] = shift['start'].replace('2026-10-04', '2026-10-05')
        shift['end'] = shift['end'].replace('2026-10-04', '2026-10-05')
    data = _assigned(data, person)
    [resolved] = active_rules([policy], context=data)
    assert resolved['definition']['severity'] == 'mandatory'
    assert all(policy_applies(data, resolved, person, shift, aware(shift['start']), aware(shift['end']))
               for shift in data['shifts'])

    check = evaluate(data)
    report = check['quota_report']
    assert report['complete']
    assert len(report['rows']) == 1, 'Two concretely applicable holiday duties lost their mandatory maximum'
    assert report['rows'][0]['actual'] == 2 and report['rows'][0]['amount'] == 0
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' and f['severity'] == 'error' for f in check['findings'])
    assert check['valid'] is False


@pytest.mark.parametrize('op,skill_interval,counted,required', [
    ('any', None, False, True),  # The selected holiday starts later within the quota block.
    ('none', None, False, True),  # Unselected days start after the holiday ends.
    ('any', {'starts_on': '2026-10-07'}, False, False),  # Skill and holiday never meet.
    ('none', {'ends_on': '2026-10-07'}, False, False),  # Skill ends before unselected days.
    ('any', None, True, True),  # The date selector survives an internal COUNT_BUCKET wrapper.
])
def test_empty_holiday_minimum_uses_coincident_skill_and_date_context(h, op, skill_interval, counted, required):
    data, _, person = scoped_holiday_quota(h, comparison='min', op=op,
                                          skill_interval=skill_interval, counted=counted)
    data['shifts'], data['focus_ids'] = [], []
    check = evaluate(data)
    report = check['quota_report']
    assert report['complete'], report
    rows = [row for row in report['rows'] if row['person_id'] == person]
    assert bool(rows) is required, report
    assert check['valid'] is not required, check
    if required:
        assert len(rows) == 1 and rows[0]['amount'] == 1 and rows[0]['actual'] == 0
        assert any(f['code'] == 'QUOTA_BELOW_MINIMUM' and f['severity'] == 'error' for f in check['findings'])
