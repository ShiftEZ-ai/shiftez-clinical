"""Catalogue conversion cannot turn protected safety limits into preferences."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import NEVER_OVERRIDDEN
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate, finding_observations
from tests.test_rule_catalogue_v2_api import preview, proposed
from tests.test_rule_safety_audit import safety_settings


BREACHES = {
    'restriction': ({}, 'RESTRICTION'),
    'min_rest': ({'hours': 20}, 'MIN_REST'),
    'forbidden_transition': ({}, 'FORBIDDEN_TRANSITION'),
    'minimum_cover': ({'minimum': 2}, 'SCOPED_COVERAGE_GAP'),
    'maximum_participants': ({'maximum': 0}, 'SCOPED_PARTICIPANT_LIMIT'),
}


def assert_refused(h, plan, source):
    refused = h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'Review safety rule conversion',
    }, status=409)
    assert refused['error']['code'] == 'CONVERSION_BLOCKED'
    assert refused['error']['details'] == plan['blocked']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Policy, source['id'])
        assert (stored.status, stored.definition) == ('active', source['definition'])
    assert h.get('approver', '/api/rules/catalogue/conversions')['records'] == []


@pytest.mark.parametrize('kind', sorted(NEVER_OVERRIDDEN))
def test_advisory_safety_conversion(h, kind):
    rule = {'kind': kind, **safety_settings(h)[kind]}
    if kind in {'supervision_limit', 'team_lead_requirement'}:
        rejected = h.command('coordinator', 'policy.create', {
            'name': 'Synthetic safety rule', 'owner': 'Synthetic owner', 'source': 'Synthetic safety fixture',
            'starts_on': '2020-01-01', 'definition': {'rule': rule, 'severity': 'advisory'},
        }, status=422)
        assert rejected['error']['code'] == 'SCHEMA_INVALID'
        assert any('are mandatory' in detail['message'] for detail in rejected['error']['details'])
        return

    if kind == 'spread_evenly':
        source = h.policy(rule, severity='advisory')
        plan = preview(h, proof=False)
        assert any(row['rule_id'] == source['id'] and row['kind'] == kind for row in plan['blocked'])
        assert_refused(h, plan, source)
        return

    settings, code = BREACHES[kind]
    workspace = h.shift(h.workspace())['workspace']
    if kind in {'min_rest', 'forbidden_transition'}:
        workspace = h.shift(workspace, '2026-10-05T04:00:00+03:00', '2026-10-05T08:00:00+03:00')['workspace']
    source = h.policy({**rule, **settings}, severity='advisory')
    assert source['definition']['severity'] == 'advisory'
    plan = preview(h, proof=False)
    if plan['blocked']:
        assert any(row['rule_id'] == source['id'] and 'mandatory' in row['reason'] for row in plan['blocked']), plan['blocked']
        assert_refused(h, plan, source)
        return

    change = proposed(h)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent safety conversion review'},
                       change['id'], change['revision'])['record']
    assert active['status'] == 'active'
    [binding] = [b for b in active['plan']['bindings'] if b['source_policy_id'] == source['id']]
    [destination] = binding['destinations']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, source['id']).status == 'retired'
        stored = db.get(m.Policy, destination['id'])
        assert stored.status == 'active'
        [line] = [line for line in stored.definition['rule']['lines'] if line['id'] == destination['line_id']]

    workspace = h.refresh_draft_baseline(workspace)
    data = h.snapshot(workspace)

    def severities(validation):
        return {f['severity'] for f in finding_observations(validation)
                if f['code'] == code and f.get('rule_id') == source['id']}

    solver_input = deepcopy(data)
    for shift in solver_input['shifts']:
        shift['locked'] = True
    result = generate(solver_input, time_budget_seconds=5, deterministic=True)
    submitted = h.lifecycle(workspace, 'submit', status=(200, 422))
    error = submitted.get('error', {})
    outcomes = {
        'line levels': {number['level'] for number in line['numbers'].values()},
        'validator severities': severities(evaluate(data)),
        'generation rejects breach': result['status'] in {'requirements_conflict', 'candidate_with_gaps'}
        and 'error' in severities({'findings': result.get('issues', []) + result.get('validation', {}).get('findings', [])}),
        'Submit refusal': error.get('code'),
        'Submit severities': severities(error.get('details') or {}),
    }
    assert outcomes == {
        'line levels': {'mandatory'},
        'validator severities': {'error'},
        'generation rejects breach': True,
        'Submit refusal': 'VALIDATION_FAILED',
        'Submit severities': {'error'},
    }
