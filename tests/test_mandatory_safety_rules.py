"""Enforcement and authoring of mandatory safety rules."""
import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.cp_sat import generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.rule_groups import is_cap
from packages.domain.rule_lines import weakens_safety_rules
from packages.domain.score import schedule_score
from packages.domain.time import aware
from packages.domain.validation import evaluate
from tests.clock_support import freeze_clock
from tests.test_conversion_safety_kinds import store_level
from tests.test_rule_catalogue_api import approve, entry, line, propose
from tests.test_rule_safety_audit import safety_settings


CASES = [
    ('restriction', 'workload_limit', 'apply', 'RESTRICTION'),
    ('min_rest', 'duty_spacing', 'min', 'MIN_REST'),
    ('forbidden_transition', 'duty_spacing', 'apply', 'FORBIDDEN_TRANSITION'),
    ('minimum_cover', 'people_per_session', 'min', 'SCOPED_COVERAGE_GAP'),
    ('maximum_participants', 'people_per_session', 'max', 'SCOPED_PARTICIPANT_LIMIT'),
]


@pytest.mark.parametrize('kind,field,old,stronger', [('min_rest', 'hours', 11, 12),
                                                     ('maximum_participants', 'maximum', 3, 2),
                                                     ('spread_evenly', 'maximum_gap', 1, 0)])
def test_effective_safety_comparison_preserves_bounds_and_dates(kind, field, old, stronger):
    def policy(value, starts_on='2026-10-01'):
        definition = PolicyDefinition.model_validate({'rule': {'kind': kind, field: value}}).model_dump(mode='json')
        return {'id': 'safety', 'kind': kind, 'active': True, 'status': 'active',
                'department_id': 'd', 'starts_on': starts_on, 'definition': definition}

    before, after = policy(old), policy(stronger)
    assert not weakens_safety_rules([before], [after])
    assert weakens_safety_rules([after], [before])
    assert weakens_safety_rules([before], [policy(stronger, '2026-10-02')])
    assert weakens_safety_rules([before], [{**after, 'active': False}])


@pytest.mark.parametrize('hours,on,weaker', [(9, True, True), (11, False, True), (12, True, False)])
def test_self_approval_cannot_weaken_an_active_safety_line(h, monkeypatch, hours, on, weaker):
    from .governance_support import identity

    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    who = 'rule_reviewer'
    reviewer = identity(h, who, ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': reviewer, 'actions': ['rule.activate'],
              'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Temporary review cover'})
    catalogue = 'rest_between_duties'
    original = line(numbers={'min': {'value': 11, 'level': 'mandatory'}})
    current = approve(h, propose(h, catalogue, [original])['record'])
    change = h.command(who, 'rule.propose', {'catalogue_id': catalogue, 'on': on,
        'lines': [{**original, 'numbers': {'min': {'value': hours, 'level': 'mandatory'}}}],
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'reason': 'Adjust the rest limit'})['record']
    review = h.get('approver', f"/api/policies/{change['id']}/review")
    assert review.get('weakens_safety', False) is weaker
    reply = h.command(who, 'policy.activate', {'reason': 'Temporary review cover'}, change['id'], change['revision'],
                      status=403 if weaker else 200)
    if weaker:
        assert reply['error']['code'] == 'SELF_APPROVAL'
        assert 'weakens a safety rule' in reply['error']['message']
        assert entry(h, catalogue)['rule']['id'] == current['id']
        assert approve(h, change)['status'] == 'active'
    else:
        activated = reply['record']
        assert activated['status'] == 'active'
        refusal = h.command(who, 'policy.retire', {'reason': 'Restore the previous limit'},
                            activated['id'], activated['revision'], status=403)
        assert 'weakens a safety rule' in refusal['error']['message']
        assert entry(h, catalogue)['rule']['id'] == activated['id']


def test_review_and_activation_compare_the_selected_older_copy(h, monkeypatch):
    from .governance_support import identity

    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    who = 'rule_reviewer'
    reviewer = identity(h, who, ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': reviewer, 'actions': ['rule.activate'],
              'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Temporary review cover'})
    payload = {'name': 'Rest standard', 'owner': 'Rule coordinator', 'source': 'Review rest requirements',
               'starts_on': '2026-10-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}}
    current = approve(h, h.command('coordinator', 'policy.create', payload)['record'])
    proposal = h.command(who, 'policy.create', {**payload,
        'definition': {'rule': {'kind': 'min_rest', 'hours': 9}}})['record']
    path = f"/api/policies/{proposal['id']}/review"
    reviewer_client = h.client('approver')
    decision = {'reason': 'Review rest requirements', 'replaces_ids': [current['id']]}

    unchanged = reviewer_client.post(path, json={})
    reduction = reviewer_client.post(path, json=decision)
    assert unchanged.status_code == reduction.status_code == 200
    assert unchanged.json() == {'policy_id': proposal['id']}
    assert reduction.json() == {'policy_id': proposal['id'], 'weakens_safety': True}
    assert h.client('staff').post(path, json=decision).status_code == 403
    assert any(p['id'] == current['id'] for p in h.get('approver', '/api/policies?status=active')['policies'])

    refusal = h.command(who, 'policy.activate', decision, proposal['id'], proposal['revision'], status=403)
    assert refusal['error']['code'] == 'SELF_APPROVAL'
    approved = h.command('approver', 'policy.activate', decision, proposal['id'], proposal['revision'])['record']
    assert approved['status'] == 'active'
    assert any(p['id'] == current['id'] for p in h.get('approver', '/api/policies?status=retired')['policies'])


def test_advisory_cover_target_is_not_a_mandatory_obligation():
    definition = PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'people_per_session',
        'lines': [line(numbers={'target': {'value': 2, 'level': 'medium'}})]}}).model_dump(mode='json')
    policy = {'id': 'cover', 'kind': 'catalogue', 'active': True, 'status': 'active',
              'department_id': 'd', 'starts_on': '2026-10-01', 'definition': definition}
    assert not weakens_safety_rules([policy], [])


@pytest.mark.parametrize('severity', ['mandatory', 'advisory'])
def test_retiring_spread_evenly_requires_independence_for_mandatory_only(h, monkeypatch, severity):
    from .governance_support import identity

    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    who = 'rule_reviewer'
    reviewer = identity(h, who, ['coordinator', 'approver'])
    h.command('admin', 'self_approval.grant', {'identity_id': reviewer, 'actions': ['rule.activate'],
              'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'reason': 'Temporary review cover'})
    proposal = h.command(who, 'policy.create', {'name': 'Supervisor spread', 'owner': 'Rule coordinator',
        'source': 'Schedule supervisors evenly', 'starts_on': '2026-10-01', 'definition': {'severity': severity,
            'rule': {'kind': 'spread_evenly', 'maximum_gap': 1}}})['record']
    current = approve(h, proposal)
    decision = {'reason': 'Remove the spread rule'}
    if severity == 'mandatory':
        refusal = h.command(who, 'policy.retire', decision, current['id'], current['revision'], status=403)
        assert refusal['error']['code'] == 'SELF_APPROVAL'
        assert 'weakens a safety rule' in refusal['error']['message']
        active = h.get('approver', '/api/policies?status=active')['policies']
        assert any(p['id'] == current['id'] for p in active)
        who = 'approver'
    retired = h.command(who, 'policy.retire', decision, current['id'], current['revision'])['record']
    assert retired['status'] == 'retired'


def rule_for(h, kind):
    settings = safety_settings(h)[kind]
    if kind == 'min_rest':
        settings['hours'] = 20
    if kind == 'maximum_participants':
        settings['maximum'] = 0
    return {'kind': kind, **settings}


def safety_workspace(h, monkeypatch, kind):
    freeze_clock(monkeypatch, aware('2026-10-04T00:00:00+03:00'))
    workspace = h.shift(h.workspace())['workspace']
    if kind in {'min_rest', 'forbidden_transition'}:
        workspace = h.shift(workspace, start='2026-10-05T04:00:00+03:00',
                            end='2026-10-05T08:00:00+03:00')['workspace']
    return workspace


def assert_hard_safety(h, workspace, data, policy, code):
    person = h.seed['people'][0]
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
    return trial


@pytest.mark.parametrize('kind,entry,slot,code', CASES, ids=[case[0] for case in CASES])
def test_stored_advisory_safety_is_hard(h, monkeypatch, kind, entry, slot, code):
    workspace = safety_workspace(h, monkeypatch, kind)
    policy = h.stored_advisory_safety(rule_for(h, kind))['id']
    data = h.snapshot(workspace)
    raw = next(p for p in data['policies'] if p['id'] == policy)
    assert raw['definition']['severity'] == 'advisory'
    trial = assert_hard_safety(h, workspace, data, policy, code)
    if kind == 'min_rest':
        greedy = greedy_generate(trial, 5, deterministic=True)
        assert greedy['status'] == 'candidate_with_gaps'
        assert len(greedy['additions']) == 1
        assert all(f.get('rule_id') != policy for f in greedy['validation']['findings'])
        mapped = h.get('coordinator', f"/api/workspaces/{workspace['id']}/rule-map")['rules']
        assert next(r for r in mapped if r['id'] == policy)['severity'] == 'mandatory'
    if kind == 'forbidden_transition':
        assert is_cap(raw)
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


@pytest.mark.parametrize('kind,catalogue_id,slot,code', CASES, ids=[case[0] for case in CASES])
def test_catalogue_stored_advisory_line_is_hard(h, monkeypatch, kind, catalogue_id, slot, code):
    workspace = safety_workspace(h, monkeypatch, kind)
    definition = catalogue_definition(h, kind, catalogue_id, slot, 'mandatory')
    source = approve(h, propose(h, catalogue_id, definition['rule']['lines'], catalogue_version=2)['record'])
    store_level(h, source, 0, slot)
    data = h.snapshot(workspace)
    raw = next(p for p in data['policies'] if p['id'] == source['id'])
    assert raw['definition']['rule']['lines'][0]['numbers'][slot]['level'] == 'medium'
    assert_hard_safety(h, workspace, data, source['id'], code)


def test_safety_kinds_are_in_policy_capabilities(h):
    from packages.contracts.schemas import ALWAYS_MANDATORY_KINDS

    data = h.get('coordinator', '/api/policies/capabilities')
    assert set(data['always_mandatory_kinds']) == ALWAYS_MANDATORY_KINDS


def test_repropose_kept_safety_number_at_its_effective_level(h):
    entry_id = 'people_per_session'
    target = {'value': 3, 'level': 'high'}
    broad = line(numbers={'min': {'value': 2, 'level': 'mandatory'}, 'target': target})
    source = approve(h, propose(h, entry_id, [broad])['record'])
    store_level(h, source, 0, 'min')
    [kept] = source['definition']['rule']['lines']
    added = line(weekdays=[1], numbers={'max': {'value': 5, 'level': 'mandatory'}})
    client = h.client('coordinator')
    check = client.post(f'/api/rules/catalogue/{entry_id}/check', json={'lines': [kept, added]})
    assert check.status_code == 200 and not check.json()['issues']
    changed = {**kept, 'numbers': {**kept['numbers'], 'min': {'value': 1, 'level': 'medium'}}}
    check = client.post(f'/api/rules/catalogue/{entry_id}/check', json={'lines': [changed, added]})
    assert check.status_code == 200
    assert any('Safety rules are always mandatory' in issue['message'] for issue in check.json()['issues'])
    result = propose(h, entry_id, [kept, added], policy_id=source['id'], revision=source['revision'])['record']
    [fixed, other] = result['definition']['rule']['lines']
    assert fixed['id'] == kept['id'] and other['id'] == added['id']
    assert fixed['numbers'] == {'min': {'value': 2, 'level': 'mandatory'}, 'target': target}
    saved = entry(h, entry_id)['rule']['definition']['rule']['lines'][0]
    assert saved['numbers']['min']['level'] == 'medium'


def test_inherited_safety_labels_use_effective_levels(h):
    entry_id = 'people_per_session'
    broad = line(numbers={'min': {'value': 2, 'level': 'mandatory'}, 'target': {'value': 3, 'level': 'high'}})
    narrower = line(who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]},
                    numbers={'max': {'value': 4, 'level': 'mandatory'}})
    blank = line(who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][1]]}]},
                 numbers={'min': {'value': 2, 'level': 'mandatory'}})
    source = approve(h, propose(h, entry_id, [broad, narrower, blank])['record'])
    store_level(h, source, 0, 'min')
    store_level(h, source, 2, 'min', None)
    catalogue = entry(h, entry_id)
    assert catalogue['inherited'][narrower['id']] == {
        'min': {'value': 2, 'level': 'mandatory', 'from': broad['id']},
        'target': {'value': 3, 'level': 'high', 'from': broad['id']},
    }
    saved = next(l for l in catalogue['rule']['definition']['rule']['lines'] if l['id'] == blank['id'])
    assert saved['numbers']['min']['level'] is None
    assert saved['mandatory_reasons'] == {'min': 'Safety rules are always mandatory'}


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
