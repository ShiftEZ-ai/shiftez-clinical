"""Policy-line metadata and version-bound scalar filters match shared condition trees."""
from copy import deepcopy
from datetime import date

import pytest

from apps.api.services import bulk_selection
from packages.domain import conditions
from .test_requests_search import both  # noqa: F401
from .test_bulk_policy_line_versions import versions


def test_policy_line_revision_is_row_metadata_in_the_domain_adapter():
    day = date(2026, 10, 4)
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'people': {}, '_bulk_day': day}
    row = {'id': 'line', 'revision': 7, 'numbers': {'max': {'value': 3}}}
    held = bulk_selection._held(data, day, day, row, 'policy_lines', {}, fields={'revision', 'max'})
    assert held('revision') == {7}
    assert held('max') == {3}
    assert conditions.evaluate({'conditions': [{'field': 'revision', 'op': 'gte', 'value': 7}]}, held,
        conditions.EvaluationContext(timezone='Asia/Riyadh', week_start=6, today=day, table_key='policy_lines'))


@pytest.mark.parametrize('binding', ['status', 'policy_id', 'unbound'])
def test_included_lines_apply_complete_flat_and_tree_predicates_when_version_bound(both, binding):
    h = both
    retired, active, lines = versions(h)
    condition = {'field': 'note', 'values': ['Does not match the authored note']}
    base = {'collection': 'policy_lines', 'include_ids': [lines[0]['id']], 'where': {}}
    if binding != 'unbound':
        base['where']['filters'] = {binding: ['retired' if binding == 'status' else retired['id']]}
    results = []
    for path in ('flat', 'tree'):
        selected = deepcopy(base)
        if path == 'flat':
            selected['where'].setdefault('filters', {})['note'] = condition['values']
        else:
            selected['where']['conditions'] = {'conditions': [condition]}
        expected = 1 if binding == 'unbound' else 0
        counts = []
        for endpoint in ('resolve', 'rows'):
            payload = selected if endpoint == 'resolve' else {'selection': selected, 'columns': ['note', 'status']}
            response = h.client('coordinator').post('/api/bulk/' + endpoint, json=payload)
            assert response.status_code == 200, response.text
            counts.append(response.json()['count'])
        assert counts == [expected, expected]
        results.append(counts)
    assert results[0] == results[1]


def test_policy_line_revision_numeric_filter_applies_in_api_reads(both):
    h = both
    _, active, lines = versions(h)
    selected = {'collection': 'policy_lines', 'where': {'filters': {'policy_id': [active['id']]},
        'conditions': {'conditions': [{'field': 'revision', 'op': 'gte', 'value': active['revision']}]}}}
    for endpoint in ('resolve', 'rows'):
        response = h.client('coordinator').post('/api/bulk/' + endpoint,
            json=selected if endpoint == 'resolve' else {'selection': selected, 'columns': ['revision']})
        assert response.status_code == 200, response.text
        assert response.json()['count'] == len(lines)
