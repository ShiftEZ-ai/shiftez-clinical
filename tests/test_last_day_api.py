"""Selection, rows and paged Requests compare the same shown last day, across New Year."""
from .clock_support import freeze_clock
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import local_midnight
from .test_requests_search import both  # noqa: F401


@pytest.mark.parametrize('collection,field,kind', [('availability_patterns', 'ends_on', 'weekly'), ('requests', 'end', 'leave'),
    ('availability_patterns', 'last_day', 'weekly'), ('requests', 'last_day', 'leave')])
def test_last_day_trees_agree_between_bulk_and_paged_sources_at_year_end(both, monkeypatch, collection, field, kind):
    h = both
    day = date(2026, 12, 31)
    freeze_clock(monkeypatch, lambda: datetime(2026, 12, 31, 6, tzinfo=timezone.utc))
    with transaction(h.app.state.engine, write=True) as db:
        zone = db.get(m.Organisation, h.seed['organisation_id']).timezone
        common = dict(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                      created_by=h.seed['identities']['coordinator'])
        patterns = [m.AvailabilityPattern(**common, starts_on=date(2026, 12, 1), ends_on=end, weekdays=[0],
            start_time='08:00', end_time='12:00', status='active', note='Synthetic last day parity')
            for end in (day, day + timedelta(days=1), day + timedelta(days=2), None)]
        requests = [m.RequestRecord(**common, start=local_midnight(date(2026, 12, 1), zone),
            end=local_midnight(day + timedelta(days=n), zone), private_reason='Synthetic last day parity')
            for n in range(3)]
        db.add_all([*patterns, *requests])
        db.flush()
        references = {'availability_patterns': [row.id for row in patterns], 'requests': [row.id for row in requests]}
    today = {'kind': 'relative', 'token': 'today'}
    cases = [('is', '2026-12-31', [1]), ('is', today, [1]),
             ('before', today, [0]), ('after', today, [2]),
             ('on_or_before', today, [0, 1]), ('on_or_after', today, [1, 2]),
             ('between', [today, '2027-01-01'], [1, 2]),
             ('within', {'kind': 'relative', 'token': 'this_year'}, [0, 1]),
             ('empty', None, [3]), ('not_empty', None, [0, 1, 2]),
             ('any', None, [1])]
    for op, operand, indexes in cases:
        expected = {references[collection][index] for index in indexes if index < len(references[collection])}
        leaf = {'field': field, 'op': op, **({'value': operand} if operand is not None else {})}
        if op == 'any':
            leaf['values'] = ['2026-12-31']
        tree = {'conditions': [leaf]}
        selection = {'collection': collection, 'where': {'conditions': tree}}
        resolved = h.client('approver').post('/api/bulk/resolve', json=selection)
        rows = h.client('approver').post('/api/bulk/rows', json={'selection': selection, 'limit': 1})
        page = h.client('approver').get('/api/requests/all', params={'type': kind, 'limit': 1,
            'filter': json.dumps({'conditions': {'conditions': [{**leaf, 'field': 'last_day' if field == 'last_day' else 'end'}]}})})
        assert resolved.status_code == rows.status_code == page.status_code == 200, resolved.text + rows.text + page.text
        assert resolved.json()['count'] == rows.json()['count'] == page.json()['total'] == len(expected)
        assert {row['id'] for row in resolved.json()['sample']} == expected
        if expected:
            assert rows.json()['rows'][0]['id'] in expected and page.json()['items'][0]['id'] in expected
        for offset in range(1, len(expected)):
            more = h.client('approver').post('/api/bulk/rows', json={'selection': selection, 'offset': offset, 'limit': 1})
            assert more.status_code == 200 and more.json()['rows'][0]['id'] in expected
    preview = h.client('approver').post('/api/conditions/resolve-operand', json={'field': 'ends_on', 'operand': today})
    assert preview.status_code == 200 and preview.json()['from'] == preview.json()['to'] == '2026-12-31'
