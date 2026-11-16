"""A kept source conflict must not silently erase unrelated mandatory minima."""
import pytest

from packages.domain.cp_sat import generate


@pytest.mark.parametrize('diagnostic', [False, True])
def test_kept_overlap_does_not_erase_unfillable_mandatory_quota(h, diagnostic):
    person = h.seed['people'][0]
    absent_type = h.configure('assignment_types', {'name': 'No sessions in selected rota',
        'starts_on': '2020-01-01', 'definition': {'requirements': []}})['record']['id']
    quota = h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1,
        'assignment_type_ids': [absent_type]}, {'person_id': person})
    booked = {'person_id': person, 'start': '2026-10-04T08:00:00+03:00',
              'end': '2026-10-04T12:00:00+03:00', 'fixed': True}
    workspace = h.shift(h.workspace(), people=[booked])['workspace']
    workspace = h.shift(workspace, location=1, people=[booked])['workspace']
    data = h.snapshot(workspace)
    result = generate(data, 5, diagnostic_proposal=diagnostic)
    if diagnostic:
        assert result['status'] == 'candidate_with_gaps', result
        assert any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in result['validation']['findings'])
        assert not result['validation']['valid']
    else:
        assert result['status'] == 'requirements_conflict', result
        assert not result['additions'] and not result['removals']
        assert any(r.get('rule_id') == quota['id'] for d in result['diagnostics']
                   for r in d.get('requirements', [])), result
