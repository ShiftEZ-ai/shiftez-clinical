"""One catalogue range contract and conservative staffing-pool warnings."""
import pytest
from pydantic import ValidationError
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import CatalogueRule, PolicyDefinition
from packages.contracts.rule_catalogue import RULE_CATALOGUE, RULE_CATALOGUE_V2, catalogue_entry, number_range_errors
from packages.domain.rota_summary import rota_summary, fix_list_context
from packages.domain.validation import evaluate
from .test_rule_catalogue_api import line, propose, approve, entry
from .test_rule_catalogue_guard import catalogue


@pytest.mark.parametrize('version,entry_id', [(v, e['id']) for v, entries in [(1, RULE_CATALOGUE), (2, RULE_CATALOGUE_V2)]
    for e in entries if 'min' in e['numbers'] and 'max' in e['numbers']])
def test_every_catalogue_range_uses_the_same_contract(version, entry_id):
    entry = catalogue_entry(entry_id, version)
    level = 'medium' if 'medium' in entry['levels'] else 'mandatory'
    with pytest.raises(ValidationError) as caught:
        CatalogueRule.model_validate({'kind': 'catalogue', 'id': entry_id, 'catalogue_version': version,
            'lines': [line(numbers={'min': {'value': 4, 'level': level}, 'max': {'value': 1, 'level': level}})]})
        assert any(e['type'] == 'invalid_number_range' and e['loc'] == ('lines', 0, 'numbers', 'max')
               for e in caught.value.errors())


@pytest.mark.parametrize('numbers', [{'min': 4, 'target': 2}, {'target': 4, 'max': 2}, {'min': 1, 'target': 0, 'max': 2}])
def test_targets_and_zero_bounds_are_ordered(numbers):
    with pytest.raises(ValidationError) as caught:
        CatalogueRule.model_validate({'kind': 'catalogue', 'id': 'people_per_session',
            'lines': [line(numbers={key: {'value': value, 'level': 'medium'} for key, value in numbers.items()})]})
    assert any(e['type'] == 'invalid_number_range' for e in caught.value.errors())
    assert number_range_errors(numbers)


def test_equal_and_missing_bounds_are_valid():
    for numbers in ({'min': 1, 'target': 1, 'max': 1}, {'min': None, 'max': 0}, {'target': 2}, {'max': 0}):
        assert not number_range_errors(numbers)


def test_range_validation_uses_the_catalogues_normalized_target():
    rule = CatalogueRule.model_validate({'kind': 'catalogue', 'id': 'duties_per_month',
        'lines': [line(numbers={key: {'value': value, 'level': 'medium'} for key, value in {'min': 1, 'target': 1.004, 'max': 1}.items()})]})
    assert rule.lines[0].numbers['target'].value == 1


@pytest.mark.postgres
def test_check_and_proposal_report_the_exact_contract_field(pg):
    bad = line(numbers={'min': {'value': 40, 'level': 'mandatory'}, 'max': {'value': 1, 'level': 'mandatory'}})
    result = pg.client('coordinator').post('/api/rules/catalogue/people_per_session/check', json={'lines': [bad]}).json()
    assert result['issues'] == [{'field': 'definition.rule.lines.0.numbers.max',
                                 'message': 'The maximum is below the minimum: no session could meet it.'}]
    refused = propose(pg, 'people_per_session', [bad], status=422)
    assert 'The maximum is below the minimum: no session could meet it.' in str(refused)
    assert entry(pg, 'people_per_session')['proposed'] is None


@pytest.mark.postgres
def test_pool_warning_reaches_proposer_and_approver_without_blocking(pg):
    candidate = line(numbers={'min': {'value': 40, 'level': 'mandatory'}}, starts_on='2026-10-04', ends_on='2026-10-11')
    found = pg.client('coordinator').post('/api/rules/catalogue/people_per_session/check', json={'lines': [candidate]}).json()
    assert not found['issues']
    [warning] = [w for w in found['warnings'] if w['kind'] == 'pool_shortfall']
    assert warning['required'] == 40 and warning['eligible_people'] == len(pg.seed['people'])
    assert warning['line_id'] == candidate['id'] and 'Rule cannot be met' in warning['message']
    proposed = propose(pg, 'people_per_session', [candidate])['record']
    page = entry(pg, 'people_per_session', 'approver')
    assert 'Rule cannot be met' in str(page)
    assert approve(pg, proposed)['status'] == 'active'


@pytest.mark.postgres
def test_old_impossible_rule_is_readable_but_cannot_be_activated(pg):
    candidate = line(numbers={'min': {'value': 2}, 'max': {'value': 1}})
    old = catalogue('people_per_session', candidate)
    PolicyDefinition.model_validate(old['definition'], context={'historical': True})
    proposed = propose(pg, 'people_per_session', [line(numbers={'min': {'value': 1}})])['record']
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Policy, proposed['id']).definition = old['definition']
    refused = pg.command('approver', 'policy.activate', {'reason': 'Review old proposal'}, proposed['id'], proposed['revision'], status=422)
    assert 'The maximum is below the minimum: no session could meet it.' in str(refused)
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.Policy, proposed['id']).status == 'proposed'


@pytest.mark.postgres
def test_fix_list_names_the_impossible_rule_and_opens_its_line(pg):
    w = pg.shift(pg.workspace(), people=[])['workspace']
    data = pg.snapshot(w)
    old = catalogue('people_per_session', line(numbers={'min': {'value': 40}, 'max': {'value': 1}}))
    old['department_id'] = pg.seed['department_id']
    data['policies'].append(old)
    checked = evaluate(data)
    checked['fix_list_context'] = fix_list_context(data)
    causes = rota_summary(checked)['fix_list']['causes']
    cause = next(c for c in causes if any(l['rule_id'] == old['id'] for l in c['rule_lines']))
    assert cause['sentence'].startswith('Rule cannot be met:')
    assert '40' in cause['sentence'] and 'eligible pool' in cause['sentence']
    assert cause['action']['kind'] == 'open_line'
