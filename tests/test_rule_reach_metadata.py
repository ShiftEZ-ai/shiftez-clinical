"""Source-row reach details and specificity reuse the domain evaluator and stay within department permissions."""
import copy
import gc
import time
from datetime import date

import coverage
import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.conditions import with_tags
from packages.domain.rule_map import reach, rule_map
from packages.domain.time import local_midnight
from .governance_support import identity
from .test_count_each_reach import bounds
from .test_rule_catalogue_api import approve, entry, line as catalogue_line, propose
from .test_rule_conditions import line, tree
from .test_rule_map import U, base, big_department, rule


def test_source_paths_include_flattened_who_where_and_nested_filter_rows(h):
    _, data = base(h)
    person = h.seed['people'][0]
    source = catalogue_line(who=tree(line('person_ids', person)), where=tree(
        line('assignment_type_ids', h.seed['type']),
        tree(line('service_ids', h.seed['services'][0]), line('service_ids', h.seed['services'][1]), match='any')),
        numbers={'max': {'value': 6}})
    policy = rule(data, 'source-paths', {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [source]})
    [row] = reach(data, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
    assert row['specificity'] == 3
    assert row['people'][0]['matched_paths'] == ['where.0', 'where.1.0', 'where.1.1', 'who.0']
    assert {clinic['service_id']: clinic['matched_paths'] for clinic in row['clinics']} == {
        h.seed['services'][0]: ['where.1.0'], h.seed['services'][1]: ['where.1.1']}


def test_count_branches_keep_source_identity_when_who_and_where_repeat_values(h):
    _, data = base(h)
    who = {**tree(line('category_ids', h.seed['category']), line('category_ids', h.seed['category']), match='any'), 'count': 'each'}
    where = {**tree(line('service_ids', h.seed['services'][0]), line('service_ids', h.seed['services'][0]), match='any'), 'count': 'each'}
    source = catalogue_line(who=who, where=where, numbers={'min': {'value': 1}})
    policy = rule(data, 'mixed-count-paths', {'kind': 'catalogue', 'id': 'people_per_session', 'lines': [source]})
    for snapshot in (data, {key: value for key, value in data.items() if key not in ('workspace', 'shifts')}):
        rows = reach(snapshot, [policy], *bounds(data), details=True)['details'][policy['id']]['lines']
        assert len(rows) == 4
        paths = {tuple(person['matched_paths']) for row in rows for person in row['people']}
        assert paths == {(f'where.{where_row}', f'who.{who_row}') for who_row in range(2) for where_row in range(2)}
        assert {tuple(clinic['matched_paths']) for row in rows for clinic in row['clinics']} == {('where.0',), ('where.1',)}
        assert all(clinic['count'] == 'each' for row in rows for clinic in row['clinics'])


def test_filtered_reach_keeps_other_rules_in_the_override_graph(h):
    _, data = base(h)
    person = h.seed['people'][0]
    broad = rule(data, 'broad-reach', {'kind': 'max_hours', 'hours': 48})
    narrow = rule(data, 'narrow-reach', {'kind': 'max_hours', 'hours': 40}, {'person_id': person}, overrides=[broad['id']])
    data['policies'] = [broad, narrow]
    entire = reach(data, data['policies'], *bounds(data), details=True)
    selected = reach(data, data['policies'], *bounds(data), details=True, policy_id=broad['id'])
    assert selected == {'reach': {broad['id']: 2}, 'details': {broad['id']: entire['details'][broad['id']]}}


@pytest.mark.parametrize('role', ['coordinator', 'programme_lead', 'auditor'])
def test_api_filters_details_to_one_line_and_returns_source_paths(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    person = h.seed['people'][0]
    first = catalogue_line(who=tree(line('person_ids', person)), where=tree(line('service_ids', h.seed['services'][0])),
                           numbers={'max': {'value': 6}})
    second = catalogue_line(who=tree(line('person_ids', person)), where=tree(line('service_ids', h.seed['services'][1])),
                            numbers={'max': {'value': 5}})
    policy = approve(h, propose(h, 'duties_per_week', [first, second])['record'])
    endpoint = f"/api/policies/reach?day=2026-10-04&details=true&policy_id={policy['id']}&line_id={first['id']}"
    result = h.get(role, endpoint)
    assert result['reach'] == {policy['id']: 1} and set(result['details']) == {policy['id']}
    [row] = result['details'][policy['id']]['lines']
    assert row['line_id'] == first['id'] and row['specificity'] == 3
    assert row['people'][0]['matched_paths'] == ['where.0', 'who.0']
    assert row['clinics'] == [{'service_id': h.seed['services'][0], 'matched_paths': ['where.0'], 'count': 'together'}]
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        definition = copy.deepcopy(policy['definition'])
        foreign_line = U('foreign-source-line')
        definition['rule']['lines'][0]['id'] = foreign_line
        foreign = m.Policy(department_id=department.id, name='Other department rule', kind='catalogue', status='active',
                           owner='Synthetic owner', source='Permission fixture', starts_on=date(2026, 1, 1),
                           created_by=h.seed['identities']['coordinator'], definition=definition)
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    h.get(role, endpoint.replace(policy['id'], foreign_id), status=404)
    h.get(role, f'/api/policies/reach?details=true&line_id={foreign_line}', status=404)
    h.get(role, endpoint.replace(first['id'], second['id']).replace(policy['id'], U('unavailable-policy')), status=404)
    h.get(role, endpoint.replace(first['id'], U('missing-line')), status=404)
    h.get('staff', endpoint, status=403)
    h.get('credentialer', endpoint, status=403)


def test_catalogue_specificity_uses_tagged_domain_lines_for_active_and_proposed_rules(h):
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        tag = m.Tag(department_id=h.seed['department_id'], name='Synthetic team', labels=['people'])
        db.add(tag)
        db.flush()
        tag_id = tag.id
        db.add(m.TagMember(department_id=h.seed['department_id'], tag_id=tag_id, kind='people', member_id=person))
    everyone = catalogue_line(where=tree(line('service_ids', h.seed['services'][0])), numbers={'max': {'value': 6}})
    group = catalogue_line(who=tree(line('person_ids', tag_id)), where=tree(line('service_ids', h.seed['services'][1])),
                           numbers={'max': {'value': 5}})
    named = catalogue_line(who=tree(line('person_ids', person)), where=tree(line('assignment_type_ids', h.seed['type'])),
                           numbers={'max': {'value': 4}})
    lines = [everyone, group, named]
    waiting = propose(h, 'duties_per_week', lines)['record']
    expected = {everyone['id']: 1, group['id']: 2, named['id']: 3}
    shown = entry(h, 'duties_per_week', 'auditor')
    assert shown['line_specificity'] == {} and shown['proposed']['line_specificity'] == expected
    assert shown['count_sides'] == ['where']
    approve(h, waiting)
    assert entry(h, 'duties_per_week', 'auditor')['line_specificity'] == expected
    checked = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': lines}).json()
    assert checked['line_specificity'] == expected


def test_catalogue_balance_switch_is_validated_saved_and_preserved_when_omitted(h):
    source = catalogue_line(numbers={'max': {'value': 6}})
    first = approve(h, propose(h, 'duties_per_week', [source], balance_between_people=False)['record'])
    assert first['definition']['balance_between_people'] is False
    second = propose(h, 'duties_per_week', [source], policy_id=first['id'], revision=first['revision'])['record']
    assert second['definition']['balance_between_people'] is False
    live = approve(h, second)
    changed = propose(h, 'duties_per_week', [source], policy_id=live['id'], revision=live['revision'], balance_between_people=True)['record']
    assert changed['definition']['balance_between_people'] is True
    assert entry(h, 'duties_per_week')['proposed']['diff']['balance_between_people'] is True
    bad = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check',
                                      json={'lines': [source], 'balance_between_people': 'false'}).json()
    assert any(issue['field'] == 'balance_between_people' for issue in bad['issues'])


@pytest.fixture(scope='module')
def one_line_details_measurement():
    data = big_department()
    template = next(iter(data['people'].values()))
    for n in range(142, 150):
        person = U(f'p{n:03}')
        data['people'][person] = {**template, 'id': person}
        data['memberships'].append({**data['memberships'][0], 'person_id': person})
    people, clinic = sorted(data['people']), sorted(data['services'])[0]
    lines = [catalogue_line(id=U(f'detail-line-{n}'), who=tree(line('person_ids', people[n % len(people)])),
                            where=tree(line('service_ids', clinic)), numbers={'max': {'value': 6}},
                            starts_on=f'2026-{1 + n // 100:02}-01', ends_on=f'2026-{2 + n // 100:02}-01') for n in range(1000)]
    policy = rule(data, 'thousand-line-details', {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': lines})
    data['policies'] = [policy]
    a, b = local_midnight(date(2026, 10, 16), data['organisation']['timezone']), local_midnight(date(2026, 10, 17), data['organisation']['timezone'])
    import packages.domain.rule_map as module
    original, asked = module.scope_matches_throughout, []
    def measured(*args, **kwargs):
        if args[2] is not None:  # Preserve the old person_in_scope observation; session filters share this helper now.
            asked.append(args[2])
        return original(*args, **kwargs)
    gc.collect()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(module, 'scope_matches_throughout', measured)
        started = time.process_time()
        result = reach(data, [policy], a, b, details=True, policy_id=policy['id'], line_id=lines[950]['id'])
        elapsed = time.process_time() - started
    print(f'one-line reach: {elapsed:.3f} CPU s for 1000 catalogue lines, {len(data["people"])} people, {len(data["shifts"])} sessions')
    return result, policy, asked, elapsed


def test_one_line_details_budget_is_independent_of_unrelated_department_lines(one_line_details_measurement):
    result, policy, asked, _ = one_line_details_measurement
    assert len(result['details'][policy['id']]['lines']) == 1 and 0 < len(asked) <= 2


@pytest.mark.cpu_budget
def test_one_line_details_budget_is_independent_of_unrelated_department_lines_cpu_budget(one_line_details_measurement):
    *_, elapsed = one_line_details_measurement
    budget = 1 * (6 if coverage.Coverage.current() else 1)
    assert elapsed < budget, (elapsed, budget)


def test_session_hit_cache_preserves_unknown_person_branches_in_or_scopes(h):
    _, data = base(h)
    first, second = h.seed['people'][:2]
    policies = [rule(data, f'or-person-{index}', {'kind': 'max_duties', 'maximum': 6},
                    {'conditions': tree(line('person_ids', person), line('service_ids', h.seed['services'][0]), match='any')})
                for index, person in enumerate((first, second))]
    data['policies'] = policies
    combined = {row['id']: row for row in rule_map(data)['rules']}
    for policy in policies:
        [alone] = rule_map({**data, 'policies': [policy]})['rules']
        assert combined[policy['id']] == alone


@pytest.mark.parametrize('balance', [True, False])
@pytest.mark.parametrize('status', ['active', 'proposed'])
def test_reach_fairness_targets_share_the_validator_comparison_with_filtered_details(balance, status):
    from packages.domain.fairness import goal_reports
    from .test_balance_between_people import catalogue
    from .test_engine_fairness_review import A, B, duty, world
    data = world([duty(n, n, [A]) for n in range(1, 5)])
    policy = catalogue(balance=balance)
    data['policies'] = [policy]
    expected = {row['person_id']: row['expected'] for report in goal_reports(data) for row in report['people']}
    assert expected == ({A: 2, B: 2} if balance else {A: 4, B: 0})
    policy['status'] = status
    entire = reach(data, [policy], *bounds(data), details=True)
    rows = entire['details'][policy['id']]['lines']
    assert {person['person_id']: count['target'] for row in rows for person in row['people']
            for count in person['counts']} == expected
    for source, row in zip(policy['definition']['rule']['lines'], rows):
        selected = reach(data, [policy], *bounds(data), details=True, policy_id=policy['id'], line_id=source['id'])
        assert selected == {'reach': {policy['id']: 1}, 'details': {policy['id']: {'lines': [row]}}}
        [person] = row['people']
        assert person['matched_paths'] == ['who.0']
