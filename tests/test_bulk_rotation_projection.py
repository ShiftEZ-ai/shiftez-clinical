"""Bulk rotation reads project the same live mentor as the canonical catalogue."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import bulk_selection as bulk
from .test_rotation_attachments import attachments, rotation_value


FIELDS = ('person_id', 'mentor_id', 'programme_id', 'starts_on', 'ends_on',
          'assignment_type_ids', 'service_ids', 'strength')


def post(h, endpoint, payload):
    response = h.client('coordinator').post('/api/bulk/' + endpoint, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def make_rotations(h):
    value = rotation_value(h)
    first = h.configure('rotations', value, role='coordinator')['record']
    second = h.configure('rotations', {**value, 'mentor_id': h.seed['people'][1],
        'starts_on': '2026-11-01', 'ends_on': '2026-12-01'}, role='coordinator')['record']
    # Legacy values deliberately disagree with both live attachments.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Rotation, first['id']).mentor_id = second['mentor_id']
        db.get(m.Rotation, second['id']).mentor_id = first['mentor_id']
    return first, second


def assert_projection(h, expected):
    canonical = {r['id']: r['mentor_id'] for r in h.get('coordinator', '/api/catalogues')['rotations']}
    assert canonical == expected
    selected = {'collection': 'rotations'}
    resolved = post(h, 'resolve', selected)
    assert {r['id']: r['mentor_id'] for r in resolved['sample']} == expected
    page = post(h, 'rows', {'selection': selected, 'columns': ['mentor_id'],
        'sort': [{'field': 'mentor_id'}], 'group': 'mentor_id'})
    assert {r['id']: r['mentor_id'] for r in page['rows']} == expected
    values = sorted(expected.values(), key=lambda v: (v is None, v or ''))
    assert [r['mentor_id'] for r in page['rows']] == values
    assert page['groups'] == [{'value': value, 'count': values.count(value)} for value in dict.fromkeys(values)]
    for mentor in h.seed['people'][1:]:
        ids = {rid for rid, current in expected.items() if current == mentor}
        for where in ({'filters': {'mentor_id': [mentor]}},
                      {'conditions': {'conditions': [{'field': 'mentor_id', 'values': [mentor]}]}}):
            selection = {'collection': 'rotations', 'where': where}
            assert {r['id'] for r in post(h, 'resolve', selection)['sample']} == ids
            assert {r['id'] for r in post(h, 'rows', {'selection': selection, 'columns': ['mentor_id']})['rows']} == ids
    with transaction(h.app.state.engine) as db:
        assert dict(db.execute(select(m.Rotation.id, m.Rotation.attachment_mentor_id)).all()) == expected


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_bulk_rotation_mentor_reads_filters_sort_and_groups_follow_live_attachment(request, database):
    h = request.getfixturevalue(database)
    first, second = make_rotations(h)
    expected = {first['id']: first['mentor_id'], second['id']: second['mentor_id']}
    assert_projection(h, expected)
    attachment = next(r for r in attachments(h) if r['mentor_id'] == first['mentor_id'])
    changed = h.configure('mentorships', {**{key: attachment[key] for key in FIELDS},
        'mentor_id': second['mentor_id']}, attachment['id'], attachment['revision'], role='coordinator')['record']
    expected[first['id']] = second['mentor_id']
    assert_projection(h, expected)
    # An independently moved attachment no longer belongs to this rotation's trainee.
    h.configure('mentorships', {**{key: changed[key] for key in FIELDS}, 'person_id': h.seed['people'][2]},
        changed['id'], changed['revision'], role='coordinator')
    expected[first['id']] = None
    assert_projection(h, expected)
    # Matching trainees still cannot borrow another department's attachment.
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Synthetic other department')
        db.add(other)
        db.flush()
        mentorship = db.get(m.Mentorship, changed['id'])
        mentorship.person_id, mentorship.department_id = first['person_id'], other.id
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Rotation, first['id']).attachment_mentor_id is None
    assert_projection(h, expected)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_rotation_page_cache_tracks_attachment_changes_without_rotation_or_input_revisions(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    first, _ = make_rotations(h)
    selection = {'collection': 'rotations', 'where': {'filters': {'mentor_id': [first['mentor_id']]}}}
    payload = {'selection': selection, 'columns': ['mentor_id'], 'group': 'mentor_id'}
    before = post(h, 'rows', payload)
    assert before['count'] == 1
    with transaction(h.app.state.engine, write=True) as db:
        link = db.get(m.RotationAttachment, first['id'])
        mentorship = db.get(m.Mentorship, link.mentorship_id)
        mentorship.mentor_id = h.seed['people'][1]
        mentorship.revision += 1
    calls, original = [], bulk.resolve_targets

    def capture(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(bulk, 'resolve_targets', capture)
    after = post(h, 'rows', payload)
    assert len(calls) == 1 and after['count'] == 0 and after['rows'] == [] and after['groups'] == []
