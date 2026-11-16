"""Rule deep links share saved reads; department-scale checks retain exact source-row answers."""
import copy
import gc
import time
from datetime import date

import coverage
import pytest
from pydantic import ValidationError
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PolicyDefinition
from packages.domain.rule_map import reach
from packages.domain.time import local_midnight
from .test_rule_catalogue_api import approve, line, propose
from .test_rule_conditions import line as condition, tree
from .test_rule_map import U, base, big_department, rule


def workload_lines(people):
    return [line(id=U(f'rc12-read-line-{n}'), who=tree(condition('person_ids', people[n % len(people)])),
                 measure='duties', period={'kind': 'calendar_week'}, weekdays=[n // len(people)],
                 numbers={'max': {'value': 5 + n % 3, 'level': 'mandatory' if n % 2 else 'medium'}})
            for n in range(1000)]


@pytest.fixture(scope='module', params=[False, True])
def reach_details_measurement(request):
    loaded = request.param
    data = big_department()
    template = next(iter(data['people'].values()))
    for n in range(142, 150):
        person = U(f'p{n:03}')
        data['people'][person] = {**template, 'id': person}
        data['memberships'].append({**data['memberships'][0], 'person_id': person})
    people = sorted(data['people'])
    lines = workload_lines(people)
    policy = rule(data, 'rc12-workload-scale', {'kind': 'catalogue', 'catalogue_version': 2,
                                               'id': 'workload_limit', 'lines': lines})
    data['policies'] = [policy]
    if not loaded:
        data.pop('workspace')
        data.pop('shifts')
    a, b = (local_midnight(day, 'Asia/Riyadh') for day in (date(2026, 10, 1), date(2026, 10, 8)))
    started = time.process_time()
    wall_started = time.perf_counter()
    found = reach(data, [policy], a, b, details=True)
    elapsed = time.process_time() - started
    wall = time.perf_counter() - wall_started
    print(f'rc12 reach details: {elapsed:.3f} CPU s / {wall:.3f} wall s for 1000 lines / 150 people, loaded={loaded}')
    # Selecting one source line must retain its exact paths and counts from the full read.
    selected = reach(data, [policy], a, b, details=True, line_id=lines[751]['id'])
    return found, policy, people, selected, elapsed


def test_thousand_line_reach_details_budget_and_source_paths(reach_details_measurement):
    found, policy, people, selected, _ = reach_details_measurement
    rows = found['details'][policy['id']]['lines']
    assert len(rows) == 1000 and found['reach'][policy['id']] == 150
    assert all(row['people'][0]['person_id'] == people[row['line'] % 150]
               and row['people'][0]['matched_paths'] == ['who.0'] for row in rows)
    assert selected['details'][policy['id']]['lines'] == [rows[751]]


@pytest.mark.cpu_budget
def test_thousand_line_reach_details_budget_and_source_paths_cpu_budget(reach_details_measurement):
    *_, elapsed = reach_details_measurement
    budget = 1.5 * (6 if coverage.Coverage.current() else 1)
    assert elapsed < budget, (elapsed, budget)


@pytest.fixture(scope='module')
def thousand_line_check_measurement(scale_harness):
    h = scale_harness('h')
    with pytest.MonkeyPatch.context() as monkeypatch:
        return thousand_line_check(h, monkeypatch)


def thousand_line_check(h, monkeypatch):
    from apps.api import main as api
    from apps.api.services import rule_catalogue
    cpu = {}
    def measured(name, method):
        def run(*args, **kwargs):
            gc.collect()  # Exclude earlier fixtures' garbage: .20 s / 18,597 objects in gen-2; GC stays enabled.
            started = time.process_time()
            result = method(*args, **kwargs)
            cpu[name] = time.process_time() - started
            return result
        return run
    monkeypatch.setattr(rule_catalogue, 'check', measured('check', rule_catalogue.check))
    monkeypatch.setattr(api, 'policy_reach', measured('reach', api.policy_reach))
    people = [U(f'rc12-check-person-{n}') for n in range(150)]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': pid, 'organisation_id': h.seed['organisation_id'],
                                      'display_name': f'Synthetic person {n}'} for n, pid in enumerate(people)])
        db.execute(insert(m.Membership), [{'person_id': pid, 'department_id': h.seed['department_id'],
                                          'category_id': h.seed['category'], 'starts_on': date(2020, 1, 1)} for pid in people])
    client = h.client('coordinator')
    started = time.process_time()
    wall_started = time.perf_counter()
    response = client.post('/api/rules/catalogue/workload_limit/check', json={'catalogue_version': 2,
                                                                           'lines': workload_lines(people)})
    elapsed = time.process_time() - started
    wall = time.perf_counter() - wall_started
    result = response.json()
    print(f'rc12 catalogue check: {cpu["check"]:.3f} core CPU s / {elapsed:.3f} endpoint CPU s / {wall:.3f} wall s for 1000 lines / 150 people')
    with transaction(h.app.state.engine, write=True) as db:
        definition = PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'catalogue_version': 2,
            'id': 'workload_limit', 'lines': workload_lines(people)}}).model_dump(mode='json')
        policy = m.Policy(department_id=h.seed['department_id'], name='Synthetic scale rule', kind='catalogue',
                          status='active', owner='Synthetic', source='Reach budget regression', starts_on=date(2020, 1, 1),
                          created_by=h.seed['identities']['coordinator'], definition=definition)
        db.add(policy)
        db.flush()
        policy_id = policy.id
    started, wall_started = time.process_time(), time.perf_counter()
    found = h.get('coordinator', f'/api/policies/reach?details=true&policy_id={policy_id}')
    elapsed, wall = time.process_time() - started, time.perf_counter() - wall_started
    print(f'rc12 saved reach API: {cpu["reach"]:.3f} core CPU s / {elapsed:.3f} endpoint CPU s / {wall:.3f} wall s for 1000 lines / 150 people')
    return response, result, found, policy_id, cpu


def test_thousand_line_check_budget(thousand_line_check_measurement):
    response, result, found, policy_id, _ = thousand_line_check_measurement
    assert response.status_code == 200, response.text
    assert result['issues'] == result['duplicates'] == result['warnings'] == []
    assert len(result['reach_details']['lines']) == len(result['line_specificity']) == 1000
    assert found['reach'] == {policy_id: 150} and len(found['details'][policy_id]['lines']) == 1000


@pytest.mark.cpu_budget
def test_thousand_line_check_budget_cpu_budget(thousand_line_check_measurement):
    *_, cpu = thousand_line_check_measurement
    budget = 1.5 * (6 if coverage.Coverage.current() else 1)
    assert cpu['check'] < budget, (cpu['check'], budget)
    assert cpu['reach'] < budget, (cpu['reach'], budget)


def test_repeated_primitive_validation_preserves_numeric_boundaries():
    from packages.contracts.schemas import _validate_primitive_value
    for value in (0, 1, 500, 1):
        assert _validate_primitive_value('max_duties', 'maximum', value) == value
    for value in (-1, 501, 1.5, float('inf'), float('nan')):
        with pytest.raises(ValidationError):
            _validate_primitive_value('max_duties', 'maximum', value)


def test_check_keeps_tag_expansion_when_raw_lines_have_distinct_who(h):
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        tag = m.Tag(department_id=h.seed['department_id'], name='Synthetic grouped staff', labels=['people'])
        db.add(tag)
        db.flush()
        db.add(m.TagMember(department_id=h.seed['department_id'], tag_id=tag.id, kind='people', member_id=person))
        tag_id = tag.id
    lines = [line(who=tree(condition('person_ids', *ids)), numbers={'max': {'value': 6}})
             for ids in ([tag_id], [tag_id, person])]
    found = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': lines}).json()
    assert found['duplicates'] == found['warnings'] == found['issues'] == []
    assert found['line_specificity'] == {source['id']: 2 for source in lines}


@pytest.mark.parametrize('role,editable', [('admin', True), ('coordinator', True), ('approver', True),
                                         ('programme_lead', False), ('auditor', False)])
def test_combined_deep_link_matches_saved_catalogue_check_and_reach(h, role, editable):
    if role == 'programme_lead':
        from .governance_support import identity
        identity(h, role, [role])
    lines = [line(who=tree(condition('person_ids', person)), numbers={'max': {'value': n + 3}})
             for n, person in enumerate(h.seed['people'])]
    policy = approve(h, propose(h, 'duties_per_week', lines)['record'])
    endpoint = f"/api/rules/catalogue/duties_per_week/detail?day=2026-10-04&line_id={lines[1]['id']}"
    result = h.get(role, endpoint)
    catalogue = h.get(role, '/api/rules/catalogue?day=2026-10-04')
    assert result['entry'] == next(entry for entry in catalogue['entries'] if entry['id'] == 'duties_per_week')
    saved = h.get(role, f"/api/policies/reach?details=true&day=2026-10-04&policy_id={policy['id']}&line_id={lines[1]['id']}")
    assert {'day': result['day'], **result['reach']} == saved
    if editable:
        checked = h.client(role).post('/api/rules/catalogue/duties_per_week/check', json={'lines': lines}).json()
        assert result['check'] == {key: value for key, value in checked.items() if key != 'reach_details'}
    else:
        assert result['check'] is None
    h.get(role, endpoint.replace(lines[1]['id'], U('missing-line')), status=404)
    h.get(role, endpoint + '&policy_id=' + U('foreign-policy'), status=404)
    h.get('staff', endpoint, status=403)
    h.get('credentialer', endpoint, status=403)


def test_combined_read_keeps_proposed_lines_and_empty_entries(h):
    empty = h.get('auditor', '/api/rules/catalogue/workload_limit/detail?version=2')
    assert empty['entry']['rule'] is None and empty['check'] is None and empty['reach'] is None
    missing = '/api/rules/catalogue/workload_limit/detail?version=2&line_id=' + U('missing-line')
    h.get('auditor', missing, status=404)
    h.get('auditor', '/api/rules/catalogue/not-a-rule/detail', status=404)
    source = line(numbers={'max': {'value': 5}}, measure='duties', period={'kind': 'calendar_week'})
    waiting = propose(h, 'workload_limit', [source], catalogue_version=2)['record']
    selected = h.get('approver', f"/api/rules/catalogue/workload_limit/detail?version=2&policy_id={waiting['id']}&line_id={source['id']}")
    assert selected['entry']['proposed']['id'] == waiting['id']
    assert selected['check']['issues'] == []
    assert selected['reach']['details'][waiting['id']]['lines'][0]['line_id'] == source['id']


def test_combined_read_retains_rota_period_counts(h):
    workspace, _ = base(h)
    source = line(numbers={'max': {'value': 6}})
    policy = approve(h, propose(h, 'duties_per_week', [source])['record'])
    # TEST-STALE: the workload rule is non-safety and was approved after the draft baseline; refresh per 425d832da/6ef713812.
    workspace = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
    query = f"day=2026-10-04&workspace_id={workspace['id']}&starts_on=2026-10-04&ends_on=2026-10-11&line_id={source['id']}"
    result = h.get('auditor', '/api/rules/catalogue/duties_per_week/detail?' + query)
    saved = h.get('auditor', f"/api/policies/reach?details=true&policy_id={policy['id']}&" + query)
    assert {'day': result['day'], **result['reach']} == saved
    counts = result['reach']['details'][policy['id']]['lines'][0]['people'][0]['counts']
    assert counts and counts[0]['actual'] is not None
    h.get('auditor', '/api/rules/catalogue/duties_per_week/detail?' + query.replace('ends_on=2026-10-11', 'ends_on=2026-11-11'), status=422)


def test_session_selection_cache_preserves_distinct_source_where_paths(h):
    _, data = base(h)
    sources = [line(who=tree(condition('person_ids', person)),
                    where=tree(condition('service_ids', h.seed['services'][0]),
                               condition('service_ids', h.seed['services'][1]), match='any'),
                    numbers={'max': {'value': 6}}) for person in h.seed['people']]
    policy = rule(data, 'cache-source-paths', {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': sources})
    data['policies'] = [policy]
    a, b = (local_midnight(day, 'Asia/Riyadh') for day in (date(2026, 10, 4), date(2026, 10, 11)))
    combined = reach(data, [policy], a, b, details=True)['details'][policy['id']]['lines']
    for source, row in zip(sources, combined):
        alone = copy.deepcopy(policy)
        alone['definition']['rule']['lines'] = [source]
        [separate] = reach({**data, 'policies': [alone]}, [alone], a, b, details=True)['details'][policy['id']]['lines']
        assert {key: value for key, value in row.items() if key != 'line'} == {key: value for key, value in separate.items() if key != 'line'}


def test_reach_record_labels_keep_each_lines_field_and_branch_identity(h):
    _, data = base(h)
    clinic, place = h.seed['services'][0], h.seed['locations'][0]
    filters = [condition('service_ids', clinic), condition('location_ids', place)]
    sources = [line(who=tree(condition('person_ids', person)), where=tree(*where),
                    numbers={'max': {'value': 6}})
               for person, where in zip(h.seed['people'], (filters, list(reversed(filters))))]
    policy = rule(data, 'source-record-paths', {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': sources})
    data['policies'] = [policy]
    a, b = (local_midnight(day, 'Asia/Riyadh') for day in (date(2026, 10, 4), date(2026, 10, 11)))
    rows = reach(data, [policy], a, b, details=True)['details'][policy['id']]['lines']
    assert len(rows) == 2
    for index, row in enumerate(rows):
        assert row['clinics'] == [{'service_id': clinic, 'matched_paths': [f'where.{index}'], 'count': 'together'}]
        assert row['places'] == [{'location_id': place, 'matched_paths': [f'where.{1 - index}'], 'count': 'together'}]
