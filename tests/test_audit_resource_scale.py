"""Resource totals and overload intervals stay exact as retained sessions grow."""
from datetime import timedelta
import random
import time

import pytest

from packages.domain import resources
from packages.domain.time import aware


def snapshot(reservations, capacity=3):
    origin = aware('2026-10-01T00:00:00Z')
    shifts = []
    for sid, start, end, units in reservations:
        shifts.append({'id': sid, 'start': (origin + timedelta(minutes=start)).isoformat(),
                       'end': (origin + timedelta(minutes=end)).isoformat(),
                       'service_ids': [], 'location_id': 'room',
                       'type_snapshot': {'definition': {'resources': [
                           {'resource_pool_id': 'pool', 'units': units, 'from_minute': 0}]}}})
    return {'organisation': {'timezone': 'UTC'}, 'services': {}, 'locations': {},
            'resource_pools': {'pool': {'name': 'Synthetic pool', 'capacity': capacity}}, 'shifts': shifts}


def findings(source, focus=None):
    shifts = source['shifts']
    result = []
    resources.evaluate_resources(source, {s['id']: s['type_snapshot']['definition'] for s in shifts}, shifts,
        {s['id'] for s in shifts} if focus is None else focus,
        lambda code, message, sid, **detail: result.append((code, sid, detail)),
        lambda *args: [], lambda *args: True)
    return result


@pytest.mark.parametrize('count', [250, 500, 1000])
@pytest.mark.parametrize('path', ['validation', 'report'])
def test_sparse_reservations_have_linear_overlap_budget(monkeypatch, count, path):
    source = snapshot([(str(i), 40*i, 40*i+20, 1) for i in range(count)], capacity=1)
    calls = 0
    original = resources.overlaps

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(resources, 'overlaps', counted)
    if path == 'validation':
        assert findings(source) == []
    else:
        row, = resources.resource_report(source)['rows']
        assert row['peak_required_units'] == 1
        assert row['sessions'] == count
        assert row['unit_hours'] == round(count / 3, 4)
    assert calls <= 4*count, f'{path}: {calls} overlap checks for {count} sparse reservations'


def test_quantities_adjacency_duplicate_demands_and_focus():
    source = snapshot([('a', 0, 60, 2), ('b', 30, 90, 2), ('c', 60, 90, 1)], capacity=3)
    # Two reservations by one session must contribute units but produce one finding per interval.
    source['shifts'][1]['type_snapshot']['definition']['resources'].append(
        {'resource_pool_id': 'pool', 'units': 1, 'from_minute': 30})
    result = findings(source, {'b'})
    origin = aware(source['shifts'][0]['start'])
    assert result == [
        ('POOLED_RESOURCE_CAPACITY', 'b', {'a': origin + timedelta(minutes=30),
            'b': origin + timedelta(minutes=60),
            'extra': {'resource_pool_id': 'pool', 'required_units': 4, 'capacity': 3}}),
        ('POOLED_RESOURCE_CAPACITY', 'b', {'a': origin + timedelta(minutes=60),
            'b': origin + timedelta(minutes=90),
            'extra': {'resource_pool_id': 'pool', 'required_units': 4, 'capacity': 3}})]
    row, = resources.resource_report(source)['rows']
    assert row['peak_required_units'] == 4
    assert row['unit_hours'] == 5
    assert row['sessions'] == 3
    assert findings(source, set()) == []


def test_sweep_matches_independent_interval_oracle():
    rng = random.Random(8)
    for _ in range(30):
        source = snapshot([(str(i), start := rng.randrange(30), start + rng.randrange(1, 12),
                            rng.randrange(1, 4)) for i in range(12)])
        focus = {str(i) for i in range(0, 12, 2)}
        intervals = [(s['id'], aware(s['start']), aware(s['end']),
                      s['type_snapshot']['definition']['resources'][0]['units']) for s in source['shifts']]
        bounds = sorted({t for _, a, b, _ in intervals for t in (a, b)})
        expected, peak = [], 0
        for a, b in zip(bounds, bounds[1:]):
            live = [(sid, units) for sid, start, end, units in intervals if start < b and a < end]
            total = sum(units for _, units in live)
            peak = max(peak, total)
            if total > 3:
                expected.extend(('POOLED_RESOURCE_CAPACITY', sid, {'a': a, 'b': b,
                    'extra': {'resource_pool_id': 'pool', 'required_units': total, 'capacity': 3}})
                    for sid in sorted({sid for sid, _ in live} & focus))
        assert findings(source, focus) == expected
        assert resources.resource_report(source)['rows'][0]['peak_required_units'] == peak


@pytest.mark.cpu_budget
def test_sparse_resource_cpu_budget():
    import coverage
    source = snapshot([(str(i), 40*i, 40*i+20, 1) for i in range(1000)], capacity=1)
    started = time.process_time()
    assert findings(source) == []
    assert resources.resource_report(source)['rows'][0]['peak_required_units'] == 1
    budget = 0.5 * (7 if coverage.Coverage.current() else 1)
    assert time.process_time() - started < budget
