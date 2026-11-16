"""Version bumps preserve immutable historical reports and personal export privacy."""
from copy import deepcopy

import pytest

from packages.domain.quotas import project_report


@pytest.mark.parametrize('version', ['block-quota-v1', 'block-quota-v2', 'block-quota-v3', 'block-quota-v4',
                                   'block-quota-v5', 'block-quota-v6'])
def test_historical_quota_reports_remain_readable_and_private(version):
    report = {'metric_version': version, 'source_publications': ['private-source'], 'empty_policy_ids': ['unused'],
        'rows': [{'person_id': 'one', 'amount': 4.57, 'actual': 4,
                  'target_calculation': {'override_reason': 'private explanation', 'override_amount': 4.57}},
                 {'person_id': 'two', 'amount': 5, 'actual': 5}]}
    before = deepcopy(report)
    assert project_report(report) == before
    assert project_report(report, 'one') == {'metric_version': version,
        'rows': [{'person_id': 'one', 'amount': 4.57, 'actual': 4,
                  'target_calculation': {'override_amount': 4.57}}]}
    assert report == before


def test_unknown_quota_report_version_is_refused():
    with pytest.raises(ValueError, match='Unsupported quota report version'):
        project_report({'metric_version': 'block-quota-v99', 'rows': []})
