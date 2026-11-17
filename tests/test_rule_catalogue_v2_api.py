"""Department conversion decisions bind a complete preview, with additive lifecycle evidence."""
import copy
import json
import uuid
import pytest

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.rule_catalogue import catalogue_counts
from packages.domain.conditions import active_rules
from packages.domain.rule_lines import SCORING_DEFAULT_SOURCE
from scripts.system_command import MAINTENANCE_ISSUER, MAINTENANCE_SUBJECT


@pytest.mark.parametrize('placement', ['rule_settings', 'line_settings'])
def test_spread_evenly_proposal_with_tags_evaluates_and_generates(h, placement):
    from packages.domain import cp_sat, generation
    from packages.domain.validation import evaluate

    workspace = h.workspace()
    h.shift(workspace, people=[])
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              change['id'], change['revision'])
    h.configure('tags', {'name': 'Synthetic team', 'labels': ['people']})
    settings = {'spread_evenly': {'holder': 'supervisors', 'within': 'session', 'mode': 'levelled'}}
    proposal = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'spread_evenly', 'catalogue_version': 2,
        'settings': settings if placement == 'rule_settings' else {},
        'lines': [{'id': str(uuid.uuid4()), 'numbers': {'max': {'value': 1, 'level': 'mandatory'}},
                   'settings': settings if placement == 'line_settings' else {}}],
        'reason': 'Keep supervision evenly distributed'})['record']
    stored = proposal['definition']['rule']
    if placement == 'line_settings':
        stored = stored['lines'][0]
    assert stored['settings']['spread_evenly']['within'] == 'session'
    active = h.command('approver', 'policy.activate', {'reason': 'Independent spread review'},
                       proposal['id'], proposal['revision'])['record']
    data = h.snapshot(workspace)
    [spread] = [p for p in active_rules(data['policies'], context=data) if p['id'] == active['id']]
    assert spread['definition']['rule']['within'] == 'session'
    original = copy.deepcopy(data)
    assert isinstance(evaluate(data)['valid'], bool)
    assert data == original
    solved = cp_sat.generate(data, 5, 1, deterministic=True)
    assert solved['status'] == 'candidate_feasible', solved
    assert solved['solver_status'] in {'OPTIMAL', 'FEASIBLE'}, solved
    assert data == original
    greedy = generation.generate(data, time_budget_seconds=5)
    assert greedy['status'] == 'candidate_feasible', greedy
    assert data == original


@pytest.mark.parametrize('measure,period,dated', [
    ('session_units', {'kind': 'calendar_month'}, True),
    ('scheduled_hours', {'kind': 'rolling', 'days': 7}, False),
    ('duties', {'kind': 'calendar_week'}, False),
])
def test_typed_workload_check_dates_only_resolved_quota_lines(h, measure, period, dated):
    line_id = str(uuid.uuid4())
    response = h.client('coordinator').post('/api/rules/catalogue/workload_limit/check', json={
        'catalogue_version': 2, 'lines': [{'id': line_id, 'measure': measure, 'period': period,
                                         'numbers': {'max': {'value': 5, 'level': 'mandatory'}}}]})
    assert response.status_code == 200, response.text
    found = response.json()
    assert found['issues'] == []
    if dated:
        start, end = found['dated'][line_id]
        assert start and end is None
    else:
        assert found['dated'] == {}


def test_history_can_read_effective_converted_sources_without_renaming_evidence(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    history = h.get('auditor', '/api/policies?status=retired&effective=true')
    stored = next(p for p in history['policies'] if p['id'] == source['id'])
    assert stored['status'] == 'retired'
    effective = history['effective'][source['id']]
    assert effective['id'] == source['id'] and effective['status'] == 'active'
    assert effective['definition']['rule'] == source['definition']['rule']
    assert 'effective' not in h.get('auditor', '/api/policies?status=retired')
    h.get('staff', '/api/policies?status=retired&effective=true', status=403)


def preview(h, proof=True):
    return h.get('coordinator', '/api/rules/catalogue/migration?version=2&proof=' + str(proof).lower())


def proposed(h):
    plan = preview(h)
    assert not plan['blocked'] and plan['proof']['identical'], (plan['blocked'], plan['proof'])
    return h.command('coordinator', 'rule.migrate_to_catalogue', {
        'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'Review the whole department conversion'})['record']


def test_one_proposal_keeps_sources_then_atomically_activates_and_undoes(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'})
    w = h.workspace()
    h.shift(w)
    before = h.snapshot(w)
    plan = preview(h)
    assert plan['proof']['identical'], plan['proof']
    change = proposed(h)
    summary = h.get('approver', '/api/rules/catalogue/conversions')['records'][0]
    assert summary['id'] == change['id'] and 'plan' not in summary and 'proof' not in summary
    assert h.get('auditor', '/api/rules/catalogue/conversions/' + change['id'])['record'] == change
    h.get('staff', '/api/rules/catalogue/conversions/' + change['id'], status=403)
    assert h.snapshot(w)['policies'] == before['policies']
    h.command('coordinator', 'rule.catalogue.activate', {'reason': 'Cannot decide own proposal'},
              change['id'], change['revision'], status=403)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent full review'},
                       change['id'], change['revision'])['record']
    after = h.snapshot(w)
    def effective(data):
        rows = [{k: v for k, v in p.items() if k != '_v2_source'}
                for p in active_rules(data['policies'], context=data)]
        return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    assert effective(before) == effective(after)
    with transaction(h.app.state.engine) as db:
        old = db.get(m.Policy, source['id'])
        assert old.status == 'retired' and old.definition == source['definition']
        assert len(list(db.scalars(select(m.CatalogueConversion)))) == 1
        assert list(db.scalars(select(m.RuleSourceBinding)))
    undone = h.command('approver', 'rule.catalogue.undo', {'reason': 'Restore predecessor representation'},
                       active['id'], active['revision'])['record']
    assert undone['status'] == 'retired'
    assert h.get('approver', '/api/rules/catalogue/conversions')['records'] == []
    assert h.get('auditor', '/api/rules/catalogue/conversions?include_history=true')['records'][0]['id'] == change['id']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, source['id']).status == 'active'
        assert list(db.scalars(select(m.RuleSourceBinding)))
        maintenance_id = db.scalar(select(m.Identity.id).where(
            m.Identity.issuer == MAINTENANCE_ISSUER, m.Identity.subject == MAINTENANCE_SUBJECT))
        assert maintenance_id
        retired_destinations = {p.id for p in db.scalars(select(m.Policy).where(m.Policy.kind == 'catalogue'))
            if not (p.source == SCORING_DEFAULT_SOURCE and p.created_by == maintenance_id == p.approved_by)}
        original_sources = {binding['source_policy_id'] for binding in active['plan']['bindings']}
        assert retired_destinations and original_sources
        assert all(db.get(m.Policy, ident).status == 'retired' for ident in retired_destinations)
        assert all(db.get(m.Policy, ident).status == 'active' for ident in original_sources)
    again = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Review a fresh conversion after rollback'}, again['id'], again['revision'])
    with transaction(h.app.state.engine) as db:
        assert {p.id for p in db.scalars(select(m.Policy).where(m.Policy.kind == 'catalogue', m.Policy.status == 'active'))}.isdisjoint(retired_destinations)
        assert all(db.get(m.Policy, ident).status == 'retired' for ident in retired_destinations)


def test_stale_inputs_decline_and_idempotency(h):
    h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    plan = preview(h)
    payload = {'version': 2, 'plan_hash': plan['plan_hash'], 'reason': 'One department review'}
    first = h.command('coordinator', 'rule.migrate_to_catalogue', payload, key='catalogue-v2-one-review')
    assert h.command('coordinator', 'rule.migrate_to_catalogue', payload, key='catalogue-v2-one-review') == first
    h.command('coordinator', 'rule.migrate_to_catalogue', payload, status=409)
    change = first['record']
    h.workspace('A new input invalidates the exact proof')
    refused = h.command('approver', 'rule.catalogue.activate', {'reason': 'Stale review'},
                        change['id'], change['revision'], status=409)
    assert refused['error']['code'] == 'STALE_INPUTS'
    declined = h.command('approver', 'rule.catalogue.decline', {'reason': 'Preview changed sources again'},
                         change['id'], change['revision'])['record']
    assert declined['status'] == 'declined'


def test_alternate_login_of_proposer_cannot_approve(h):
    h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    change = proposed(h)
    with transaction(h.app.state.engine, write=True) as db:
        for role in ('coordinator', 'approver'):
            db.get(m.Identity, h.seed['identities'][role]).person_id = h.seed['people'][0]
    denied = h.command('approver', 'rule.catalogue.activate', {'reason': 'Same person another login'},
                       change['id'], change['revision'], status=403)
    assert denied['error']['code'] == 'SELF_APPROVAL'


def test_v2_authorable_metadata_and_expiry_filter(h):
    page = h.get('auditor', '/api/rules/catalogue?version=2')
    assert page['catalogue_counts'] == {**catalogue_counts(), 'in_force': 1,
                                       'lines': len(page['entries'][0]['rule']['definition']['rule']['lines'])}
    assert page['catalogue_counts']['authorable'] == len(page['authorable_entries']) == catalogue_counts()['authorable']
    assert [entry['id'] for entry in page['entries']] == ['fair_share']
    assert all(e['id'] not in {'overtime_offers', 'duties_per_month', 'keep_off'} for e in page['authorable_entries'])
    h.get('staff', '/api/rules/catalogue/migration?version=2', status=403)


def test_v2_migration_preview_exposes_source_census_and_retained_audit_refs(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    plan = h.get('coordinator', '/api/rules/catalogue/migration?version=2&details=true')
    row = next(item for item in plan['census']['sources'] if item['source_policy_id'] == source['id'])
    assert row['source_revision'] == source['revision']
    assert row['source_hash'] and row['audit_refs']['definition_history_ids']
    line = next(item for item in plan['census']['lines'] if item['source_policy_id'] == source['id'])
    assert line['source_line_id'] in row['line_ids'] and line['line_hash']
    assert plan['catalogue_counts'] == catalogue_counts()


def test_v2_current_projection_uses_selected_day_and_keeps_history(h):
    import uuid
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Review the seeded department conversion'},
        conversion['id'], conversion['revision'])
    rule = h.command('coordinator', 'rule.propose', {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
        'lines': [{'id': str(uuid.uuid4()), 'measure': 'duties', 'period': {'kind': 'calendar_week'},
                   'ends_on': '2026-10-10', 'numbers': {'max': {'value': 3}}}],
        'reason': 'Synthetic dated workload limit'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent dated review'}, rule['id'], rule['revision'])
    current = h.get('auditor', '/api/rules/catalogue?version=2&day=2026-10-09')
    assert {entry['id'] for entry in current['entries']} == {'fair_share', 'workload_limit'}
    assert current['day'] == '2026-10-09'
    workload = next(entry for entry in current['entries'] if entry['id'] == 'workload_limit')
    assert workload['rule']['definition']['rule']['lines'][0]['measure'] == 'duties'
    assert [entry['id'] for entry in h.get('auditor', '/api/rules/catalogue?version=2&day=2026-10-10')['entries']] == ['fair_share']
    retained = h.get('auditor', '/api/rules/catalogue?version=2&day=2026-10-10&include_hidden=true')
    assert next(e for e in retained['entries'] if e['id'] == 'workload_limit')['rule']['definition']['rule']['lines']


def test_disabled_and_historical_sources_are_not_retired_by_conversion(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    old = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'day'})
    h.command('approver', 'policy.retire', {'reason': 'Ended original'}, old['id'], old['revision'])
    with transaction(h.app.state.engine) as db:
        retained = copy.deepcopy(db.get(m.Policy, old['id']).definition)
        revision = db.get(m.Policy, old['id']).revision
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent review'}, change['id'], change['revision'])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, source['id']).status == 'retired'
        assert db.get(m.Policy, old['id']).definition == retained and db.get(m.Policy, old['id']).revision == revision


def test_converted_source_still_accepts_exception_and_rota_adjustment(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, exception=True)
    w = h.workspace()
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent review'}, change['id'], change['revision'])
    denied = h.command('approver', 'policy.restore', {'reason': 'Do not reactivate a converted predecessor'},
                       source['id'], source['revision'] + 1, status=409)
    assert denied['error']['code'] == 'WORKFLOW_STATE'
    exc = h.command('coordinator', 'exception.create', {'policy_id': source['id'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'reason': 'Explicit converted rule exception'})['record']
    assert exc['policy_revision'] == source['revision']
    h.command('approver', 'exception.approve', {'reason': 'Independent exception review'}, exc['id'], exc['revision'])
    adjusted = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': source['id'], 'mode': 'set',
        'amount': 2, 'reason': 'Reviewed rota workload adjustment'}, w['id'], w['revision'])['workspace']
    assert adjusted['rule_adjustments'][0]['policy_revision'] == source['revision']
    assert any(p['id'] == source['id'] and p['definition']['rule']['maximum'] == 2
               for p in active_rules(h.snapshot(adjusted)))


def test_converted_person_target_override_preserves_source_revision(h):
    source = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 3,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'prorate_department_days': True},
        {'person_id': h.seed['people'][0]}, severity='advisory')
    w = h.workspace()
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent review'}, change['id'], change['revision'])
    adjusted = h.command('coordinator', 'workspace.target_override', {'policy_id': source['id'],
        'person_id': h.seed['people'][0], 'amount': 2, 'reason': 'Reviewed rota target'}, w['id'], w['revision'])['workspace']
    assert adjusted['target_overrides'][0]['policy_revision'] == source['revision']
    report = h.lifecycle(adjusted, 'validate')['workspace']['validation']['quota_report']
    assert any(row['policy_id'] == source['id'] and row['amount'] == 2 for row in report['rows'])
    workload = h.get('coordinator', f"/api/people/{h.seed['people'][0]}/workload?on=2026-10-06&workspace_id={adjusted['id']}")
    period = next(p for p in workload['periods'] if p['key'] == 'this_rota')
    assert any(row['policy_id'] == source['id'] and row['amount'] == 2 for row in period['quotas'])


def test_entry_authoring_cannot_bypass_department_conversion_or_downgrade_it(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    payload = {'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'lines': [], 'reason': 'No partial conversion'}
    assert h.command('coordinator', 'rule.propose', payload, status=409)['error']['code'] == 'DEPARTMENT_CONVERSION_REQUIRED'
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent department review'}, change['id'], change['revision'])
    assert h.command('coordinator', 'rule.propose', {**payload, 'catalogue_id': 'duties_per_week', 'catalogue_version': 1},
                     status=409)['error']['code'] == 'USE_CATALOGUE_V2'
    assert h.command('coordinator', 'policy.create', {k: source[k] for k in ('name', 'owner', 'source', 'starts_on', 'definition')},
                     status=409)['error']['code'] == 'USE_CATALOGUE_V2'


def test_converted_destination_activation_reports_impact_by_original_source_id(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 9, 'window': 'week'})
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent department review'}, change['id'], change['revision'])
    w = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[h.seed['people'][0]])
    current = h.get('coordinator', '/api/rules/catalogue/workload_limit/detail?version=2')['entry']['rule']
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers']['max']['value'] = 1
    changed = h.command('coordinator', 'rule.propose', {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'], 'lines': lines,
        'reason': 'Reviewed tighter shared limit'})['record']
    result = h.command('approver', 'policy.activate', {'reason': 'Independent tighter limit review'}, changed['id'], changed['revision'])
    assert any(f['rule_id'] == source['id'] for impact in result['impact'] for f in impact['findings'])


def test_conversion_undo_preserves_later_native_target_goal_reference(h):
    import uuid
    from tests.test_scheduling_facts_api import decide
    source = h.policy({'kind': 'balance_goal', 'metric': 'hours', 'aim': 'target', 'amount': 10}, severity='advisory')
    change = proposed(h)
    active = h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent department review'}, change['id'], change['revision'])['record']
    target = h.command('coordinator', 'person.workload.propose', {'kind': 'target', 'person_id': h.seed['people'][0],
        'catalogue_id': 'fair_share', 'goal_ref': {'policy_id': source['id'], 'revision': source['revision']},
        'line': {'id': str(uuid.uuid4()), 'numbers': {'target': {'value': 7, 'level': 'medium'}},
                 'settings': {'balance_goal': {'metric': 'hours', 'aim': 'target'}}},
        'reason': 'A later personal amount uses the retained source goal'})['fact']
    decide(h, target)
    assert h.command('approver', 'rule.catalogue.undo', {'reason': 'Would stale the personal goal reference'},
        active['id'], active['revision'], status=409)['error']['code'] == 'GOAL_HAS_PERSONAL_TARGETS'
    assert h.get('coordinator', f"/api/rules/catalogue/conversions/{active['id']}")['record']['status'] == 'active'


@pytest.mark.postgres
def test_postgres_atomic_activation_undo_and_retained_evidence(pg):
    test_one_proposal_keeps_sources_then_atomically_activates_and_undoes(pg)


@pytest.mark.postgres
def test_postgres_stale_proof_and_single_proposal(pg):
    test_stale_inputs_decline_and_idempotency(pg)
