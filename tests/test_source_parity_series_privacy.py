"""Recurring provenance stays private in every staff projection; publication evidence stays unchanged."""
import copy
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.schedule_view import staff_shift
from packages.contracts.schemas import PERMISSIONS
from tests.governance_support import identity
from tests.test_series_reconciliation import template, materialise
from tests.test_source_parity_series import leave


FIELDS = [{'key': 'visible_note', 'label': 'Visible note', 'kind': 'text', 'visibility': 'staff'},
          {'key': 'private_note', 'label': 'Private note', 'kind': 'text', 'visibility': 'planners'}]
ATTRIBUTES = {'visible_note': 'Synthetic staff message', 'private_note': 'Synthetic confidential planner text'}


def test_shared_staff_projection_keeps_only_own_and_paired_metadata_without_mutating_source():
    shift = {'participants': [{'person_id': 'mine', 'supervisor_person_id': 'paired'}, {'person_id': 'paired'},
                              {'person_id': 'unrelated'}],
             'attributes': ATTRIBUTES, 'type_snapshot': {'definition': {'fields': FIELDS}},
             'series_snapshot': {'definition': {'default_person_ids': ['mine', 'paired', 'unrelated'], 'attributes': ATTRIBUTES}},
             'template_skipped': [{'person_id': 'unrelated', 'code': 'EXTERNAL_ROTATION'}],
             'usual_assignee_unavailability': [{'person_id': pid, 'codes': ['UNAVAILABLE'], 'fully_unavailable': True}
                                               for pid in ('mine', 'paired', 'unrelated')]}
    before = copy.deepcopy(shift)
    projected = staff_shift(shift, 'mine')
    assert {row['person_id'] for row in projected['participants']} == {'mine', 'paired'}
    assert {row['person_id'] for row in projected['usual_assignee_unavailability']} == {'mine', 'paired'}
    assert projected['attributes'] == {'visible_note': ATTRIBUTES['visible_note']}
    assert projected['type_snapshot']['definition']['fields'] == FIELDS[:1]
    assert 'series_snapshot' not in projected and 'unrelated' not in json.dumps(projected)
    assert 'template_skipped' not in projected
    assert shift == before


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_staff_publication_and_window_hide_private_series_data_but_planners_keep_frozen_evidence(database, request):
    h = request.getfixturevalue(database)
    own, other = h.seed['people'][:2]
    duty = h.configure('assignment_types', {'name': 'Synthetic private recurring duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'fields': FIELDS}})['record']
    recurring = template(h, days=(0,), assignment_type_id=duty['id'], definition={'anchor': '2026-10-04',
        'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [own, other],
        'attributes': ATTRIBUTES, 'close_when_all_usual_unavailable': True})
    absence = leave(h, person=other)
    workspace = materialise(h, recurring)
    publication_id = h.publish(h.detail(workspace)['workspace'])['publication_id']
    with transaction(h.app.state.engine) as db:
        frozen = copy.deepcopy(db.get(m.Publication, publication_id).snapshot)
    publication_path = '/api/publications/' + publication_id
    window_path = '/api/schedule/sessions?start=2026-10-05&end=2026-10-06'
    coordinator = h.get('coordinator', publication_path)['snapshot']
    planned = coordinator['shifts'][0]
    assert planned['series_snapshot']['definition']['attributes'] == ATTRIBUTES
    assert planned['usual_assignee_unavailability'] == [{'person_id': other, 'codes': ['UNAVAILABLE'], 'fully_unavailable': True}]
    assert [(row['person_id'], row['code']) for row in planned['template_skipped']] == [(other, 'UNAVAILABLE')]
    assert not planned['closed'] and planned['series_state_frozen']
    for data in (h.get('staff', publication_path)['snapshot'], h.get('staff', window_path)):
        [shift] = data['shifts']
        assert shift['attributes'] == {'visible_note': ATTRIBUTES['visible_note']}
        assert shift['type_snapshot']['definition']['fields'][0]['key'] == 'visible_note'
        assert len(shift['type_snapshot']['definition']['fields']) == 1
        assert 'series_snapshot' not in shift and shift['usual_assignee_unavailability'] == []
        assert 'template_skipped' not in shift
        assert ATTRIBUTES['private_note'] not in json.dumps(data) and other not in json.dumps(data)
    for format in ('json', 'csv', 'assignments', 'ics', 'print'):
        response = h.client('staff').get(f'/api/reports/{publication_id}?format={format}')
        assert response.status_code == 200, response.text
        assert ATTRIBUTES['private_note'] not in response.text and other not in response.text
        assert 'series_snapshot' not in response.text and 'usual_assignee_unavailability' not in response.text
    calendar = h.get('staff', '/api/calendar?start=2026-10-05&end=2026-10-06')
    assert len(calendar['events']) == 1 and calendar['events'][0]['person_id'] == own
    assert calendar['sessions'] == [] and other not in json.dumps(calendar)
    assert ATTRIBUTES['private_note'] not in json.dumps(calendar) and 'series_snapshot' not in json.dumps(calendar)
    feed = h.command('staff', 'calendar_feed.create', {})['path']
    subscribed = h.client('staff').get(feed)
    assert subscribed.status_code == 200 and 'BEGIN:VEVENT' in subscribed.text
    assert ATTRIBUTES['private_note'] not in subscribed.text and other not in subscribed.text
    assert 'series_snapshot' not in subscribed.text and 'usual_assignee_unavailability' not in subscribed.text
    assert h.get('coordinator', window_path)['shifts'][0]['series_snapshot'] == planned['series_snapshot']
    assert h.get('coordinator', window_path)['shifts'][0]['template_skipped'] == planned['template_skipped']
    h.command('coordinator', 'availability.retract', {'reason': 'Synthetic retraction'}, absence['id'], absence['revision'])
    assert h.get('coordinator', publication_path)['snapshot'] == coordinator
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == frozen


def test_team_permission_map_controls_skipped_placement_disclosure_in_both_reads(h, monkeypatch):
    own, other = h.seed['people'][:2]
    identity(h, 'linked_planner', ['staff', 'coordinator'], own)
    identity(h, 'staff_lead', ['staff', 'programme_lead'], own)
    leave(h, person=other)
    recurring = template(h, days=(0,), acknowledge_overlaps=True, definition={'anchor': '2026-10-04',
        'days': [{'weekday': 0, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [own, other]})
    h.policy({'kind': 'max_hours', 'hours': 1}, {'person_id': other}, severity='advisory')
    workspace = materialise(h, recurring)
    first = h.detail(workspace)['shifts'][0]['id']
    workspace = h.shift(workspace, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00',
                        people=[other])['workspace']
    publication = h.publish(workspace)['publication_id']
    paths = ['/api/publications/' + publication, '/api/schedule/sessions?start=2026-10-05&end=2026-10-06']
    # Preserve the existing narrower publication audience for a staff member who is also a programme lead.
    lead_publication = h.get('staff_lead', paths[0])
    assert 'template_skipped' not in lead_publication['snapshot']['shifts'][0]
    assert set(lead_publication['original_validation']) == {'valid_at_publication'}
    assert h.get('staff_lead', paths[1])['shifts'][0]['template_skipped']
    for permitted in (('coordinator',), ('staff',), ()):
        for permission in ('schedule.read_team', 'publications.read_team'):
            monkeypatch.setitem(PERMISSIONS, permission, permitted)
        for role in ('linked_planner', 'staff'):
            for path in paths:
                data = h.get(role, path)
                shift = next(row for row in data.get('snapshot', data)['shifts'] if row['id'] == first)
                allowed = bool(set(permitted) & ({'staff', 'coordinator'} if role == 'linked_planner' else {'staff'}))
                assert bool(shift.get('template_skipped')) is allowed
                assert (other in json.dumps(shift)) is allowed
                if 'original_validation' in data:
                    assert ('findings' in data['original_validation']) is allowed
            current = h.get(role, paths[0] + '/coverage')['evaluation']
            assert bool(current['findings']) is allowed
            assert h.client(role).get('/api/reports/' + publication + '?person_id=' + other).status_code == (200 if allowed else 403)
