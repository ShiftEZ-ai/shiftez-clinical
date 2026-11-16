import pytest
from pydantic import ValidationError

from packages.contracts.schemas import FindingCause, FindingFixHint


def test_finding_cause_contract_is_structured_and_validated():
    cause = FindingCause(code='no_candidate_sessions', sessions_found=0, eligible_people=0,
                         days_in_window=7, required=1)
    assert cause.model_dump()['basis'] == 'known_records_not_solver_proof'
    hint = FindingFixHint(action='add_sessions', prefill={
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'assignment_type_ids': ['type']})
    assert hint.prefill.assignment_type_ids == ['type']
    with pytest.raises(ValidationError):
        FindingCause(**{**cause.model_dump(), 'sessions_found': -1})
    with pytest.raises(ValidationError):
        FindingFixHint(action='arbitrary_action', prefill=hint.prefill)
