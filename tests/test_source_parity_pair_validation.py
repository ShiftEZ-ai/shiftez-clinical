"""Paired settings validate after inheritance and identify the field an operator can fix."""
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import BalanceGoal, CatalogueRule, MinimumCover, PolicyDefinition
from .test_source_parity_api import propose_current


def selector():
    return {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [str(uuid4())]}]}


def catalogue(kind, settings, *, global_settings=False, lines=True, number=None):
    entry, key = ('people_per_session', 'min') if kind == 'minimum_cover' else ('fair_share', 'apply')
    own = {'id': str(uuid4()), 'numbers': {number or key: {'value': None if key == 'apply' else 1,
        'level': 'medium' if key == 'apply' else 'mandatory'}}}
    body = {'kind': 'catalogue', 'catalogue_version': 2, 'id': entry, 'lines': [own] if lines else []}
    if global_settings:
        body['settings'] = {kind: settings}
    else:
        own['settings'] = {kind: settings}
    return body


CASES = [
    ('minimum_cover', lambda: {'credit_conditions': selector()}, 'credit_minimum_others'),
    ('minimum_cover', lambda: {'credit_minimum_others': 1}, 'credit_conditions'),
    ('balance_goal', lambda: {'metric': 'duties', 'distribution': 'share'}, 'within'),
    ('balance_goal', lambda: {'metric': 'duties', 'within': selector()}, 'within'),
    ('balance_goal', lambda: {'metric': 'duties', 'target_share': 0}, 'target_share'),
]


@pytest.mark.parametrize('kind,settings,field', CASES)
@pytest.mark.parametrize('global_settings', [False, True])
def test_catalogue_pairs_reject_half_settings_at_the_editable_field(kind, settings, field, global_settings):
    body = catalogue(kind, settings(), global_settings=global_settings)
    with pytest.raises(ValidationError) as raised:
        PolicyDefinition.model_validate({'rule': body})
    prefix = ('rule', 'catalogue') + (() if global_settings else ('lines', 0)) + ('settings', kind)
    assert (*prefix, field) in {error['loc'] for error in raised.value.errors()}


@pytest.mark.parametrize('kind,settings,field', CASES)
def test_global_pair_is_validated_without_an_operative_primitive(kind, settings, field):
    for empty in (False, True):
        body = catalogue(kind, settings(), global_settings=True, lines=not empty,
                         number='max' if kind == 'minimum_cover' else None)
        with pytest.raises(ValidationError) as raised:
            CatalogueRule.model_validate(body)
        assert ('settings', kind, field) in {error['loc'] for error in raised.value.errors()}


@pytest.mark.parametrize('kind,global_values,own_values', [
    ('minimum_cover', {'credit_conditions': selector()}, {'credit_minimum_others': 1}),
    ('minimum_cover', {'credit_minimum_others': 1}, {'credit_conditions': selector()}),
    ('minimum_cover', {'credit_conditions': selector(), 'credit_minimum_others': 1},
        {'credit_conditions': None, 'credit_minimum_others': 0}),
    ('balance_goal', {'metric': 'duties', 'distribution': 'share'}, {'within': selector()}),
    ('balance_goal', {'metric': 'duties', 'within': selector()}, {'distribution': 'share', 'target_share': 0}),
    ('balance_goal', {'metric': 'duties', 'distribution': 'share', 'within': selector(), 'target_share': 0.2},
        {'distribution': 'people', 'within': None, 'target_share': 0.5}),
])
def test_valid_global_line_pairs_and_explicit_neutral_overrides_preserve_storage(kind, global_values, own_values):
    body = catalogue(kind, own_values)
    body['settings'] = {kind: global_values}
    before = deepcopy(body)
    saved = CatalogueRule.model_validate(body).model_dump(mode='json')
    assert saved['settings'] == before['settings']
    assert saved['lines'][0]['settings'] == before['lines'][0]['settings']
    assert body == before


def test_unrelated_line_kind_does_not_break_a_valid_inherited_pair():
    body = catalogue('minimum_cover', {'credit_minimum_others': 1})
    body['settings'] = {'minimum_cover': {'credit_conditions': selector()}}
    body['lines'].append({'id': str(uuid4()), 'numbers': {'max': {'value': 3}}})
    assert len(CatalogueRule.model_validate(body).lines) == 2


def test_primitive_pair_guards_preserve_neutral_defaults_and_share_default_ratio():
    assert MinimumCover.model_validate({'kind': 'minimum_cover', 'minimum': 1}).credit_minimum_others == 0
    share = BalanceGoal.model_validate({'kind': 'balance_goal', 'metric': 'duties',
        'distribution': 'share', 'within': selector()})
    assert share.target_share == 0.5
    assert BalanceGoal.model_validate({'kind': 'balance_goal', 'metric': 'duties', 'within': None,
        'target_share': 0.5}).distribution == 'people'
    for model, body, field in [
        (MinimumCover, {'kind': 'minimum_cover', 'minimum': 1, 'credit_minimum_others': 1}, 'credit_conditions'),
        (BalanceGoal, {'kind': 'balance_goal', 'metric': 'duties', 'distribution': 'share'}, 'within'),
    ]:
        with pytest.raises(ValidationError) as raised:
            model.model_validate(body)
        assert (field,) in {error['loc'] for error in raised.value.errors()}


def test_source_default_uses_the_same_inherited_settings_as_runtime():
    body = catalogue('minimum_cover', {'credit_minimum_others': 1})
    body['settings'] = {'minimum_cover': {'credit_conditions': selector()}}
    body['lines'][0]['source_default'] = {'id': str(uuid4()), 'numbers': {'min': {'value': 1}},
        'settings': {'minimum_cover': {}}}
    before = deepcopy(body)
    with pytest.raises(ValidationError) as raised:
        CatalogueRule.model_validate(body)
    assert ('lines', 0, 'source_default', 'settings', 'minimum_cover', 'credit_minimum_others') in {
        error['loc'] for error in raised.value.errors()}
    assert body == before
    body['settings']['minimum_cover']['credit_minimum_others'] = 1
    saved = CatalogueRule.model_validate(body).model_dump(mode='json')
    assert saved['lines'][0]['source_default']['settings'] == {'minimum_cover': {}}
    body['lines'][0]['source_default']['settings']['minimum_cover']['credit_conditions'] = None
    with pytest.raises(ValidationError) as raised:
        CatalogueRule.model_validate(body)
    assert ('lines', 0, 'source_default', 'settings', 'minimum_cover', 'credit_conditions') in {
        error['loc'] for error in raised.value.errors()}


def counts(h):
    with transaction(h.app.state.engine) as db:
        return tuple(db.scalar(select(func.count()).select_from(model))
            for model in (m.Policy, m.DefinitionHistory, m.AuditEvent))


@pytest.mark.parametrize('kind,settings,field', CASES)
@pytest.mark.parametrize('global_settings', [False, True])
def test_check_and_propose_report_the_same_field_without_writing(h, kind, settings, field, global_settings):
    body = catalogue(kind, settings(), global_settings=global_settings)
    h.client('coordinator')
    before = counts(h)
    response = h.client('coordinator').post('/api/rules/catalogue/' + body['id'] + '/check', json=body)
    assert response.status_code == 200, response.text
    issues = response.json()['issues']
    prefix = 'rule.catalogue.' + ('' if global_settings else 'lines.0.') + 'settings.' + kind + '.'
    assert any(issue['field'] == prefix + field for issue in issues)
    error = propose_current(h, {'catalogue_id': body['id'], 'catalogue_version': 2,
        'settings': body.get('settings', {}), 'lines': body['lines'],
        'reason': 'Synthetic incomplete configuration'}, status=422)['error']
    assert error['code'] == 'SOLVER_RULE_INCOMPATIBLE'
    assert error['details'] == issues
    assert counts(h) == before


@pytest.mark.parametrize('global_settings', [False, True])
@pytest.mark.parametrize('kind,settings,field', [
    ('minimum_cover', {'credit_minimum_others': -1}, 'credit_minimum_others'),
    ('balance_goal', {'target_share': 1.1}, 'target_share'),
])
def test_catalogue_ranges_keep_their_setting_path(kind, settings, field, global_settings):
    with pytest.raises(ValidationError) as raised:
        CatalogueRule.model_validate(catalogue(kind, settings, global_settings=global_settings))
    prefix = (() if global_settings else ('lines', 0)) + ('settings', kind)
    assert (*prefix, field) in {error['loc'] for error in raised.value.errors()}
