"""Rolling lead fairness keeps activation linear in publication duties, not duties times all rules."""
from copy import deepcopy
from datetime import date, timedelta
import json

import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain import leadership
from packages.domain.conditions import _month_shift
from packages.domain.quotas import METRIC_VERSION
from tests.test_backend_scale import scale_department, measure
from tests.test_performance_budget import python_budget


def lead_history_case(h, months=11, sessions=600, historical_shift=None):
    workspace, *_ = scale_department(h)
    with transaction(h.app.state.engine, write=True) as db:
        shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace['id'])))
        for shift in shifts:
            typ = deepcopy(shift.type_snapshot)
            typ['definition']['lead'] = {'minimum': 1, 'maximum': 1, 'category_ids': [], 'capability_ids': []}
            shift.type_snapshot = typ
        assignments = list(db.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_([r.id for r in shifts]))))
        for row in assignments:
            row.team_lead = True
    data = h.snapshot(workspace)
    people = list(data['people'])
    with transaction(h.app.state.engine, write=True) as db:
        definition = s.PolicyDefinition.model_validate({'severity': 'advisory',
            'rule': {'kind': 'max_duties', 'maximum': 20, 'window': 'week'},
            'per_person': [{'person_id': pid, 'amount': 21} for pid in people[:38]]}).model_dump(mode='json')
        db.add_all([m.Policy(department_id=h.seed['department_id'], name=f'Synthetic old rule {i}',
            kind='max_duties', definition=definition, status='active' if i < 10 else 'retired', active=i < 10,
            starts_on=date(2020, 1, 1), ends_on=date(2025, 1, 1), owner='Synthetic', source='Scale history',
            created_by=h.seed['identities']['coordinator']) for i in range(1000)])
        # Keep the synthetic successor's old 12 monthly person lines, adding only rolling lead fairness.
        old = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id'],
            m.Policy.kind == 'catalogue', m.Policy.status == 'active'))
        lines = [{'id': m.uid(), 'who': {'conditions': [{'field': 'person_ids', 'values': [pid]}]},
                  'numbers': {'apply': {'value': None, 'level': 'medium'}},
                  'settings': {'balance_goal': {'metric': 'duties', 'period': 'month'}}} for pid in people[:12]]
        old.definition = s.PolicyDefinition.model_validate({'balance_between_people': False,
            'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': lines}}).model_dump(mode='json')
        definition = deepcopy(old.definition)
        definition['replaces_id'] = old.id
        definition['rule']['lines'].append({'id': m.uid(),
            'who': {'conditions': [{'field': 'category_ids',
                'values': sorted({row['category_id'] for row in data['memberships']})}]}, 'numbers': {'apply': {'value': None, 'level': 'medium'}},
            'settings': {'balance_goal': {'metric': 'lead_turns', 'period': 'rolling', 'period_months': 12,
                                         'comparison': 'within_group', 'share_by': 'available_days'}}})
        definition = s.PolicyDefinition.model_validate(definition).model_dump(mode='json')
        successor = m.Policy(department_id=h.seed['department_id'], name='Synthetic lead successor', kind='catalogue',
            definition=definition, status='proposed', active=True, starts_on=date(2020, 1, 1),
            owner='Synthetic', source='Scale regression', created_by=h.seed['identities']['coordinator'])
        db.add(successor); db.flush()
        target, revision = successor.id, successor.revision
        template = data['shifts'][0]
        for month in range(months):
            first = _month_shift(date(2026, 10, 1), -month-1); last = _month_shift(first, 1)
            shifts = []
            for i in range(sessions):
                day = first + timedelta(days=i % 28)
                start, end = f'{day}T08:00:00+03:00', f'{day}T12:00:00+03:00'
                shifts.append({**template, 'id': m.uid(), 'start': start, 'end': end,
                    'participants': [{**template['participants'][0], 'person_id': people[i % len(people)],
                                      'start': start, 'end': end, 'team_lead': True}]})
                if historical_shift is not None:
                    historical_shift(shifts[-1], people)
            prior = m.Workspace(department_id=h.seed['department_id'], name=f'Synthetic history {month}',
                starts_on=first, ends_on=last, status='published', created_by=h.seed['identities']['coordinator'],
                last_edited_by=h.seed['identities']['coordinator'])
            db.add(prior); db.flush()
            snapshot = {'timezone': data['organisation']['timezone'], 'shifts': shifts}
            db.add(m.Publication(department_id=h.seed['department_id'], workspace_id=prior.id, sequence=month+1,
                starts_on=first, ends_on=last, current=True, published_by=h.seed['identities']['coordinator'],
                fingerprint='0'*64, snapshot=snapshot, validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'}}))
    return workspace, target, revision, old.id


@pytest.mark.postgres
def test_successor_activation_with_rolling_lead_history_is_bounded(pg, monkeypatch, request):
    workspace, target, revision, predecessor = lead_history_case(pg)
    pg.client('approver')
    calls, original = [], leadership.active_rules
    def checked(data):
        calls.append(len(data.get('policies', [])))
        return original(data)
    monkeypatch.setattr(leadership, 'active_rules', checked)
    from apps.api.services import fairness as service
    from packages.domain import fairness
    histories, original_history = [], service.history
    def checked_history(db, data, goals):
        result = original_history(db, data, goals)
        histories.extend(result[fairness.goal_identity(goal)] for goal in goals
                         if goal['definition']['rule']['metric'] == 'lead_turns')
        return result
    monkeypatch.setattr(service, 'history', checked_history)
    reply, timing = measure(pg, 'lead-history-activation', lambda: pg.command('approver', 'policy.activate',
        {'reason': 'Synthetic independent review', 'acknowledge_unresolved': True}, target, revision))
    evidence = {'timing': timing, 'rule_resolutions': len(calls), 'historical_sessions': 6600,
                'current_sessions': 768, 'people': 163, 'stored_old_rules': 1000}
    request.node.user_properties.append(('lead_history_activation', json.dumps(evidence)))
    assert reply['record']['status'] == 'active' and reply['record']['revision'] == revision + 1
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.Policy, predecessor).status == 'retired'
    assert len(histories) == 1
    assert sum(row['value'] for row in histories[0]['people'].values()) == 6600
    # One resolution per pass/publication, not per duty or candidate person (17,785 before).
    assert len(calls) <= 30, evidence
    assert sum(timing['sql'].values()) <= 110, evidence
    return locals()


@pytest.mark.cpu_budget
@pytest.mark.postgres
def test_successor_activation_cpu_budget(pg, monkeypatch, request):
    state = test_successor_activation_with_rolling_lead_history_is_bounded(pg, monkeypatch, request)
    assert state['timing']['cpu_seconds'] < python_budget(5), state['evidence']


@pytest.mark.wall_benchmark
@pytest.mark.postgres
def test_successor_activation_wall_budget(pg, monkeypatch, request):
    state = test_successor_activation_with_rolling_lead_history_is_bounded(pg, monkeypatch, request)
    assert state['timing']['wall_seconds'] < python_budget(10), state['evidence']


def test_unqualified_people_do_not_resolve_all_rules_for_every_duty(monkeypatch):
    from packages.domain.fairness import lead_eligible
    from tests.test_engine_fairness_review import A, B, duty, policy, uid, world
    shifts = [duty(i, '2026-10-05') for i in range(600)]
    for shift in shifts:
        shift['type_snapshot']['definition']['lead'] = {
            'minimum': 1, 'maximum': 1, 'category_ids': [], 'capability_ids': [uid(9999)]}
    data = world(shifts)
    data['policies'] = [policy({'metric': 'duties', 'period': 'rolling', 'period_months': 12})]
    calls, original = [], leadership.active_rules
    def counted(data):
        calls.append(1)
        return original(data)
    monkeypatch.setattr(leadership, 'active_rules', counted)
    assert lead_eligible(data, [], [A, B]) == set()
    assert len(calls) == 1
    assert '_leadership_policies' not in data and '_leadership_cache' not in data
# Include this file in hosted supervision and team-lead qualification.
