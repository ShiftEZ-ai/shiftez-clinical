"""Configured usual people select sessions identically before and after allocation."""
import copy
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.conditions import (has_person_lines, has_session_lines, scope_matches_throughout,
                                        tag_scope, with_tags)
from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.rule_map import reach_details, rule_map
from packages.domain.time import aware
from packages.domain.validation import evaluate
from .test_rule_conditions import OCT, SHIFT, data, line, tree


def configured(shift, *people):
    return {**shift, 'series_snapshot': {'definition': {'default_person_ids': list(people)}}}


@pytest.mark.parametrize('op,values,expected', [
    ('any', ['p'], True), ('any', ['missing'], False),
    ('all', ['p', 'q'], True), ('all', ['p', 'missing'], False),
    ('none', ['p'], False), ('none', ['missing'], True),
])
def test_usual_people_are_session_facts_independent_of_candidate_and_actual_crew(op, values, expected):
    source = data()
    selector = tree(line('usual_assignee_ids', *values, op=op))
    s.Conditions.model_validate(selector)
    assert has_session_lines(selector) and not has_person_lines(selector)
    shift = configured(SHIFT, 'p', 'q')
    for crew in ([], [{'person_id': 'missing', 'team_lead': True}], [{'person_id': 'p'}]):
        shift['participants'] = crew
        for person in (None, 'p', 'q'):
            assert scope_matches_throughout(source, {'conditions': selector}, person, shift, *OCT) is expected
    # Before any session is known, its condition is deliberately unknown rather than rejecting a person.
    assert scope_matches_throughout(source, {'conditions': selector}, 'p', None, *OCT)


def test_absent_series_provenance_never_infers_usual_people_from_current_assignments():
    shift = {**SHIFT, 'participants': [{'person_id': 'p', 'team_lead': True}], 'series_id': 'unfrozen'}
    positive = {'conditions': tree(line('usual_assignee_ids', 'p'))}
    negative = {'conditions': tree(line('usual_assignee_ids', 'p', op='none'))}
    assert not scope_matches_throughout(data(), positive, 'p', shift, *OCT)
    assert scope_matches_throughout(data(), negative, 'p', shift, *OCT)
    mixed = {'conditions': tree(tree(line('usual_assignee_ids', 'q'), line('person_ids', 'p')),
                                line('usual_assignee_ids', 'missing'), match='any')}
    assert scope_matches_throughout(data(), mixed, 'p', configured(shift, 'q'), *OCT)
    assert not scope_matches_throughout(data(), mixed, 'q', configured(shift, 'q'), *OCT)


def test_usual_people_are_where_conditions_and_cannot_be_who_audience_or_pair_partners():
    selector = tree(line('usual_assignee_ids', str(uuid.uuid4())))
    assert s.RuleLine.model_validate({'id': str(uuid.uuid4()), 'where': selector,
                                     'numbers': {'min': {'value': 1}}}).where.fields() == {'usual_assignee_ids'}
    with pytest.raises(ValidationError, match='Who cannot filter'):
        s.RuleLine.model_validate({'id': str(uuid.uuid4()), 'who': selector, 'numbers': {'min': {'value': 1}}})
    with pytest.raises(ValidationError, match='An audience cannot filter'):
        s.Audience.model_validate({'where': selector})
    with pytest.raises(ValidationError, match='Partners cannot filter'):
        s.BalanceGoal.model_validate({'kind': 'balance_goal', 'metric': 'pair_exposure', 'aim': 'min_per_pair',
                                     'amount': 1, 'partners': selector})


@pytest.mark.parametrize('op', ['any', 'all', 'none'])
def test_usual_people_share_existing_person_tag_expansion(op):
    source = data()
    source['tags'] = {'team': {'name': 'Provider group', 'members': {'people': ['p', 'q']}}}
    scope = tag_scope(source, {'conditions': tree(line('usual_assignee_ids', 'team', op=op))})
    assert scope_matches_throughout(source, scope, None, configured(SHIFT, 'p', 'q'), *OCT) is (op != 'none')
    assert scope_matches_throughout(source, scope, None, configured(SHIFT, 'elsewhere'), *OCT) is (op == 'none')


def test_staffing_selection_order_reuses_tag_expansion_in_primitive_and_catalogue_settings():
    person, tag = str(uuid.uuid4()), str(uuid.uuid4())
    settings = {'selection_order': [tree(line('person_ids', tag))]}
    primitive = {'kind': 'minimum_cover', 'minimum': 1, **settings}
    catalogue = {'kind': 'catalogue', 'id': 'kind_per_session', 'settings': {'minimum_cover': settings},
                 'lines': [{'id': str(uuid.uuid4()), 'settings': {'minimum_cover': settings}}]}
    source = with_tags({'tags': {tag: {'name': 'Tier group', 'members': {'people': [person]}}},
                        'policies': [{'definition': {'scope': {}, 'rule': rule}} for rule in (primitive, catalogue)]})
    first, grouped = [p['definition']['rule'] for p in source['policies']]
    selectors = [first['selection_order'], grouped['settings']['minimum_cover']['selection_order'],
                 grouped['lines'][0]['settings']['minimum_cover']['selection_order']]
    assert all(person in order[0]['conditions'][0]['values'] for order in selectors)


@pytest.mark.postgres
def test_usual_people_rule_scope_has_review_undo_audit_and_reference_permissions(pg):
    h = pg
    person, owner, other_owner = h.seed['people']
    original = {'id': str(uuid.uuid4()), 'who': tree(line('person_ids', person)),
                'where': tree(line('usual_assignee_ids', owner)), 'numbers': {'apply': {}}}
    payload = {'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'lines': [original],
               'reason': 'Synthetic provider session scope'}
    h.command('staff', 'rule.propose', payload, status=403)
    first = h.command('coordinator', 'rule.propose', payload)['record']
    h.command('coordinator', 'policy.activate', {'reason': 'Requires independent review'},
              first['id'], first['revision'], status=403)
    active = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                       first['id'], first['revision'])['record']
    changed = {**original, 'where': tree(line('usual_assignee_ids', other_owner))}
    proposed = h.command('coordinator', 'rule.propose', {**payload, 'lines': [changed],
        'expected_policy_id': active['id'], 'expected_revision': active['revision']})['record']
    newer = h.command('approver', 'policy.activate', {'reason': 'Review provider change'},
                      proposed['id'], proposed['revision'])['record']
    assert h.command('approver', 'policy.retire', {'reason': 'Undo provider configuration'},
                     newer['id'], newer['revision'])['restored_ids'] == [active['id']]
    saved = next(p for p in h.get('auditor', '/api/policies?status=active')['policies'] if p['id'] == active['id'])
    assert saved['definition']['rule']['lines'][0]['where'] == original['where']
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == newer['id'])))
    with transaction(h.app.state.engine, write=True) as db:
        organisation = m.Organisation(name='Separate test organisation')
        db.add(organisation)
        db.flush()
        foreign = m.Person(organisation_id=organisation.id, display_name='Separate test person')
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    for reference in (foreign_id, str(uuid.uuid4())):
        response = h.client('coordinator').post('/api/policies/check', json={'definition': {
            'rule': {'kind': 'min_rest', 'hours': 11},
            'scope': {'conditions': tree(line('usual_assignee_ids', reference))}}})
        assert response.status_code == 404, response.text
    response = h.client('coordinator').post('/api/policies/check', json={'definition': {
        'rule': {'kind': 'minimum_cover', 'minimum': 1,
                 'selection_order': [tree(line('person_ids', str(uuid.uuid4())))]}}})
    assert response.status_code == 404, response.text


def condition_snapshot(h):
    person, alternate, excluded = h.seed['people']
    restriction = {'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'}
    blocked = h.policy(restriction, {'conditions': tree(line('person_ids', person), line('usual_assignee_ids', person))})
    h.policy(restriction, {'person_id': excluded})
    h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': person}, 'advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    source['shifts'] = [configured(shift, owner) for shift, owner in zip(source['shifts'], (person, alternate))]
    return source, blocked, person, alternate


@pytest.mark.parametrize('engine', [cp_generate, greedy_generate], ids=['cp_sat', 'greedy'])
def test_both_engines_honour_the_configured_provider_without_moving_the_scope(h, engine):
    source, blocked, person, alternate = condition_snapshot(h)
    result = engine(copy.deepcopy(source), 8)
    assert result['status'] == 'candidate_feasible', result
    who = {addition['shift_id']: addition['participant']['person_id'] for addition in result['additions']}
    assert who == {source['shifts'][0]['id']: alternate, source['shifts'][1]['id']: person}
    for shift in source['shifts']:
        shift['participants'] = [addition['participant'] for addition in result['additions'] if addition['shift_id'] == shift['id']]
    assert not [finding for finding in evaluate(source)['findings'] if finding.get('rule_id') == blocked['id']]
    first = source['shifts'][0]
    first['participants'][0]['person_id'] = person
    assert [finding['shift_id'] for finding in evaluate(source)['findings'] if finding.get('rule_id') == blocked['id']] == [first['id']]


def test_rule_map_cache_distinguishes_usual_people_at_identical_clinic_place_and_duty(h):
    source, blocked, _, _ = condition_snapshot(h)
    source['policies'] = [blocked]
    mapped = rule_map(source)
    selected = next(rule for rule in mapped['rules'] if rule['id'] == blocked['id'])
    assert selected['session_side'] and selected['sessions'] == [0]
    # The reach cache must also keep different providers apart when they share all other session properties.
    source['shifts'].reverse()
    mapped = rule_map(source)
    assert next(rule for rule in mapped['rules'] if rule['id'] == blocked['id'])['sessions'] == [1]
    reached = reach_details(source, [blocked], aware(source['shifts'][1]['start']), aware(source['shifts'][0]['end']))
    assert reached[blocked['id']]['lines'][0]['clinics'][0]['service_id'] == source['shifts'][1]['service_ids'][0]


@pytest.mark.parametrize('counted', [True, False], ids=['counted-contract', 'normalized-shared-scope'])
def test_ordinary_goal_same_day_cache_respects_usual_provider_session_scope(counted):
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_goals import add_balance_goals
    from packages.domain.fairness import goal_reports
    from .test_engine_fairness_review import A, B, duty, policy, world

    shifts = [configured(duty(1, 5, [A]), B), configured(duty(2, 5, [A]), A)]
    second = shifts[1]
    second['start'], second['end'] = second['start'].replace('08:00', '13:00'), second['end'].replace('12:00', '17:00')
    second['participants'][0].update(start=second['start'], end=second['end'])
    source = world(shifts)
    selector = tree({**line('usual_assignee_ids', B), **({'count': 'each'} if counted else {})})
    goal = policy({'metric': 'duties', 'aim': 'target', 'amount': 1}, {'person_ids': [A]})
    definition = {**goal['definition'], 'scope': {'person_ids': [A], 'conditions': selector}}
    if counted:
        goal['definition'] = s.PolicyDefinition.model_validate(definition).model_dump(mode='json')
    else:
        # Exercise normalized compiler input directly; ordinary authoring reserves session filters for Count trees.
        goal['definition'] = definition
    source['policies'] = [goal]
    [report] = goal_reports(source)
    assert [(row['actual'], row['expected'], row['cost']) for row in report['people']] == [(1, 1, 0)]

    model = cp_model.CpModel()
    choices, participants = {}, {}
    for shift in shifts:
        key = shift['id'], A
        choices[key] = model.new_bool_var('attendance')
        model.add(choices[key] == 1)
        participants[key] = shift['participants'][0]
    terms, _ = add_balance_goals(model, source, choices, participants, {}, {}, set(source['focus_ids']),
                                 {'low': 1, 'medium': 3, 'high': 9})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.objective_value == 0


@pytest.mark.parametrize('matching_first', [False, True])
def test_inherited_strength_cache_respects_frozen_usual_provider(matching_first):
    from packages.domain.conditions import active_rules
    from .test_engine_fairness_review import A, B, DEPT, POLICY, duty, uid, world

    shift = configured(duty(1, 5, [A]), A)
    source, skill = world([shift]), uid(50)
    source['capabilities'] = {skill: {'active': True}}
    source['grants'] = [{'person_id': A, 'department_id': DEPT, 'capability_id': skill,
                         'service_id': None, 'starts_on': '2020-01-01'}]
    lines = [{'id': uid(n), 'where': tree(line('usual_assignee_ids', A)),
              'numbers': {'max': {'value': amount, 'level': level}}, **({'who': who} if who else {})}
             for n, amount, level, who in [(20, 1, 'low', None),
                 (21, 2, 'high', tree(line('capability_ids', skill))),
                 (22, 3, None, tree(line('person_ids', A)))]]
    policy = {'id': POLICY, 'name': 'Synthetic provider inheritance', 'kind': 'catalogue',
              'department_id': DEPT, 'status': 'active', 'active': True, 'starts_on': '2020-01-01',
              'ends_on': None, 'revision': 1, 'definition': s.PolicyDefinition.model_validate({
                  'rule': {'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit',
                           'lines': lines}}).model_dump(mode='json')}
    source['policies'] = active_rules([policy])
    source['_line_index_cache'], source['_person_dates_cache'] = {}, {}
    inherited = [p for p in source['policies'] if p['template_line']['id'] == uid(22)]
    a, b = aware(shift['start']), aware(shift['end'])
    for provider in (A, B) if matching_first else (B, A):
        # A provider edit must not reuse either inherited-level cache for the same duty id and time.
        shift['series_snapshot']['definition']['default_person_ids'] = [provider]
        strengths = [p['definition']['strength'] for p in inherited
                     if scope_matches_throughout(source, p['definition']['scope'], A, shift, a, b)]
        assert strengths == (['high'] if provider == A else [])
