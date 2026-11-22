"""Reviewed workload edits keep independent listed quota blocks compilable."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.conditions import active_rules
from packages.domain.rule_lines import expand, fact_policy
from packages.domain.rule_migration_proof import canonical_full_model
from packages.domain.solver_rules import compatibility_issues, definition_issues
from .test_rule_catalogue_v2_api import proposed


@pytest.mark.parametrize('same_person', [False, True])
@pytest.mark.parametrize('change', ['future_block', 'earlier_block', 'rolling_window', 'fractional_metric'])
def test_approved_listed_quota_rows_keep_independent_dates_and_settings(h, same_person, change):
    first, second = h.seed['people'][:2]
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic independent listed quota periods', 'owner': 'Synthetic department',
        'source': 'Independent review regression', 'starts_on': '2020-01-01',
        'definition': {'listed_only': True,
            'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
                     'amount': 8, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'per_person': [
                {'person_id': first, 'amount': 3, 'starts_on': '2026-10-01', 'ends_on': '2026-10-10'},
                {'person_id': first if same_person else second, 'amount': 4,
                 'starts_on': '2026-10-11', 'ends_on': '2026-10-16'}]}})['record']
    original = h.command('approver', 'policy.activate', {'reason': 'Independent original review'},
                         proposal['id'], proposal['revision'])['record']
    workspace = h.workspace()
    h.shift(workspace)
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine) as db:
        previous = next(fact for fact in db.scalars(select(m.SchedulingFact))
                        if fact.data['line']['numbers']['max']['value'] ==
                        (4 if change == 'fractional_metric' else 3))
        payload = {'kind': previous.kind, 'person_id': previous.person_id,
                   'replaces_id': previous.id, 'expected_revision': previous.revision,
                   'reason': 'Independent approved workload edit', **deepcopy(previous.data)}
    payload.pop('exception_allowed', None)
    line = payload['line']
    settings = line['settings']['block_quota']
    if change in {'future_block', 'earlier_block'}:
        year = '2028' if change == 'future_block' else '2024'
        line.update(starts_on=f'{year}-10-01', ends_on=f'{year}-10-10')
        settings.update(starts_on=line['starts_on'], ends_on=line['ends_on'])
    elif change == 'rolling_window':
        settings['window_days'] = 7
    else:
        settings['metric'] = 'scheduled_hours'
        line['numbers']['max']['value'] = 4.5
    revised = h.command('coordinator', 'person.workload.propose', payload)['fact']
    assert definition_issues(fact_policy(revised)['definition']) == []
    h.command('approver', 'person.workload.activate', {'reason': 'Independent edited workload review'},
              revised['id'], revised['revision'])

    snapshot = h.snapshot(workspace)
    assert compatibility_issues(snapshot) == []
    copies = sorted((copy for copy in active_rules(snapshot) if copy['id'] == original['id']),
                    key=lambda copy: copy['template_line']['line'])
    assert [copy['template_line']['line'] for copy in copies] == [0, 1]
    assert [copy['template_line']['person_id'] for copy in copies] == [first, first if same_person else second]
    # Compare complete primitive rule bodies with separately approved native facts.
    native = [copy for fact in snapshot['scheduling_facts'] if fact['status'] == 'active'
              for copy in expand(fact_policy(fact), outrank=False)]
    assert sorted((copy['definition']['rule'] for copy in copies), key=lambda rule: rule['amount']) == sorted(
        (copy['definition']['rule'] for copy in native), key=lambda rule: rule['amount'])
    model = canonical_full_model(snapshot)
    assert model['ready'], model


@pytest.mark.parametrize('phase', ['propose', 'activate'])
def test_fact_edits_validate_the_preserved_exception_flag_before_writing(h, phase):
    person = h.seed['people'][0]
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic fixed quota with exceptions', 'owner': 'Synthetic department',
        'source': 'Independent preserved-flag regression', 'starts_on': '2020-01-01',
        'definition': {'listed_only': True, 'exception_allowed': True,
            'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
                     'amount': 8, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'per_person': [{'person_id': person, 'amount': 3}]}})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent fixed-block review'},
              proposal['id'], proposal['revision'])
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine) as db:
        previous = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.status == 'active'))
        old_id, old_revision = previous.id, previous.revision
        payload = {'kind': previous.kind, 'person_id': person, 'replaces_id': old_id,
                   'expected_revision': old_revision, 'reason': 'Independent rolling quota edit',
                   **deepcopy(previous.data)}
        assert payload.pop('exception_allowed') is True
    if phase == 'propose':
        payload['line']['settings']['block_quota']['window_days'] = 7
        response = h.command('coordinator', 'person.workload.propose', payload, status=422)
    else:
        revised = h.command('coordinator', 'person.workload.propose', payload)['fact']
        # Approval must independently validate the stored effective payload too.
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.SchedulingFact, revised['id'])
            data = deepcopy(row.data)
            data['line']['settings']['block_quota']['window_days'] = 7
            row.data = data
        response = h.command('approver', 'person.workload.activate', {'reason': 'Independent rolling review'},
                             revised['id'], revised['revision'], status=422)
    assert response['error']['code'] == 'SOLVER_RULE_INCOMPATIBLE'
    with transaction(h.app.state.engine) as db:
        previous = db.get(m.SchedulingFact, old_id)
        assert previous.status == 'active' and previous.revision == old_revision
        if phase == 'activate':
            assert db.get(m.SchedulingFact, revised['id']).status == 'proposed'


def test_undo_does_not_restore_a_predecessor_accepted_with_the_old_missing_flag_check(h, monkeypatch):
    from apps.api.services import scheduling_facts

    person = h.seed['people'][0]
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic historical fixed quota', 'owner': 'Synthetic department',
        'source': 'Independent historical Undo regression', 'starts_on': '2020-01-01',
        'definition': {'listed_only': True, 'exception_allowed': True,
            'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
                     'amount': 8, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'per_person': [{'person_id': person, 'amount': 3}]}})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent original review'},
              proposal['id'], proposal['revision'])
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine) as db:
        previous = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.status == 'active'))
        payload = {'kind': previous.kind, 'person_id': person, 'replaces_id': previous.id,
                   'expected_revision': previous.revision, 'reason': 'Synthetic historical rolling edit',
                   **deepcopy(previous.data)}
        assert payload.pop('exception_allowed') is True
    payload['line']['settings']['block_quota']['window_days'] = 7
    checked = scheduling_facts._references
    # Reproduce the pre-repair API admission: the effective flag was omitted during both checks.
    with monkeypatch.context() as legacy:
        legacy.setattr(scheduling_facts, '_references',
                       lambda db, actor, value, **kwargs: checked(db, actor, value, exception_allowed=False))
        invalid = h.command('coordinator', 'person.workload.propose', payload)['fact']
        invalid = h.command('approver', 'person.workload.activate', {'reason': 'Historical independent review'},
                            invalid['id'], invalid['revision'])['fact']
    fixed_payload = {'kind': invalid['kind'], 'person_id': person, 'replaces_id': invalid['id'],
                     'expected_revision': invalid['revision'], 'reason': 'Restore valid fixed-block semantics',
                     **deepcopy(invalid['data'])}
    assert fixed_payload.pop('exception_allowed') is True
    fixed_payload['line']['settings']['block_quota']['window_days'] = None
    fixed = h.command('coordinator', 'person.workload.propose', fixed_payload)['fact']
    fixed = h.command('approver', 'person.workload.activate', {'reason': 'Independent corrected fact review'},
                      fixed['id'], fixed['revision'])['fact']
    with transaction(h.app.state.engine) as db:
        retired_revision = db.get(m.SchedulingFact, invalid['id']).revision
    response = h.command('approver', 'person.workload.undo', {'reason': 'Review historical predecessor restoration'},
                         fixed['id'], fixed['revision'], status=422)
    assert response['error']['code'] == 'SOLVER_RULE_INCOMPATIBLE'
    with transaction(h.app.state.engine) as db:
        predecessor, current = db.get(m.SchedulingFact, invalid['id']), db.get(m.SchedulingFact, fixed['id'])
        assert (predecessor.status, predecessor.revision) == ('retired', retired_revision)
        assert (current.status, current.revision) == ('active', fixed['revision'])


@pytest.mark.parametrize('edited_metric', [False, True])
def test_converted_listed_prorated_target_keeps_operative_header_and_override_admission(h, edited_metric):
    from apps.api.auth import authenticate
    from apps.api.services.policies import effective_source

    first, second = h.seed['people'][:2]
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic listed prorated duty targets', 'owner': 'Synthetic department',
        'source': 'Independent public-source regression', 'starts_on': '2020-01-01',
        'definition': {'listed_only': True, 'severity': 'advisory',
            'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target',
                     'amount': 16, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                     'prorate_department_days': True},
            'per_person': [{'person_id': first, 'amount': 16}, {'person_id': second, 'amount': 12}]}})['record']
    original = h.command('approver', 'policy.activate', {'reason': 'Independent target review'},
                         proposal['id'], proposal['revision'])['record']
    workspace = h.shift(h.workspace())['workspace']
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('coordinator').cookies['rota_session'])
        source = effective_source(db, actor, db.get(m.Policy, original['id']))
    rule = source['definition']['rule']
    assert {key: rule[key] for key in ('metric', 'prorate_department_days', 'starts_on', 'ends_on')} == {
        'metric': 'duties', 'prorate_department_days': True,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    if edited_metric:
        with transaction(h.app.state.engine) as db:
            previous = db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.person_id == second,
                                                               m.SchedulingFact.status == 'active'))
            change = {'kind': previous.kind, 'person_id': second, 'replaces_id': previous.id,
                      'expected_revision': previous.revision, 'reason': 'Use the approved hours target',
                      **deepcopy(previous.data)}
        change.pop('exception_allowed', None)
        change['line']['settings']['block_quota']['metric'] = 'scheduled_hours'
        revised = h.command('coordinator', 'person.workload.propose', change)['fact']
        h.command('approver', 'person.workload.activate', {'reason': 'Independent hours target review'},
                  revised['id'], revised['revision'])
    amount = 1.005 if edited_metric else 6
    payload = {'policy_id': original['id'], 'person_id': second, 'amount': amount,
               'reason': 'Independent rota duty target adjustment'}
    fractional = h.command('coordinator', 'workspace.target_override', {**payload, 'person_id': first, 'amount': 32 / 7},
                           workspace['id'], workspace['revision'])
    first_target = next(row for row in fractional['validation']['quota_report']['rows']
                        if row['policy_id'] == original['id'] and row['person_id'] == first)
    assert first_target['metric'] == 'duties' and first_target['amount'] == 4.57
    assert fractional['workspace']['target_overrides'][0]['policy_revision'] == first_target['policy_revision'] == original['revision']
    assert not any(f['code'] == 'TARGET_OVERRIDE_STALE' for f in fractional['validation']['findings'])
    workspace = fractional['workspace']
    updated = h.command('coordinator', 'workspace.target_override', payload,
                        workspace['id'], workspace['revision'])
    rows = updated['validation']['quota_report']['rows']
    actual = next(row for row in rows if row['policy_id'] == original['id'] and row['person_id'] == second)
    assert actual['metric'] == ('scheduled_hours' if edited_metric else 'duties') and actual['amount'] == amount
    assert actual['target_calculation']['override_amount'] == amount
