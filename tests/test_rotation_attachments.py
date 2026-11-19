"""One live attachment for rotation edits and manual duty pairing, with reversible writes."""
from datetime import date

import pytest

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from tests.test_alpha_calendar import programme
from tests.test_attachments import END, START, people, setup
from tests.test_residency import lead_of, other_programme


REASON = 'Synthetic attachment lifecycle check'


def rotation_value(h, service=True):
    p, member, cohort = programme(h)
    h.configure('programme_memberships', {'person_id': member['person_id'], 'programme_id': p['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'}, member['id'], member['revision'], role='coordinator')
    typ = h.configure('rotation_types', {'name': 'Synthetic attachment rotation', 'starts_on': '2026-01-01'})['record']
    return {'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][2], 'cohort_id': cohort['id'],
            'rotation_type_id': typ['id'], 'service_id': h.seed['services'][0] if service else None,
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}


def attachments(h):
    return h.get('coordinator', '/api/catalogues')['mentorships']


def remove_rotation(h, rotation, kind='rotation.remove', role='coordinator', status=200):
    return h.command(role, kind, {'items': [{'id': rotation['id'], 'revision': rotation['revision']}],
                                  'reason': REASON}, status=status)


def legacy_shared_rotations(h, value):
    first = h.configure('rotations', value, role='coordinator')['record']
    # Old snapshots may contain overlapping rotations; new writes correctly reject them.
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Rotation(department_id=h.seed['department_id'], **{key: val for key, val in value.items()
            if key not in ('mentor_id', 'starts_on', 'ends_on')},
            starts_on=date.fromisoformat(value['starts_on']), ends_on=date.fromisoformat(value['ends_on']))
        db.add(row)
        db.flush()
        link = db.get(m.RotationAttachment, first['id'])
        db.add(m.RotationAttachment(rotation_id=row.id, mentorship_id=link.mentorship_id,
                                   synced_revision=link.synced_revision))
        db.flush()
        second = record(row)
    return first, second


def assert_attachment(attachment, value):
    assert {key: attachment[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')} == {
        key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')}
    assert attachment['service_ids'] == ([value['service_id']] if value['service_id'] else [])
    assert attachment['programme_id'] is None and attachment['assignment_type_ids'] == []
    assert attachment['strength'] == 'preferred'


@pytest.mark.parametrize('service', [False, True])
def test_rotation_creates_updates_clears_and_undoes_one_attachment(h, service):
    value = rotation_value(h, service)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    [attachment] = attachments(h)
    assert_attachment(attachment, value)
    assert rotation['mentor_id'] == value['mentor_id']
    # Re-saving never appends a second record.
    rotation = h.configure('rotations', value, rotation['id'], rotation['revision'], role='coordinator')['record']
    assert [row['id'] for row in attachments(h)] == [attachment['id']]
    changed_value = {**value, 'mentor_id': h.seed['people'][1], 'starts_on': '2026-10-02',
                     'ends_on': '2026-10-29', 'service_id': h.seed['services'][1]}
    changed = h.configure('rotations', changed_value, rotation['id'], rotation['revision'], role='coordinator')
    assert changed['previous_value'] == {**value, 'department_available': True}
    [updated] = attachments(h)
    assert updated['id'] == attachment['id']
    assert_attachment(updated, changed_value)
    undone = h.configure('rotations', changed['previous_value'], rotation['id'], changed['record']['revision'],
                        role='coordinator')['record']
    assert_attachment(attachments(h)[0], value)
    cleared = h.configure('rotations', {**value, 'mentor_id': None}, undone['id'], undone['revision'], role='coordinator')
    assert cleared['record']['mentor_id'] is None and attachments(h) == []
    restored = h.configure('rotations', cleared['previous_value'], undone['id'], cleared['record']['revision'],
                          role='coordinator')['record']
    assert restored['mentor_id'] == value['mentor_id']
    assert_attachment(attachments(h)[0], value)


@pytest.mark.parametrize('kind', ['rotation.remove', 'rotation.undo_plan'])
def test_rotation_removal_undo_and_revision_guard(h, kind):
    value = rotation_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    stale = remove_rotation(h, {**rotation, 'revision': rotation['revision'] + 1}, kind)
    assert stale['removed'] == 0 and stale['kept'] == 1 and len(attachments(h)) == 1
    remove_rotation(h, rotation, kind, role='staff', status=403)
    removed = remove_rotation(h, rotation, kind)
    assert removed['removed_ids'] == [rotation['id']]
    assert attachments(h) == []
    assert h.get('coordinator', '/api/catalogues')['rotations'] == []
    fields = ('person_id', 'mentor_id', 'cohort_id', 'rotation_type_id', 'service_id', 'starts_on', 'ends_on', 'department_available')
    restored = h.configure('rotations', {key: removed['records'][0][key] for key in fields}, role='coordinator')['record']
    assert restored['mentor_id'] == value['mentor_id']
    assert_attachment(attachments(h)[0], value)


def test_undo_plan_cannot_remove_another_actors_or_edited_rotation(h):
    value = rotation_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    assert remove_rotation(h, rotation, 'rotation.undo_plan', role='admin')['kept'] == 1
    changed = h.configure('rotations', {**value, 'ends_on': '2026-10-30'}, rotation['id'], rotation['revision'],
                          role='coordinator')['record']
    assert changed['revision'] > rotation['revision']
    assert remove_rotation(h, rotation, 'rotation.undo_plan')['kept'] == 1
    assert len(attachments(h)) == 1


@pytest.mark.parametrize('action', ['clear', 'rotation.remove', 'rotation.undo_plan'])
def test_rotation_removal_preserves_a_separately_edited_attachment(h, action):
    value = rotation_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    [attachment] = attachments(h)
    fields = ('person_id', 'mentor_id', 'programme_id', 'starts_on', 'ends_on', 'assignment_type_ids', 'service_ids', 'strength')
    edited = h.configure('mentorships', {**{key: attachment[key] for key in fields}, 'strength': 'required'},
                         attachment['id'], attachment['revision'], role='coordinator')['record']
    if action == 'clear':
        h.configure('rotations', {**value, 'mentor_id': None}, rotation['id'], rotation['revision'], role='coordinator')
    else:
        assert remove_rotation(h, rotation, action)['removed'] == 1
    assert attachments(h) == [edited]


def test_rotation_adopts_an_existing_required_attachment_without_overwriting_it(h):
    value = rotation_value(h)
    attachment_value = {key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')}
    existing = h.configure('mentorships', {**attachment_value, 'service_ids': [value['service_id']], 'strength': 'required'},
                           role='coordinator')['record']
    rotation = h.configure('rotations', value, role='coordinator')['record']
    assert attachments(h) == [existing]
    remove_rotation(h, rotation)
    assert attachments(h) == [existing]


@pytest.mark.parametrize('same_programme', [False, True])
@pytest.mark.parametrize('services', ['all', 'exact', 'multiple'])
def test_rotation_reuses_only_its_programmes_attachment_and_preserves_independent_ownership(h, same_programme, services):
    value = rotation_value(h)
    cohort = next(row for row in h.get('coordinator', '/api/catalogues')['cohorts'] if row['id'] == value['cohort_id'])
    programme_id = cohort['programme_id'] if same_programme else h.configure('programmes', {
        'name': 'Unrelated attachment programme', 'starts_on': '2026-01-01'})['record']['id']
    service_ids = [] if services == 'all' else [value['service_id']] if services == 'exact' else h.seed['services']
    existing = h.configure('mentorships', {**{key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')},
        'programme_id': programme_id, 'service_ids': service_ids, 'strength': 'required'}, role='coordinator')['record']
    rotation = h.configure('rotations', value, role='coordinator')['record']
    rotation = h.configure('rotations', value, rotation['id'], rotation['revision'], role='coordinator')['record']
    rows = attachments(h)
    assert len(rows) == (1 if same_programme else 2)
    assert next(row for row in rows if row['id'] == existing['id']) == existing
    assert {row['id'] for row in h.get('staff', '/api/me/profile')['catalogues']['mentorships']} == {row['id'] for row in rows}
    with transaction(h.app.state.engine) as db:
        link = db.get(m.RotationAttachment, rotation['id'])
        assert (link.mentorship_id == existing['id']) == same_programme
        if same_programme:
            assert link.synced_revision is None
    remove_rotation(h, rotation)
    assert attachments(h) == [existing]


@pytest.mark.parametrize('multi_clinic', [False, True])
def test_rotation_reuses_a_broad_existing_attachment_without_narrowing_or_removing_it(h, multi_clinic):
    value = rotation_value(h)
    existing = h.configure('mentorships', {**{key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')},
        'service_ids': h.seed['services'] if multi_clinic else [], 'strength': 'required'}, role='coordinator')['record']
    rotation = h.configure('rotations', value, role='coordinator')['record']
    assert attachments(h) == [existing]
    rotation = h.configure('rotations', value, rotation['id'], rotation['revision'], role='coordinator')['record']
    assert attachments(h) == [existing]
    remove_rotation(h, rotation)
    assert attachments(h) == [existing]


def test_rotation_preserves_an_other_clinic_attachment_and_creates_its_own_clinic_attachment(h):
    value = rotation_value(h)
    existing = h.configure('mentorships', {**{key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')},
        'service_ids': [h.seed['services'][1]], 'strength': 'required'}, role='coordinator')['record']
    rotation = h.configure('rotations', value, role='coordinator')['record']
    rows = attachments(h)
    assert len(rows) == 2 and next(row for row in rows if row['id'] == existing['id']) == existing
    assert_attachment(next(row for row in rows if row['id'] != existing['id']), value)
    remove_rotation(h, rotation)
    assert attachments(h) == [existing]


@pytest.mark.parametrize('programme_scope', [False, True])
def test_rotation_prefers_the_exact_clinic_attachment_when_a_broad_legacy_attachment_also_exists(h, programme_scope):
    value = rotation_value(h)
    cohort = next(row for row in h.get('coordinator', '/api/catalogues')['cohorts'] if row['id'] == value['cohort_id'])
    broad = h.configure('mentorships', {**{key: value[key] for key in ('person_id', 'mentor_id', 'starts_on', 'ends_on')},
        'programme_id': cohort['programme_id'] if programme_scope else None, 'strength': 'required'}, role='coordinator')['record']
    # Legacy data can contain overlapping scopes that today's command validator refuses to create.
    with transaction(h.app.state.engine, write=True) as db:
        exact = m.Mentorship(department_id=h.seed['department_id'], person_id=value['person_id'], mentor_id=value['mentor_id'],
            starts_on=date.fromisoformat(value['starts_on']), ends_on=date.fromisoformat(value['ends_on']),
            service_ids=[value['service_id']], assignment_type_ids=[], strength='preferred')
        db.add(exact)
        db.flush()
        exact_id = exact.id
    before = attachments(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RotationAttachment, rotation['id']).mentorship_id == exact_id
    assert attachments(h) == before and any(row['id'] == broad['id'] for row in before)
    remove_rotation(h, rotation)
    assert attachments(h) == before


def test_moving_an_attachment_to_another_person_clears_the_live_rotation_mentor_and_preserves_the_edit(h):
    value = rotation_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    [attachment] = attachments(h)
    fields = ('person_id', 'mentor_id', 'programme_id', 'starts_on', 'ends_on', 'assignment_type_ids', 'service_ids', 'strength')
    moved = h.configure('mentorships', {**{key: attachment[key] for key in fields}, 'person_id': h.seed['people'][1]},
                        attachment['id'], attachment['revision'], role='coordinator')['record']
    live = next(row for row in h.get('coordinator', '/api/catalogues')['rotations'] if row['id'] == rotation['id'])
    assert live['mentor_id'] is None
    removed = remove_rotation(h, rotation)
    assert removed['records'][0]['mentor_id'] is None
    assert attachments(h) == [moved]


def test_bulk_rotation_dates_keep_the_live_mentor_and_update_the_attachment(h):
    value = rotation_value(h)
    rotation = h.configure('rotations', value, role='coordinator')['record']
    result = h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'rotations',
        'items': [{'id': rotation['id'], 'revision': rotation['revision']}], 'set': {'ends_on': '2026-10-28'}, 'reason': REASON})
    assert result['records'][0]['mentor_id'] == value['mentor_id']
    assert_attachment(attachments(h)[0], {**value, 'ends_on': '2026-10-28'})


@pytest.mark.parametrize('manually_edited', [False, True])
@pytest.mark.parametrize('remove_first', [0, 1])
def test_shared_rotation_attachment_is_removed_with_its_last_owner_unless_edited(h, manually_edited, remove_first):
    value = rotation_value(h)
    rotations = list(legacy_shared_rotations(h, value))
    [attachment] = attachments(h)
    if manually_edited:
        fields = ('person_id', 'mentor_id', 'programme_id', 'starts_on', 'ends_on', 'assignment_type_ids', 'service_ids', 'strength')
        attachment = h.configure('mentorships', {**{key: attachment[key] for key in fields}, 'strength': 'required'},
            attachment['id'], attachment['revision'], role='coordinator')['record']
    assert [row['id'] for row in attachments(h)] == [attachment['id']]
    for index, position in enumerate((remove_first, 1 - remove_first)):
        rotation = rotations[position]
        remove_rotation(h, rotation)
        remaining = attachments(h)
        if index == 0 or manually_edited:
            assert len(remaining) == 1 and remaining[0]['id'] == attachment['id']
            if manually_edited:
                assert remaining == [attachment]
        else:
            assert remaining == []


def test_shared_attachment_dates_edit_refuses_overlap_without_changing_either_rotation(h):
    value = rotation_value(h)
    first, second = legacy_shared_rotations(h, value)
    original = attachments(h)
    result = h.configure('rotations', {**value, 'starts_on': '2026-10-02'}, first['id'], first['revision'],
                         role='coordinator', status=409)
    assert result['error']['code'] == 'ROTATION_OVERLAP'
    assert attachments(h) == original
    current = {row['id']: row for row in h.get('coordinator', '/api/catalogues')['rotations']}
    assert current[first['id']] == first and current[second['id']] == second


def test_shared_attachment_can_fork_for_nonoverlapping_rotation_dates(h):
    value = rotation_value(h)
    first, second = legacy_shared_rotations(h, value)
    [original] = attachments(h)
    moved_value = {**value, 'starts_on': '2026-11-01', 'ends_on': '2026-12-01'}
    moved = h.configure('rotations', moved_value, first['id'], first['revision'], role='coordinator')['record']
    rows = attachments(h)
    assert len(rows) == 2
    assert next(row for row in rows if row['id'] == original['id']) == original
    assert_attachment(next(row for row in rows if row['id'] != original['id']), moved_value)
    remove_rotation(h, moved)
    assert attachments(h) == [original]
    remove_rotation(h, second)
    assert attachments(h) == []


def test_plan_copy_and_undo_use_the_live_attachment_instead_of_the_legacy_mentor(h):
    value = rotation_value(h)
    planned = h.command('coordinator', 'rotation.plan', {'cohort_id': value['cohort_id'],
        'person_ids': [value['person_id']], 'starts_on': value['starts_on'], 'reason': REASON,
        'blocks': [{'rotation_type_id': value['rotation_type_id'], 'service_id': value['service_id'],
                    'length': 1, 'unit': 'months'}]})['created']
    assert len(planned) == 1 and planned[0]['mentor_id'] is None and attachments(h) == []
    rotation = h.configure('rotations', value, planned[0]['id'], planned[0]['revision'], role='coordinator')['record']
    [original] = attachments(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Rotation, rotation['id']).mentor_id = h.seed['people'][1]
    copied = h.command('coordinator', 'rotation.copy', {'starts_on': value['starts_on'], 'ends_on': value['ends_on'],
        'months': 1, 'reason': REASON})['created']
    assert len(copied) == 1 and copied[0]['mentor_id'] == value['mentor_id']
    added = next(row for row in attachments(h) if row['id'] != original['id'])
    assert_attachment(added, {**value, 'starts_on': '2026-11-01', 'ends_on': '2026-12-01'})
    assert remove_rotation(h, copied[0], 'rotation.undo_plan')['removed'] == 1
    assert attachments(h) == [original]


def test_approved_rotation_change_and_undo_keep_the_live_attachment_mentor(h):
    value = rotation_value(h)
    cohort = next(row for row in h.get('coordinator', '/api/catalogues')['cohorts'] if row['id'] == value['cohort_id'])
    lead_of(h, {'id': cohort['programme_id']})
    rotation = h.configure('rotations', value, role='coordinator')['record']
    [original] = attachments(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Rotation, rotation['id']).mentor_id = h.seed['people'][1]
    changed_value = {**value, 'starts_on': '2026-10-02', 'ends_on': '2026-10-29', 'service_id': h.seed['services'][1]}
    requested = h.command('staff', 'rotation.request_change', {'rotation_id': rotation['id'],
        **{key: changed_value[key] for key in ('rotation_type_id', 'service_id', 'starts_on', 'ends_on')},
        'private_reason': REASON})['record']
    approved = h.command('programme_lead', 'request.decide', {'decision': 'approve', 'reason': REASON},
                         requested['id'], requested['revision'])['record']
    [changed] = attachments(h)
    assert changed['id'] == original['id']
    assert_attachment(changed, changed_value)
    live = next(row for row in h.get('coordinator', '/api/catalogues')['rotations'] if row['id'] == rotation['id'])
    assert live['mentor_id'] == value['mentor_id']
    h.command('programme_lead', 'request.undo', {'reason': REASON}, requested['id'], approved['revision'])
    assert_attachment(attachments(h)[0], value)
    restored = next(row for row in h.get('coordinator', '/api/catalogues')['rotations'] if row['id'] == rotation['id'])
    assert restored['mentor_id'] == value['mentor_id']
    assert remove_rotation(h, restored)['kept'] == 1  # Request history preserves the rotation and its attachment.
    assert_attachment(attachments(h)[0], value)


@pytest.mark.parametrize('kind', ['rotation.remove', 'rotation.undo_plan'])
def test_rotation_removal_refuses_another_programme_without_removing_its_attachment(h, kind):
    value = rotation_value(h)
    other_programme(h, h.seed['people'][1])
    rotation = h.configure('rotations', value, role='coordinator')['record']
    before = attachments(h)
    remove_rotation(h, rotation, kind, role='other_lead', status=403)
    assert attachments(h) == before
    assert h.get('coordinator', '/api/catalogues')['rotations'] == [rotation]


def supervisor(shift, trainee):
    return next(p['supervisor_person_id'] for p in shift['participants'] if p['person_id'] == trainee)


@pytest.mark.parametrize('strength', ['required', 'preferred'])
def test_manual_save_returns_and_persists_the_attached_supervisor_and_undo_restores_it(h, strength):
    # D65 (docs/user-guide.md:49-55): a new or worsened blocker refuses the edit, even with a reason.
    trainee, b, c, typ = setup(h, strength)
    reply = h.shift(h.workspace(), typ=typ, people=people(trainee, b, c))
    assert supervisor(reply['shifts'][0], trainee) == c
    before_detail = h.detail(reply['workspace'])
    before = before_detail['shifts'][0]
    assert supervisor(before, trainee) == c
    payload = {key: before[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=reply['workspace']['id'], shift_id=before['id'], participants=people(trainee, b, c, paired=b))
    if strength == 'required':
        refused = h.command('coordinator', 'shift.update', payload, revision=reply['workspace']['revision'], status=422)
        assert refused['error']['code'] == 'EDIT_INVALID'
        assert 'ATTACHMENT_PAIRING' in {f['code'] for f in refused['error']['details']['findings']}
        assert h.detail(reply['workspace']) == before_detail
        return
    changed = h.command('coordinator', 'shift.update', payload, revision=reply['workspace']['revision'])
    assert supervisor(changed['shifts'][0], trainee) == b  # Explicit operator decisions remain explicit.
    payload['participants'] = people(trainee, b, c)
    paired = h.command('coordinator', 'shift.update', payload, revision=changed['workspace']['revision'])
    assert supervisor(paired['shifts'][0], trainee) == c
    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON},
                       reply['workspace']['id'], paired['workspace']['revision'])
    assert supervisor(undone['shifts'][0], trainee) == b
    assert supervisor(h.detail(undone['workspace'])['shifts'][0], trainee) == b


@pytest.mark.parametrize('required_blocked', [False, True])
def test_manual_save_prefers_required_attachments_without_creating_a_wrong_pair(h, required_blocked):
    trainee, b, c, typ = setup(h, 'preferred')
    p, _, _ = programme(h)
    h.configure('mentorships', {'person_id': trainee, 'mentor_id': b, 'programme_id': p['id'],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'strength': 'required'}, role='coordinator')
    if required_blocked:
        h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0}, {'person_id': b})
    reply = h.shift(h.workspace(), typ=typ, people=people(trainee, b, c), seed_restricted=required_blocked)
    assert supervisor(reply['shifts'][0], trainee) == (None if required_blocked else b)


@pytest.mark.parametrize('boundary', ['absent', 'presence_gap', 'unqualified', 'other_type', 'other_service', 'outside_dates',
                                     'programme', 'rule_capacity', 'not_supervised'])
def test_manual_save_leaves_missing_supervisor_when_attachment_cannot_apply(h, boundary):
    extra = {}
    if boundary == 'other_type':
        extra['assignment_type_ids'] = [h.seed['type']]
    elif boundary == 'other_service':
        extra['service_ids'] = [h.seed['services'][1]]
    elif boundary == 'outside_dates':
        extra['starts_on'] = '2026-10-05'
    elif boundary == 'programme':
        p = h.configure('programmes', {'name': 'Synthetic unjoined programme', 'starts_on': '2026-01-01'})['record']
        extra['programme_id'] = p['id']
    trainee, b, c, typ = setup(h, **extra)
    participants = people(trainee, b, c)
    if boundary == 'absent':
        participants = [p for p in participants if p['person_id'] != c]
    elif boundary == 'presence_gap':
        next(p for p in participants if p['person_id'] == c)['segments'] = [
            {'start': START, 'end': '2026-10-04T09:00:00+03:00'},
            {'start': '2026-10-04T10:00:00+03:00', 'end': END}]
    elif boundary == 'unqualified':
        with transaction(h.app.state.engine, write=True) as db:
            for grant in db.scalars(select(m.CapabilityGrant).where(m.CapabilityGrant.person_id == c)):
                grant.revoked = True
    elif boundary == 'rule_capacity':
        h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 0}, {'person_id': c})
    elif boundary == 'not_supervised':
        typ = h.seed['type']
        participants = [{**p, 'participation': 'staff'} for p in participants]
    reply = h.shift(h.workspace(), typ=typ, people=participants, seed_restricted=boundary == 'rule_capacity')
    assert supervisor(reply['shifts'][0], trainee) is None


def test_manual_save_names_supervision_when_a_person_fact_changes_during_an_overnight_duty(h):
    trainee, mentor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2026-10-05'})
    typ = h.configure('assignment_types', {'name': 'Synthetic overnight supervision', 'starts_on': '2020-01-01',
        'definition': {'default_timing': {'start_time': '22:00', 'end_time': '02:00'},
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1}}})['record']
    h.configure('mentorships', {'person_id': trainee, 'mentor_id': mentor,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'strength': 'required'}, role='coordinator')
    start, end = '2026-10-04T22:00:00+03:00', '2026-10-05T02:00:00+03:00'
    participants = [{'person_id': person, 'start': start, 'end': end, 'participation': 'staff'}
                    for person in (trainee, mentor)]
    reply = h.shift(h.workspace(), start=start, end=end, typ=typ['id'], people=participants)
    assert supervisor(reply['shifts'][0], trainee) == mentor
    assert supervisor(h.detail(reply['workspace'])['shifts'][0], trainee) == mentor
    assert not {f['code'] for f in reply['validation']['findings']} & {'SUPERVISION_PAIRING', 'ATTACHMENT_PAIRING'}


@pytest.mark.postgres
def test_rotation_attachment_and_manual_pairing_postgres(pg):
    test_rotation_creates_updates_clears_and_undoes_one_attachment(pg, True)
    # The rotation's attachment becomes the same input used by a hand-saved duty.
    trainee, b, c = pg.seed['people']
    pg.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': pg.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = pg.configure('assignment_types', {'name': 'Synthetic attached duty', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [pg.seed['trainee_category']],
            'capability_id': pg.seed['supervisor_capability'], 'trainees_per_supervisor': 1}}})['record']
    reply = pg.shift(pg.workspace(), typ=typ['id'], people=people(trainee, b, c))
    assert supervisor(reply['shifts'][0], trainee) == c
# Include this file in hosted supervision and team-lead qualification.
