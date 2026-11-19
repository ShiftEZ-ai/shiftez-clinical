import pytest
from packages.domain.validation import evaluate


def test_named_supervision_requires_present_qualified_person_and_survives_amendment(h):
    trainee, supervisor = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Paired training', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [{'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
              {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor', 'team_lead': True}]
    result = h.shift(h.workspace(), typ=typ['id'], people=people)
    assert result['validation']['valid']
    data = h.snapshot(result['workspace'])
    next(p for p in data['shifts'][0]['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = h.seed['people'][2]
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in evaluate(data)['findings']}
    published = h.publish(result['workspace'])
    # All export formats use the issued relationship; staff see only their own pair (their supervisor), no other colleague.
    import csv
    import io
    url = '/api/reports/' + published['publication_id']
    supervisor_name = h.get('coordinator', '/api/publications/' + published['publication_id'])['snapshot']['people'][supervisor]['display_name']
    exported = list(csv.DictReader(io.StringIO(h.client('coordinator').get(url + '?format=assignments').text)))
    assert next(r for r in exported if r['participation'] == 'trainee')['supervisor'] == supervisor_name
    assert next(r for r in exported if r['participation'] == 'supervisor')['team_lead'] == 'true'
    for role in ('coordinator', 'staff'):
        for format in ('assignments', 'print', 'ics'):
            response = h.client(role).get(url + '?format=' + format + ('&person_id=' + trainee if format == 'ics' else ''))
            assert response.status_code == 200
            text = response.text.replace('\r\n ', '')  # Unfold iCalendar lines.
            assert supervisor_name in text  # staff (the trainee here) see who supervises them
    amended = h.workspace(name='Paired amendment', base=published['publication_id'])
    participants = h.detail(amended)['shifts'][0]['participants']
    assert next(p for p in participants if p['person_id'] == trainee)['supervisor_person_id'] == supervisor
    assert next(p for p in participants if p['person_id'] == supervisor)['team_lead'] is True


def test_supervisor_rank_and_capacity_limits_require_pairing_and_authority(h):
    trainee, supervisor = h.seed['people'][:2]
    rank = h.seed['trainee_category']
    unsafe = {'name': 'Unsafe supervision configuration', 'owner': 'Synthetic owner',
        'source': 'Synthetic safety-boundary check', 'starts_on': '2020-01-01',
        'definition': {'severity': 'advisory', 'rule': {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 1}}}
    h.command('coordinator', 'policy.create', unsafe, status=422)
    unsafe['definition'].update(severity='mandatory', exception_allowed=True)
    h.command('coordinator', 'policy.create', unsafe, status=422)
    typ = h.configure('assignment_types', {'name': 'Scoped supervision', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [rank],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3}}})['record']
    h.policy({'kind': 'supervision_limit', 'mode': 'allowed_ranks', 'trainee_category_ids': [rank]},
             {'person_id': supervisor})
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [{'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
              {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'}]
    result = h.shift(h.workspace(), typ=typ['id'], people=people)
    assert 'SUPERVISION_RANK' in {f['code'] for f in result['validation']['findings']}
    h.lifecycle(result['workspace'], 'submit', status=422)
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank, 'starts_on': '2026-10-01'})
    data = h.snapshot(result['workspace'])
    assert evaluate(data)['valid']
    import copy
    missing_pair = copy.deepcopy(data)
    next(p for p in missing_pair['shifts'][0]['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = None
    assert 'SUPERVISION_PAIRING' in {f['code'] for f in evaluate(missing_pair)['findings']}
    missing_authority = copy.deepcopy(data)
    missing_authority['grants'] = [g for g in data['grants'] if g['person_id'] != supervisor]
    assert 'SUPERVISION_GAP' in {f['code'] for f in evaluate(missing_authority)['findings']}
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0, 'trainee_category_ids': [rank]},
             {'person_id': supervisor, 'weekdays': [6], 'start_time': '09:00', 'end_time': '10:00'})
    limited = evaluate(h.snapshot(result['workspace']))
    findings = [f for f in limited['findings'] if f['code'] == 'SUPERVISOR_SCOPED_CAPACITY']
    assert len(findings) == 1 and findings[0]['detail'] == {'maximum': 0, 'scheduled': 1, 'rule_severity': 'mandatory', 'rule_strength': 'medium'}
    from packages.domain.time import aware
    assert aware(findings[0]['start']) == aware('2026-10-04T09:00:00+03:00')
    assert aware(findings[0]['end']) == aware('2026-10-04T10:00:00+03:00')
    h.lifecycle(result['workspace'], 'submit', status=422)


def test_allocation_tries_another_supervisor_when_first_has_a_scoped_limit(h):
    from packages.domain.generation import generate
    trainee = h.seed['people'][0]
    blocked, allowed = sorted(h.seed['people'][1:])[:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0}, {'person_id': blocked})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': trainee})
    typ = h.configure('assignment_types', {'name': 'Alternative named supervisors', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [{'person_id': pid, 'start': start, 'end': end, 'participation': 'supervisor'} for pid in (blocked, allowed)]
    w = h.shift(h.workspace(), typ=typ['id'], people=people)['workspace']
    candidate = generate(h.snapshot(w))
    assert candidate['status'] == 'candidate_feasible'
    additions = [r['participant'] for r in candidate['additions']]
    assert len(additions) == 1 and additions[0]['person_id'] == trainee
    assert additions[0]['supervisor_person_id'] == allowed
    assert len(h.detail(w)['shifts'][0]['participants']) == 2  # Suggestions do not mutate the draft.


@pytest.mark.postgres
def test_supervisor_limits_postgres(pg):
    test_supervisor_rank_and_capacity_limits_require_pairing_and_authority(pg)


@pytest.mark.postgres
def test_supervisor_overflow_postgres(pg):
    test_supervisor_overflow_is_rank_limited_and_preserves_hard_ratio(pg)


def test_supervisor_overflow_is_rank_limited_and_preserves_hard_ratio(h):
    import copy
    trainee, other, supervisor = h.seed['people']
    rank = h.seed['trainee_category']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank, 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Explicit extra supervision place', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [rank],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3, 'require_pairing': True}}})['record']
    policy = h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 1,
                       'overflow_maximum': 1, 'overflow_category_ids': [rank]})
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    participants = [{'person_id': pid, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor}
                    for pid in (trainee, other)]
    participants.append({'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'})
    result = h.shift(h.workspace(), typ=typ['id'], people=participants)
    assert result['validation']['valid']
    warning = next(f for f in result['validation']['findings'] if f['code'] == 'CAPACITY_OVERFLOW')
    assert warning['severity'] == 'warning' and warning['detail']['supervisor_person_id'] == supervisor
    data = h.snapshot(result['workspace'])
    limited = copy.deepcopy(data)
    limited['shifts'][0]['type_snapshot']['definition']['supervision']['trainees_per_supervisor'] = 1
    assert 'SUPERVISOR_CAPACITY' in {f['code'] for f in evaluate(limited)['findings']}
    no_rank = copy.deepcopy(data)
    next(p for p in no_rank['policies'] if p['id'] == policy['id'])['definition']['rule']['overflow_category_ids'] = ['unmatched-rank']
    assert 'SUPERVISOR_SCOPED_CAPACITY' in {f['code'] for f in evaluate(no_rank)['findings']}
    from packages.domain.generation import introduced_overflow
    moved = copy.deepcopy(warning)
    moved['detail']['supervisor_person_id'] = other
    assert introduced_overflow({'findings': [warning]}, {'findings': [moved]}) == [moved]
    h.publish(result['workspace'])


@pytest.mark.parametrize('normal_place', [True, False])
def test_allocation_uses_another_supervisors_normal_place_before_overflow(h, normal_place):
    from packages.domain.generation import generate
    trainee = h.seed['people'][0]
    first, second = sorted(h.seed['people'][1:])
    rank = h.seed['trainee_category']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank, 'starts_on': '2020-01-01'})
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0,
        'overflow_maximum': 1, 'overflow_category_ids': [rank]}, {'person_id': first})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': trainee})
    typ = h.configure('assignment_types', {'name': 'Prefer normal supervision place', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [rank],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    people = [{'person_id': pid, 'start': start, 'end': end, 'participation': 'supervisor'} for pid in ((first, second) if normal_place else (first,))]
    result = generate(h.snapshot(h.shift(h.workspace(), typ=typ['id'], people=people)['workspace']))
    assert result['status'] == 'candidate_feasible'
    assert result['additions'][0]['participant']['supervisor_person_id'] == (second if normal_place else first)
    assert any(f['code'] == 'CAPACITY_OVERFLOW' for f in result['validation']['findings']) is not normal_place


def test_supervision_extra_places_require_complete_capacity_configuration():
    from pydantic import ValidationError
    from packages.contracts.schemas import SupervisionLimit
    from uuid import uuid4
    rank = str(uuid4())
    for invalid in ({'overflow_maximum': 1}, {'overflow_category_ids': [rank]},
                    {'overflow_maximum': 1, 'overflow_category_ids': [rank, rank]},
                    {'maximum': 100, 'overflow_maximum': 1, 'overflow_category_ids': [rank]},
                    {'mode': 'allowed_ranks', 'maximum': None, 'trainee_category_ids': [rank],
                     'overflow_maximum': 1, 'overflow_category_ids': [rank]}):
        with pytest.raises(ValidationError):
            SupervisionLimit.model_validate({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 1, **invalid})
