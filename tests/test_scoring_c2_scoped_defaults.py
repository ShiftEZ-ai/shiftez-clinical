"""Lead defaults fill the dates and rotas an explicit goal does not cover."""
from copy import deepcopy
from datetime import date
import importlib
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import scoring_defaults
from packages.contracts.schemas import PolicyDefinition, TypeDefinition
from packages.domain.fairness import balance_goals, goal_intervals
from packages.domain.validation import evaluate
from .test_scoring_c2_equivalence import existing_department


@pytest.mark.parametrize('seeder', ['runtime', 'migration'])
@pytest.mark.parametrize('coverage', ['future', 'rota', 'bounded', 'catalogue-future'])
def test_explicit_lead_goal_keeps_defaults_outside_its_coverage(h, seeder, coverage):
    workspace = existing_department(h, 'high')
    rule = {'kind': 'balance_goal', 'metric': 'lead_turns', 'aim': 'equal_share',
            'period': 'rota', 'assignment_type_ids': [h.seed['type']]}
    if coverage != 'catalogue-future':
        explicit = h.policy(rule, severity='advisory',
                            workspace_id=workspace['id'] if coverage == 'rota' else None)
    with transaction(h.app.state.engine, write=True) as db:
        if coverage == 'catalogue-future':
            policy = db.scalar(select(m.Policy).where(m.Policy.kind == 'catalogue',
                m.Policy.department_id == h.seed['department_id']))
            policy.definition = {**policy.definition, 'rule': {**policy.definition['rule'], 'lines': [{
                'id': str(uuid.uuid4()), 'starts_on': '2027-01-01',
                'numbers': {'apply': {'level': 'high'}}, 'settings': {'balance_goal': rule}}]}}
        else:
            policy = db.get(m.Policy, explicit['id'])
            if coverage == 'future':
                policy.starts_on = date(2027, 1, 1)
            elif coverage == 'bounded':
                policy.starts_on, policy.ends_on = date(2026, 10, 6), date(2026, 10, 8)
        db.flush()
        original = deepcopy(record(policy))
        if seeder == 'runtime':
            scoring_defaults.seed(db, h.seed['department_id'])
        else:
            importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(db.connection())
            db.expire_all()
        assert record(db.get(m.Policy, original['id'])) == original

    default_id = scoring_defaults.line_id(h.seed['department_id'], 'lead:' + h.seed['type'])

    def default_intervals(workspace):
        data = h.snapshot(workspace)
        [default] = [p for p in balance_goals(data)
            if (p.get('template_line') or {}).get('id', p['id']) == default_id]
        return goal_intervals(data, default, h.seed['people'][0],
                              date.fromisoformat(workspace['starts_on']), date.fromisoformat(workspace['ends_on']))

    if coverage == 'bounded':
        assert default_intervals(workspace) == [(date(2026, 10, 4), date(2026, 10, 6)),
                                                (date(2026, 10, 8), date(2026, 10, 11))]
    elif coverage == 'rota':
        assert default_intervals(workspace) == []
        other = h.workspace()
        assert default_intervals(other) == [(date(2026, 10, 4), date(2026, 10, 11))]
    else:
        assert default_intervals(workspace) == [(date(2026, 10, 4), date(2026, 10, 11))]
        december = h.workspace(start='2026-12-06', end='2026-12-13')
        assert default_intervals(december) == [(date(2026, 12, 6), date(2026, 12, 13))]
        january = h.workspace(start='2027-01-04', end='2027-01-11')
        assert default_intervals(january) == []


@pytest.mark.parametrize('seeder', ['runtime', 'migration'])
def test_catalogue_start_does_not_clip_earlier_defaults(h, seeder):
    workspace = h.workspace(start='2026-09-06', end='2026-09-13')
    with transaction(h.app.state.engine, write=True) as db:
        fair = db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id']))
        fair.starts_on = date(2026, 10, 1)
        fair.definition = {**fair.definition, 'rule': {**fair.definition['rule'], 'lines': []}}
        cap = m.Policy(department_id=fair.department_id, name='Later Workload limit', kind='catalogue',
            definition=PolicyDefinition.model_validate({'rule': {'kind': 'catalogue', 'id': 'workload_limit',
                'catalogue_version': 2, 'lines': []}}).model_dump(mode='json'), starts_on=fair.starts_on,
            active=True, status='active', owner='Department', source='Synthetic later-start catalogue',
            created_by=h.seed['identities']['coordinator'], approved_by=h.seed['identities']['approver'])
        db.add(cap)
        typ = db.get(m.AssignmentType, h.seed['type'])
        typ.definition = TypeDefinition.model_validate({**typ.definition,
            'lead': {'minimum': 1, 'maximum': 1, 'most_turns': 1, 'most_turns_per': 'month'}}).model_dump(mode='json')
        db.flush()
        original = {p.id: deepcopy(record(p)) for p in (fair, cap)}
        if seeder == 'runtime':
            scoring_defaults.seed(db, h.seed['department_id'])
        else:
            importlib.import_module('migrations.versions.0109_fair_share_dials').backfill(db.connection())
            db.expire_all()
        assert {ident: record(db.get(m.Policy, ident)) for ident in original} == original

    for day in (7, 8):
        start, end = f'2026-09-{day:02}T08:00:00+03:00', f'2026-09-{day:02}T12:00:00+03:00'
        workspace = h.shift(workspace, start=start, end=end, people=[{
            'person_id': h.seed['people'][0], 'start': start, 'end': end, 'team_lead': True}])['workspace']
    data = h.snapshot(workspace)
    defaults = [p for p in data['policies'] if p['id'] in {
        scoring_defaults.line_id(h.seed['department_id'], key)
        for key in ('workload', 'lead:' + h.seed['type'], 'lead-cap:' + h.seed['type'])}]
    assert len(defaults) == 3
    assert all(p['starts_on'] == '2000-01-01' for p in defaults)
    goals = balance_goals(data)
    assert {p['definition']['rule']['metric'] for p in goals} == {'workload', 'lead_turns'}
    assert all(goal_intervals(data, p, h.seed['people'][0], date(2026, 9, 6), date(2026, 9, 13)) == [
        (date(2026, 9, 6), date(2026, 9, 13))] for p in goals)
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in evaluate(data)['findings'])
# Include this file in hosted supervision and team-lead qualification.
