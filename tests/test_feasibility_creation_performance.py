"""Admission of a department-sized rule batch does not construct unused reach previews."""
from datetime import date
import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import policies
from packages.contracts import schemas as s
from tests.test_backend_scale import scale_department, measure
from tests.test_performance_budget import python_budget

pytestmark = pytest.mark.postgres


def proposed_batch(h, data, count=12):
    with transaction(h.app.state.engine, write=True) as db:
        definition = s.PolicyDefinition.model_validate({'severity': 'advisory',
            'rule': {'kind': 'max_duties', 'maximum': 20, 'window': 'week'},
            'per_person': [{'person_id': pid, 'amount': 21} for pid in list(data['people'])[:38]]}).model_dump(mode='json')
        rows = [m.Policy(department_id=h.seed['department_id'], name=f'Synthetic batch rule {i}', kind='max_duties',
            definition=definition, status='proposed', active=True, starts_on=date(2026, 10, 1),
            ends_on=date(2026, 11, 1), owner='Synthetic', source='Scale regression',
            created_by=h.seed['identities']['coordinator']) for i in range(count)]
        db.add_all(rows)
        db.flush()
        return [{'policy_id': row.id, 'expected_revision': row.revision} for row in rows]


def test_feasibility_batch_admission_budget(pg, monkeypatch):
    workspace, data, *_ = scale_department(pg)
    changes = proposed_batch(pg, data)
    pg.client('coordinator')
    checked, original = [], policies.check_policy
    def admission(*args, **kwargs):
        assert kwargs.get('preview') is False
        result = original(*args, **kwargs)
        checked.append(result)
        return result
    monkeypatch.setattr(policies, 'check_policy', admission)
    reply, timing = measure(pg, 'batch4-feasibility', lambda: pg.command('coordinator', 'job.create',
        {'kind': 'feasibility', 'workspace_id': workspace['id'], 'rule_changes': changes}, revision=workspace['revision']))
    assert len(checked) == len(changes) and all(row['compatible'] for row in checked)
    assert reply['job']['kind'] == 'feasibility'
    assert sum(timing['sql'].values()) <= 170, timing
    return locals()


@pytest.mark.wall_benchmark
def test_feasibility_batch_admission_wall_budget(pg, monkeypatch):
    state = test_feasibility_batch_admission_budget(pg, monkeypatch)
    assert state['timing']['wall_seconds'] < python_budget(5), state['timing']
