"""Refusals expose only the clash fields the planner consumes."""
import json
from apps.api.services.planning import refusal_details


def test_refusal_projection_drops_large_diagnostics_and_advisory_findings():
    clash = {'code': 'OVERLAP', 'severity': 'error', 'person_id': 'person', 'shift_id': 'session',
             'start': '2027-06-01T08:00:00+03:00', 'message': 'Person is already on duty on 1 June',
             'detail': {'private_evidence': 'x' * 1_700_000}, 'candidates': list(range(10000))}
    result = refusal_details([clash, {**clash, 'severity': 'warning'}])
    assert result['findings'] == [{k: v for k, v in clash.items() if k not in {'detail', 'candidates'}}]
    assert len(json.dumps(result)) < 1000
