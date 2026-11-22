"""Enforcement and authoring of mandatory safety rules."""
import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.cp_sat import generate
from packages.domain.score import schedule_score
from packages.domain.time import aware
from packages.domain.validation import evaluate
from tests.clock_support import freeze_clock
from tests.test_rule_catalogue_api import line
from tests.test_rule_safety_audit import safety_settings


CASES = [
    ('restriction', 'workload_limit', 'apply', 'RESTRICTION'),
    ('min_rest', 'duty_spacing', 'min', 'MIN_REST'),
    ('forbidden_transition', 'duty_spacing', 'apply', 'FORBIDDEN_TRANSITION'),
    ('minimum_cover', 'people_per_session', 'min', 'SCOPED_COVERAGE_GAP'),
    ('maximum_participants', 'people_per_session', 'max', 'SCOPED_PARTICIPANT_LIMIT'),
]


def rule_for(h, kind):
    settings = safety_settings(h)[kind]
    if kind == 'min_rest':
        settings['hours'] = 20
    if kind == 'maximum_participants':
        settings['maximum'] = 0
    return {'kind': kind, **settings}


@pytest.mark.parametrize('kind,entry,slot,code', CASES, ids=[case[0] for case in CASES])
def test_stored_advisory_safety_is_hard(h, monkeypatch, kind, entry, slot, code):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    person = h.seed['people'][0]
    workspace = h.shift(h.workspace())['workspace']
    if kind in {'min_rest', 'forbidden_transition'}:
        workspace = h.shift(workspace, start='2026-10-05T04:00:00+03:00',
                            end='2026-10-05T08:00:00+03:00')['workspace']
    policy = h.stored_advisory_safety(rule_for(h, kind))['id']
    data = h.snapshot(workspace)
    assert next(p for p in data['policies'] if p['id'] == policy)['definition']['severity'] == 'advisory'
    findings = [f for f in evaluate(data)['findings'] if f.get('rule_id') == policy]
    trial = {**data, 'people': {person: data['people'][person]},
             'shifts': [{**shift, 'participants': []} for shift in data['shifts']]}
    result = generate(trial, 5, feasibility_only=True)
    refusal = h.lifecycle(workspace, 'submit', status=(200, 422))
    assert (
        {(f['code'], f['severity']) for f in findings},
        result['status'],
        refusal.get('error', {}).get('code'),
    ) == ({(code, 'error')}, 'requirements_conflict', 'VALIDATION_FAILED')
    assert any(f.get('rule_id') == policy and f['code'] == code and f['severity'] == 'error'
               for f in refusal['error']['details']['findings'])
    saved = next(p for p in h.get('coordinator', '/api/policies?status=active')['policies'] if p['id'] == policy)
    assert saved['definition']['severity'] == 'advisory'
    assert saved['effective_severity'] == 'mandatory'
    assert saved['severity_reason'] == 'Safety rules are always mandatory'


@pytest.mark.parametrize('kind,entry,slot,code', CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize('replaces', [False, True], ids=['create', 'revise'])
def test_legacy_authoring_refuses_advisory_safety(h, kind, entry, slot, code, replaces):
    rule = rule_for(h, kind)
    definition = {'rule': rule, 'severity': 'advisory'}
    if replaces:
        definition['replaces_id'] = h.policy(rule)['id']
    result = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic rule', 'owner': 'Synthetic owner', 'source': 'Synthetic fixture',
        'starts_on': '2020-01-01', 'definition': definition,
    }, status=(200, 422))
    error = result.get('error', {})
    assert error.get('code') == 'SCHEMA_INVALID'
    assert any('safety rules are always mandatory' in detail['message'].lower() for detail in error['details'])


def catalogue_definition(h, kind, entry, slot, level):
    rule = rule_for(h, kind)
    rule.pop('kind')
    field = {'min_rest': 'hours', 'minimum_cover': 'minimum', 'maximum_participants': 'maximum'}.get(kind)
    value = rule.pop(field) if field else None
    return {'rule': {'kind': 'catalogue', 'catalogue_version': 2, 'id': entry, 'lines': [
        line(numbers={slot: {'value': value, 'level': level}}, settings={kind: rule} if rule else {}),
    ]}}


@pytest.mark.parametrize('kind,entry,slot,code', CASES, ids=[case[0] for case in CASES])
def test_catalogue_authoring_refuses_advisory_safety(h, kind, entry, slot, code):
    from pydantic import ValidationError

    definition = catalogue_definition(h, kind, entry, slot, 'medium')
    with pytest.raises(ValidationError, match='[Ss]afety rules are always mandatory'):
        PolicyDefinition.model_validate(definition)
    response = h.client('coordinator').post(f'/api/rules/catalogue/{entry}/check', json=definition['rule'])
    assert response.status_code == 200
    assert any('Safety rules are always mandatory' in issue['message'] for issue in response.json()['issues'])


def test_advisory_spread_evenly_remains_advisory(h):
    from packages.domain.conditions import active_rules

    policy = h.policy(rule_for(h, 'spread_evenly'), severity='advisory')
    [effective] = active_rules([policy])
    assert effective['definition']['severity'] == 'advisory'


def test_cover_target_remains_scored_without_blocking(h):
    from packages.domain.conditions import active_rules

    workspace = h.shift(h.workspace())['workspace']
    data = h.snapshot(workspace)
    definition = PolicyDefinition.model_validate(
        catalogue_definition(h, 'minimum_cover', 'people_per_session', 'target', 'high')).model_dump(mode='json')
    data['policies'] = [{'id': 'cover-target', 'name': 'Cover target', 'kind': 'catalogue',
                         'status': 'active', 'department_id': h.seed['department_id'], 'revision': 1, 'definition': definition}]
    [effective] = active_rules(data)
    assert effective['definition']['severity'] == 'advisory'
    validation = evaluate(data)
    assert validation['valid']
    assert any(f['code'] == 'SCOPED_COVERAGE_GAP' and f['severity'] == 'warning' for f in validation['findings'])
    assert schedule_score(data, validation)['components']['rules']['value'] < 1
    person = h.seed['people'][0]
    data['people'] = {person: data['people'][person]}
    assert generate(data, 5, feasibility_only=True)['status'] == 'candidate_feasible'


def test_advisory_block_quota_cap_does_not_block(h, monkeypatch):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    workspace = h.workspace()
    person = h.seed['people'][0]
    cap = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 0,
                    'starts_on': workspace['starts_on'], 'ends_on': workspace['ends_on']},
                   {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    data = h.snapshot(workspace)
    [finding] = [f for f in evaluate(data)['findings'] if f.get('rule_id') == cap['id']]
    assert (finding['code'], finding['severity']) == ('QUOTA_ABOVE_MAXIMUM', 'warning')
    data['people'] = {person: data['people'][person]}
    assert generate(data, 5, feasibility_only=True)['status'] == 'candidate_feasible'
    assert h.publish(workspace)['publication_id']
