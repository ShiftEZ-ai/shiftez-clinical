"""Recurring candidate filtering supports hypothetical duties and existing manual participants."""
import pytest

from packages.domain.candidate_evidence import candidate_evidence
from .test_restriction_performance import duty, restriction_data


@pytest.mark.parametrize('fixed,participants,person,allowed', [
    (False, None, '1', True),
    (True, None, '0', True),
    (True, None, '1', False),
    (True, [], '1', False),
    (True, [{'person_id': '1'}], '1', True),
])
def test_hypothetical_candidate_respects_configured_usual_people_and_retains_existing_participants(fixed, participants, person, allowed):
    data = restriction_data(2)
    shift, _, _ = duty(0)
    shift['series_snapshot'] = {'definition': {'usual_assignees_fixed': fixed, 'default_person_ids': ['0']}}
    if participants is not None:
        shift['participants'] = participants
    result = candidate_evidence(data, person, shift)
    assert result['eligible_and_available'] is allowed
    assert ('USUAL_ASSIGNEES_FIXED' in {reason['code'] for reason in result['exclusions']}) is (not allowed)
