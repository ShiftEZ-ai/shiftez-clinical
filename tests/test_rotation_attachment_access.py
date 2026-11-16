"""Rotation visibility follows the current attachment, including independently edited relationships."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services.training import catalogue_scope
from .governance_support import identity
from .test_rotation_attachments import attachments, rotation_value


def assert_rotation_access(h, role, person_id, trainee_id, rotation_id, allowed):
    actor = Actor(identity_id=h.seed['identities'][role], department_id=h.seed['department_id'],
                  organisation_id=h.seed['organisation_id'], display_name='Synthetic mentor',
                  roles=frozenset({'staff'}), person_id=person_id, session_id='synthetic')
    expected = [rotation_id] if allowed else []
    with transaction(h.app.state.engine) as db:
        visible = db.scalars(select(m.Rotation.id).where(m.Rotation.department_id == actor.department_id,
            catalogue_scope(db, actor, 'rotations', m.Rotation)))
        assert list(visible) == expected
    for subject in (person_id, trainee_id):
        rows = h.get(role, f'/api/people/{subject}/rotations?start=2026-10-01&end=2026-11-01')['rotations']
        assert [row['id'] for row in rows] == expected


def test_staff_rotation_reads_follow_attachment_edits_and_ignore_the_retired_mentor_column(h):
    value = rotation_value(h)
    trainee, stale, current = h.seed['people']
    successor = h.configure('people', {'display_name': 'Synthetic successor supervisor'})['record']['id']
    rotation = h.configure('rotations', value, role='coordinator')['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Rotation, rotation['id']).mentor_id = stale
    for role, person in (('staff_stale_mentor', stale), ('staff_current_mentor', current), ('staff_successor', successor),
                         ('staff_trainee', trainee)):
        identity(h, role, ['staff'], person=person)
    assert_rotation_access(h, 'staff_current_mentor', current, trainee, rotation['id'], True)
    assert_rotation_access(h, 'staff_stale_mentor', stale, trainee, rotation['id'], False)
    assert_rotation_access(h, 'staff_successor', successor, trainee, rotation['id'], False)
    assert_rotation_access(h, 'staff_trainee', trainee, trainee, rotation['id'], True)

    [attachment] = attachments(h)
    fields = ('person_id', 'mentor_id', 'programme_id', 'starts_on', 'ends_on',
              'assignment_type_ids', 'service_ids', 'strength')
    changed = h.configure('mentorships', {**{key: attachment[key] for key in fields}, 'mentor_id': successor},
                          attachment['id'], attachment['revision'], role='coordinator')['record']
    assert_rotation_access(h, 'staff_current_mentor', current, trainee, rotation['id'], False)
    assert_rotation_access(h, 'staff_successor', successor, trainee, rotation['id'], True)
    assert_rotation_access(h, 'staff_stale_mentor', stale, trainee, rotation['id'], False)
    live = h.get('staff_successor', f'/api/people/{trainee}/rotations?start=2026-10-01&end=2026-11-01')['rotations']
    assert live[0]['mentor_id'] == successor

    h.configure('mentorships', {**{key: changed[key] for key in fields}, 'person_id': stale},
                changed['id'], changed['revision'], role='coordinator')
    assert_rotation_access(h, 'staff_successor', successor, trainee, rotation['id'], False)
    assert_rotation_access(h, 'staff_stale_mentor', stale, trainee, rotation['id'], False)
    assert_rotation_access(h, 'staff_trainee', trainee, trainee, rotation['id'], True)
