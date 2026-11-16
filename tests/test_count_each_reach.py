"""Reach keeps source branches, exact counting windows and the evaluator's bucket identity."""
import copy
import time
from datetime import date
import pytest

from packages.domain.rule_map import reach, rule_map
from packages.domain.time import local_midnight
from .test_rule_conditions import line, tree
from .test_rule_map import U, base, big_department, rule
from .test_rule_templates import activate, propose


def bounds(data):
    return tuple(local_midnight(date.fromisoformat(data['workspace'][key]), data['organisation']['timezone'])
                 for key in ('starts_on', 'ends_on'))


def test_reach_counts_shared_duty_once_in_each_branch_and_omits_excluded_people(h):
    workspace, data = base(h)
    person, excluded, _ = h.seed['people']
    services = h.seed['services']
    sample = data['shifts'][0]
    data['shifts'] = []
    for index, clinics in enumerate([[services[0]]] * 3 + [[services[1]]] * 3 + [services]):
        shift = copy.deepcopy(sample)
        shift.update(id=f'duty-{index}', service_ids=list(clinics))
        shift['participants'][0]['id'] = f'assignment-{index}'
        data['shifts'].append(shift)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    branch = tree(line('service_ids', services[0]), line('service_ids', services[1]), match='any')
    scope = {'conditions': tree(line('person_ids', excluded, op='none'), branch)}
    together = rule(data, 'together', {'kind': 'max_duties', 'maximum': 6}, scope)
    each = copy.deepcopy(together)
    each.update(id=U('each'), name='each')
    each['definition']['scope']['conditions']['conditions'][1]['count'] = 'each'
    data['policies'] = [together, each]
    result = reach(data, data['policies'], *bounds(data), details=True)
    assert result['reach'] == {together['id']: 2, each['id']: 2}
    combined = result['details'][together['id']]['lines']
    split = result['details'][each['id']]['lines']
    assert len(combined) == 1 and len(split) == 2
    actual = lambda rows: [max(c['actual'] for p in row['people'] if p['person_id'] == person for c in p['counts']) for row in rows]
    assert actual(combined) == [7] and actual(split) == [4, 4]
    assert all(excluded not in {p['person_id'] for p in row['people']} for row in combined + split)
    assert {clinic['count'] for row in split for clinic in row['clinics']} == {'each'}
    assert all(p['matched_paths'] for row in split for p in row['people'])
    assert reach(data, data['policies'], *bounds(data), details=True) == result
    # The map still reaches the actual shared session once, irrespective of counting fan-out.
    assert all(row['sessions'] == list(range(7)) for row in rule_map(data)['rules'])


def test_reach_staffing_reports_the_zero_rank_bucket(h):
    _, data = base(h)
    rank = h.seed['category']
    missing = U('missing-rank')
    data['categories'][missing] = {'id': missing, 'active': True}
    policy = rule(data, 'ranks', {'kind': 'minimum_cover', 'minimum': 1},
                  {'conditions': tree({**line('category_ids', rank, missing), 'count': 'each'})})
    data['policies'] = [policy]
    found = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    assert len(found) == 2
    by_value = {row['bucket'][0][1]: row for row in found}
    assert by_value[missing]['people'] == []
    assert by_value[missing]['sessions'] and all(c['actual'] == 0 and c['limit'] == 1 for c in by_value[missing]['sessions'])
    assert any(c['actual'] > 0 for c in by_value[rank]['sessions'])


@pytest.mark.parametrize('kind', ['minimum_cover', 'maximum_participants'])
def test_reach_staffing_shares_validator_presence_triggers_and_overflow(h, kind):
    from packages.domain.validation import evaluate
    _, data = base(h)
    trigger, second, absent = h.seed['people']
    first, peer = data['shifts'][:2]
    peer.update(start=first['start'], end=first['end'], service_ids=first['service_ids'])
    template = first['participants'][0]
    for shift, people in ((first, [trigger, second]), (peer, [second, absent])):
        shift['participants'] = [dict(template, id=f"{shift['id']}-{pid}", person_id=pid) for pid in people]
    data['shifts'], data['focus_ids'] = [first, peer], [first['id'], peer['id']]
    data['availability'] = [{'id': 'peer-absence', 'person_id': absent, 'kind': 'recorded_leave',
                             'start': first['start'], 'end': first['end']}]
    data['eligibilities'] = [row for row in data['eligibilities'] if row['person_id'] != absent]
    staffing = ({'kind': kind, 'aggregation': 'service_group', 'minimum': 2, 'when_person_ids': [trigger]}
                if kind == 'minimum_cover' else {'kind': kind, 'aggregation': 'service', 'maximum': 1,
                    'overflow_maximum': 1, 'overflow_category_ids': [h.seed['category']]})
    policy = rule(data, 'shared-staffing-count', staffing, {'service_ids': first['service_ids']})
    data['policies'] = [policy]
    findings = [f for f in evaluate(data)['findings'] if f['rule_id'] == policy['id']]
    [line] = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    key, expected = ('available', 1) if kind == 'minimum_cover' else ('scheduled', 3)
    assert findings and all(f['detail'][key] == expected for f in findings)
    assert line['sessions'] and all(row['actual'] == expected for row in line['sessions'])
    if kind == 'maximum_participants':
        assert all(f['detail']['permitted_maximum'] == 2 for f in findings)
        assert all(row['permitted_maximum'] == 2 for row in line['sessions'])
    else:
        first['participants'] = [p for p in first['participants'] if p['person_id'] != trigger]
        assert not [f for f in evaluate(data)['findings'] if f['rule_id'] == policy['id']]
        [line] = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
        assert line['sessions'] == []


def test_reach_places_keep_the_listed_ancestor_and_clinic_count_mode(h):
    _, data = base(h)
    wing = U('count-wing')
    data['locations'][wing] = {'id': wing, 'parent_id': None}
    data['locations'][h.seed['locations'][0]]['parent_id'] = wing
    scope = {'conditions': tree({**line('location_ids', wing), 'count': 'each'}, line('service_ids', h.seed['services'][0]))}
    policy = rule(data, 'places', {'kind': 'max_duties', 'maximum': 6}, scope)
    data['policies'] = [policy]
    result = reach(data, [policy], *bounds(data), details=True)
    [row] = result['details'][policy['id']]['lines']
    assert row['places'] == [{'location_id': wing, 'matched_paths': ['0.0'], 'count': 'each'}]
    assert {c['count'] for c in row['clinics']} == {'together'}
    assert {c['service_id'] for c in row['clinics']} == {h.seed['services'][0]}


def test_person_branch_each_does_not_relabel_an_outside_clinic_filter(h):
    _, data = base(h)
    scope = {'conditions': tree({**tree(line('category_ids', h.seed['category']),
                                       line('category_ids', U('other-rank')), match='any'), 'count': 'each'},
                                line('service_ids', h.seed['services'][0]))}
    policy = rule(data, 'who-branches', {'kind': 'minimum_cover', 'minimum': 1}, scope)
    rows = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    assert len(rows) == 2
    assert all(c['count'] == 'together' for row in rows for c in row['clinics'])


def test_person_branch_each_keeps_implicit_clinics_together(h):
    _, data = base(h)
    scope = {'conditions': tree({**tree(line('category_ids', h.seed['category']),
                                       line('category_ids', U('other-rank')), match='any'), 'count': 'each'})}
    policy = rule(data, 'implicit-where', {'kind': 'minimum_cover', 'minimum': 1}, scope)
    for snapshot in (data, {key: value for key, value in data.items() if key not in ('workspace', 'shifts')}):
        rows = reach(snapshot, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
        assert len(rows) == 2
        assert all(c['count'] == 'together' for row in rows for c in row['clinics'])


def test_unloaded_reach_preserves_flat_clinic_place_and_duty_requirements(h):
    _, data = base(h)
    unloaded = {key: value for key, value in data.items() if key not in ('workspace', 'shifts')}
    clinic, place, typ = h.seed['services'][0], h.seed['locations'][0], h.seed['type']
    for field, value in (('service_id', clinic), ('service_ids', [clinic]), ('location_id', place)):
        policy = rule(data, 'flat-' + field, {'kind': 'max_duties', 'maximum': 6}, {field: value})
        [row] = reach(unloaded, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
        if field.startswith('service'):
            assert [c['service_id'] for c in row['clinics']] == [clinic]
        else:
            assert [p['location_id'] for p in row['places']] == [place]
    for flat, positive, value in (('service_id', 'service_ids', clinic), ('location_id', 'location_ids', place),
                                  ('assignment_type_id', 'assignment_type_ids', typ)):
        policy = rule(data, 'contradictory-flat-' + flat, {'kind': 'max_duties', 'maximum': 6},
                      {flat: value, 'conditions': tree(line(positive, value, op='none'),
                          {**(line('location_ids', place) if flat == 'service_id' else line('service_ids', clinic)), 'count': 'each'})})
        found = reach(unloaded, [policy], *bounds(data), details=True)
        assert found['details'].get(policy['id'], {'lines': []})['lines'] == []


def test_reach_records_exclude_cancelled_and_outside_period_sessions(h):
    _, data = base(h)
    outside = copy.deepcopy(data['shifts'][1])
    outside.update(id='outside', start='2026-11-05T08:00:00+03:00', end='2026-11-05T12:00:00+03:00')
    data['shifts'][1]['cancelled'] = True
    data['shifts'].append(outside)
    policy = rule(data, 'clinics', {'kind': 'max_duties', 'maximum': 6},
                  {'conditions': tree({**line('service_ids', *h.seed['services']), 'count': 'each'})})
    data['policies'] = [policy]
    found = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    rows = {row['bucket'][0][1]: row for row in found}
    assert rows[h.seed['services'][1]]['clinics'] == []


def test_reach_clinics_require_a_session_while_the_line_is_in_force(h):
    _, data = base(h)
    data['shifts'] = data['shifts'][:2]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    policy = rule(data, 'activated-on-monday', {'kind': 'max_duties', 'maximum': 6},
                  {'conditions': tree({**line('service_ids', *h.seed['services']), 'count': 'each'})})
    policy['starts_on'] = '2026-10-05'
    rows = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    clinics = {c['service_id'] for row in rows for c in row['clinics']}
    assert clinics == {h.seed['services'][1]}


def test_reach_membership_and_rule_dates_must_intersect(h):
    _, data = base(h)
    person = h.seed['people'][0]
    policy = rule(data, 'future-person-line', {'kind': 'max_duties', 'maximum': 6}, {'person_id': person})
    policy['starts_on'] = '2026-10-06'
    for member in data['memberships']:
        if member['person_id'] == person:
            member['ends_on'] = '2026-10-05'
    for snapshot in (data, {key: value for key, value in data.items() if key not in ('workspace', 'shifts')}):
        found = reach(snapshot, [policy], *bounds(data), details=True)
        assert found['reach'][policy['id']] == 0
        assert found['details'][policy['id']]['lines'][0]['people'] == []


def test_reach_includes_a_person_and_line_joining_during_the_period(h):
    _, data = base(h)
    person = h.seed['people'][0]
    for member in data['memberships']:
        if member['person_id'] == person:
            member['starts_on'] = '2026-10-06'
    shift = data['shifts'][0]
    shift.update(start='2026-10-07T08:00:00+03:00', end='2026-10-07T12:00:00+03:00')
    shift['participants'][0].update(start=shift['start'], end=shift['end'])
    policy = rule(data, 'joining-line', {'kind': 'max_duties', 'maximum': 6}, {'person_id': person})
    policy['starts_on'] = '2026-10-06'
    found = reach(data, [policy], *bounds(data), details=True)
    assert found['reach'][policy['id']] == 1
    [reached] = found['details'][policy['id']]['lines'][0]['people']
    assert reached['person_id'] == person and max(c['actual'] for c in reached['counts']) == 1


def test_reach_api_adds_workspace_counts_and_preserves_legacy_totals(h):
    person, excluded, _ = h.seed['people']
    policy = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 6},
                                {'conditions': tree(line('person_ids', excluded, op='none'),
                                                     {**line('service_ids', *h.seed['services']), 'count': 'each'})}))
    workspace, _ = base(h)
    endpoint = f"/api/policies/reach?day=2026-10-04&workspace_id={workspace['id']}&starts_on=2026-10-04&ends_on=2026-10-11"
    result = h.get('coordinator', endpoint)
    assert result['day'] == '2026-10-04' and result['workspace_id'] == workspace['id']
    assert (result['starts_on'], result['ends_on']) == ('2026-10-04', '2026-10-11')
    default = h.get('coordinator', f"/api/policies/reach?workspace_id={workspace['id']}")
    assert (default['starts_on'], default['ends_on']) == ('2026-10-04', '2026-10-11')
    assert result['reach'][policy['id']] == 2
    rows = result['details'][policy['id']]['lines']
    assert any(c['actual'] == 1 for row in rows for p in row['people'] if p['person_id'] == person for c in p['counts'])
    no_schedule = h.get('coordinator', '/api/policies/reach?day=2026-10-04&details=true')
    assert no_schedule['reach'][policy['id']] == 2
    assert all(c['actual'] is None for row in no_schedule['details'][policy['id']]['lines'] for p in row['people'] for c in p['counts'])
    for suffix in ('&starts_on=2026-10-04', '&starts_on=2026-10-05&ends_on=2026-10-04', '&starts_on=2026-09-01&ends_on=2026-10-05'):
        h.get('coordinator', f"/api/policies/reach?workspace_id={workspace['id']}{suffix}", status=422)
    h.get('staff', endpoint, status=403)
    h.get('credentialer', endpoint, status=403)
    h.get('credentialer', '/api/policies/reach?day=2026-10-04')


def test_unsaved_policy_preview_includes_matched_branches(h):
    person, excluded, _ = h.seed['people']
    response = h.client('coordinator').post('/api/policies/check', json={'definition': {
        'rule': {'kind': 'max_duties', 'maximum': 6}, 'scope': {'conditions': tree(line('person_ids', excluded, op='none'),
            {**line('service_ids', *h.seed['services']), 'count': 'each'})}}})
    assert response.status_code == 200
    found = response.json()
    assert found['compatible'] and found['people_now'] == 2
    rows = found['reach_details']['lines']
    assert len(rows) == 2 and all(excluded not in {p['person_id'] for p in row['people']} for row in rows)
    assert all(c['actual'] is None for row in rows for p in row['people'] for c in p['counts'])


def test_catalogue_preview_keeps_line_identity_and_count_modes(h):
    line_id = U('preview-count-line')
    response = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': [{
        'id': line_id, 'where': tree({**line('service_ids', *h.seed['services']), 'count': 'each'}),
        'numbers': {'max': {'value': 6}}}]})
    assert response.status_code == 200
    found = response.json()
    assert found['issues'] == []
    assert len(found['reach_details']['lines']) == 2
    assert {row['line_id'] for row in found['reach_details']['lines']} == {line_id}
    assert all(c['count'] == 'each' for row in found['reach_details']['lines'] for c in row['clinics'])


def test_reach_quota_counts_reuse_the_complete_report_for_each_bound(h):
    _, data = base(h)
    policy = rule(data, 'quota-reach', {'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                  'metric': 'duties', 'comparison': 'range', 'amount': 1, 'maximum_amount': 6, 'target_amount': 2},
                  {'person_id': h.seed['people'][0],
                   'conditions': tree({**line('service_ids', *h.seed['services']), 'count': 'each'})})
    data['policies'] = [policy]
    a, b = bounds(data)
    data['quota_context'] = {'start': a.isoformat(), 'end': b.isoformat(), 'complete': True, 'limit_exceeded': False}
    rows = reach(data, [policy], a, b, details=True)['details'][policy['id']]['lines']
    assert len(rows) == 6
    assert {c['comparison'] for row in rows for p in row['people'] for c in p['counts']} == {'minimum', 'maximum', 'target'}
    for row in rows:
        [person] = row['people']
        [count] = person['counts']
        assert count['actual'] == (1 if row['bucket'][0][1] == h.seed['services'][0] else 0)
        assert count['starts_on'] == '2026-10-04' and count['ends_on'] == '2026-10-11'


def test_impossible_each_branch_reaches_no_people_or_clinics(h):
    _, data = base(h)
    clinic = h.seed['services'][0]
    policy = rule(data, 'contradictory', {'kind': 'max_duties', 'maximum': 6},
                  {'conditions': tree({**line('service_ids', clinic), 'count': 'each'}, line('service_ids', clinic, op='none'))})
    for snapshot in (data, {key: value for key, value in data.items() if key not in ('workspace', 'shifts')}):
        found = reach(snapshot, [policy], *bounds(data), details=True)
        assert found['reach'][policy['id']] == 0
        assert found['details'].get(policy['id'], {'lines': []})['lines'] == []


@pytest.mark.parametrize('kind,days,expected', [
    ({'kind': 'max_consecutive_days', 'maximum': 2}, [4, 5, 6], [1, 2, 3]),
    ({'kind': 'max_consecutive_weekends', 'maximum': 2, 'weekend_start': 6, 'weekend_days': 1}, [4, 11, 18], [1, 2, 3]),
    ({'kind': 'run_length', 'minimum': 2, 'maximum': 4}, [4, 5, 8], [2, 2, 1, 1]),
    ({'kind': 'days_off_run', 'minimum': 2}, [4, 6], [1]),
])
def test_reach_numeric_runs_share_the_validator_and_shape_observations(h, kind, days, expected):
    _, data = base(h)
    person, sample = h.seed['people'][0], data['shifts'][0]
    data['workspace']['ends_on'] = '2026-10-25'
    shifts = []
    for index, day in enumerate(days):
        shift = copy.deepcopy(sample)
        shift.update(id=f'run-{index}', start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00')
        shift['participants'][0].update(id=f'person-{index}', start=shift['start'], end=shift['end'])
        shifts.append(shift)
    data['shifts'], data['focus_ids'] = shifts, [s['id'] for s in shifts]
    advisory = kind['kind'] in {'run_length', 'days_off_run'}
    policy = rule(data, 'numeric-runs', kind, {'person_id': person}, **({'severity': 'advisory'} if advisory else {}))
    data['policies'] = [policy]
    [row] = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    [reached] = row['people']
    assert [count['actual'] for count in reached['counts']] == expected
    assert all(count['limit'] in {2, 4} and count['metric'] in {'days', 'weekends'} for count in reached['counts'])


@pytest.mark.parametrize('travel', [False, True])
def test_reach_exposes_rest_and_travel_pair_gaps(h, travel):
    _, data = base(h)
    first, second = data['shifts'][:2]
    second.update(start='2026-10-04T18:00:00+03:00', end='2026-10-04T22:00:00+03:00')
    second['participants'][0].update(person_id=h.seed['people'][0], start=second['start'], end=second['end'])
    data['shifts'] = [first, second]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    kind = ({'kind': 'travel_buffer', 'minutes': 400, 'from_location_id': first['location_id'], 'to_location_id': second['location_id']}
            if travel else {'kind': 'min_rest', 'hours': 11})
    policy = rule(data, 'pair-gaps', kind, {'person_id': h.seed['people'][0]})
    data['policies'] = [policy]
    [row] = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    [reached] = row['people']
    [gap] = reached['counts']
    assert gap['actual'] == (360 if travel else 6) and gap['limit'] == (400 if travel else 11)
    assert gap['comparison'] == 'minimum'
    assert (gap['previous_shift_id'], gap['next_shift_id']) == (first['id'], second['id'])


@pytest.mark.parametrize('period', ['rota', 'month'])
def test_reach_fairness_target_reuses_goal_report_for_every_bucket(h, period):
    from packages.domain.conditions import active_rules, counting_views
    from packages.domain.fairness import goal_report
    _, data = base(h)
    person = h.seed['people'][0]
    policy = rule(data, 'goal-reach', {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 2, 'period': period},
                  {'person_id': person, 'conditions': tree({**line('service_ids', *h.seed['services']), 'count': 'each'})}, severity='advisory')
    data['policies'] = [policy]
    copies = counting_views(data, active_rules([policy]))
    expected = {copy['count_bucket']: goal_report(data, copy)['people'][0] for copy in copies}
    rows = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    assert len(rows) == 2
    for row in rows:
        [reached] = row['people']
        [count] = reached['counts']
        report = expected[tuple(tuple(atom) for atom in row['bucket'])]
        assert count['actual'] == report['actual'] and count['target'] == report['expected']
        assert count['planned'] == report['planned'] and count['comparison'] == 'target'
        assert (count['starts_on'], count['ends_on']) == (('2026-10-04', '2026-10-11') if period == 'rota' else ('2026-10-01', '2026-11-01'))


def test_reach_supervision_reports_the_authoritative_paired_capacity(h):
    trainee, supervisor = h.seed['people'][:2]
    rank = h.seed['trainee_category']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank, 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Reach capacity', 'starts_on': '2020-01-01', 'definition': {
        'supervision': {'trainee_category_ids': [rank], 'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 3}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[
        {'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': start, 'end': end, 'participation': 'supervisor'}])['workspace']
    data = h.snapshot(workspace)
    policy = rule(data, 'supervision-reach', {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0}, {'person_id': supervisor})
    data['policies'] = [policy]
    [row] = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    assert row['sessions'] and all(count['person_id'] == supervisor and count['actual'] == 1 and count['limit'] == 0 for count in row['sessions'])


@pytest.fixture(scope='module')
def department_counted_reach():
    data = big_department()
    template = next(iter(data['people'].values()))
    for index in range(142, 150):
        pid = U(f'p{index:03}')
        data['people'][pid] = {**template, 'id': pid}
        data['memberships'].append({**data['memberships'][0], 'person_id': pid})
    policies = []
    people = sorted(data['people'])
    services = sorted(data['services'])[:2]
    for index in range(1000):
        policies.append(rule(data, f'count-{index}', {'kind': 'max_duties', 'maximum': 6},
                             {'person_id': people[index % len(people)],
                              'conditions': tree({**line('service_ids', *services), 'count': 'each'})}))
    data['policies'] = policies
    import packages.domain.rule_map as reach_module
    original, checks = reach_module.scope_matches_throughout, [0]
    def counted(*args, **kwargs):
        if args[2] is None:  # The old scope_matches spy counted session filters, not person-scope checks.
            checks[0] += 1
        return original(*args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reach_module, 'scope_matches_throughout', counted)
        started = time.process_time()
        found = reach(data, policies, *bounds(data), details=True)
        elapsed = time.process_time() - started
    return found, checks[0], elapsed


def test_counted_reach_is_bounded_at_department_scale(department_counted_reach):
    found, checks, _ = department_counted_reach
    assert 0 < checks < 1000, checks  # Shared session signatures do not grow by named person.
    assert len(found['details']) == 1000
    assert sum(len(item['lines']) for item in found['details'].values()) == 2000
    assert all(len(row['people']) <= 1 for item in found['details'].values() for row in item['lines'])


@pytest.mark.cpu_budget
def test_counted_reach_is_bounded_at_department_scale_cpu_budget(department_counted_reach):
    import coverage
    _, _, elapsed = department_counted_reach
    budget = 30 if coverage.Coverage.current() else 6
    print(f'counted reach: {elapsed:.3f} CPU s for 1000 rules, 150 people')
    assert elapsed < budget, (elapsed, budget)
