"""A clinic's derived locations follow planner edits across cached collection pages."""
import pytest


def post(h, endpoint, payload):
    response = h.client('coordinator').post('/api/bulk/' + endpoint, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('path', ['related_filter', 'condition', 'sort_group'])
def test_cached_related_clinic_locations_follow_session_location_changes(h, path):
    first = h.shift(h.workspace())
    second = h.shift(first['workspace'], service=1, location=1)
    selection = {'collection': 'eligibilities', 'where': {'as_of': '2026-10-04',
        'filters': {'service_id': h.seed['services'][:2]}}}
    if path == 'related_filter':
        selection['where']['filters']['service.location_ids'] = [h.seed['locations'][0]]
    elif path == 'condition':
        selection['where']['conditions'] = {'conditions': [
            {'field': 'location_ids', 'values': [h.seed['locations'][0]]}]}
    payload = {'selection': selection, 'columns': ['service_id', 'service.location_ids'],
        'sort': [{'field': 'service.location_ids'}], 'group': 'service.location_ids', 'limit': 1}
    before = post(h, 'rows', payload)
    assert before['count'] > 1 and before['has_more']
    assert post(h, 'rows', payload) == before
    h.command('coordinator', 'shift.update', {
        'workspace_id': first['workspace']['id'], 'shift_id': first['shift_id'],
        'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][1],
        'service_ids': [h.seed['services'][0]], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'participants': [{'person_id': h.seed['people'][0],
            'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}],
        'attributes': {}}, revision=second['workspace']['revision'])
    live = post(h, 'resolve', selection)
    first_page = post(h, 'rows', payload)
    later_page = post(h, 'rows', {**payload, 'offset': 1})
    assert first_page['count'] == later_page['count'] == live['count']
    assert first_page['groups'] == later_page['groups']
    if path == 'sort_group':
        assert live['count'] == before['count']
        assert first_page['groups'] == [{'value': [h.seed['locations'][1]], 'count': live['count']}]
        assert first_page['rows'][0]['service.location_ids'] == later_page['rows'][0]['service.location_ids'] == [h.seed['locations'][1]]
    else:
        assert live['count'] == 0
        assert first_page['rows'] == later_page['rows'] == first_page['groups'] == []
