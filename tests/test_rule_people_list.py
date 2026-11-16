"""One rule can name several people instead of one copy per person."""
import pytest
from pydantic import ValidationError
from packages.contracts.schemas import Scope
from packages.domain.validation import person_scope_ids, scope_matches


def test_people_list_matches_only_named_people():
    shift = {'service_ids': [], 'location_id': 'l', 'assignment_type_id': 't'}
    scope = {'person_ids': ['a', 'b'], 'weekdays': [], 'start_time': '00:00', 'end_time': '24:00'}
    assert person_scope_ids(scope) == ['a', 'b'] and person_scope_ids({'person_id': 'a'}) == ['a'] and person_scope_ids({}) == []
    # Person check runs first, so someone not named is excluded before any time or place lookup.
    assert scope_matches({}, scope, 'c', shift, None, None) is False


def test_single_person_and_list_are_exclusive_and_unique():
    with pytest.raises(ValidationError):
        Scope(person_id='00000000-0000-0000-0000-000000000001', person_ids=['00000000-0000-0000-0000-000000000002'])
    with pytest.raises(ValidationError):
        Scope(person_ids=['00000000-0000-0000-0000-000000000002'] * 2)
    assert Scope(person_ids=['00000000-0000-0000-0000-000000000002']).person_ids


def test_rule_with_a_people_list_applies_to_each_named_person_only(h):
    a, b, c = h.seed['people'][:3]
    rest = h.policy({'kind': 'min_rest', 'hours': 12}, scope={'person_ids': [a, b]})
    w = h.workspace('People list rota', '2026-10-04', '2026-10-11')
    for i, person in enumerate((a, b, c)):
        w = h.shift(w, f'2026-10-0{4 + i}T08:00:00+03:00', f'2026-10-0{4 + i}T20:00:00+03:00', people=[person])['workspace']
        w = h.shift(w, f'2026-10-0{5 + i}T04:00:00+03:00', f'2026-10-0{5 + i}T10:00:00+03:00', people=[person], location=1)['workspace']
    hit = {f.get('person_id') for f in h.lifecycle(w, 'validate')['workspace']['validation']['findings'] if f.get('rule_id') == rest['id']}
    assert hit == {a, b}
