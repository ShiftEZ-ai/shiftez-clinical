"""Exposure's server projection keeps full-axis totals while bounding the dense matrix."""
import json
from datetime import date

import pytest
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction

from tests.test_backend_scale import measure, scale_department


@pytest.fixture(scope='module')
def exposure_measurement(scale_harness):
    h = scale_harness()
    scale_department(h, assigned=False)
    h.client('coordinator')
    path = '/api/fairness/exposure?starts_on=2026-10-01&ends_on=2026-11-01'
    full, before = measure(h, 'exposure-full', lambda: h.client('coordinator').get(path))
    limited, after = measure(h, 'exposure-limit-30', lambda: h.client('coordinator').get(path + '&limit=30'))
    assert full.status_code == limited.status_code == 200
    measured = {'full': dict(before, payload_bytes=len(full.content),
                            cells=sum(map(len, full.json()['cells']))),
                'limit_30': dict(after, payload_bytes=len(limited.content),
                                cells=sum(map(len, limited.json()['cells'])))}
    print('exposure payload', measured)
    return full.json(), limited.json(), measured


def test_exposure_department_read_measurement(exposure_measurement, record_property):
    full, _, measured = exposure_measurement
    assert len(full['rows']) == len(full['cols']) == 163
    record_property('measurement', json.dumps(measured))


def test_department_exposure_payload_and_query_budget(exposure_measurement):
    full, limited, measured = exposure_measurement
    assert limited['total_rows'] == limited['total_cols'] == 163
    assert len(limited['rows']) == len(limited['cols']) == 30
    assert limited['rows'] == full['rows'][:30] and limited['cols'] == full['cols'][:30]
    assert limited['cells'] == [row[:30] for row in full['cells'][:30]]
    assert limited['row_totals'] == [0] * 30
    assert measured['limit_30']['cells'] == 900
    assert measured['limit_30']['payload_bytes'] < 12_000
    assert measured['limit_30']['payload_bytes'] < measured['full']['payload_bytes'] / 10
    assert measured['limit_30']['sql'].get('SELECT', 0) <= 30


@pytest.fixture
def paired_exposure(h):
    publication_id = h.publish(h.shift(h.workspace())['workspace'])['publication_id']
    a, b, c = h.seed['people']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.FairnessLedger), [
            {'department_id': h.seed['department_id'], 'publication_id': publication_id,
             'person_id': row, 'other_person_id': col, 'metric_key': key,
             'bucket': date(2026, 10, 4), 'value': value * multiplier}
            for key, multiplier in [('pair', 1), ('supervised', 2)]
            for row, col, value in [(a, b, 10), (a, c, 30), (b, a, 50), (b, c, 10), (c, a, 5), (c, b, 5)]])
    return h, (a, b, c)


@pytest.mark.parametrize('pairing,multiplier', [('present', 1), ('supervised', 2)])
def test_top_exposure_preserves_full_axes_totals_and_cells(paired_exposure, pairing, multiplier):
    h, (a, b, c) = paired_exposure
    path = f'/api/fairness/exposure?starts_on=2026-10-01&ends_on=2026-11-01&pairing={pairing}'
    full = h.get('coordinator', path)
    limited = h.get('coordinator', path + '&limit=2')
    assert [r['person_id'] for r in limited['rows']] == [b, a]
    assert [r['person_id'] for r in limited['cols']] == [a, c]
    assert limited['cells'] == [[50 * multiplier, 10 * multiplier], [0, 30 * multiplier]]
    assert limited['row_totals'] == [60 * multiplier, 40 * multiplier]
    assert full['row_totals'] == [sum(row) for row in full['cells']]
    assert h.get('coordinator', path + '&limit=3') == full


def test_top_exposure_counts_only_matching_audiences_and_private_rows(paired_exposure):
    h, (a, b, c) = paired_exposure
    path = '/api/fairness/exposure?starts_on=2026-10-01&ends_on=2026-11-01&limit=1'
    who = json.dumps({'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [a]}]})
    only_a = h.get('coordinator', path + '&rows=' + who)
    assert only_a['total_rows'] == 1 and only_a['total_cols'] == 3
    assert only_a['rows'][0]['person_id'] == a and only_a['cols'][0]['person_id'] == c
    assert only_a['cells'] == [[30]] and only_a['row_totals'] == [40]
    only_c = h.get('coordinator', path + '&cols=' + who.replace(a, c))
    assert only_c['total_rows'] == 3 and only_c['total_cols'] == 1
    assert only_c['rows'][0]['person_id'] == a and only_c['cols'][0]['person_id'] == c
    assert only_c['cells'] == [[30]] and only_c['row_totals'] == [30]
    staff = h.get('staff', path)
    assert staff == only_a
    empty = h.get('staff', path + '&rows=' + who.replace(a, b))
    assert empty['total_rows'] == 0 and empty['rows'] == empty['cells'] == empty['row_totals'] == []


@pytest.mark.parametrize('limit', ['0', '501', '-1', '1.5', 'invalid'])
def test_exposure_limit_is_bounded(h, limit):
    h.get('coordinator', '/api/fairness/exposure?limit=' + limit, status=422)
