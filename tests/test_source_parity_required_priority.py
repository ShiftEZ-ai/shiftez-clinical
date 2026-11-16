"""Configured fill tiers follow coverage, including diagnostic proposals with soft cover."""
from collections import Counter

import pytest

from packages.domain.cp_sat import generate


@pytest.mark.parametrize('people_count,high_count', [(2, 1), (3, 2)])
def test_desired_high_priority_seat_cannot_displace_required_cover(h, people_count, high_count):
    high = h.configure('assignment_types', {'name': 'Synthetic desired cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'target': 2, 'maximum': 2}]}})['record']['id']
    low = h.configure('assignment_types', {'name': 'Synthetic required cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 10},
             {'service_ids': [h.seed['services'][0]]}, 'advisory')
    workspace = h.shift(h.workspace(), people=[], typ=high)['workspace']
    workspace = h.shift(workspace, people=[], typ=low, service=1, location=1)['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in h.seed['people'][:people_count]}
    data['generation_options'] = {'allocation_order': 'tiered'}
    result = generate(data, 5, diagnostic_proposal=True)
    assigned = Counter(row['shift_id'] for row in result['additions'])
    by_type = {shift['assignment_type_id']: shift['id'] for shift in data['shifts']}
    assert assigned[by_type[low]] == 1
    assert assigned[by_type[high]] == high_count
    order = result['diagnostic_objective_order']
    assert order.index('coverage') < min(i for i, name in enumerate(order) if name.startswith('fill:'))
    assert result['validation']['valid']
    assert result['status'] == 'candidate_with_gaps', result  # Explicit diagnostic mode never certifies operational feasibility.
