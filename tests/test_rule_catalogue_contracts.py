"""The rule catalogue contract (docs/design/rule-catalogue.md §1–2): entries, slots, lines and their validation."""
import uuid
from typing import get_args

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from packages.contracts.rule_catalogue import CATALOGUE_BY_ID, FAMILIES, LEVELS, NUMBER_ORDER, RULE_CATALOGUE

MODELS = {get_args(m.model_fields['kind'].annotation)[0]: m for m in get_args(get_args(s.RulePrimitive)[0])}


def rule(entry, *lines, **more):
    return {'rule': {'kind': 'catalogue', 'id': entry, 'lines': [{'id': str(uuid.uuid4()), **line} for line in lines], **more}}


def test_every_entry_names_real_kinds_fields_and_families():
    assert len(RULE_CATALOGUE) == 28 and len(CATALOGUE_BY_ID) == 28
    assert CATALOGUE_BY_ID['preferences']['name'] == 'Preferred people'
    families = {f for f, _ in FAMILIES}
    for entry in RULE_CATALOGUE:
        allowed = set(LEVELS) | ({'off'} if entry['id'] == 'fair_share' else set())
        assert entry['family'] in families and entry['numbers'] and set(entry['levels']) <= allowed
        assert list(entry['numbers']) == [k for k in NUMBER_ORDER if k in entry['numbers']]
        for key, slots in entry['numbers'].items():
            for slot in slots:
                model = MODELS[slot['kind']]
                assert slot['kind'] != 'catalogue'
                assert (slot['field'] is None) == (key == 'apply'), (entry['id'], key)
                assert slot['field'] is None or slot['field'] in model.model_fields
                assert set(slot['fixed']) <= set(model.model_fields)
        assert entry['safety'] == any(k in s.NEVER_OVERRIDDEN for slots in entry['numbers'].values() for k in (x['kind'] for x in slots))
        for kind, fields in entry['line_settings'].items():
            assert set(fields) <= set(MODELS[kind].model_fields) - {'kind'}
        if entry['default_line']:
            s.PolicyDefinition.model_validate(rule(entry['id'], entry['default_line']))


def test_safety_entries_are_the_never_overridden_kinds():
    safe = {e['id'] for e in RULE_CATALOGUE if e['safety']}
    assert {'team_leads', 'people_per_session', 'kind_per_session', 'supervision', 'rest_between_duties', 'not_after', 'companion_cover',
            'protected_time', 'keep_off'} == safe


def test_count_sides_follow_the_shared_workload_count_contract():
    for entry in RULE_CATALOGUE:
        kinds = {slot['kind'] for slots in entry['numbers'].values() for slot in slots}
        if kinds & s.WORKLOAD_COUNT_KINDS:
            assert entry['count_sides'] == ['where']
    assert CATALOGUE_BY_ID['duties_per_week']['count_sides'] == ['where']
    assert CATALOGUE_BY_ID['fair_share']['count_sides'] == ['where']
    assert CATALOGUE_BY_ID['not_after']['count_sides'] == ['where']
    assert CATALOGUE_BY_ID['people_per_session']['count_sides'] == ['who', 'where']
    assert CATALOGUE_BY_ID['together_apart']['count_sides'] == ['who', 'where']
    for name in ('protected_time', 'keep_off', 'allocation_order', 'preferences', 'overtime_offers'):
        assert CATALOGUE_BY_ID[name]['count_sides'] == []


def test_a_catalogue_rule_validates_its_lines():
    value = s.PolicyDefinition.model_validate(rule('duties_per_week', {
        'numbers': {'min': {'value': 2}, 'max': {'value': 5, 'level': 'mandatory'}, 'target': {'value': 4, 'level': 'high'}},
        'who': {'conditions': [{'field': 'tags', 'values': ['vip']}]},
        'where': {'conditions': [{'field': 'service_ids', 'values': [str(uuid.uuid4())]}]},
        'settings': {'block_quota': {'window_alignment': 'calendar_week'}}, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}))
    assert value.rule.on and value.rule.lines[0].numbers['target'].level == 'high'


@pytest.mark.parametrize('definition, message', [
    (rule('nonsense', {'numbers': {'max': {'value': 1}}}), 'Not a rule in the catalogue'),
    (rule('duties_per_week', {'numbers': {'apply': {'value': None}}}), 'has no apply number'),
    (rule('keep_off', {'numbers': {'apply': {'value': 3}}}), 'this rule has no number'),
    (rule('duties_per_week', {'numbers': {'target': {'value': 3}}}), 'the target cannot be mandatory'),
    (rule('days_off_together', {'numbers': {'min': {'value': 2}}}), 'the min cannot be mandatory'),
    # Safety kinds are always mandatory. Keep the same invalid supervision case under the more specific error.
    pytest.param(rule('supervision', {'numbers': {'max': {'value': 2, 'level': 'high'}}}),
                 'Safety rules are always mandatory', id='definition5-the max cannot be high'),
    (rule('duties_per_week', {'numbers': {'max': {'value': 5}}, 'settings': {'min_rest': {'hours': 3}}}), 'has no settings for min_rest'),
    (rule('duties_per_week', {'numbers': {'max': {'value': 5}}}, settings={'restriction': {}}), 'has no settings for restriction'),
    (rule('duties_per_week', {'numbers': {'max': {'value': 5}}, 'where': {'conditions': [{'field': 'tags', 'values': ['x']}]}}),
     'Where cannot filter on: tags'),
    (rule('duties_per_week', {'numbers': {'max': {'value': 5}}, 'who': {'conditions': [{'field': 'service_ids', 'values': ['x']}]}}),
     'Who cannot filter on: service_ids'),
    (rule('duties_per_week', {'numbers': {'max': {'value': 5}}, 'starts_on': '2026-10-02', 'ends_on': '2026-10-01'}), 'must end after'),
    ({**rule('duties_per_week', {'numbers': {'max': {'value': 5}}}), 'severity': 'advisory'}, 'says who, where, when and how much on its lines'),
    ({**rule('duties_per_week', {'numbers': {'max': {'value': 5}}}), 'scope': {'weekdays': [0]}}, 'on its lines'),
])
def test_invalid_catalogue_rules_are_refused(definition, message):
    with pytest.raises(ValidationError, match=message):
        s.PolicyDefinition.model_validate(definition)


def test_line_ids_are_unique():
    line = {'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 5}}}
    with pytest.raises(ValidationError, match='its own id'):
        s.PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [line, line]}})


def test_line_key_is_the_same_line_whatever_the_order():
    a = {'who': {'conditions': [{'field': 'tags', 'values': ['B', 'a']}, {'field': 'category_ids', 'values': ['y', 'x']}]},
         'weekdays': [2, 0, 1], 'numbers': {'max': {'value': 3}}}
    b = {'who': {'conditions': [{'field': 'category_ids', 'values': ['x', 'y']}, {'field': 'tags', 'values': ['a', 'b']}]},
         'weekdays': [0, 1, 2], 'numbers': {'max': {'value': 9}}, 'starts_on': '2026-01-01'}
    assert s.line_key('duties_per_week', a) == s.line_key('duties_per_week', b)
    assert s.line_key('duties_per_week', a) != s.line_key('duties_per_month', a)
    assert s.line_key('duties_per_week', a) != s.line_key('duties_per_week', {**a, 'start_time': '08:00'})


@pytest.mark.parametrize('field,values', [('tags', ['NIGHT', 'other']),
                                         ('category_ids', ['rank-b', 'rank-a']),
                                         ('service_ids', ['clinic-b', 'clinic-a'])])
@pytest.mark.parametrize('legacy_op,typed_op', [('any', 'any'), ('any', 'is'),
                                              ('none', 'none'), ('none', 'is_not')])
@pytest.mark.parametrize('wrapped', [False, True])
def test_line_key_matches_legacy_and_typed_literal_choices(field, values, legacy_op, typed_op, wrapped):
    part = 'where' if field == 'service_ids' else 'who'
    values = values if wrapped else values[:1]
    legacy = {part: {'conditions': [{'field': field, 'op': legacy_op, 'values': values}]}}
    selected = list(reversed(values))
    if field == 'tags':
        selected = [value.lower() for value in selected]
    typed = {part: {'conditions': [{'field': field, 'op': typed_op,
                                   'value': {'kind': 'absolute', 'value': selected} if wrapped else selected[0]}]}}
    for source in (legacy, typed):
        parsed = s.RuleLine.model_validate({'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 3}}, **source})
        assert s.line_key('duties_per_week', parsed) == s.line_key('duties_per_week', legacy)


def test_generated_contracts_carry_the_catalogue():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / 'apps/web/src/generated.ts').read_text(encoding='utf-8')
    assert 'export const RULE_CATALOGUE: readonly RuleCatalogueEntry[]' in text and 'export interface RuleLine ' in text
    assert 'export type RuleLine = RuleLine;' not in text
    assert '"people_per_session"' in text and 'export const RULE_FAMILIES' in text
