"""Rules explained reads the same effective line precedence as the checker."""

import copy
import logging

import pytest
from pydantic import ValidationError

import packages.domain.rule_explainer as rule_explainer
from packages.domain.rule_explainer import (_condition_text, _number_text, _person_noun, _rank_group,
                                             _period_words, _short_number, _when_text, explain_rules)
from packages.contracts.rule_catalogue import (QUESTION_GROUPS, QUESTION_GROUP_ORDER, RULE_CATALOGUE_V2,
                                                line_comparison, question_group_entry, question_group_for_effective_kind)
from .test_rule_catalogue_api import approve, line, person, propose


def _synthetic_error(error_type):
    if error_type is ValidationError:
        return ValidationError.from_exception_data(
            'SyntheticExplanation',
            [{'type': 'value_error', 'loc': (), 'input': None, 'ctx': {'error': 'synthetic explainer failure'}}],
        )
    return error_type('synthetic explainer failure')


def _catalogue(h):
    rank, alex, clinic = h.seed['category'], h.seed['people'][0], h.seed['services'][0]
    where = {'conditions': [{'field': 'service_ids', 'values': [clinic]}]}
    everyone = line(numbers={'max': {'value': 5}})
    ranked = line(who={'conditions': [{'field': 'category_ids', 'values': [rank]}]}, where=where,
                  numbers={'max': {'value': 4}})
    personal = line(who=person(alex), where=where, numbers={'max': {'value': 3}})
    result = propose(h, 'duties_per_week', [
        everyone, ranked, personal,
    ], acknowledged=[everyone['id'], ranked['id']])
    return approve(h, result['record']), rank, alex, clinic


def test_explainer_resolves_rank_person_clinic_and_specificity(h):
    policy, rank, alex, clinic = _catalogue(h)
    workspace = h.shift(h.workspace('Explainer'), people=[alex], service=0)['workspace']
    data = h.snapshot(workspace)

    person_result = explain_rules(data, who=f'person:{alex}')
    rule = next(row for row in person_result['rules'] if row['rule_id'] == policy['id'])
    assert person_result['workspace_id'] == workspace['id']
    assert person_result['who'] == f'person:{alex}'
    assert any('at most 3 duties' in sentence['text'] for sentence in rule['sentences'])
    assert not any('at most 4 duties' in sentence['text'] for sentence in rule['sentences'])
    assert any(sentence['where'] == data['services'][clinic]['name'] for sentence in rule['sentences'])

    rank_result = explain_rules(data, who=f'rank:{rank}')
    rank_rule = next(row for row in rank_result['rules'] if row['rule_id'] == policy['id'])
    assert rank_result['who'] == f'rank:{rank}'
    assert any('at most 4 duties' in sentence['text'] for sentence in rank_rule['sentences'])
    assert all(sentence['source_line_ids'] for sentence in rank_rule['sentences'])
    assert rank_rule['question_group'] == QUESTION_GROUPS['workload_group']
    assert QUESTION_GROUPS['workload_group'] in {sentence['question_group'] for sentence in rank_rule['sentences']}


def test_explainer_when_text_separates_rule_dates_from_rota_dates():
    workspace = {'starts_on': '2026-11-11', 'ends_on': '2026-11-13'}
    copy = {'starts_on': '2026-11-10', 'ends_on': '2026-11-15', 'definition': {'scope': {
        'weekdays': [2], 'start_time': '23:30', 'end_time': '01:30'}}}

    assert _when_text(copy, workspace, 'Asia/Riyadh', rota=False) == \
        '10 November 2026–14 November 2026, Wednesday, 23:30–01:30'
    assert _when_text(copy, workspace, 'Asia/Riyadh', rota=True) == \
        '11 November 2026–12 November 2026, Wednesday, 23:30–01:30'
    assert _when_text({**copy, 'starts_on': None, 'ends_on': None}, workspace, 'Asia/Riyadh', rota=False) == \
        'Wednesday, 23:30–01:30'


@pytest.mark.parametrize('rule, expected', [
    ({'kind': 'max_duties', 'window': 'week'}, 'a week'),
    ({'kind': 'max_hours', 'window': 'rolling', 'window_days': 3}, 'in any 3 days'),
    ({'kind': 'block_quota', 'window_alignment': 'fixed', 'window_days': 7}, 'per 7-day block'),
    ({'kind': 'block_quota', 'window_alignment': 'rolling', 'window_days': 7}, 'in any 7 days'),
])
def test_legacy_period_words_keep_the_configured_count_window(rule, expected):
    assert _period_words(rule, {}) == expected


def test_v2_catalogue_owns_the_six_rules_explained_question_groups():
    expected = set(QUESTION_GROUPS.values())
    assert {entry['question_group'] for entry in RULE_CATALOGUE_V2} <= expected
    assert QUESTION_GROUP_ORDER == tuple(QUESTION_GROUPS.values())
    assert {entry['question_group'] for entry in RULE_CATALOGUE_V2} == {
        QUESTION_GROUPS[key] for key in ('staffing', 'supervision', 'workload_person', 'preferences')
    }
    workload = next(entry for entry in RULE_CATALOGUE_V2 if entry['id'] == 'workload_limit')
    assert workload['question_group_aliases']['keep_off'] == QUESTION_GROUPS['who_where']
    assert workload['question_group_by_slot']['restriction'] == QUESTION_GROUPS['who_where']
    assert workload['question_group_by_slot_level']['block_quota']['advisory'] == QUESTION_GROUPS['preferences']
    assert workload['question_group_by_scope']['group'] == QUESTION_GROUPS['workload_group']
    assert question_group_entry('protected_time')['question_group_aliases']['protected_time'] == QUESTION_GROUPS['who_where']
    assert question_group_entry('overtime_offers')['question_group_aliases']['overtime_offers'] == QUESTION_GROUPS['preferences']
    assert question_group_for_effective_kind('minimum_cover') == QUESTION_GROUPS['staffing']
    assert question_group_for_effective_kind('block_quota', severity='advisory', scope='person') == QUESTION_GROUPS['preferences']
    assert question_group_for_effective_kind('block_quota', severity='mandatory', scope='group') == QUESTION_GROUPS['workload_group']


def test_rules_explained_endpoint_matches_contract_and_permissions(h):
    policy, rank, alex, _ = _catalogue(h)
    workspace = h.shift(h.workspace('Explainer endpoint'), people=[alex])['workspace']
    result = h.get('coordinator', f'/api/workspaces/{workspace["id"]}/rules-explained?who=rank:{rank}')
    assert result['workspace_id'] == workspace['id']
    assert result['who'] == f'rank:{rank}'
    assert any(row['rule_id'] == policy['id'] for row in result['rules'])
    assert h.get('staff', f'/api/workspaces/{workspace["id"]}/rules-explained', status=403)['error']['code'] == 'FORBIDDEN'


def test_explainer_renders_authored_rotation_and_restriction_conditions(h):
    rank, _, clinic = h.seed['category'], h.seed['people'][0], h.seed['services'][0]
    duty = h.seed['type']
    rotation = h.configure('rotation_types', {'name': 'Urgent care fixture rotation', 'starts_on': '2026-01-01'})['record']
    policy = propose(h, 'keep_off', [line(
        who={'match': 'all', 'conditions': [
            {'field': 'category_ids', 'values': [rank]},
            {'field': 'rotation_type_ids', 'values': [rotation['id']]},
        ]},
        where={'match': 'any', 'conditions': [
            {'field': 'service_ids', 'op': 'none', 'values': [clinic]},
            {'field': 'assignment_type_ids', 'op': 'none', 'values': [duty]},
        ]},
        numbers={'apply': {}},
    )])
    approve(h, policy['record'])
    workspace = h.shift(h.workspace('Explainer authored conditions'), people=[h.seed['people'][0]])['workspace']
    data = h.snapshot(workspace)
    result = explain_rules(data)
    sentence = next(sentence for rule in result['rules'] for sentence in rule['sentences']
                    if sentence['source_line_ids'] == [policy['record']['definition']['rule']['lines'][0]['id']])
    assert f"may work only {data['services'][clinic]['name']} on {data['types'][duty]['name']}" in sentence['text']
    assert 'otherwise must have no duties' in sentence['text']
    assert 'Urgent care fixture rotation' in sentence['who']
    assert 'all clinics and duties' not in sentence['text'].lower()


def test_explainer_direct_restriction_keeps_rank_and_clinic(h):
    rank, _, clinic = h.seed['category'], h.seed['people'][0], h.seed['services'][0]
    policy = propose(h, 'keep_off', [line(
        who={'conditions': [{'field': 'category_ids', 'values': [rank]}]},
        where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
        numbers={'apply': {}},
    )])
    approve(h, policy['record'])
    workspace = h.shift(h.workspace('Explainer direct restriction'), people=[h.seed['people'][0]], seed_restricted=True)['workspace']
    data = h.snapshot(workspace)
    result = explain_rules(data, who=f'rank:{rank}')
    sentence = next(sentence for rule in result['rules'] for sentence in rule['sentences']
                    if sentence['source_line_ids'] == [policy['record']['definition']['rule']['lines'][0]['id']])
    assert f"never work {data['services'][clinic]['name']}" in sentence['text']
    assert 'must have no duties' not in sentence['text']
    assert sentence['who'] == data['categories'][rank]['name']


def test_explainer_plain_rank_group_and_cover_nouns():
    assert _rank_group(['Resident R1', 'Resident R2', 'Resident R3']) == 'Residents (R1–R3)'
    assert _person_noun('Resident R2', singular=True) == 'resident R2'
    assert _person_noun('Residents (R1–R3)', singular=False) == 'residents from R1–R3'


def test_explainer_renders_usual_assignee_condition_without_placeholder(h):
    data = h.snapshot(h.workspace('Explainer usual assignee'))
    person_id = h.seed['people'][0]
    rendered = _condition_text(data, {'match': 'all', 'conditions': [
        {'field': 'usual_assignee_ids', 'op': 'any', 'values': [person_id]},
    ]})
    assert rendered.startswith('usual assignees are ')
    assert 'conditions apply' not in rendered


def test_explainer_collapses_equal_mandatory_minimum_and_advisory_target(h):
    rank, person_id, clinic = h.seed['category'], h.seed['people'][0], h.seed['services'][0]
    policy = propose(h, 'kind_per_session', [line(
        who={'conditions': [{'field': 'category_ids', 'values': [rank]}]},
        where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
        numbers={'min': {'value': 2, 'level': 'mandatory'},
                 'target': {'value': 2, 'level': 'medium'}},
    )])
    approve(h, policy['record'])
    workspace = h.shift(h.workspace('Explainer equal target'), people=[person_id], service=0)['workspace']
    result = explain_rules(h.snapshot(workspace), who=f'rank:{rank}')
    rule = next(row for row in result['rules'] if row['rule_id'] == policy['record']['id'])
    source_id = policy['record']['definition']['rule']['lines'][0]['id']
    sentences = [sentence for sentence in rule['sentences'] if sentence['source_line_ids'] == [source_id]]
    assert len(sentences) == 1
    assert sentences[0]['level'] == 'mandatory'
    assert sentences[0]['weight'] is None
    assert '(preference, medium)' not in sentences[0]['text']


def test_explainer_words_equal_mandatory_bounds_as_exact(h):
    person_id = h.seed['people'][0]
    policy = propose(h, 'duties_per_month', [line(
        who=person(person_id),
        numbers={'min': {'value': 16, 'level': 'mandatory'}, 'max': {'value': 16, 'level': 'mandatory'}},
    )])
    approve(h, policy['record'])
    workspace = h.shift(h.workspace('Explainer exact count'), people=[person_id])['workspace']
    rule = next(row for row in explain_rules(h.snapshot(workspace), who=f'person:{person_id}')['rules']
                if row['rule_id'] == policy['record']['id'])
    sentence = rule['sentences'][0]
    assert 'exactly 16 duties' in sentence['text']
    assert sentence['cell'].startswith('exactly 16 duties')
    assert sentence['short'] == '= 16'
    assert 'at most 16' not in sentence['text'] and 'at least 16' not in sentence['text']


def test_explainer_keeps_legacy_per_person_number_in_draft(h):
    person_id = h.seed['people'][0]
    policy = h.command('coordinator', 'policy.create', {
        'name': 'Legacy weekly duty cap', 'owner': 'Test policy owner',
        'source': 'Constructed fixture', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                       'scope': {}, 'severity': 'mandatory',
                       'per_person': [{'person_id': person_id, 'amount': 2}]},
    })['record']
    policy = approve(h, policy)
    workspace = h.workspace('Legacy explainer draft')
    rule = next(row for row in explain_rules(h.snapshot(workspace), who=f'person:{person_id}')['rules']
                if row['rule_id'] == policy['id'])
    sentence = rule['sentences'][0]
    assert 'at most 2 duties' in sentence['text']
    assert 'at most 2 duties' in sentence['cell']


def test_line_comparison_and_short_words_cover_all_number_forms():
    forms = [
        ({'min': {'value': 2, 'level': 'mandatory'}}, 'minimum', '≥ 2'),
        ({'min': {'value': 16, 'level': 'mandatory'}, 'max': {'value': 16, 'level': 'mandatory'}}, 'exact', '= 16'),
        ({'max': {'value': 5, 'level': 'mandatory'}}, 'maximum', '≤ 5'),
        ({'min': {'value': 2, 'level': 'mandatory'}, 'max': {'value': 5, 'level': 'mandatory'}}, 'range', '2–5'),
        ({'target': {'value': 4, 'level': 'medium'}}, 'target', 'aim 4'),
    ]
    assert [(line_comparison(numbers), _short_number(numbers)) for numbers, _, _ in forms] == [
        (comparison, short) for _, comparison, short in forms
    ]
    assert _number_text({'kind': 'max_duties', 'maximum': 5}, line_numbers=forms[3][0]) == 'between 2 and 5 duties'


def test_line_comparison_ignores_explicit_null_number_slots():
    target = {'min': {'value': None, 'level': 'mandatory'},
              'target': {'value': 2, 'level': 'medium'}}
    maximum = {'min': {'value': None, 'level': 'mandatory'},
               'max': {'value': 3, 'level': 'mandatory'}}
    empty = {'min': {'value': None, 'level': 'mandatory'},
             'max': {'value': None, 'level': 'mandatory'},
             'target': {'value': None, 'level': 'medium'}}
    assert line_comparison(target) == 'target' and _short_number(target) == 'aim 2'
    assert line_comparison(maximum) == 'maximum' and _short_number(maximum) == '≤ 3'
    assert line_comparison(empty) is None and _short_number(empty) is None


def test_explainer_renders_target_when_minimum_is_explicitly_null(h):
    rank, person_id, clinic = h.seed['category'], h.seed['people'][0], h.seed['services'][0]
    source = line(
        who={'conditions': [{'field': 'category_ids', 'values': [rank]}]},
        where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
        numbers={'min': {'value': None, 'level': 'mandatory'},
                 'target': {'value': 2, 'level': 'medium'}},
        settings={'minimum_cover': {'aggregation': 'assignment', 'when_person_ids': []}},
    )
    policy = approve(h, propose(h, 'kind_per_session', [source])['record'])
    workspace = h.shift(h.workspace('Explainer explicit null minimum'), people=[person_id], service=0)['workspace']

    result = explain_rules(h.snapshot(workspace), who=f'rank:{rank}')
    rule = next((row for row in result['rules'] if row['rule_id'] == policy['id']), None)
    assert rule and rule['sentences']
    assert len(rule['sentences']) == 1
    sentence = rule['sentences'][0]
    assert sentence['short'] == 'aim 2'
    assert sentence['source_line_ids'] == [source['id']]
    assert '2' in sentence['text']


def test_explainer_renders_transition_conditions():
    data = {'types': {
        'from': {'name': 'Evening shift'},
        'to': {'name': 'Day shift'},
    }}
    text = _number_text({'kind': 'forbidden_transition', 'from_type_ids': ['from'],
                         'to_type_ids': ['to'], 'overtime_only': False}, data)
    assert text == 'must not schedule Day shift after Evening shift'


def test_explainer_orders_safety_then_must_then_prefer(h):
    rank, _, clinic = h.seed['category'], h.seed['people'][0], h.seed['services'][0]
    ordinary = propose(h, 'duties_per_week', [line(
        who={'conditions': [{'field': 'category_ids', 'values': [rank]}]},
        numbers={'max': {'value': 4}},
    )])
    safety = propose(h, 'keep_off', [line(
        who={'conditions': [{'field': 'category_ids', 'values': [rank]}]},
        where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
        numbers={'apply': {}},
    )])
    approve(h, ordinary['record'])
    approve(h, safety['record'])
    workspace = h.shift(h.workspace('Explainer ordering'), people=[h.seed['people'][0]], seed_restricted=True)['workspace']
    rows = explain_rules(h.snapshot(workspace), who=f'rank:{rank}')['rules']
    assert rows and rows[0]['safety']
    levels = [('mandatory' if row['level'] == 'mandatory' else 'advisory') for row in rows]
    assert levels == sorted(levels, key=lambda value: 0 if value == 'mandatory' else 1)



def test_explainer_skips_malformed_line_and_logs_problem(h, monkeypatch, caplog):
    policy, _, alex, _ = _catalogue(h)
    workspace = h.shift(h.workspace('Explainer malformed line'), people=[alex])['workspace']
    data = h.snapshot(workspace)
    valid = next(row for row in data['policies'] if row['id'] == policy['id'])
    broken = copy.deepcopy(valid)
    broken['id'] = 'broken-explanation-policy'
    broken['definition']['rule']['lines'][0]['id'] = 'broken-explanation-line'
    broken['definition']['rule'].pop('id', None)
    broken['template_line'] = broken['definition']['rule']['lines'][0]
    data['policies'].append(broken)
    monkeypatch.setattr(rule_explainer, 'active_rules', lambda *_args, **_kwargs: [valid, broken])

    with caplog.at_level(logging.WARNING, logger='packages.domain.rule_explainer'):
        result = explain_rules(data)

    assert any(row['rule_id'] == policy['id'] for row in result['rules'])
    assert not any(row['rule_id'] == broken['id'] for row in result['rules'])
    assert 'Skipping malformed rule line in explanation' in caplog.text

    problem = next(record for record in caplog.records if getattr(record, 'policy_id', None) == broken['id'])
    assert problem.error_type == 'RuleExplanationInputError'


@pytest.mark.parametrize('failure_point, error_type, message', [
    ('_cell_text', RuntimeError, 'Rule line explanation failed'),
    ('_cell_text', ValueError, 'Rule line explanation failed'),
    ('safety_rule', RuntimeError, 'Rule safety classification failed'),
    ('safety_rule', ValueError, 'Rule safety classification failed'),
    ('safety_rule', rule_explainer.RuleExplanationInputError, 'Rule safety classification failed'),
    ('safety_rule', ValidationError, 'Rule safety classification failed'),
])
def test_explainer_keeps_failed_safety_explanations_visible(h, monkeypatch, caplog,
                                                            failure_point, error_type, message):
    policy = approve(h, propose(h, 'keep_off', [line(numbers={'apply': {}})])['record'])
    workspace = h.workspace('Explainer failure visible')
    data = h.snapshot(workspace)
    original = getattr(rule_explainer, failure_point)

    def fail(value, *args, **kwargs):
        target = ((value.get('rule') or {}).get('kind') == 'restriction'
                  if failure_point == 'safety_rule' else value.get('id') == policy['id'])
        if target:
            raise _synthetic_error(error_type)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(rule_explainer, failure_point, fail)
    with caplog.at_level(logging.ERROR, logger='packages.domain.rule_explainer'):
        result = explain_rules(data)

    row = next(item for item in result['rules'] if item['rule_id'] == policy['id'])
    assert row['title'] == policy['name']
    assert row['level'] == 'mandatory' and row['safety'] is True
    assert row['unexplained'] is True and row['sentences'] == []
    failure = next(record for record in caplog.records if record.message == message)
    assert failure.policy_id == policy['id']
    assert failure.error_type == error_type.__name__ and failure.line_id

    day = h.get('coordinator', '/api/rules/catalogue')['day']
    department = h.get('coordinator', f'/api/rules-explained?at={day}')
    department_row = next(item for item in department['rules'] if item['rule_id'] == policy['id'])
    assert department_row['unexplained'] is True and department_row['safety'] is True


def test_malformed_safety_line_upgrades_an_existing_explained_row(h, monkeypatch):
    policy = approve(h, propose(h, 'keep_off', [line(numbers={'apply': {}})])['record'])
    workspace = h.workspace('Partially explained safety rule')
    data = h.snapshot(workspace)
    source = next(row for row in data['policies'] if row['id'] == policy['id'])
    source['definition']['rule'] = {'kind': 'catalogue', 'id': 'workload_limit', 'catalogue_version': 2, 'lines': [
        line(numbers={'max': {'value': 5, 'level': 'medium'}}, settings={'max_duties': {'window': 'week'}}),
        line(numbers={'apply': {'level': 'mandatory'}}, settings={'restriction': {}}),
    ]}
    original = rule_explainer._cell_text

    def fail_safety_line(copy, *args, **kwargs):
        if copy['id'] == policy['id'] and copy['definition']['rule']['kind'] == 'restriction':
            raise rule_explainer.RuleExplanationInputError('synthetic malformed safety line')
        return original(copy, *args, **kwargs)

    monkeypatch.setattr(rule_explainer, '_cell_text', fail_safety_line)
    result = explain_rules(data)
    row = next(item for item in result['rules'] if item['rule_id'] == policy['id'])
    assert row['sentences'] and all(not sentence['safety'] for sentence in row['sentences'])
    assert row['title'] == policy['name']
    assert row['safety'] is True and row['unexplained'] is True and row['level'] == 'mandatory'


@pytest.mark.parametrize('error_type', [rule_explainer.RuleExplanationInputError, ValidationError])
def test_explainer_skips_same_malformed_errors_for_non_safety_rules(h, monkeypatch, caplog, error_type):
    policy = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 4}})])['record'])
    data = h.snapshot(h.workspace('Non-safety malformed explanation'))
    original = rule_explainer._cell_text

    def fail(copy, *args, **kwargs):
        if copy['id'] == policy['id']:
            raise _synthetic_error(error_type)
        return original(copy, *args, **kwargs)

    monkeypatch.setattr(rule_explainer, '_cell_text', fail)
    with caplog.at_level(logging.WARNING, logger='packages.domain.rule_explainer'):
        result = explain_rules(data)

    assert not any(row['rule_id'] == policy['id'] for row in result['rules'])
    warning = next(record for record in caplog.records
                   if record.message == 'Skipping malformed rule line in explanation'
                   and record.policy_id == policy['id'])
    assert warning.error_type == error_type.__name__
