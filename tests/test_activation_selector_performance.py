"""Expanded override copies are indexed without changing their date/scope semantics."""
from copy import deepcopy
import pytest

from packages.domain import conditions
from packages.domain.validation import evaluate
from tests.test_backend_scale import scale_department, measure
from tests.test_feasibility_creation_performance import proposed_batch
from tests.test_performance_budget import python_budget


@pytest.mark.postgres
def test_department_batch_activation_budget(pg, monkeypatch):
    workspace, data, *_ = scale_department(pg)
    changes = proposed_batch(pg, data)
    pg.client('approver')
    reply, timing = measure(pg, 'batch4-activation', lambda: pg.command('approver', 'policy.activate_many',
        {'items': changes, 'reason': 'Synthetic independent scale review'}))
    assert len(reply['records']) == len(changes)
    assert sum(timing['sql'].values()) <= 155, timing
    snapshot = pg.snapshot(workspace)
    actual = evaluate(deepcopy(snapshot))
    def scanned(data, values, *, person_id=None):
        return (p for p in data.get('policies', []) if p['id'] in values and
            (person_id is None or (p.get('template_line') or {}).get('person_id') == person_id))
    monkeypatch.setattr(conditions, 'selector_policies', scanned)
    assert evaluate(deepcopy(snapshot)) == actual
    return locals()


@pytest.mark.wall_benchmark
@pytest.mark.postgres
def test_department_batch_activation_wall_budget(pg, monkeypatch):
    state = test_department_batch_activation_budget(pg, monkeypatch)
    assert state['timing']['wall_seconds'] < python_budget(5), state['timing']


def test_selector_index_preserves_copies_and_rebuilds():
    copies = [{'id': 'same', 'template_line': {'person_id': owner},
               'definition': {'rule': {'kind': 'max_duties'}}, 'starts_on': day}
              for owner, day in [('a', '2026-10-01'), ('a', '2026-11-01'), ('b', '2026-10-01')]]
    data = {'policies': copies, '_line_index_cache': {}}
    assert list(conditions.selector_policies(data, ['same'], person_id='a')) == copies[:2]
    assert list(conditions.selector_policies(data, ['same'])) == copies
    copies.append({**copies[0], 'starts_on': '2026-12-01'})
    assert list(conditions.selector_policies(data, ['same'], person_id='a')) == [copies[0], copies[1], copies[3]]
    data['policies'] = copies[:1]
    assert list(conditions.selector_policies(data, ['same'])) == copies[:1]
    assert list(conditions.selector_policies(data, ['missing'])) == []
