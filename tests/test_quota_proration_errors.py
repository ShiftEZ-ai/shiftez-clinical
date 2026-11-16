"""Quota configuration failures remain findings; programming failures must escape."""
import pytest

from packages.domain import quotas
from packages.domain.validation import evaluate
from .test_finding_causes import cause_data, cause_snapshot  # noqa: F401
from .test_quota_working_day_proration import add_quota, working_data  # noqa: F401


@pytest.mark.parametrize('error_type', [KeyError, TypeError])
def test_proration_does_not_hide_programming_failures(working_data, monkeypatch, error_type):
    data, person = working_data
    add_quota(data, person, starts_on='2026-10-01')
    failure = error_type('Synthetic proration implementation failure')

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(quotas, 'window_proration', broken)
    with pytest.raises(error_type) as caught:
        evaluate(data)
    assert caught.value is failure


def test_expected_proration_value_error_keeps_compliance_unresolved(working_data, monkeypatch):
    data, person = working_data
    policy = add_quota(data, person, starts_on='2026-10-01')

    def unresolved(*args, **kwargs):
        raise ValueError('Synthetic unresolved calendar boundary')

    monkeypatch.setattr(quotas, 'window_proration', unresolved)
    checked = evaluate(data)
    assert checked['valid'] is False and checked['quota_report']['complete'] is False
    assert checked['quota_report']['rows'] == []
    [finding] = [finding for finding in checked['findings'] if finding['code'] == 'QUOTA_PRORATION_UNRESOLVED']
    assert finding['severity'] == 'error' and finding['rule_id'] == policy['id']
    assert finding['person_id'] == person and finding['message'] == 'Synthetic unresolved calendar boundary'
