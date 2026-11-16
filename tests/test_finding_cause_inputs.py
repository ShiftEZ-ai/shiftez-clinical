"""Pure cause-input regressions; no database, scheduler or application fixture."""
from copy import deepcopy

import pytest

from packages.contracts.schemas import FindingCause, PolicyDefinition
from packages.domain.finding_causes import FindingCauses
from packages.domain.quotas import window_proration
from packages.domain.time import aware
from packages.domain import validation


@pytest.mark.parametrize('case,expected', [
    ('none', True), ('leave', False), ('rotation', False), ('mixed', False),
    ('other-person', True), ('adjacent', True), ('rotation-ended', True), ('rotation-available', True),
])
def test_coverage_availability_still_means_only_rotation_or_overlapping_person_records(case, expected):
    a, b = aware('2026-10-04T08:00:00Z'), aware('2026-10-04T12:00:00Z')
    data = {'organisation': {'timezone': 'UTC'}, 'availability': [], 'rotations': [],
            '_candidate_absence_cache': {}, '_coverage_availability_cache': {}}
    if case in {'leave', 'mixed', 'other-person', 'adjacent'}:
        data['availability'] = [{'id': 'leave', 'person_id': 'q' if case == 'other-person' else 'p',
            'start': '2026-10-04T12:00:00Z' if case == 'adjacent' else '2026-10-04T08:00:00Z',
            'end': '2026-10-04T16:00:00Z'}]
    if case in {'rotation', 'mixed', 'rotation-ended', 'rotation-available'}:
        data['rotations'] = [{'id': 'away', 'person_id': 'p', 'starts_on': '2026-10-01',
            'ends_on': '2026-10-04' if case == 'rotation-ended' else '2026-10-05',
            'department_available': case == 'rotation-available'}]
    assert validation.available_for_coverage(data, 'p', a, b) is expected
    assert validation.available_for_coverage(data, 'p', a, b) is expected


@pytest.mark.parametrize('blocker', ['leave', 'rotation'])
def test_empty_pools_reuse_person_interval_availability_for_every_peer(blocker, monkeypatch):
    people = ['p0', 'p1', 'p2']
    shifts = [{'id': f's{n}', 'assignment_type_id': 't', 'service_ids': [], 'location_id': 'l',
        'start': f'2026-10-{4 + (n == 7):02d}T08:00:00+00:00',
        'end': f'2026-10-{4 + (n == 7):02d}T12:00:00+00:00',
        'type_snapshot': {'definition': {'requirements': [{'from_minute': 0, 'maximum': 2}]}},
        'participants': []} for n in range(8)]
    data = {'people': people, 'shifts': shifts, 'policies': [], 'organisation': {'timezone': 'UTC'},
            'workspace': {'department_id': 'd'}, 'availability': [], 'rotations': []}
    if blocker == 'leave':
        data['availability'] = [{'id': 'leave-' + p, 'person_id': p, 'start': '2026-10-04T00:00:00Z',
                                'end': '2026-10-06T00:00:00Z'} for p in people]
    else:
        data['rotations'] = [{'id': 'away-' + p, 'person_id': p, 'department_available': False,
            'starts_on': '2026-10-04', 'ends_on': '2026-10-06'} for p in people]
    scans, synthetic = [], []
    available = validation.available_for_coverage

    def counted(snapshot, pid, a, b):
        scans.append((pid, a, b))
        return available(snapshot, pid, a, b)

    monkeypatch.setattr(validation, 'available_for_coverage', counted)
    monkeypatch.setattr(validation, 'qualified', lambda *args: True)
    monkeypatch.setattr(validation, 'participation_for', lambda *args: synthetic.append(args))
    frozen = deepcopy(data)
    causes = FindingCauses(data)
    results = [causes.coverage('COVERAGE_GAP', sh['id'], aware(sh['start']), aware(sh['end']), {'required': 1})
               for sh in shifts]
    assert len(scans) == len(set(scans)) == 2 * len(people)
    assert not synthetic and data == frozen
    assert all(r['cause']['eligible_people'] == 0 and r['cause']['possible'] == 0 for r in results)
    scans.clear()
    fresh = FindingCauses(data)
    fresh.coverage('COVERAGE_GAP', shifts[0]['id'], aware(shifts[0]['start']), aware(shifts[0]['end']), {'required': 1})
    assert len(scans) == len(people)  # An evaluation cannot inherit another evaluation's answers.


def test_batch_availability_cache_is_scoped_to_each_snapshot(monkeypatch):
    from packages.domain import validation

    people = ['p0', 'p1']
    shift = {'id': 's', 'assignment_type_id': 't', 'service_ids': [], 'location_id': 'l',
             'start': '2026-10-04T08:00:00+00:00', 'end': '2026-10-04T12:00:00+00:00',
             'type_snapshot': {'definition': {'requirements': [{'from_minute': 0, 'minimum': 1, 'maximum': 2}]}},
             'participants': []}
    data = {'fingerprint': 'snapshot-a', 'people': people, 'shifts': [shift], 'policies': [],
            'organisation': {'timezone': 'UTC'}, 'workspace': {'department_id': 'd'},
            'availability': [], 'rotations': [{'person_id': pid, 'department_available': False,
                'starts_on': '2026-10-01', 'ends_on': '2026-10-05'} for pid in people]}
    monkeypatch.setattr(validation, 'qualified', lambda *args: True)
    a, b = aware(shift['start']), aware(shift['end'])
    shared = {}
    first = FindingCauses(data, coverage_availability=shared).coverage('COVERAGE_GAP', 's', a, b, {'required': 1})
    changed = deepcopy(data)
    changed['fingerprint'] = 'snapshot-b'
    for rotation in changed['rotations']:
        rotation['department_available'] = True
    second = FindingCauses(changed, coverage_availability=shared).coverage('COVERAGE_GAP', 's', a, b, {'required': 1})
    assert first['cause']['code'] == 'person_unavailable'
    assert second['cause']['code'] == 'unfilled'


def test_batch_availability_cache_without_fingerprint_fails_closed(monkeypatch):
    from packages.domain import validation

    shift = {'id': 's', 'assignment_type_id': 't', 'service_ids': [], 'location_id': 'l',
             'start': '2026-10-04T08:00:00+00:00', 'end': '2026-10-04T12:00:00+00:00',
             'type_snapshot': {'definition': {'requirements': [{'from_minute': 0, 'minimum': 1, 'maximum': 2}]}},
             'participants': []}
    data = {'people': ['p'], 'shifts': [shift], 'policies': [],
            'organisation': {'timezone': 'UTC'}, 'workspace': {'department_id': 'd'},
            'availability': [], 'rotations': [{'person_id': 'p', 'department_available': False,
                'starts_on': '2026-10-01', 'ends_on': '2026-10-05'}]}
    monkeypatch.setattr(validation, 'qualified', lambda *args: True)
    start, end = aware(shift['start']), aware(shift['end'])
    shared = {}
    first = FindingCauses(data, coverage_availability=shared).coverage('COVERAGE_GAP', 's', start, end, {'required': 1})
    changed = deepcopy(data)
    changed['rotations'][0]['department_available'] = True
    second = FindingCauses(changed, coverage_availability=shared).coverage('COVERAGE_GAP', 's', start, end, {'required': 1})
    assert first['cause']['code'] == 'person_unavailable'
    assert second['cause']['code'] == 'unfilled'
    assert shared == {}


@pytest.mark.parametrize('comparison,state,partial,expected', [
    ('minimum', 'below_minimum', True, 'partial_window'),
    ('target', 'below_target', True, 'partial_window'),
    ('minimum', 'below_minimum', False, 'no_candidate_sessions'),
    ('target', 'above_target', True, 'above_limit'),
    ('maximum', 'above_maximum', False, 'above_limit'),
])
def test_empty_quota_keeps_proration_metadata_and_upper_bound_priority(comparison, state, partial, expected):
    a, b = aware('2026-10-01T00:00:00Z'), aware('2026-10-04T00:00:00Z')
    definition = PolicyDefinition.model_validate({'rule': {'kind': 'block_quota', 'metric': 'duties',
        'comparison': comparison, 'amount': 6, 'starts_on': '2026-09-27', 'ends_on': '2026-10-04'},
        'scope': {'person_id': '00000000-0000-4000-9000-000000000001'},
        'severity': 'advisory' if comparison == 'target' else 'mandatory'}).model_dump(mode='json')
    policy = {'id': 'quota', 'department_id': 'd', 'definition': definition}
    proration = {'configured_amount': 6, 'amount': 2, 'working_days': 1, 'full_working_days': 5,
        'starts_on': '2026-09-27', 'ends_on': '2026-10-04', 'calendar_id': None, 'rounding': 'up'} if partial else None
    data = {'shifts': [], 'organisation': {'timezone': 'UTC'},
            'workspace': {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-10-04'}}
    result = FindingCauses(data).quota(policy, 'p', a, b, 2 if partial else 6,
                                     state=state, actual=0, proration=proration)
    assert result['cause']['code'] == expected and result['cause']['sessions_found'] == 0
    assert result['cause']['required'] == (2 if partial else 6)
    FindingCause.model_validate(result['cause'])
    if partial:
        assert result['cause']['proration'] == proration
    assert [hint['action'] for hint in result['fix_hints']] == (
        ['review_assignments'] if expected == 'above_limit' else ['add_sessions'])
    if comparison == 'maximum':
        assert window_proration({}, policy, 'p', 6, {}, None) == (6, None)
