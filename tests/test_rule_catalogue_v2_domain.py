"""Catalogue-v2 reuses original source partitions and the one primitive evaluator."""
import copy
import json
from collections import Counter

import pytest

from packages.contracts.rule_catalogue import RULE_CATALOGUE
from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules, with_tags
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_lines import fact_policy, outranking_by_number
from .test_rule_simplification import PERSON, SERVICE, production_families, quota, rule


def effective(data):
    return [{k: v for k, v in p.items() if k != '_v2_source'} for p in active_rules(data['policies'], context=data)]


def converted(policies):
    data = {'policies': policies}
    proposal = plan_v2(policies, department_id='d')
    assert proposal['blocked'] == []
    return data, after_snapshot(data, proposal), proposal


def test_conversion_dry_run_keeps_source_and_retained_nested_evidence_independent():
    source = rule('Moving cap', {'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    retained = {**rule('Retired evidence', {'kind': 'max_duties', 'maximum': 7}),
                'status': 'retired', 'active': False}
    data = {'policies': [retained, source, retained], 'people': {'person': {'tags': ['original']}},
            'scheduling_facts': [], 'source_bindings': []}
    proposal = plan_v2(data['policies'], department_id='d')
    original, original_proposal = copy.deepcopy(data), copy.deepcopy(proposal)
    after = after_snapshot(data, proposal)
    assert [p['id'] for p in after['policies'][:2]] == [retained['id'], retained['id']]
    assert after['policies'][:2] == [retained, retained]
    after['policies'][0]['definition']['rule']['maximum'] = 99
    after['people']['person']['tags'].append('dry-run edit')
    after['policies'][-1]['definition']['rule']['lines'].clear()
    after['source_bindings'][0]['source_revision'] = 99
    assert data == original and proposal == original_proposal


def catalogue(entry, lines, *, settings=None, on=True):
    value = rule(entry, {'kind': 'catalogue', 'id': entry, 'lines': lines, 'settings': settings or {}, 'on': on})
    value['definition'] = PolicyDefinition.model_validate(value['definition'], context={'historical': True}).model_dump(mode='json')
    return value


def line(ident, numbers, who=None, settings=None, **extra):
    return {'id': ident, 'numbers': numbers, 'who': who, 'settings': settings or {}, **extra}


def who(person):
    return {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': [person], 'op': 'any'}]}


@pytest.mark.parametrize('placement', ['primitive', 'rule_settings', 'line_settings'])
def test_spread_evenly_with_tags_preserves_enum_settings_and_stored_policy(placement):
    settings = {'holder': 'supervisors', 'within': 'session', 'mode': 'levelled'}
    if placement == 'primitive':
        policy = rule('Spread evenly', {'kind': 'spread_evenly', **settings, 'maximum_gap': 1})
    else:
        policy = rule('Spread evenly', {'kind': 'catalogue', 'id': 'spread_evenly', 'catalogue_version': 2,
            'lines': [line(PERSON['Badr'], {'max': {'value': 1}},
                settings={'spread_evenly': settings} if placement == 'line_settings' else {})],
            'settings': {'spread_evenly': settings} if placement == 'rule_settings' else {}})
        policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    original = copy.deepcopy(policy)
    data = with_tags({'policies': [policy], 'tags': {'team': {'name': 'Team', 'members': {}}}})
    [resolved] = active_rules(data['policies'], context=data)
    assert resolved['definition']['rule']['within'] == 'session'
    assert policy == original


@pytest.mark.parametrize('kind,field,many', [
    ('minimum_cover', 'credit_conditions', False), ('minimum_cover', 'selection_order', True),
    ('balance_goal', 'within', False), ('balance_goal', 'partners', False),
])
@pytest.mark.parametrize('placement', ['primitive', 'rule_settings', 'line_settings', 'source_default'])
def test_embedded_selector_tags_expand_without_writing_stored_trees(kind, field, many, placement):
    selector = who('team')
    settings = {field: [selector] if many else selector}
    if placement == 'primitive':
        policy = rule('Embedded selector', {'kind': kind, **settings})
    else:
        row = {'id': PERSON['Badr'], 'numbers': {'min': {'value': 1}}, 'settings': {}}
        if placement == 'rule_settings':
            rule_settings = {kind: settings}
        else:
            rule_settings = {}
            if placement == 'source_default':
                row['source_default'] = {'id': PERSON['Carla'], 'settings': {kind: settings}}
            else:
                row['settings'] = {kind: settings}
        policy = rule('Embedded selector', {'kind': 'catalogue', 'settings': rule_settings, 'lines': [row]})
    original = copy.deepcopy(policy)
    data = with_tags({'policies': [policy], 'tags': {
        'team': {'name': 'Team', 'members': {'people': [PERSON['Kopofiku']]}}}})
    resolved = data['policies'][0]['definition']['rule']
    if placement == 'rule_settings':
        resolved = resolved['settings'][kind]
    elif placement in {'line_settings', 'source_default'}:
        resolved = resolved['lines'][0]
        if placement == 'source_default':
            resolved = resolved['source_default']
        resolved = resolved['settings'][kind]
    tree = resolved[field][0] if many else resolved[field]
    assert tree['conditions'][0]['values'] == ['team', PERSON['Kopofiku']]
    assert policy == original


def test_every_real_shaped_legacy_family_retains_the_exact_copies_and_source_ids():
    before, after, proposal = converted(production_families())
    assert effective(before) == effective(after)
    assert {p['catalogue_id'] for p in proposal['targets']} <= {'workload_limit', 'people_per_session', 'preferences'}
    assert Counter(f['kind'] for f in proposal['scheduling_facts'])['overtime_offer'] == 4
    assert plan_v2(before['policies'], department_id='d') == proposal
    assert effective(after) == [{k: v for k, v in p.items() if k != '_v2_source'}
                                for p in active_rules(active_rules(after), context=after)]


def test_reconversion_keeps_source_identity_but_uses_fresh_destination_records():
    sources = production_families()
    first = plan_v2(sources, department_id='d')
    revised = copy.deepcopy(sources)
    for source in revised:
        source['revision'] += 2  # Reviewed conversion and undo retain both generations.
    second = plan_v2(revised, department_id='d')
    assert not first['blocked'] and not second['blocked']
    assert second == plan_v2(revised, department_id='d')
    for key, get_id in [('targets', lambda item: item['row']['id']), ('scheduling_facts', lambda item: item['id'])]:
        assert {get_id(item) for item in first[key]}.isdisjoint(get_id(item) for item in second[key])
    assert [binding['source_policy_id'] for binding in first['bindings']] == [binding['source_policy_id'] for binding in second['bindings']]
    assert [[line['id'] for line in target['payload']['definition']['rule']['lines']] for target in first['targets']] == [
        [line['id'] for line in target['payload']['definition']['rule']['lines']] for target in second['targets']]
    assert effective({'policies': revised}) == effective(after_snapshot({'policies': revised}, second))


def test_independent_duplicate_and_more_specific_sources_do_not_cross_outrank():
    general = catalogue('duties_per_week', [line(PERSON['Carla'], {'max': {'value': 5, 'level': 'mandatory'}})])
    own = catalogue('duties_per_week', [line(PERSON['Quartz'], {'max': {'value': 3, 'level': 'mandatory'}}, who(PERSON['Kopofiku']))])
    duplicate = copy.deepcopy(general)
    duplicate.update(id=PERSON['Emad'], name='Independent duplicate')
    before, after, proposal = converted([general, own, duplicate])
    assert effective(before) == effective(after)
    assert len(effective(after)) == 3
    assert len(proposal['targets']) == 1


def test_explicit_none_inherited_strength_dates_and_primitive_slot_are_preserved():
    # A block minimum must not turn a max_duties maximum into block_quota when families are merged.
    lines = [line(PERSON['Carla'], {'max': {'value': 5, 'level': 'critical'}}, settings={'max_duties': {'window': 'week'}}),
             line(PERSON['Quartz'], {'max': {'value': None, 'level': None}}, who(PERSON['Kopofiku']), settings={'max_duties': {'window': 'week'}}),
             line(PERSON['Emad'], {'max': {'value': 3, 'level': None}}, who(PERSON['Badr']),
                  settings={'max_duties': {'window': 'week'}}, starts_on='2026-09-01', ends_on='2026-10-01')]
    source = catalogue('duties_per_week', lines)
    before, after, proposal = converted([source])
    assert effective(before) == effective(after)
    assert Counter(f['person_id'] for f in proposal['scheduling_facts']) == Counter({PERSON['Kopofiku']: 1, PERSON['Badr']: 1})
    assert all(p['kind'] == 'max_duties' for p in effective(after))


@pytest.mark.parametrize('listed_only', [False, True])
def test_raw_per_person_blank_amount_and_range_target_keep_original_row_shapes(listed_only):
    source = rule('Shared quota', quota('2026-09-01', '2026-10-01', 6, 'range', 'duties', maximum_amount=12, target_amount=8),
                  per_person=[{'person_id': PERSON['Kopofiku'], 'amount': None},
                              {'person_id': PERSON['Badr'], 'amount': 3, 'settings': {'maximum_amount': 5, 'target_amount': 4}}],
                  listed_only=listed_only)
    if listed_only:
        proposal = plan_v2([source], department_id='d')
        assert proposal['blocked'][0]['rule_id'] == source['id']
        assert 'inherited default' in proposal['blocked'][0]['reason']
        with pytest.raises(ValueError, match='blocked'):
            after_snapshot({'policies': [source]}, proposal)
        return
    before, after, _ = converted([source])
    assert effective(before) == effective(after)
    assert json.dumps(effective(before), sort_keys=True) == json.dumps(effective(after), sort_keys=True)


def test_count_tree_and_path_order_not_wrapped_reordered_or_flattened():
    source = rule('Count quota', quota('2026-09-01', '2026-10-01', 4, 'maximum', 'duties'),
                  {'conditions': {'match': 'all', 'count': 'each', 'conditions': [
                      {'field': 'service_ids', 'op': 'any', 'values': list(SERVICE.values())[:2], 'count': 'each'}]}})
    before, after, _ = converted([source])
    assert effective(before) == effective(after)


def test_fair_share_person_amounts_remain_on_their_original_goal_and_long_period():
    source = rule('Annual targets', {'kind': 'balance_goal', 'metric': 'hours', 'aim': 'target', 'period': 'custom',
                  'starts_on': '2026-01-01', 'ends_on': '2028-01-01', 'person_amounts': {PERSON['Kopofiku']: 500, PERSON['Badr']: 600}},
                  severity='advisory')
    before, after, proposal = converted([source])
    assert effective(before) == effective(after)
    assert Counter(f['kind'] for f in proposal['scheduling_facts']) == Counter(target=2)
    assert not proposal['targets'][0]['row']['definition']['rule']['lines'][0]['settings']['balance_goal'].get('person_amounts')
    assert {f['data']['goal_ref']['policy_id'] for f in proposal['scheduling_facts']} == {source['id']}


def test_calendar_link_moves_exact_restriction_and_name_alone_does_not_make_calendar_data():
    named = rule('Academic name without calendar', {'kind': 'restriction'}, {'weekdays': [3]})
    linked = rule('Academic protection', {'kind': 'restriction', 'leave_buffer_days': 1}, {'weekdays': [2], 'start_time': '08:00', 'end_time': '12:00'},
                  calendar_entry_id=PERSON['Emad'])
    before, after, proposal = converted([named, linked])
    assert effective(before) == effective(after)
    assert [f['calendar_entry_id'] for f in proposal['scheduling_facts']] == [PERSON['Emad']]


def test_empty_off_and_expired_catalogue_sources_are_retained_without_new_effects():
    policies = [catalogue(e['id'], [], on=n % 2 == 0) for n, e in enumerate(RULE_CATALOGUE)]
    expired = catalogue('keep_off', [line(PERSON['Carla'], {'apply': {'value': None, 'level': 'mandatory'}}, ends_on='2026-09-18')])
    before, after, proposal = converted([*policies, expired])
    assert effective(before) == effective(after)
    assert len(proposal['bindings']) == len(policies) + 1
    assert {b['source_policy_id'] for b in proposal['bindings']} == {p['id'] for p in [*policies, expired]}


def test_blocked_department_is_not_partially_converted():
    source = rule('Simple', {'kind': 'max_duties', 'maximum': 5})
    pending = {**source, 'id': PERSON['Kopofiku'], 'status': 'proposed'}
    proposal = plan_v2([source, pending], department_id='d')
    assert proposal['blocked']
    with pytest.raises(ValueError, match='blocked'):
        after_snapshot({'policies': [source, pending]}, proposal)


def test_fact_replacement_chain_uses_current_approved_values_and_retirement_removes_source():
    source = rule('Own cap', {'kind': 'max_duties', 'maximum': 5}, {'person_id': PERSON['Kopofiku']})
    _, after, _ = converted([source])
    original = after['scheduling_facts'][0]
    replacement = copy.deepcopy(original)
    replacement.update(id=PERSON['Badr'], replaces_id=original['id'], revision=2, approved_by=PERSON['Emad'])
    replacement['data']['line']['numbers']['max']['value'] = 3
    original['status'] = 'retired'
    after['scheduling_facts'].append(replacement)
    assert effective(after)[0]['definition']['rule']['maximum'] == 3
    replacement['status'] = 'retired'
    assert effective(after) == []


def test_source_revision_advances_for_approved_edits_replacements_and_undo():
    source = rule('Own cap', {'kind': 'max_duties', 'maximum': 5}, {'person_id': PERSON['Kopofiku']})
    source['revision'] = 7
    _, after, _ = converted([source])
    original = after['scheduling_facts'][0]
    assert effective(after)[0]['revision'] == 7
    original['revision'] = 2
    original['data']['line']['numbers']['max']['value'] = 4
    assert effective(after)[0]['revision'] == 8

    replacement = copy.deepcopy(original)
    replacement.update(id=PERSON['Badr'], replaces_id=original['id'], revision=2, approved_by=PERSON['Emad'])
    replacement['data']['line']['numbers']['max']['value'] = 3
    original.update(status='retired', revision=3)
    after['scheduling_facts'].append(replacement)
    [current] = effective(after)
    assert current['revision'] == 10 and current['definition']['rule']['maximum'] == 3

    replacement.update(status='retired', revision=3)
    original.update(status='active', revision=4)
    original['data']['line']['numbers']['max']['value'] = 5
    [restored] = effective(after)
    assert restored['revision'] == 12 and restored['definition']['rule']['maximum'] == 5


def test_catalogue_replacements_follow_definition_replaces_id():
    source = rule('Department cap', {'kind': 'max_duties', 'maximum': 5})
    _, after, _ = converted([source])
    original = after['policies'][0]
    replacement = copy.deepcopy(original)
    replacement.update(id=PERSON['Badr'], revision=2)
    replacement['definition']['replaces_id'] = original['id']
    replacement['definition']['rule']['lines'][0]['numbers']['max']['value'] = 3
    original.update(status='retired', revision=2)
    after['policies'].append(replacement)
    [current] = effective(after)
    assert current['definition']['rule']['maximum'] == 3
    assert current['revision'] == source['revision'] + 2


@pytest.mark.parametrize('source_catalogue', [False, True])
def test_disabled_catalogue_default_keeps_independent_person_amounts_and_undo_advances_revision(source_catalogue):
    if source_catalogue:
        source = catalogue('duties_per_week', [line(PERSON['Carla'], {'max': {'value': 5, 'level': 'mandatory'}}),
                            line(PERSON['Quartz'], {'max': {'value': 3, 'level': 'mandatory'}}, who(PERSON['Kopofiku']))])
    else:
        source = rule('Own caps', {'kind': 'max_duties', 'maximum': 5},
                      per_person=[{'person_id': PERSON['Kopofiku'], 'amount': 3}])
    _, after, _ = converted([source])
    original = after['policies'][0]
    replacement = copy.deepcopy(original)
    replacement.update(id=PERSON['Badr'], revision=2, approved_by=PERSON['Emad'])
    replacement['definition']['replaces_id'] = original['id']
    replacement['definition']['rule']['on'] = False
    original.update(status='retired', revision=2)
    after['policies'].append(replacement)
    [own] = effective(after)
    assert own['definition']['rule']['maximum'] == 3
    assert own['revision'] == source['revision'] + 2

    replacement.update(status='retired', revision=3)
    original.update(status='active', revision=3)
    restored = effective(after)
    assert sorted(p['definition']['rule']['maximum'] for p in restored) == [3, 5]
    assert {p['revision'] for p in restored} == {source['revision'] + 4}


def test_declining_an_unapproved_catalogue_replacement_does_not_stale_source_revision():
    source = rule('Department cap', {'kind': 'max_duties', 'maximum': 5})
    _, after, _ = converted([source])
    declined = copy.deepcopy(after['policies'][0])
    declined.update(id=PERSON['Badr'], status='retired', revision=2, approved_by=None)
    declined['definition']['replaces_id'] = after['policies'][0]['id']
    after['policies'].append(declined)
    assert effective(after)[0]['revision'] == source['revision']


def test_blank_person_amount_keeps_the_live_default_when_its_catalogue_rule_is_off():
    source = rule('Inherited own cap', {'kind': 'max_duties', 'maximum': 5},
                  per_person=[{'person_id': PERSON['Kopofiku'], 'amount': None}])
    before, after, _ = converted([source])
    assert effective(before) == effective(after)
    after['policies'][0]['definition']['rule']['on'] = False
    after['policies'][0]['revision'] = 2
    [own] = effective(after)
    assert own['definition']['rule']['maximum'] == 5
    assert own['template_line']['amount'] is None
    assert own['revision'] == source['revision'] + 1


def test_native_fact_scope_always_includes_its_person():
    fact = {'id': PERSON['Badr'], 'person_id': PERSON['Kopofiku'], 'status': 'active', 'on': True, 'department_id': 'd',
            'data': {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
                     'line': line(PERSON['Carla'], {'max': {'value': 5, 'level': 'mandatory'}}, settings={'max_duties': {'window': 'week'}})}}
    policy = fact_policy(fact)
    assert policy['definition']['rule']['lines'][0]['who']['conditions'][0]['values'] == [PERSON['Kopofiku']]


def test_approved_raw_fact_edit_changes_its_scope_level_and_period_without_changing_source_identity():
    source = rule('Own cap', {'kind': 'max_duties', 'maximum': 5}, {'person_id': PERSON['Kopofiku'], 'service_id': SERVICE['Physical UCC']})
    _, after, _ = converted([source])
    clinical = after['scheduling_facts'][0]['data']['line']
    clinical['numbers']['max']['level'] = 'high'
    clinical['settings']['max_duties']['window'] = 'day'
    clinical['where'] = {'match': 'all', 'conditions': [{'field': 'service_ids', 'op': 'any', 'values': [SERVICE['Clinic A']]}]}
    [copy] = effective(after)
    assert copy['id'] == source['id'] and copy['revision'] == source['revision']
    assert copy['definition']['severity'] == 'advisory' and copy['definition']['strength'] == 'high'
    assert copy['definition']['rule']['window'] == 'day'
    assert copy['definition']['scope']['service_ids'] == [SERVICE['Clinic A']]


def test_retiring_first_catalogue_fact_retains_remaining_original_line_index():
    source = catalogue('duties_per_week', [line(PERSON['Carla'], {'max': {'value': 5, 'level': 'mandatory'}}, who(PERSON['Kopofiku'])),
                                          line(PERSON['Quartz'], {'max': {'value': 3, 'level': 'mandatory'}}, who(PERSON['Badr']))])
    _, after, _ = converted([source])
    after['scheduling_facts'][0]['status'] = 'retired'
    [copy] = effective(after)
    assert copy['template_line']['id'] == PERSON['Quartz']
    assert copy['template_line']['line'] == 1


def test_retiring_middle_raw_person_row_preserves_original_index_and_remaining_scope():
    source = rule('Own caps', {'kind': 'max_duties', 'maximum': 5},
                  per_person=[{'person_id': PERSON['Kopofiku'], 'amount': 3}, {'person_id': PERSON['Carla'], 'amount': 4},
                              {'person_id': PERSON['Badr'], 'amount': 2}], listed_only=False)
    before, after, _ = converted([source])
    after['scheduling_facts'][1]['status'] = 'retired'
    original = next(p for p in effective(before) if p.get('template_line', {}).get('person_id') == PERSON['Badr'])
    current = next(p for p in effective(after) if p.get('template_line', {}).get('person_id') == PERSON['Badr'])
    assert current == original


def test_persisted_rank_line_retains_null_settings_and_float_amount_shape():
    source = rule('Rank cap', {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                  listed_only=True, per_person=[{'category_ids': [PERSON['Carla']], 'amount': 3}])
    source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before, after, _ = converted([source])
    assert effective(before) == effective(after)
    assert json.dumps(effective(before), sort_keys=True) == json.dumps(effective(after), sort_keys=True)
    prior, current = effective(before)[0], effective(after)[0]
    assert isinstance(prior['template_line']['amount'], float) and isinstance(current['template_line']['amount'], float)
    assert prior['template_line']['settings'] is None and current['template_line']['settings'] is None


@pytest.mark.parametrize('catalogue_source', [False, True])
@pytest.mark.parametrize('selected', ['priority', 'session_priority'])
def test_both_allocation_priorities_remain_on_one_exact_primitive(catalogue_source, selected):
    priorities = {'priority': -1, 'session_priority': 2}
    other = next(key for key in priorities if key != selected)
    if catalogue_source:
        entry = 'allocation_order' if selected == 'session_priority' else 'preferences'
        source = catalogue(entry, [line(PERSON['Carla'], {selected: {'value': priorities[selected], 'level': 'medium'}},
                                       settings={'allocation_preference': {other: priorities[other]}})])
    else:
        source = rule('Both priorities', {'kind': 'allocation_preference', **priorities},
                      severity='advisory', line_number=selected)
        source['definition'] = PolicyDefinition.model_validate(source['definition'], context={'historical': True}).model_dump(mode='json')
    before, after, _ = converted([source])
    assert json.dumps(effective(before), sort_keys=True) == json.dumps(effective(after), sort_keys=True)
    [copy] = effective(after)
    assert {key: copy['definition']['rule'][key] for key in priorities} == priorities


def test_selected_allocation_number_omits_its_unused_common_setting_even_when_explicit_none():
    source = catalogue('preferences', [line(PERSON['Carla'], {'priority': {'value': 3, 'level': 'medium'}}),
                       line(PERSON['Quartz'], {'priority': {'value': None, 'level': None}}, who(PERSON['Kopofiku']))],
                       settings={'allocation_preference': {'priority': 5, 'session_priority': 2}})
    before, after, proposal = converted([source])
    assert json.dumps(effective(before), sort_keys=True) == json.dumps(effective(after), sort_keys=True)
    assert all('priority' not in row['settings']['allocation_preference']
               for row in proposal['targets'][0]['row']['definition']['rule']['lines'])


def test_raw_person_line_accepts_new_approved_settings_without_changing_its_siblings():
    source = rule('Own caps', {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                  per_person=[{'person_id': PERSON['Kopofiku'], 'amount': 3}, {'person_id': PERSON['Badr'], 'amount': 2}])
    before, after, _ = converted([source])
    clinical = after['scheduling_facts'][0]
    clinical['revision'] += 1
    clinical['data']['line']['settings']['max_duties']['window'] = 'day'
    copies = effective(after)
    own = next(p for p in copies if p.get('template_line', {}).get('person_id') == PERSON['Kopofiku'])
    sibling = next(p for p in copies if p.get('template_line', {}).get('person_id') == PERSON['Badr'])
    assert own['definition']['rule']['window'] == 'day'
    assert own['definition']['rule']['maximum'] == 3
    prior_sibling = next(p for p in effective(before) if p.get('template_line', {}).get('person_id') == PERSON['Badr'])
    assert sibling['definition'] == prior_sibling['definition']
    assert sibling['template_line'] == prior_sibling['template_line']


def test_native_personal_target_merges_exact_goal_instead_of_creating_an_independent_goal():
    goal = rule('Hours target', {'kind': 'balance_goal', 'metric': 'hours', 'aim': 'target', 'amount': 10}, severity='advisory')
    fact = {'id': PERSON['Badr'], 'person_id': PERSON['Kopofiku'], 'status': 'active', 'on': True, 'department_id': 'd', 'kind': 'target',
            'data': {'catalogue_id': 'fair_share', 'catalogue_version': 2,
                     'line': line(PERSON['Carla'], {'target': {'value': 7, 'level': 'medium'}}, settings={'balance_goal': {'metric': 'hours', 'aim': 'target'}}),
                     'goal_ref': {'policy_id': goal['id'], 'revision': 1, 'line_id': None, 'slot': 'target'}}}
    [copy] = effective({'policies': [goal], 'scheduling_facts': [fact]})
    assert copy['id'] == goal['id']
    assert copy['definition']['rule']['person_amounts'] == {PERSON['Kopofiku']: 7}


def test_native_mixed_safety_catalogue_only_nonsafety_numbers_outrank():
    broad = line(PERSON['Carla'], {'max': {'value': 5, 'level': 'mandatory'}}, settings={'max_duties': {'window': 'week'}})
    own = line(PERSON['Quartz'], {'max': {'value': 3, 'level': 'mandatory'}}, who(PERSON['Kopofiku']), settings={'max_duties': {'window': 'week'}})
    source = rule('Workload', {'kind': 'catalogue', 'id': 'workload_limit', 'catalogue_version': 2, 'lines': [broad, own]})
    assert outranking_by_number(source) == {(0, 'max'): [1]}
    source['definition']['rule']['lines'] = [line(PERSON['Carla'], {'apply': {'value': None, 'level': 'mandatory'}}, settings={'restriction': {}}),
                                           line(PERSON['Quartz'], {'apply': {'value': None, 'level': 'mandatory'}}, who(PERSON['Kopofiku']), settings={'restriction': {}})]
    assert outranking_by_number(source) == {}


def test_native_v2_rule_map_decodes_version_and_uses_actual_primitive_safety(h):
    from .test_rule_map import base, entry, mapped, rule as map_rule
    _, data = base(h)
    def native(ident, entry_id, numbers, settings):
        return map_rule(data, ident, {'kind': 'catalogue', 'id': entry_id, 'catalogue_version': 2,
                        'lines': [line(PERSON['Carla'], numbers, settings=settings)]})
    result = mapped(data,
                    native('cap', 'workload_limit', {'max': {'value': 5, 'level': 'mandatory'}}, {'max_duties': {'window': 'week'}}),
                    native('off', 'workload_limit', {'apply': {'value': None, 'level': 'mandatory'}}, {'restriction': {}}),
                    native('rest', 'duty_spacing', {'min': {'value': 11, 'level': 'mandatory'}}, {'min_rest': {}}),
                    native('avoid', 'duty_spacing', {'apply': {'value': None, 'level': 'medium'}},
                           {'shift_succession': {'mode': 'avoid', 'from_type_ids': [h.seed['type']], 'to_type_ids': [h.seed['type']]}}))
    assert entry(result, 'cap')['catalogue_id'] == 'workload_limit' and not entry(result, 'cap')['safety']
    assert entry(result, 'off')['safety']
    assert entry(result, 'rest')['catalogue_id'] == 'duty_spacing' and entry(result, 'rest')['safety']
    assert not entry(result, 'avoid')['safety']
