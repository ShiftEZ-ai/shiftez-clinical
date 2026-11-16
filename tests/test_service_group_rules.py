import pytest
from packages.domain.validation import evaluate
from packages.domain.generation import generate


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_pooled_clinic_cover_counts_people_once_and_splits_peer_absence(request, database):
    import copy
    h = request.getfixturevalue(database)
    first, second = h.seed['people'][:2]
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
        {'service_ids': h.seed['services'][:2]})
    w = h.shift(h.workspace(), people=[first])['workspace']
    w = h.shift(w, start='2026-10-04T09:00:00+03:00', end='2026-10-04T11:00:00+03:00', people=[second], service=1)['workspace']
    findings = [f for f in evaluate(h.snapshot(w))['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']
    assert len(findings) == 2 and all(f['detail']['available'] == 1 for f in findings)
    h.command('coordinator', 'availability.record', {'person_id': second, 'kind': 'recorded_leave',
        'start': '2026-10-04T09:30:00+03:00', 'end': '2026-10-04T10:00:00+03:00', 'private_reason': 'Synthetic peer absence'})
    data = h.snapshot(w)
    gaps = [f for f in evaluate(data)['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']
    assert len(gaps) == 3
    middle = next(f for f in gaps if f['start'].startswith('2026-10-04T06:30'))
    assert middle['end'].startswith('2026-10-04T07:00') and middle['detail']['available'] == 1
    assert len(middle['detail']['affected_shift_ids']) == 2
    ineligible = copy.deepcopy(data)
    ineligible['availability'] = []
    ineligible['eligibilities'] = [r for r in ineligible['eligibilities'] if r['person_id'] != second]
    assert all(f['detail']['available'] == 1 for f in evaluate(ineligible)['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP')
    # A duplicated employee across services is never two people (and overlap still blocks).
    data['availability'] = []
    peer = next(s for s in data['shifts'] if s['service_ids'] == [h.seed['services'][1]])
    peer['participants'][0]['person_id'] = first
    assert all(f['detail']['available'] == 1 for f in evaluate(data)['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP')
    # Allocation may fill either clinic; the pooled finding identifies all affected sessions.
    template = data['shifts'][0]
    peer['start'], peer['end'] = template['start'], template['end']
    peer['participants'] = []
    data['people'] = {pid:p for pid,p in data['people'].items() if pid in {first, second}}
    before = copy.deepcopy(data)
    generated = generate(data)
    assert generated['status'] == 'candidate_feasible', generated
    assert len(generated['additions']) == 1 and generated['additions'][0]['participant']['person_id'] == second
    assert data == before
    # A later evaluation must not reuse the preceding rota's peer set.
    data['shifts'].remove(peer)
    gaps = [f for f in evaluate(data)['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']
    assert gaps and all(len(f['detail']['affected_shift_ids']) == 1 for f in gaps)


def test_clinic_group_shares_daily_and_quota_limits_without_double_counting(h):
    from .test_block_quotas import quota
    services = h.seed['services'][:2]
    typ = h.configure('assignment_types', {'name': 'Reviewed shared clinic duty', 'starts_on': '2020-01-01',
        'definition': {'allow_shared_services': True, 'shared_duty_reason': 'Synthetic shared-duty scope check',
            'requirements': [{'capabilities': [{'capability_id': h.seed['capability'], 'minimum': 1}]}]}})['record']
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, {'service_ids': services})
    quota(h, amount=1, scope={'person_id': h.seed['people'][0], 'service_ids': services})
    first = h.shift(h.workspace(), typ=typ['id'])['workspace']
    data = h.snapshot(first)
    data['shifts'][0]['service_ids'] = services
    assert evaluate(data)['valid'], evaluate(data)['findings']  # Shared duty counts once.
    second = h.shift(first, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', service=1, typ=typ['id'])
    codes = {f['code'] for f in second['validation']['findings']}
    assert 'MAX_DUTIES' in codes and any(c.startswith('QUOTA_') for c in codes)
    h.lifecycle(second['workspace'], 'submit', status=422)
    data = h.snapshot(second['workspace'])
    second_shift = next(s for s in data['shifts'] if s['service_ids'] == [services[1]])
    second_shift['participants'] = []
    candidate = generate(data)
    assert candidate['status'] == 'candidate_feasible'
    assert candidate['additions'] and all(a['participant']['person_id'] != h.seed['people'][0] for a in candidate['additions'])
    # Legacy single-service scopes remain accepted; ambiguous and unknown groups do not.
    h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'day'}, {'service_id': services[0]})
    payload = {'name': 'Invalid group', 'owner': 'Synthetic owner', 'source': 'Synthetic scope test',
        'starts_on': '2026-01-01', 'definition': {'scope': {'service_id': services[0], 'service_ids': services},
        'rule': {'kind': 'max_duties', 'maximum': 1}}}
    h.command('coordinator', 'policy.create', payload, status=422)


def test_overtime_permit_waits_for_its_mandatory_caps_and_stops_when_retired(h):
    from packages.domain.generation import allocation_settings
    cap = h.command('coordinator', 'policy.create', {'name': 'Monthly overtime cap', 'owner': 'Synthetic owner',
        'source': 'Synthetic offer with independent review', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'definition': {'scope': {'person_id': h.seed['people'][0]}, 'rule': {'kind': 'block_quota',
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
            'comparison': 'maximum', 'amount': 2, 'overtime': 'only'}}})['record']
    h.policy({'kind': 'allocation_overtime', 'required_policy_ids': [cap['id']]}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    assert allocation_settings(data, data['shifts'][0], h.seed['people'][0])[1] is False
    cap = h.command('approver', 'policy.activate', {'reason': 'Independent cap review'}, cap['id'], cap['revision'])['record']
    # TEST-STALE: the mandatory cap is non-safety; refresh this draft per 425d832da/6ef713812.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    data = h.snapshot(w)
    assert allocation_settings(data, data['shifts'][0], h.seed['people'][0])[1] is True
    h.command('approver', 'policy.retire', {'reason': 'Withdraw synthetic cap'}, cap['id'], cap['revision'])
    # TEST-STALE: retirement is a reviewed non-safety source change; the freeze matrix keeps the older, stricter cap until refresh (425d832da/6ef713812), the safe direction.
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    data = h.snapshot(w)
    assert allocation_settings(data, data['shifts'][0], h.seed['people'][0])[1] is False


def test_rank_group_capacity_and_quota_do_not_double_count_memberships(h):
    from .test_block_quotas import quota
    categories = [h.configure('categories', {'name': name, 'starts_on': '2020-01-01'})['record']['id']
                  for name in ('Synthetic residents', 'Synthetic rotators')]
    for person, category in [(0, 0), (0, 1), (1, 1)]:
        h.command('coordinator', 'membership.create', {'person_id': h.seed['people'][person],
            'category_id': categories[category], 'starts_on': '2026-10-01'})
    scope = {'category_ids': categories, 'service_ids': [h.seed['services'][0]], 'weekdays': [6]}
    h.policy({'kind': 'maximum_participants', 'maximum': 1}, scope)
    h.policy({'kind': 'minimum_cover', 'minimum': 2}, scope)
    quota(h, metric='duties', amount=10, scope={'category_ids': categories})
    result = h.shift(h.workspace(), people=h.seed['people'][:2])
    limits = [f for f in result['validation']['findings'] if f['code'] == 'SCOPED_PARTICIPANT_LIMIT']
    assert limits and all(f['detail']['scheduled'] == 2 for f in limits)
    assert 'SCOPED_COVERAGE_GAP' not in {f['code'] for f in result['validation']['findings']}
    rows = result['validation']['quota_report']['rows']
    assert len(rows) == 2 and all(r['actual'] == 1 for r in rows)
    h.lifecycle(result['workspace'], 'submit', status=422)
    data = h.snapshot(result['workspace'])
    for membership in data['memberships']:
        if membership['category_id'] in categories and membership['person_id'] == h.seed['people'][0]:
            membership['ends_on'] = '2026-10-04'
    assert 'SCOPED_PARTICIPANT_LIMIT' not in {f['code'] for f in evaluate(data)['findings']}
    assert next(r for r in evaluate(data)['quota_report']['rows'] if r['person_id'] == h.seed['people'][0])['actual'] == 0
    # Legacy single rank remains supported; ambiguous selectors and unknown IDs fail closed.
    import uuid
    payload = {'name': 'Invalid rank group', 'owner': 'Synthetic owner', 'source': 'Synthetic scope test',
        'starts_on': '2026-01-01', 'definition': {'scope': {'category_ids': categories, 'category_id': categories[0]},
        'rule': {'kind': 'maximum_participants', 'maximum': 1}}}
    h.command('coordinator', 'policy.create', payload, status=422)
    payload['definition']['scope'] = {'category_ids': [str(uuid.uuid4())]}
    h.command('coordinator', 'policy.create', payload, status=404)


def test_clinic_capacity_counts_published_peer_assignments_at_exact_overlap(h):
    from packages.domain.time import aware
    category = h.configure('categories', {'name': 'Synthetic capacity group', 'starts_on': '2020-01-01'})['record']['id']
    for person in h.seed['people'][:2]:
        h.command('coordinator', 'membership.create', {'person_id': person, 'category_id': category, 'starts_on': '2026-10-01'})
    h.policy({'kind': 'maximum_participants', 'maximum': 1, 'aggregation': 'service'},
             {'category_ids': [category], 'service_ids': [h.seed['services'][0]]})
    h.publish(h.shift(h.workspace())['workspace'])
    result = h.shift(h.workspace(), start='2026-10-04T10:00:00+03:00', end='2026-10-04T14:00:00+03:00',
                     people=[h.seed['people'][1]], location=1)
    limits = [f for f in result['validation']['findings'] if f['code'] == 'SCOPED_PARTICIPANT_LIMIT']
    assert limits and all(f['detail']['scheduled'] == 2 for f in limits)
    assert all(aware(f['start']) == aware('2026-10-04T10:00:00+03:00') and
               aware(f['end']) == aware('2026-10-04T12:00:00+03:00') for f in limits)
    h.lifecycle(result['workspace'], 'submit', status=422)
    data = h.snapshot(result['workspace'])
    focal = next(s for s in data['shifts'] if s['id'] in data['focus_ids'])
    focal['participants'] = []
    candidate = generate(data)
    assert candidate['additions'] and candidate['status'] == 'candidate_feasible'
    assert all(a['participant']['person_id'] not in h.seed['people'][:2] for a in candidate['additions'])
    # Another clinic and an end-exclusive boundary cannot consume this clinic's seats.
    later = h.shift(h.workspace(), start='2026-10-04T12:00:00+03:00', end='2026-10-04T16:00:00+03:00',
                    people=[h.seed['people'][1]], location=1)
    assert 'SCOPED_PARTICIPANT_LIMIT' not in {f['code'] for f in later['validation']['findings']}
    other = h.shift(h.workspace(), people=[h.seed['people'][1]], location=1, service=1)
    assert 'SCOPED_PARTICIPANT_LIMIT' not in {f['code'] for f in other['validation']['findings']}


@pytest.mark.postgres
def test_clinic_capacity_postgres(pg):
    test_clinic_capacity_counts_published_peer_assignments_at_exact_overlap(pg)


@pytest.mark.parametrize('aggregation', ['assignment', 'service'])
def test_capacity_overflow_requires_selected_rank_and_never_waives_rest(h, aggregation):
    rank = h.configure('categories', {'name': 'Extra-place eligibility', 'starts_on': '2020-01-01'})['record']['id']
    h.command('coordinator', 'membership.create', {'person_id': h.seed['people'][0], 'category_id': rank, 'starts_on': '2020-01-01'})
    h.policy({'kind': 'maximum_participants', 'maximum': 1, 'aggregation': aggregation,
              'overflow_maximum': 1, 'overflow_category_ids': [rank]}, {'service_id': h.seed['services'][0]})
    valid = h.shift(h.workspace(), people=h.seed['people'][:2])
    assert valid['validation']['valid']
    warnings = [f for f in valid['validation']['findings'] if f['code'] == 'CAPACITY_OVERFLOW']
    assert warnings and all(f['severity'] == 'warning' and f['detail']['permitted_maximum'] == 2 for f in warnings)
    for people in (h.seed['people'][1:3], h.seed['people'][:3]):
        invalid = h.shift(h.workspace(), people=people)
        assert 'SCOPED_PARTICIPANT_LIMIT' in {f['code'] for f in invalid['validation']['findings']}
        h.lifecycle(invalid['workspace'], 'submit', status=422)
    h.publish(valid['workspace'])
    h.policy({'kind': 'min_rest', 'hours': 12})
    result = h.shift(h.workspace(), start='2026-10-04T16:00:00+03:00', end='2026-10-04T20:00:00+03:00', people=h.seed['people'][:2])
    assert 'MIN_REST' in {f['code'] for f in result['validation']['findings']}
    h.lifecycle(result['workspace'], 'submit', status=422)


@pytest.mark.parametrize('normal_alternative', [True, False])
def test_allocation_uses_normal_places_before_explicit_overflow(h, normal_alternative):
    h.policy({'kind': 'maximum_participants', 'maximum': 1, 'overflow_maximum': 1,
              'overflow_category_ids': [h.seed['category']]})
    for person in h.seed['people'][:2]:
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
            'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': person})
    w = h.shift(h.workspace(), people=[])['workspace']
    if normal_alternative:
        w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    result = generate(h.snapshot(w))
    assert result['status'] == 'candidate_feasible'
    assert len(result['additions']) == 2
    overflow = [f for f in result['validation']['findings'] if f['code'] == 'CAPACITY_OVERFLOW']
    assert bool(overflow) is not normal_alternative
    assert len({a['shift_id'] for a in result['additions']}) == (2 if normal_alternative else 1)


@pytest.mark.postgres
def test_capacity_overflow_postgres(pg):
    test_capacity_overflow_requires_selected_rank_and_never_waives_rest(pg, 'service')


def test_allocation_does_not_spend_existing_resident_overflow_on_another_rank(h):
    resident, other = h.seed['people'][:2]
    rank = h.configure('categories', {'name': 'Permitted overflow rank', 'starts_on': '2020-01-01'})['record']['id']
    h.command('coordinator', 'membership.create', {'person_id': resident, 'category_id': rank, 'starts_on': '2020-01-01'})
    h.policy({'kind': 'maximum_participants', 'maximum': 1, 'overflow_maximum': 1, 'overflow_category_ids': [rank]})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': other})
    w = h.shift(h.workspace(), people=[resident])['workspace']
    result = generate(h.snapshot(w))
    assert result['status'] == 'candidate_with_gaps' and not result['additions']
    assert any(f['code'] == 'QUOTA_BELOW_MINIMUM' for f in result['unmet_requirements'])


def test_conditional_cover_requires_other_eligible_people_and_guides_allocation(h):
    from packages.domain.validation import evaluate
    trigger, first, second = h.seed['people']
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'when_person_ids': [trigger]},
             {'category_id': h.seed['category'], 'service_id': h.seed['services'][0]})
    without = h.shift(h.workspace(), people=[first])
    assert without['validation']['valid']
    insufficient = h.shift(h.workspace(), people=[trigger, first])
    gap = next(f for f in insufficient['validation']['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP')
    assert gap['detail'] == {'required': 2, 'available': 1, 'rule_severity': 'mandatory', 'rule_strength': 'medium', 'rule_tier': 2}
    h.lifecycle(insufficient['workspace'], 'submit', status=422)
    locked = h.shift(h.workspace(), people=[trigger])
    result = generate(h.snapshot(locked['workspace']))
    assert result['status'] == 'candidate_feasible'
    assert {r['participant']['person_id'] for r in result['additions']} == {first, second}
    valid = h.shift(h.workspace(), people=[trigger, first, second])
    assert valid['validation']['valid']
    data = h.snapshot(valid['workspace'])
    data['eligibilities'] = [e for e in data['eligibilities'] if e['person_id'] != second]
    assert 'SCOPED_COVERAGE_GAP' in {f['code'] for f in evaluate(data)['findings']}
    h.publish(valid['workspace'])


@pytest.mark.postgres
def test_conditional_cover_postgres(pg):
    test_conditional_cover_requires_other_eligible_people_and_guides_allocation(pg)
