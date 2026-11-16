"""Candidate checks share immutable restriction selection without losing full rule context."""
import copy
import pytest

from packages.domain import validation
from packages.domain.candidate_evidence import candidate_evidence
from .test_finding_causes import cause_data, cause_snapshot, policy, uid


class CountedPolicies(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.visits = 0

    def __iter__(self):
        for row in super().__iter__():
            self.visits += 1
            yield row


def rules(data, pid):
    unused = policy(data, {'kind': 'max_duties', 'maximum': 5}, name='irrelevant-workload')
    restriction = policy(data, {'kind': 'restriction', 'weekdays': [6], 'start_time': '08:00', 'end_time': '12:00'},
                         {'person_id': pid}, 'person-restriction')
    return [{**unused, 'id': uid(f'candidate-unused-{n}')} for n in range(1000)] + [restriction]


def codes(row):
    return {reason['code'] for reason in row['exclusions']}


@pytest.mark.parametrize('cache_key', ['_line_index_cache', '_restriction_index_cache'])
def test_repeated_candidate_checks_visit_unused_rules_once_and_preserve_canonical_exclusions(cause_data, monkeypatch, cache_key):
    data, pid = cause_data
    shift = data['shifts'][0]
    source = rules(data, pid)
    data['availability'] = [{'id': uid('candidate-leave'), 'person_id': pid, 'kind': 'approved_leave',
                             'start': shift['start'], 'end': shift['end']}]
    uncached = {**data, 'policies': source, '_line_index_cache': None, '_restriction_index_cache': None}
    expected = {person: candidate_evidence(uncached, person, shift) for person in data['people']}
    assert codes(expected[pid]) == {'UNAVAILABLE', 'RESTRICTION'}
    assert all(not codes(row) for person, row in expected.items() if person != pid)
    counted = CountedPolicies(source)
    indexed = {**data, 'policies': counted, cache_key: {}}
    original = validation.applicable_restriction_intervals
    context_checks = []

    def observe(context, *args, **kwargs):
        context_checks.append(context['policies'] is counted)
        return original(context, *args, **kwargs)

    monkeypatch.setattr(validation, 'applicable_restriction_intervals', observe)
    for _ in range(3):
        trial = {**indexed, 'fingerprint': 'A participant-only trial with the same rule inputs'}
        for person in data['people']:
            assert candidate_evidence(trial, person, shift) == expected[person]
    assert counted.visits == len(counted)
    assert context_checks and all(context_checks)


@pytest.mark.parametrize('cache_key', ['_line_index_cache', '_restriction_index_cache'])
def test_new_policy_source_does_not_reuse_another_snapshot_restriction_selection(cause_data, cache_key):
    data, pid = cause_data
    shift = data['shifts'][0]
    other = next(person for person in data['people'] if person != pid)
    source = rules(data, pid)
    first = CountedPolicies(source)
    cache = {}
    view = {**data, 'policies': first, cache_key: cache}
    assert 'RESTRICTION' in codes(candidate_evidence(view, pid, shift))
    changed = copy.deepcopy(source[-1])
    changed['definition']['scope']['person_id'] = other
    replacement = CountedPolicies(source[:-1] + [changed])
    trial = {**view, 'policies': replacement}
    assert 'RESTRICTION' not in codes(candidate_evidence(trial, pid, shift))
    assert 'RESTRICTION' in codes(candidate_evidence(trial, other, shift))
    assert first.visits == len(first) and replacement.visits == len(replacement)
