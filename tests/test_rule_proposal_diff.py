"""Proposals preserve historical lines and describe only the operator's actual changes."""
import copy
import uuid

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.rule_catalogue import _diff
from packages.contracts.schemas import RuleLine
from .test_rule_catalogue_api import approve, entry, propose


def scope(field, value):
    return {'conditions': [{'field': field, 'values': [value]}]}


def legacy_rule(h, catalogue_id, lines, settings=None):
    """An independently approved rule whose persisted lines predate newer schema defaults."""
    live = approve(h, propose(h, catalogue_id, lines, settings=settings or {})['record'])
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.get(m.Policy, live['id'])
        definition = copy.deepcopy(policy.definition)
        definition['rule']['lines'] = copy.deepcopy(lines)
        policy.definition = definition
    assert entry(h, catalogue_id)['rule']['definition']['rule']['lines'] == lines
    return live


def weekly_lines(h):
    # Fourteen distinct historical lines, with nested conditions and primitive settings
    # but without the source metadata and other defaults introduced by newer schemas.
    return [{
        'id': str(uuid.uuid4()),
        'where': scope('service_ids', h.seed['services'][n // 7]),
        'weekdays': [n % 7],
        'numbers': {'max': {'value': 5}},
        'settings': {'max_duties': {'window': 'week'}},
        'note': "From 'Weekly duty ceiling' (synthetic fixture)",
    } for n in range(14)]


def test_proposing_one_added_line_preserves_all_fourteen_legacy_lines(h):
    before = weekly_lines(h)
    assert 'source_default' not in before[0] and 'source_bindings' not in before[0]
    assert 'match' not in before[0]['where'] and 'op' not in before[0]['where']['conditions'][0]
    assert 'level' not in before[0]['numbers']['max']
    live = legacy_rule(h, 'duties_per_week', before)
    added = {
        'id': str(uuid.uuid4()),
        'where': {'match': 'all', 'conditions': [
            {'field': 'service_ids', 'values': [h.seed['services'][0]]},
            {'field': 'assignment_type_ids', 'values': [h.seed['type']]},
        ]},
        'numbers': {'max': {'value': 2, 'level': 'mandatory'}},
    }
    record = propose(h, 'duties_per_week', [*before, added],
                     policy_id=live['id'], revision=live['revision'])['record']

    assert record['definition']['rule']['lines'][:14] == before
    assert len(record['definition']['rule']['lines']) == 15
    waiting = entry(h, 'duties_per_week')['proposed']
    assert waiting['definition']['rule']['lines'][:14] == before
    assert waiting['diff'] == {
        'added': [added['id']], 'removed': [], 'changed': [], 'on': None, 'settings': None,
    }
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, record['id']).definition['rule']['lines'][:14] == before
        assert db.get(m.Policy, live['id']).definition['rule']['lines'] == before


def test_an_overlap_acknowledged_on_sparse_legacy_lines_stays_acknowledged_after_activation(h):
    before = weekly_lines(h)
    live = legacy_rule(h, 'duties_per_week', before)
    added = {
        'id': str(uuid.uuid4()), 'who': scope('person_ids', h.seed['people'][0]),
        'where': before[0]['where'], 'weekdays': before[0]['weekdays'],
        'numbers': {'max': {'value': 2, 'level': 'mandatory'}},
    }
    waiting = propose(h, 'duties_per_week', [*before, added],
                      policy_id=live['id'], revision=live['revision'], acknowledged=[before[0]['id']])['record']
    current = approve(h, waiting)
    assert current['definition']['rule']['lines'][:14] == before
    issues = h.get('auditor', '/api/rules/catalogue')['attention']
    overlap = next(x for x in issues if x['kind'] == 'overlap' and x['facts']['loser'] == before[0]['id'])
    assert overlap['acknowledged']['note'] == 'When proposed: Weekly duty caps'
    assert overlap['acknowledged']['by']


def test_sparse_resubmission_preserves_existing_defaults_and_only_edits_the_note(h):
    submitted = weekly_lines(h)
    live = approve(h, propose(h, 'duties_per_week', submitted)['record'])
    before = live['definition']['rule']['lines']
    assert before[0]['source_bindings'] == {} and before[0]['where']['match'] == 'all'
    submitted[0]['note'] = 'Reviewed weekly limit'
    waiting = propose(h, 'duties_per_week', submitted,
                      policy_id=live['id'], revision=live['revision'])['record']
    expected = copy.deepcopy(before)
    expected[0]['note'] = submitted[0]['note']
    assert waiting['definition']['rule']['lines'] == expected
    assert entry(h, 'duties_per_week')['proposed']['diff']['changed'] == [before[0]['id']]


def test_default_normalization_does_not_hide_real_omission_or_null_edits(h):
    before = weekly_lines(h)
    before[0]['numbers']['max']['level'] = None  # Explicit inheritance differs from omitted mandatory.
    live = legacy_rule(h, 'duties_per_week', before)
    edited = copy.deepcopy(before)
    del edited[0]['numbers']['max']['level']
    del edited[1]['note']
    edited[2]['numbers']['max']['level'] = None
    waiting = propose(h, 'duties_per_week', edited,
                      policy_id=live['id'], revision=live['revision'])['record']
    after = waiting['definition']['rule']['lines']
    assert after[0]['numbers']['max']['level'] == 'mandatory'
    assert after[1]['note'] == ''
    assert after[2]['numbers']['max']['level'] is None
    assert after[3:] == before[3:]
    assert entry(h, 'duties_per_week')['proposed']['diff']['changed'] == [line['id'] for line in before[:3]]


@pytest.mark.parametrize('field', ['note', 'settings', 'numbers', 'where'])
def test_a_real_line_edit_changes_only_that_legacy_line(h, field):
    before = weekly_lines(h)
    live = legacy_rule(h, 'duties_per_week', before)
    edited = copy.deepcopy(before)
    changes = {
        'note': 'Reviewed weekly limit',
        'settings': {'max_duties': {'window': 'week', 'include_overtime': False}},
        'numbers': {'max': {'value': 4, 'level': 'mandatory'}},
        'where': {'match': 'all', 'conditions': [
            {'field': 'service_ids', 'values': [h.seed['services'][0]]},
            {'field': 'assignment_type_ids', 'values': [h.seed['type']]},
        ]},
    }
    edited[0][field] = changes[field]
    record = propose(h, 'duties_per_week', edited,
                     policy_id=live['id'], revision=live['revision'])['record']

    assert record['definition']['rule']['lines'][0][field] == RuleLine.model_validate(edited[0]).model_dump(mode='json')[field]
    assert record['definition']['rule']['lines'][1:] == before[1:]
    assert entry(h, 'duties_per_week')['proposed']['diff'] == {
        'added': [], 'removed': [], 'changed': [before[0]['id']], 'on': None, 'settings': None,
    }


@pytest.mark.parametrize('existing_rule', [False, True])
def test_rest_proposal_from_zero_to_one_line_and_later_edit(h, existing_rule):
    live = legacy_rule(h, 'rest_between_duties', []) if existing_rule else None
    added = {'id': str(uuid.uuid4()), 'numbers': {'min': {'value': 11}}}
    proposed = propose(h, 'rest_between_duties', [added],
                       policy_id=live['id'] if live else None,
                       revision=live['revision'] if live else None)['record']
    assert len(proposed['definition']['rule']['lines']) == 1
    assert proposed['definition']['rule']['lines'][0]['numbers'] == {'min': {'value': 11, 'level': 'mandatory'}}
    assert entry(h, 'rest_between_duties')['proposed']['diff'] == {
        'added': [added['id']], 'removed': [], 'changed': [], 'on': None, 'settings': None,
    }
    current = approve(h, proposed)
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers']['min']['value'] = 12
    edited = propose(h, 'rest_between_duties', lines,
                     policy_id=current['id'], revision=current['revision'])['record']
    assert edited['definition']['rule']['lines'][0]['numbers']['min']['value'] == 12
    assert entry(h, 'rest_between_duties')['proposed']['diff'] == {
        'added': [], 'removed': [], 'changed': [added['id']], 'on': None, 'settings': None,
    }


def test_line_diff_ignores_dictionary_key_order_but_stays_strict():
    before = {
        'id': str(uuid.uuid4()), 'note': 'Existing note', 'weekdays': [0, 1],
        'numbers': {'max': {'value': 5, 'level': 'mandatory'}},
        'settings': {'max_duties': {'window': 'week', 'include_overtime': True}},
    }
    reordered = {key: before[key] for key in reversed(before)}
    reordered['numbers'] = {'max': {'level': 'mandatory', 'value': 5}}
    reordered['settings'] = {'max_duties': {'include_overtime': True, 'window': 'week'}}
    assert _diff({'lines': [before]}, {'lines': [reordered]})['changed'] == []

    for field, value in (
        ('note', 'Changed note'),
        ('settings', {'max_duties': {'window': 'week', 'include_overtime': False}}),
        ('numbers', {'max': {'value': 4, 'level': 'mandatory'}}),
        ('weekdays', [1, 0]),
        ('source_bindings', {}),
    ):
        after = {**before, field: value}
        assert _diff({'lines': [before]}, {'lines': [after]})['changed'] == [before['id']]
