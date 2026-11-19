"""C2 migrations preserve the C1 score and solver tradeoffs on the same department."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import importlib
import json
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import scoring_defaults
from packages.contracts.schemas import PolicyDefinition, TypeDefinition
from packages.domain import cp_sat, cp_sat_goals, cp_sat_quotas, fairness, score
from packages.domain.conditions import active_rules
from packages.domain.time import aware
from packages.domain.validation import evaluate, policy_applies
from ._c1_scoring import BASELINE_C1_SHA, FORMULAS


@contextmanager
def c1_formulas(monkeypatch):
    """Restore only changed C1 formulas; unrelated validation and solver machinery stays shared."""
    with monkeypatch.context() as patch:
        for name, source in FORMULAS.items():
            module = importlib.import_module('packages.domain.' + name)
            namespace = dict(vars(module))
            exec(compile(source, 'C1 ' + BASELINE_C1_SHA + '/' + name + '.py', 'exec'), namespace)
            for key, value in namespace.items():
                if callable(value) and getattr(value, '__globals__', None) is namespace:
                    # New callers pass this optional argument; C1's fixed half-worst formula is unchanged.
                    if key == 'share_satisfaction':
                        frozen = value
                        def compatible_share(mean, worst, *, emphasis=0.5, _frozen=frozen, **kwargs):
                            assert emphasis == 0.5
                            return _frozen(mean, worst, **kwargs)
                        value = compatible_share
                    patch.setattr(module, key, value, raising=False)
        patch.setattr(cp_sat_quotas, 'applicable_policies', importlib.import_module(
            'packages.domain.quotas').applicable_policies)
        original = cp_sat_goals.add_score_goals
        frozen_workload = importlib.import_module('packages.domain.cp_sat_score').workload_component

        def with_builtin_workload(model, data, choices, participants, overtime, pairings, focus, strengths, leads=None,
                                  *, presence=None):
            # C1 put workload beside goals in the same weighted numerator/denominator.
            goals = original(model, data, choices, participants, overtime, pairings, focus, strengths, leads,
                             presence=presence)
            workload, present = frozen_workload(model, data, choices, participants,
                data['generation_options'].get('balance_measure', 'reserved_hours'))
            if present and presence is not None:
                presence.insert(0, present)
            return ([(1, workload)] if present else []) + goals

        patch.setattr(cp_sat_goals, 'add_score_goals', with_builtin_workload)
        yield


def existing_department(h, strength, *, explicit=False, cap=None, independent=False):
    """Start with C1 records, without invoking C2's new-type seeding path."""
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.generation_defaults = {'scoring': {'lead_balance': strength}}
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'requirements': [{'minimum': 2, 'maximum': 2}],
            'lead': {'minimum': 1, 'maximum': 1,
                **({'most_turns': 1, 'most_turns_per': cap} if cap else {})}}).model_dump(mode='json')
        fair = db.scalar(select(m.Policy).where(m.Policy.department_id == department.id))
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'],
            'catalogue_version': 1, 'on': not independent, 'lines': []}}
        if independent:
            db.add(m.Policy(department_id=department.id, name='C1 disabled lead-limit catalogue', kind='catalogue',
                definition=PolicyDefinition.model_validate({'rule': {'kind': 'catalogue',
                    'id': 'other_counts_per_period', 'catalogue_version': 1, 'on': False,
                    'lines': []}}).model_dump(mode='json'), starts_on=fair.starts_on,
                active=True, status='active', owner='Department', source='C1 synthetic fixture',
                created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver']))
    if explicit:
        h.policy({'kind': 'balance_goal', 'metric': 'lead_turns', 'aim': 'equal_share',
            'share_by': 'available_days', 'period': 'rolling', 'period_days': 182,
            'assignment_type_ids': [h.seed['type']]}, severity='advisory')
    if independent:
        # A real v1 non-catalogue rule makes the version-one path explicit.
        h.policy({'kind': 'max_duties', 'maximum': 8, 'window': 'week'})
    first, second = h.seed['people'][:2]
    workspace = h.workspace()
    for day in (5, 6, 7, 8):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        workspace = h.shift(workspace, start=start, end=end, people=[
            {'person_id': person, 'start': start, 'end': end, 'team_lead': person == first}
            for person in (first, second)])['workspace']
    return workspace


def comparable_findings(validation):
    """Rule names/IDs and new line provenance change representation, not the finding's clinical facts."""
    provenance = {'rule_name', 'rule_id', 'policy_name', 'policy_id', 'line_id', 'line', 'slot',
        'count_bucket', 'rule_severity', 'rule_strength', 'rule_weight', 'rule_tier'}
    result = []
    for finding in validation['findings']:
        detail = {key: value for key, value in finding['detail'].items() if key not in provenance}
        result.append({key: finding.get(key) for key in ('code', 'severity', 'person_id', 'shift_id',
            'start', 'end')} | {'detail': detail})
    return sorted(result, key=lambda row: json.dumps(row, sort_keys=True))


def checked_result(data):
    validation = evaluate(deepcopy(data))
    result = score.schedule_score(deepcopy(data), validation)
    generated = cp_sat.generate(deepcopy(data), 5, deterministic=True)
    assert generated['status'] == 'candidate_feasible', json.dumps({key: value for key, value in generated.items()
        if key not in {'summary', 'candidate_evidence'}}, default=str)
    assert generated['validation']['valid'], generated['validation']['findings']
    assert generated['optimality_proven'], generated
    return validation, result, generated


def two_person_snapshot(h, workspace):
    data = h.snapshot(workspace)
    people = set(h.seed['people'][:2])
    data['people'] = {pid: person for pid, person in data['people'].items() if pid in people}
    data['memberships'] = [row for row in data['memberships'] if row['person_id'] in people]
    return data


@pytest.mark.parametrize('seeder', ['fresh', 'backfill'])
@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('other_goal', [False, True])
def test_singleton_defaults_preserve_c1_active_weights_and_goal_mean(h, monkeypatch, seeder, measure, other_goal):
    from ortools.sat.python import cp_model
    from packages.domain import cp_sat_score
    from tests.test_engine_fairness_review import duty
    from tests.test_fairness_context_cache import uncached

    first_person, manual_person = h.seed['people'][:2]

    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Singleton parity fixture',
                                 generation_defaults={'scoring': {'lead_balance': 'off'}})
        db.add(department)
        db.flush()
        department_id = department.id
        if seeder == 'fresh':
            scoring_defaults.seed(db, department_id)
        rules = [{'kind': 'max_duties', 'maximum': 1, 'window': 'week'}]
        if other_goal:
            rules.append({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 8,
                          'period': 'rota'})
        authored = []
        for rule in rules:
            row = m.Policy(department_id=department_id, name='Authored singleton ' + rule['kind'], kind=rule['kind'],
                definition=PolicyDefinition.model_validate({'severity': 'advisory', 'scope': {'person_ids': [first_person]},
                    'rule': rule}).model_dump(mode='json'), starts_on=date(2020, 1, 1), active=True, status='active',
                owner='Synthetic owner', source='Authored parity fixture',
                created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'])
            db.add(row)
            db.flush()
            authored.append(record(row))
    if seeder == 'backfill':
        with h.app.state.engine.begin() as connection:
            importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(connection)
    with transaction(h.app.state.engine) as db:
        seeded = scoring_defaults.seeded_rows(db, [record(row) for row in db.scalars(
            select(m.Policy).where(m.Policy.department_id == department_id))])
    data = two_person_snapshot(h, h.workspace(start='2026-10-01', end='2026-11-01'))
    data['workspace']['department_id'] = department_id
    for field in ('memberships', 'eligibilities', 'grants'):
        for row in data[field]:
            row['department_id'] = department_id
    for typ in data['types'].values():
        typ['department_id'] = department_id
    typ = data['types'][h.seed['type']]
    definition = TypeDefinition.model_validate({**typ['definition'], 'lead': None,
        'requirements': [{'minimum': 1, 'maximum': 2}]}).model_dump(mode='json')
    data['shifts'] = [duty(n, 5 + n, [first_person]) for n in range(4)]
    for session in data['shifts']:
        session.update(assignment_type_id=h.seed['type'], type_revision=typ['revision'],
                       location_id=h.seed['locations'][0],
                       service_ids=[h.seed['services'][0]], attributes={},
                       type_snapshot={'name': typ['name'], 'definition': definition},
                       series_snapshot={'definition': {'usual_assignees_fixed': True,
                                                       'default_person_ids': [first_person]}})
        for participant in session['participants']:
            participant['participation'] = 'staff'
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    first = data['shifts'][0]
    first['participants'].append({**first['participants'][0], 'person_id': manual_person})
    data.update(policies=authored, generation_options={'balance_measure': measure})
    workload = [p for p in active_rules(seeded) if p['definition']['rule'].get('aim') == 'workload_spread']
    assert len(workload) == 1
    assert workload[0]['template_line']['id'] == scoring_defaults.line_id(department_id, 'workload')
    for cached in (False, True):
        for remove_manual in (False, True):
            before = deepcopy(data)
            if remove_manual:
                for session in before['shifts']:
                    session['participants'] = [p for p in session['participants'] if p['person_id'] != manual_person]
            if cached:
                before.update(_distribution_cache={}, _workload_normalization_cache={})
            participants = {(s['id'], p['person_id']): p for s in before['shifts'] for p in s['participants']}
            choices = dict.fromkeys(participants, 1)
            with c1_formulas(monkeypatch):
                old_validation = evaluate(deepcopy(before))
                old = score.schedule_score(deepcopy(before), old_validation)
                assert cp_sat_score.workload_component(cp_model.CpModel(), before, choices, participants, measure) == (0, 0)
            after = {**before, 'policies': seeded}
            new_validation = evaluate(deepcopy(after))
            new = score.schedule_score(deepcopy(after), new_validation)
            assert old_validation['valid'], old_validation['findings']
            assert new_validation['valid'], new_validation['findings']
            assert old['components']['rules']['value'] < 1
            if other_goal:
                assert 0 < old['components']['fairness']['value'] < 1
                for validation in (old_validation, new_validation):
                    rows = [row for goal in validation['goals'] if goal['policy_id'] == authored[-1]['id']
                            for row in goal['people']]
                    assert [(row['person_id'], row['actual'], row['expected'], row['cost']) for row in rows] == [
                        (first_person, 4, 8, .375)]
            else:
                assert old['components']['fairness']['value'] is None
            assert new['score'] == old['score']
            assert new['components'] == old['components']
            assert comparable_findings(new_validation) == comparable_findings(old_validation)
            parameters = fairness.workload_parameters(after, workload[0])
            assert parameters['people'] == [first_person] and not parameters['present'] and not parameters['legacy']
            report = fairness.goal_report(after, workload[0])
            assert len(report['people']) == 1 and report['value'] == 1 and not report['score_present']
            assert parameters == fairness.workload_parameters(uncached(after), workload[0])
            assert report == fairness.goal_report(uncached(after), workload[0])
            assert cp_sat_score.workload_component(cp_model.CpModel(), after, choices, participants,
                                                   measure, policy=workload[0]) == (0, 0)


@pytest.mark.parametrize('strength,explicit,cap,independent', [
    *[(strength, False, None, False) for strength in ('off', 'low', 'medium', 'high', 'critical')],
    ('high', True, None, False),
    ('medium', False, 'week', False),
    ('medium', False, 'month', False),
    ('high', False, 'week', True),
], ids=['lead-off', 'lead-low', 'lead-medium', 'lead-high', 'lead-critical', 'explicit-lead-goal',
        'lead-cap-week', 'lead-cap-month', 'v1-independent-defaults'])
def test_c1_scores_findings_and_solver_components_survive_backfill(h, monkeypatch, strength, explicit, cap, independent):
    workspace = existing_department(h, strength, explicit=explicit, cap=cap, independent=independent)
    with c1_formulas(monkeypatch):
        before = two_person_snapshot(h, workspace)
        before['lead_balance'] = strength  # C1 snapshots carried the old department strength separately.
        old_validation, old_score, old_generated = checked_result(before)
    migration = importlib.import_module('migrations.versions.0109_fair_share_dials')
    with h.app.state.engine.begin() as connection:
        migration.backfill(connection)
    after = two_person_snapshot(h, workspace)
    new_validation, new_score, new_generated = checked_result(after)
    assert new_score['score'] == old_score['score']
    assert new_score['components'] == old_score['components']
    assert comparable_findings(new_validation) == comparable_findings(old_validation)
    assert new_generated['objective_components'] == old_generated['objective_components']
    assert [tier['value'] for tier in new_generated['objectives']] == [
        tier['value'] for tier in old_generated['objectives']]
    if cap:
        assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in old_validation['findings'])
    if explicit:
        lead_goals = [report for report in new_validation['goals']
            if report['metric'] == 'lead_turns' and report['people']]
        assert len(lead_goals) == 1
        assert lead_goals[0]['policy_id'] in {p['id'] for p in before['policies']}
    if independent:
        defaults = [p for p in after['policies'] if p.get('source') == scoring_defaults.SOURCE
            and p['kind'] != 'catalogue']
        assert {p['kind'] for p in defaults} == {'balance_goal', 'block_quota'}
        assert len(defaults) == 3


@pytest.mark.parametrize('representation', ['catalogue', 'independent', 'converted'])
def test_admin_edited_migrated_lead_cap_participates_in_score(h, representation):
    from packages.domain.rule_catalogue_migration import after_snapshot, plan_v2

    workspace = existing_department(h, 'medium', cap='week', independent=representation == 'independent')
    with h.app.state.engine.begin() as connection:
        importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(connection)
    data = two_person_snapshot(h, workspace)
    original = score.schedule_score(data, evaluate(data))
    if representation == 'converted':
        proposal = plan_v2(data['policies'], department_id=h.seed['department_id'])
        assert proposal['blocked'] == []
        data = after_snapshot(data, proposal)
        converted = score.schedule_score(data, evaluate(data))
        assert converted['score'] == original['score']
        assert converted['components'] == original['components']
    edited = deepcopy(data)
    for policy in edited['policies']:
        rule = policy['definition']['rule']
        if rule['kind'] == 'block_quota':
            rule['amount'] = 2
        elif rule['kind'] == 'catalogue':
            for line in rule.get('lines', []):
                if 'block_quota' in line.get('settings', {}):
                    line['numbers']['max']['value'] = 2
    cap_id = scoring_defaults.line_id(h.seed['department_id'], 'lead-cap:' + h.seed['type'])
    effective = [p for p in active_rules(edited) if p['definition']['rule']['kind'] == 'block_quota']
    assert len(effective) == 1
    assert (effective[0].get('template_line') or {}).get('id', effective[0]['id']) == cap_id
    changed_validation = evaluate(edited)
    changed = score.schedule_score(edited, changed_validation)
    assert original['components']['rules']['value'] is None
    assert changed['components']['rules']['value'] == 0.5
    assert changed['score'] < original['score']
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in changed_validation['findings'])


def test_seeded_lead_cap_with_narrower_person_line_scores_derived_scope(h):
    workspace = existing_department(h, 'medium', cap='week')
    with h.app.state.engine.begin() as connection:
        importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(connection)
    data = two_person_snapshot(h, workspace)
    original = score.schedule_score(data, evaluate(data))
    cap_id = scoring_defaults.line_id(h.seed['department_id'], 'lead-cap:' + h.seed['type'])
    for policy in data['policies']:
        rule = policy['definition']['rule']
        if rule['kind'] != 'catalogue':
            continue
        seed = next((line for line in rule.get('lines', []) if line['id'] == cap_id), None)
        if seed:
            narrow = deepcopy(seed)
            narrow.update(id=str(uuid.uuid4()), who={'match': 'all', 'conditions': [
                {'match': 'all', 'conditions': [
                    {'field': 'person_ids', 'op': 'any', 'values': [h.seed['people'][0]]}]}]})
            narrow['numbers']['max']['value'] = 2
            rule['lines'].append(narrow)
    effective = [p for p in active_rules(data) if p['definition']['rule']['kind'] == 'block_quota']
    seeded = next(p for p in effective if p['template_line']['id'] == cap_id)
    narrowed = next(p for p in effective if p['template_line']['id'] == narrow['id'])
    for shift in data['shifts']:
        start, end = aware(shift['start']), aware(shift['end'])
        assert {pid for pid in data['people'] if policy_applies(data, seeded, pid, shift, start, end)} == {h.seed['people'][1]}
        assert {pid for pid in data['people'] if policy_applies(data, narrowed, pid, shift, start, end)} == {h.seed['people'][0]}
    changed = score.schedule_score(data, evaluate(data))
    assert original['components']['rules']['value'] is None
    assert changed['components']['rules']['value'] == 0.5
    assert changed['score'] < original['score']


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('strength', ['off', 'low', 'medium', 'high', 'critical'])
@pytest.mark.parametrize('existing', ['none', 'seeded', 'explicit-type', 'explicit-all', 'catalogue'])
def test_frozen_and_authoring_default_additions_match(version, strength, existing):
    department = 'department'
    type_a, type_b = '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002'
    types = [{'id': tid, 'name': tid, 'definition': {'lead': {'most_turns': 2, 'most_turns_per': period}}}
        for tid, period in [(type_a, 'week'), (type_b, 'month')]]
    policies = []
    if existing in {'explicit-type', 'explicit-all'}:
        policies = [{'id': 'explicit', 'active': True, 'status': 'active', 'kind': 'balance_goal',
            'definition': PolicyDefinition.model_validate({'severity': 'advisory', 'rule': {
                'kind': 'balance_goal', 'metric': 'lead_turns', 'aim': 'equal_share',
                'assignment_type_ids': [type_a] if existing == 'explicit-type' else []}}).model_dump(mode='json')}]
    elif existing in {'seeded', 'catalogue'}:
        lines = scoring_defaults.additions(department, types[:1], [], strength, catalogue_version=version)['fair_share']
        if existing == 'catalogue':
            lines = [line for line in lines if line['settings']['balance_goal']['metric'] == 'lead_turns']
        policies = [{'id': 'fair', 'active': True, 'status': 'active', 'kind': 'catalogue',
            'definition': PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'fair_share',
                'catalogue_version': version, 'lines': lines}}).model_dump(mode='json')}]
    migration = importlib.import_module('migrations.versions.0109_fair_share_dials')
    added = scoring_defaults.additions(department, types, policies, strength, catalogue_version=version)
    assert migration.additions(department, types, policies, strength, version) == added
    caps = added['workload_limit' if version == 2 else 'other_counts_per_period']
    assert len(caps) == 2
    for line in caps:
        quota = PolicyDefinition.model_validate({'severity': 'advisory', 'rule': {
            'kind': 'block_quota', **line['settings']['block_quota'], 'comparison': 'maximum',
            'amount': line['numbers']['max']['value']}}).rule
        assert quota.starts_on.isoformat() == '2000-01-01' and quota.ends_on is None and quota.repeat
        assert quota.window_alignment in ('calendar_week', 'calendar_month')
# Include this file in hosted supervision and team-lead qualification.
