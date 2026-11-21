"""Catalogue-v2 proof must retain all obligations, identities and optimization tiers."""
import copy
import json
from collections import Counter
from datetime import date, timedelta
from time import process_time
from types import SimpleNamespace
from uuid import UUID

import pytest

from packages.contracts.rule_catalogue import RULE_CATALOGUE
from packages.contracts.schemas import PolicyDefinition, RequirementSegment
from packages.domain import cp_sat
from packages.domain.conditions import active_rules
from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2
from packages.domain.rule_migration_proof import canonical_full_model, comparison, strict_expanded, strict_proof
from packages.domain.validation import evaluate


def uid(number):
    return str(UUID(int=number))


def source(number, rule, *, who=None, severity='mandatory', ends_on=None, **definition):
    value = PolicyDefinition.model_validate({'rule': rule, 'scope': {'person_id': who} if who else {},
        'severity': severity, **definition}, context={'historical': True}).model_dump(mode='json')
    return {'id': uid(10000 + number), 'kind': rule['kind'], 'name': f'Synthetic source {number}', 'department_id': uid(10),
            'status': 'active', 'active': True, 'revision': 1, 'starts_on': '2026-01-01', 'ends_on': ends_on,
            'definition': value, 'owner': 'Synthetic department', 'source': 'Synthetic fixture'}


def real_shaped_rules(people, typ, service):
    """The audited shape, with synthetic ids, exact separate day/type offers and empty history."""
    quota = [source(i, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target' if i >= 22 and i % 3 == 0 else 'maximum',
                       'amount': 4 + i % 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                       'prorate_department_days': i >= 22 and i % 6 == 0}, who=people[i % len(people)],
                    severity='advisory' if i >= 22 and i % 3 == 0 else 'mandatory') for i in range(148)]
    offers = [source(200 + i, {'kind': 'allocation_overtime', 'required_policy_ids': [quota[i]['id']]},
                     who=people[i], severity='advisory') for i in range(22)]
    for i, offer in enumerate(offers):
        offer['definition']['scope'].update(assignment_type_id=typ, service_ids=[service], weekdays=[i % 7])
    empty = [source(300 + i, {'kind': 'catalogue', 'id': entry['id'], 'on': True, 'lines': []})
             for i, entry in enumerate(RULE_CATALOGUE[:12])]
    expired = source(400, {'kind': 'restriction'}, ends_on='2026-09-18', calendar_entry_id=uid(500))
    expired['definition']['scope'].update(programme_id=uid(7000), weekdays=[0, 1, 2, 3], start_time='09:00', end_time='10:00')
    return quota + offers + empty + [expired]


def fixture(h, *, people=150, sessions=12):
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    h.shift(workspace, start='2026-10-01T08:00:00+03:00', end='2026-10-01T12:00:00+03:00')
    programme = h.configure('programmes', {'name': 'Synthetic medical-student rotation', 'starts_on': '2026-01-01'})['record']
    programme_membership = h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': programme['id'],
        'starts_on': '2026-09-01', 'ends_on': '2026-10-01'})['record']
    calendar_entry = h.configure('programme_calendar_entries', {'programme_id': programme['id'], 'kind': 'academic_block',
        'name': 'Synthetic September protection', 'starts_on': '2026-09-01', 'ends_on': '2026-09-18'})['record']
    base = h.snapshot(workspace)
    # Clone the persisted contract shapes, so every engine consumes the same complete
    # shift/type/participant metadata as an actual rota instead of sparse test doubles.
    shift_template = copy.deepcopy(base['shifts'][0])
    participant_template = copy.deepcopy(shift_template['participants'][0])
    person_template = copy.deepcopy(base['people'][h.seed['people'][0]])
    membership_templates = [row for row in base['memberships'] if row['person_id'] == h.seed['people'][0]]
    eligibility_templates = [row for row in base['eligibilities'] if row['person_id'] == h.seed['people'][0]]
    base['people'] = {uid(100 + i): {**person_template, 'id': uid(100 + i), 'display_name': f'Synthetic person {i}', 'tags': [], 'active': True}
                      for i in range(people)}
    person_ids = list(base['people'])
    base['memberships'] = [{**row, 'id': uid(200000 + i * len(membership_templates) + j), 'person_id': pid}
                           for i, pid in enumerate(person_ids) for j, row in enumerate(membership_templates)]
    base['eligibilities'] = [{**row, 'id': uid(300000 + i * len(eligibility_templates) + j), 'person_id': pid}
                            for i, pid in enumerate(person_ids) for j, row in enumerate(eligibility_templates)]
    base['grants'] = []
    base['availability'] = []
    base['programmes'] = {uid(7000): {**programme, 'id': uid(7000)}}
    base['programme_memberships'] = [{**programme_membership, 'id': uid(400000 + i), 'person_id': pid, 'programme_id': uid(7000)}
                                     for i, pid in enumerate(person_ids[:10])]
    base['programme_calendar_entries'] = {uid(500): {**calendar_entry, 'id': uid(500), 'programme_id': uid(7000)}}
    base['rotations'] = []
    base['mentorships'] = []
    base['wishes'] = []
    base['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0}
    typ, service, location = h.seed['type'], h.seed['services'][0], h.seed['locations'][0]
    definition = copy.deepcopy(base['types'][typ]['definition'])
    definition['requirements'] = [RequirementSegment(minimum=1, maximum=1).model_dump(mode='json')]
    definition['supervision'] = None
    definition['lead'] = None
    definition['allowed_category_ids'] = []
    base['types'][typ]['definition'] = copy.deepcopy(definition)
    base['shifts'] = []
    for i in range(sessions):
        day = date(2026, 10, 1) + timedelta(days=i % 30)
        start, end = f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00'
        pid = person_ids[i % people]
        base['shifts'].append({**copy.deepcopy(shift_template), 'id': uid(2000 + i), 'assignment_type_id': typ, 'service_ids': [service], 'location_id': location,
            'start': start, 'end': end, 'cancelled': False, 'locked': False, 'attributes': {},
            'type_snapshot': {**copy.deepcopy(shift_template['type_snapshot']), 'name': 'Synthetic duty', 'definition': copy.deepcopy(definition)},
            'participants': [{**copy.deepcopy(participant_template), 'id': uid(3000 + i), 'shift_id': uid(2000 + i), 'person_id': pid, 'start': start, 'end': end,
                              'participation': 'staff', 'overtime': False, 'team_lead': False}]})
    base['focus_ids'] = [s['id'] for s in base['shifts']]
    base['policies'] = real_shaped_rules(person_ids, typ, service)
    # All synthetic commitments in this complete source window are loaded above.
    # The source API snapshot had no quotas until this pure fixture added them.
    base['quota_context'] = {'start': '2026-09-01T00:00:00+03:00', 'end': '2026-12-01T00:00:00+03:00',
                             'complete': True, 'limit_exceeded': False, 'publications': []}
    for policy in base['policies']:
        policy['department_id'] = base['workspace']['department_id']
    return base


def converted(data):
    plan = plan_v2(data['policies'], week_start=data['organisation']['week_start'], department_id=data['workspace']['department_id'])
    assert not plan['blocked'], plan['blocked']
    return after_snapshot(data, plan), plan


def test_full_comparison_keeps_duplicate_findings_and_all_identity_details():
    finding = {'code': 'QUOTA_BELOW_TARGET', 'message': 'Full message', 'rule_id': uid(1), 'detail': {'line': 1, 'slot': 'target', 'rule_strength': 'high'}}
    assert not comparison([finding, finding], [finding], multiset=True)['equal']
    for path, value in [('message', 'Changed message'), ('rule_id', uid(2)), ('detail', {**finding['detail'], 'slot': 'min'})]:
        assert not comparison([finding], [{**finding, path: value}], multiset=True)['equal']


def test_all_27_legacy_catalogue_paths_preserve_the_complete_primitive_multiset(h):
    data = fixture(h, people=22, sessions=1)
    data['policies'] = []
    pid = next(iter(data['people']))
    for i, entry in enumerate(RULE_CATALOGUE):
        settings = copy.deepcopy(entry['default_settings'])
        for kind, fields in entry['needs'].items():
            settings.setdefault(kind, {}).update({field: 'together' if field == 'mode' else uid((700 if field.startswith('to_') else 600) + i) if field.endswith('_id')
                                                 else [uid(600 + i), uid(700 + i)] for field in fields})
        line = {'id': uid(800 + i), **copy.deepcopy(entry['default_line']),
                'settings': settings, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                'who': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [pid]}]}}
        data['policies'].append(source(500 + i, {'kind': 'catalogue', 'id': entry['id'], 'lines': [line]}))
        data['policies'][-1]['department_id'] = data['workspace']['department_id']
    after, plan = converted(data)
    assert len(plan['bindings']) == len(RULE_CATALOGUE)
    assert {b['source_policy_id'] for b in plan['bindings']} == {p['id'] for p in data['policies']}
    assert strict_expanded(data, after)['equal']


def test_real_shaped_fixture_has_exact_counts_and_preserves_effective_sources(h):
    data = fixture(h)
    after, plan = converted(data)
    kinds = Counter(p['definition']['rule']['kind'] for p in data['policies'])
    assert kinds == {'block_quota': 148, 'allocation_overtime': 22, 'catalogue': 12, 'restriction': 1}
    assert len(data['people']) == 150
    assert strict_expanded(data, after)['equal']
    assert len([f for f in plan['scheduling_facts'] if f['kind'] == 'workload']) == 148
    assert len([f for f in plan['scheduling_facts'] if f['kind'] == 'overtime_offer']) == 22
    assert strict_proof(data, after)['identical']


@pytest.mark.parametrize('state', ['empty', 'locked', 'invalid', 'historical'])
def test_complete_findings_quota_fairness_score_survive_every_rota_state(h, state):
    data = fixture(h, people=22, sessions=5)
    if state == 'empty':
        for shift in data['shifts']:
            shift['participants'] = []
    elif state == 'locked':
        for shift in data['shifts']:
            shift['locked'] = True
    elif state == 'invalid':
        data['policies'].append(source(1000, {'kind': 'restriction'}, who=next(iter(data['people']))))
        data['policies'][-1]['department_id'] = data['workspace']['department_id']
    else:
        data['workspace'].update(starts_on='2026-09-01', ends_on='2026-10-01')
        for shift in data['shifts']:
            shift['start'], shift['end'] = shift['start'].replace('-10-', '-09-'), shift['end'].replace('-10-', '-09-')
            for participant in shift['participants']:
                participant.update(start=shift['start'], end=shift['end'])
    after, _ = converted(data)
    proof = strict_proof(data, after)
    assert all(proof[key]['equal'] for key in ('expanded', 'findings', 'quota', 'fairness', 'coverage', 'validation', 'score'))
    if not proof['model']['before']['ready']:
        assert not proof['identical']  # invalid inputs supply no structural model proof
    else:
        assert proof['identical']


def test_full_model_captures_weight_changes_and_ignores_retired_tolerance(h):
    data = fixture(h, people=22, sessions=5)
    goal = source(1500, {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 4,
                         'period': 'month'}, severity='advisory')
    goal['department_id'] = data['workspace']['department_id']
    data['policies'].append(goal)
    model = canonical_full_model(data)
    assert model['ready'], model
    assert [tier['name'] for tier in model['tiers']] == ['coverage', 'weighted']
    assert any('coeffs:' in tier['objective'] for tier in model['tiers'])
    assert all(tier['bound_probes'] and tier['bound_template'] for tier in model['tiers'])
    changed = copy.deepcopy(data)
    changed['generation_options']['scoring'] = {'tolerance': {'preferences': 5}}
    assert canonical_full_model(changed)['digest'] == model['digest']
    changed['generation_options']['scoring'] = {'weights': {'fairness': 40}}
    assert canonical_full_model(changed)['digest'] != model['digest']


def test_person_targets_retain_goal_history_and_expected_values(h):
    from packages.domain.fairness import goal_identity
    data = fixture(h, people=22, sessions=3)
    pid = next(iter(data['people']))
    goal = source(1500, {'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 4,
                         'period': 'month', 'person_amounts': {pid: 7}}, severity='advisory')
    goal['department_id'] = data['workspace']['department_id']
    data['policies'].append(goal)
    data['fairness_history'] = {goal_identity(goal): {'people': {pid: {'value': 2, 'available': 3}}, 'history': True}}
    after, plan = converted(data)
    assert any(f['kind'] == 'target' for f in plan['scheduling_facts'])
    proof = strict_proof(data, after)
    assert proof['fairness']['count_before'] and proof['fairness']['equal']
    assert proof['score']['equal'] and proof['identical']


def test_nested_count_buckets_keep_separate_penalties_and_source_identities(h):
    data = fixture(h, people=22, sessions=3)
    counted = source(1700, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 2,
                            'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'window_alignment': 'calendar_week'}, severity='advisory')
    counted['department_id'] = data['workspace']['department_id']
    counted['definition']['scope']['conditions'] = {'match': 'all', 'conditions': [
        {'match': 'any', 'count': 'each', 'conditions': [
            {'field': 'service_ids', 'op': 'any', 'count': 'each', 'values': list(data['services'])[:2]},
            {'field': 'assignment_type_ids', 'op': 'any', 'values': [h.seed['type']]}]}]}
    data['policies'].append(counted)
    after, _ = converted(data)
    proof = strict_proof(data, after)
    assert proof['expanded']['count_before'] > 170
    assert proof['expanded']['equal'] and proof['quota']['equal'] and proof['model']['equal']
    assert proof['identical']


def test_a_lost_duplicate_or_changed_fact_blocks_strict_proof(h):
    data = fixture(h, people=22, sessions=3)
    duplicated = copy.deepcopy(data)
    duplicated['policies'].append(copy.deepcopy(data['policies'][0]))
    assert not strict_expanded(duplicated, data)['equal']
    after, _ = converted(data)
    number = next(iter(after['scheduling_facts'][0]['data']['line']['numbers'].values()))
    number['value'] += 1
    assert not strict_expanded(data, after)['equal']


def test_unknown_search_is_inconclusive_and_model_skip_is_not_a_pass(h, monkeypatch):
    data = fixture(h, people=22, sessions=1)
    after, _ = converted(data)
    assert not strict_proof(data, after, model=False)['identical']
    original = cp_sat.generate
    def incomplete(snapshot, *args, **kwargs):
        return original(snapshot, *args, **kwargs) if kwargs.get('_on_objectives') else {'status': 'unknown_time_limit', 'additions': [], 'removals': []}
    monkeypatch.setattr(cp_sat, 'generate', incomplete)
    proof = strict_proof(data, after, solve=True)
    assert proof['solve']['cp_sat']['equal'] is None
    assert proof['solve']['cp_sat']['solver_status_before'] == 'UNKNOWN'
    assert not proof['identical']


def test_strict_solve_passes_cancellation_and_budget_to_both_engines(monkeypatch):
    from packages.domain import generation, rule_migration_proof

    calls = []
    cancelled = lambda: False
    def run(snapshot, seconds, *args, **kwargs):
        calls.append(seconds)
        assert kwargs.get('cancelled') is cancelled
        assert kwargs.get('deterministic') is True
        return {'status': 'candidate_feasible', 'not_applied': []}
    monkeypatch.setattr(cp_sat, 'generate', run)
    monkeypatch.setattr(generation, 'generate', run)
    assert all(row['equal'] for row in rule_migration_proof._solve({}, {}, 2, cancelled=cancelled).values())
    assert calls == [2] * 4


def test_bounded_deterministic_cp_sat_and_greedy_candidates_remain_equal(h, monkeypatch):
    data = fixture(h, people=22, sessions=3)
    after, _ = converted(data)
    # Compare completed greedy runs regardless of CI contention, as in the performance tests.
    monkeypatch.setattr('packages.domain.generation.time', SimpleNamespace(monotonic=lambda: 0.0))
    proof = strict_proof(data, after, solve=True, seconds=2)
    assert proof['model']['equal']
    assert proof['solve']['cp_sat']['solver_status_before'] in {'OPTIMAL', 'FEASIBLE'}
    for row in proof['solve'].values():
        assert row['status_before'] == row['status_after'] == 'candidate_feasible', row
    assert all(row['equal'] is True for row in proof['solve'].values()), proof['solve']
    assert proof['identical']


def test_v2_cli_commands_have_receipts_and_idempotent_retry(h, capsys):
    from sqlalchemy import func, select
    from apps.api import models as m
    from apps.api.db import transaction
    from scripts.catalogue_rules import v2
    from scripts.group_rules import actor_for
    h.policy({'kind': 'max_duties', 'maximum': 5})
    args = SimpleNamespace(activate=False, undo=False, decline=False, propose=False, conversion=None,
                           idempotency_key=None, reason='', json=True, solve=False)
    with transaction(h.app.state.engine) as db:
        before = db.scalar(select(func.count()).select_from(m.CatalogueConversion))
        plan, proof, result = v2(db, actor_for(db, 'coordinator'), args)
        assert result is None and proof['identical'] and plan['plan_hash']
        assert db.scalar(select(func.count()).select_from(m.CatalogueConversion)) == before
    args.propose, args.reason, args.idempotency_key = True, 'CLI department conversion review', 'catalogue-v2-cli-retry-test'
    with transaction(h.app.state.engine, write=True) as db:
        _, _, first = v2(db, actor_for(db, 'coordinator'), args)
        assert first['receipt_id']
    with transaction(h.app.state.engine, write=True) as db:
        again = v2(db, actor_for(db, 'coordinator'), args)
        assert again == first
        assert db.scalar(select(func.count()).select_from(m.CatalogueConversion)) == before + 1
        assert db.scalar(select(func.count()).select_from(m.CommandReceipt).where(m.CommandReceipt.key == args.idempotency_key)) == 1
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.correlation_id == args.idempotency_key)) == 1
    capsys.readouterr()


@pytest.fixture(scope='module')
def counted_obligations_measurement(scale_harness):
    h = scale_harness('h')
    from packages.domain.fairness import goal_identity
    data = fixture(h, sessions=500)
    service_template = next(iter(data['services'].values()))
    location_template = next(iter(data['locations'].values()))
    clinics = [uid(4000 + i) for i in range(20)]
    places = [uid(5000 + i) for i in range(20)]
    data['services'] = {sid: {**service_template, 'id': sid, 'name': f'Synthetic clinic {i}'} for i, sid in enumerate(clinics)}
    data['locations'] = {lid: {**location_template, 'id': lid, 'name': f'Synthetic place {i}'} for i, lid in enumerate(places)}
    eligibility = data['eligibilities'][0]
    data['eligibilities'] = [{**eligibility, 'person_id': pid, 'service_id': clinics[i % len(clinics)]}
                              for i, pid in enumerate(data['people'])]
    for i, shift in enumerate(data['shifts']):
        day = date(2026, 10, 1) + timedelta(days=i // len(clinics))
        shift.update(service_ids=[clinics[i % len(clinics)]], location_id=places[i % len(places)], participants=[],
                     start=f'{day}T08:00:00+03:00', end=f'{day}T12:00:00+03:00')
    for policy in data['policies']:
        if policy['kind'] == 'allocation_overtime':
            policy['definition']['scope']['service_ids'] = [clinics[0]]
    # Domain snapshots contain ledger aggregates. Exercise the aggregate of 23 earlier
    # monthly publications plus this month, over the full custom two-year goal period.
    goal = source(2000, {'kind': 'balance_goal', 'metric': 'duties', 'period': 'custom',
                        'starts_on': '2024-11-01', 'ends_on': '2026-11-01'}, severity='advisory')
    goal.update(department_id=data['workspace']['department_id'], starts_on='2024-11-01')
    data['policies'].append(goal)
    data['fairness_history'] = {goal_identity(goal): {'people': {
        pid: {'value': 23 * (3 + i % 5), 'available': 23 * (20 + i % 3)}
        for i, pid in enumerate(data['people'])}, 'history': True}}
    for i in range(816):
        data['policies'].append(source(1000 + i, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                                       who=list(data['people'])[i % 150], ends_on='2026-09-01'))
        data['policies'][-1]['department_id'] = data['workspace']['department_id']
    began = process_time()
    after, _ = converted(data)
    plan_seconds = process_time() - began
    began = process_time()
    expanded = strict_expanded(data, after)
    resolver_seconds = process_time() - began
    validations, validation_seconds, models, model_seconds = [], [], [], []
    for snapshot in (data, after):
        began = process_time()
        validations.append(evaluate(copy.deepcopy(snapshot)))
        validation_seconds.append(process_time() - began)
        began = process_time()
        models.append(canonical_full_model(snapshot))
        model_seconds.append(process_time() - began)
    evidence = {'clock': 'process_time', 'people': 150, 'monthly_sessions': 500, 'source_definitions': 1000, 'history_periods': 24,
                'expanded_primitives': expanded['count_before'], 'plan_seconds': plan_seconds, 'resolver_seconds': resolver_seconds,
                'validation_seconds_before_after': validation_seconds, 'model_seconds_before_after': model_seconds,
                'model_variables': models[0]['variables'], 'model_constraints': models[0]['constraints'],
                'model_digest': models[0]['digest'], 'objective_tiers': [tier['name'] for tier in models[0]['tiers']]}
    return data, after, expanded, validations, models, evidence


def test_department_scale_counted_obligations_remain_exact(counted_obligations_measurement, record_property):
    from packages.domain.score import schedule_score
    data, after, expanded, validations, models, evidence = counted_obligations_measurement
    assert len(data['policies']) == 1000 and len(data['shifts']) == 500
    assert expanded['equal']
    assert all(model['ready'] for model in models), models
    assert models[0]['digest'] == models[1]['digest']
    assert comparison({k: v for k, v in validations[0].items() if k != 'fingerprint'},
                      {k: v for k, v in validations[1].items() if k != 'fingerprint'})['equal']
    assert validations[0]['goals']  # the ledger history participates in this scale proof
    assert comparison(schedule_score(data, validations[0], data.get('generation_defaults') or data['generation_options']),
                      schedule_score(after, validations[1], after.get('generation_defaults') or after['generation_options']))['equal']
    record_property('catalogue_v2_scale_proof', json.dumps(evidence, sort_keys=True))


@pytest.mark.cpu_budget
def test_department_scale_counted_obligations_remain_exact_cpu_budget(counted_obligations_measurement):
    from .test_scheduler_performance import _budget
    *_, evidence = counted_obligations_measurement
    assert evidence['resolver_seconds'] < _budget(20)
    assert max(evidence['validation_seconds_before_after']) < _budget(30)
    assert max(evidence['model_seconds_before_after']) < _budget(90)
