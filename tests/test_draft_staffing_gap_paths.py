"""Draft staffing gaps are checked before an edit can save any changes."""
import pytest

from .legacy_lead import historical_type
from .test_be_wave4_draft_actions import DAY, NEXT, REASON, edit_command, post, state, supervised_type
from .test_bulk import preview
from .test_bulk_draft_session_creation import creation
from .test_fill_options import options


def placement(person, **fields):
    return {'person_id': person, 'start': DAY[0], 'end': DAY[1], **fields}


def lead_type(h, minimum=1):
    return historical_type(h, {'name': 'Synthetic lead coverage', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'lead': {'minimum': minimum}}})['id']


def staffing(h, code):
    if code == 'LEAD_GAP':
        return lead_type(h), placement(h.seed['people'][0], team_lead=True), placement(h.seed['people'][2])
    trainee, supervisor = h.seed['people'][:2]
    rank = h.seed['trainee_category']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': rank,
        'starts_on': '2020-01-01'})
    capability = h.configure('capabilities', {'name': 'Synthetic supervision authority',
        'starts_on': '2020-01-01'})['record']['id']
    h.command('credentialer', 'grant.create', {'person_id': supervisor, 'capability_id': capability,
        'starts_on': '2020-01-01', 'evidence': 'Synthetic supervision qualification'})
    typ = h.configure('assignment_types', {'name': 'Synthetic supervised coverage', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'supervision': {'trainee_category_ids': [rank],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': False}}})['record']['id']
    return typ, placement(supervisor, participation='supervisor'), placement(trainee, participation='trainee')


def action(w, sid, kind):
    return {'kind': 'shift.action', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'shift_ids': [sid], 'action': kind, 'reason': REASON}}


def absence(h, person, *, partial=False):
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'actual_absence',
        'start': DAY[0], 'end': '2026-10-04T10:00:00+03:00' if partial else DAY[1],
        'private_reason': 'Synthetic absence'})


def assert_refusal(error, code, finding, sid=None):
    assert error['code'] == code
    assert any(row['code'] == finding and (sid is None or row['shift_id'] == sid)
               for row in error['details']['findings'])


def assert_planner_refusal(h, w, value, code, finding, sid=None):
    before = state(h, w)
    trial = h.client('coordinator').post('/api/planner/preview', json=value)
    assert trial.status_code == 422, trial.text
    assert_refusal(trial.json()['error'], code, finding, sid)
    assert state(h, w) == before
    assert_refusal(post(h, value, status=422)['error'], code, finding, sid)
    assert state(h, w) == before


def bulk_edit(w, shift, people):
    payload = edit_command(w, shift, people)['payload']
    return {'items': [{'item_kind': 'shift.update', 'target_id': w['id'],
                       'expected_revision': w['revision'], 'payload': payload}], 'reason': REASON}


@pytest.mark.parametrize('finding', ['LEAD_GAP', 'SUPERVISION_GAP'])
def test_bulk_edit_refuses_new_staffing_gap(h, finding):
    typ, cover, other = staffing(h, finding)
    w = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])['workspace']
    [shift] = h.detail(w)['shifts']
    body = bulk_edit(w, shift, [other])
    before = state(h, w)
    trial = preview(h, 'coordinator', body)
    assert trial['counts']['problem'] == 1
    assert trial['items'][0]['issues'][0]['code'] == 'BULK_EDIT_INVALID'
    assert state(h, w) == before
    error = h.command('coordinator', 'bulk.apply', body, status=409)['error']
    assert_refusal(error, 'BULK_EDIT_INVALID', finding, shift['id'])
    assert state(h, w) == before


def test_bulk_edit_allows_filling_a_session_with_an_unchanged_lead_gap(h):
    w = h.shift(h.workspace(), *DAY, typ=lead_type(h), people=[])['workspace']
    [shift] = h.detail(w)['shifts']
    body = bulk_edit(w, shift, [placement(h.seed['people'][2])])
    assert preview(h, 'coordinator', body)['counts']['ready'] == 1
    assert h.command('coordinator', 'bulk.apply', body)['counts']['changed'] == 1
    assert h.detail(w)['shifts'][0]['participants'][0]['person_id'] == h.seed['people'][2]


def test_bulk_staffing_checks_the_final_edit(h):
    typ, cover, other = staffing(h, 'LEAD_GAP')
    created = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])
    w = created['workspace']
    [shift] = h.detail(w)['shifts']
    body = bulk_edit(w, shift, [other])
    body['items'].extend(bulk_edit(w, shift, [other | {'team_lead': True}])['items'])
    assert preview(h, 'coordinator', body)['counts']['ready'] == 2
    assert h.command('coordinator', 'bulk.apply', body)['counts']['changed'] == 2
    [participant] = h.detail(w)['shifts'][0]['participants']
    assert participant['person_id'] == other['person_id'] and participant['team_lead']


def test_clear_people_refuses_new_lead_gap(h):
    typ, cover, other = staffing(h, 'LEAD_GAP')
    created = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])
    w, sid = created['workspace'], created['shift_id']
    assert_planner_refusal(h, w, action(w, sid, 'unassign'), 'UNASSIGN_INVALID', 'LEAD_GAP', sid)


def test_clear_people_keeps_an_unchanged_lead_gap_editable(h):
    created = h.shift(h.workspace(), *DAY, typ=lead_type(h), people=[])
    w, sid = created['workspace'], created['shift_id']
    [shift] = h.detail(w)['shifts']
    w = post(h, edit_command(w, shift, [placement(h.seed['people'][2])]))['workspace']
    result = post(h, action(w, sid, 'unassign'))
    assert not h.detail(w)['shifts'][0]['participants']
    assert any(f['code'] == 'LEAD_GAP' and f['shift_id'] == sid
               for f in h.detail(result['workspace'])['workspace']['validation']['findings'])


@pytest.mark.parametrize('finding', ['LEAD_GAP', 'SUPERVISION_GAP'])
def test_release_unavailable_refuses_widening_an_interval_gap(h, finding):
    typ, cover, other = staffing(h, finding)
    created = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])
    w, sid = created['workspace'], created['shift_id']
    absence(h, cover['person_id'], partial=True)
    assert_planner_refusal(h, w, action(w, sid, 'release_unavailable'),
                           'RELEASE_UNAVAILABLE_INVALID', finding, sid)


def test_release_unavailable_allows_an_unchanged_gap(h):
    typ, cover, other = staffing(h, 'LEAD_GAP')
    created = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])
    w, sid = created['workspace'], created['shift_id']
    absence(h, cover['person_id'])
    result = post(h, action(w, sid, 'release_unavailable'))
    detail = h.detail(result['workspace'])
    assert [row['person_id'] for row in detail['shifts'][0]['participants']] == [other['person_id']]
    assert any(f['code'] == 'LEAD_GAP' and f['shift_id'] == sid
               for f in detail['workspace']['validation']['findings'])


def create_value(h, w, typ, people, day=None):
    item = creation(h, w, day=day)
    item['payload'].update(assignment_type_id=typ, participants=people)
    return item


def creation_staffing_gap(h, finding):
    if finding == 'LEAD_GAP':
        return lead_type(h), [placement(pid, team_lead=True) for pid in h.seed['people'][:2]]
    typ, _, trainee = staffing(h, finding)
    return typ, [trainee]


@pytest.mark.parametrize('finding', ['LEAD_GAP', 'SUPERVISION_GAP'])
def test_new_staffed_session_refuses_a_new_gap(h, finding):
    typ, people = creation_staffing_gap(h, finding)
    w = h.workspace()
    item = create_value(h, w, typ, people)
    assert_planner_refusal(h, w, {'kind': 'shift.create', 'expected_revision': w['revision'],
                                  'payload': item['payload']}, 'CREATE_INVALID', finding)


@pytest.mark.parametrize('finding', ['LEAD_GAP', 'SUPERVISION_GAP'])
def test_bulk_creation_refuses_a_new_gap_and_rolls_back_the_batch(h, finding):
    typ, people = creation_staffing_gap(h, finding)
    w = h.workspace()
    body = {'items': [creation(h, w, day='2026-10-05'), create_value(h, w, typ, people)], 'reason': REASON}
    before = state(h, w)
    trial = preview(h, 'coordinator', body)
    assert trial['items'][1]['result'] == 'problem'
    assert trial['items'][1]['issues'][0]['code'] == 'CREATE_INVALID'
    assert state(h, w) == before
    error = h.command('coordinator', 'bulk.apply', body, status=409)['error']
    assert_refusal(error, 'CREATE_INVALID', finding)
    assert state(h, w) == before


def assert_created_without_lead(h, w, person):
    detail = h.detail(w)
    [shift] = detail['shifts']
    [participant] = shift['participants']
    assert participant['person_id'] == person and participant['team_lead'] is False
    assert any(f['code'] == 'LEAD_GAP' and f['shift_id'] == shift['id']
               for f in detail['workspace']['validation']['findings'])


def test_new_staffed_session_allows_the_same_lead_gap_as_an_empty_session(h):
    w = h.workspace()
    person = h.seed['people'][0]
    item = create_value(h, w, lead_type(h), [placement(person)])
    value = {'kind': 'shift.create', 'expected_revision': w['revision'], 'payload': item['payload']}
    trial = h.client('coordinator').post('/api/planner/preview', json=value)
    assert trial.status_code == 200, trial.text
    result = post(h, value)
    assert_created_without_lead(h, result['workspace'], person)


def test_bulk_creation_allows_the_same_lead_gap_as_an_empty_session(h):
    w = h.workspace()
    person = h.seed['people'][0]
    body = {'items': [create_value(h, w, lead_type(h), [placement(person)])], 'reason': REASON}
    assert preview(h, 'coordinator', body)['counts']['ready'] == 1
    assert h.command('coordinator', 'bulk.apply', body)['counts']['changed'] == 1
    assert_created_without_lead(h, w, person)


def test_new_empty_required_sessions_and_unrelated_edits_allow_an_unchanged_gap(h):
    typ = lead_type(h)
    first = h.shift(h.workspace(), *DAY, typ=typ, people=[])
    w = first['workspace']
    supervised, _, _ = supervised_type(h)
    body = {'items': [create_value(h, w, typ, [], day='2026-10-05'),
                      create_value(h, w, supervised, [], day='2026-10-06')], 'reason': REASON}
    assert h.command('coordinator', 'bulk.apply', body)['counts']['changed'] == 2
    w = h.detail(w)['workspace']
    w = h.shift(w, start='2026-10-07T08:00:00+03:00', end='2026-10-07T12:00:00+03:00')['workspace']
    assert any(f['code'] == 'LEAD_GAP' and f['shift_id'] == first['shift_id']
               for f in h.detail(w)['workspace']['validation']['findings'])


def test_swap_cannot_open_a_supervision_gap(h):
    typ, cover, other = staffing(h, 'SUPERVISION_GAP')
    first = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])
    second = h.shift(first['workspace'], *NEXT, people=[h.seed['people'][2]])
    w = second['workspace']
    rows = {row['id']: row for row in h.detail(w)['shifts']}
    source = next(row for row in rows[first['shift_id']]['participants'] if row['person_id'] == cover['person_id'])
    [target] = rows[second['shift_id']]['participants']
    value = {'kind': 'draft.swap', 'expected_revision': w['revision'], 'payload': {
        'workspace_id': w['id'], 'first_assignment_id': source['id'],
        'second_assignment_id': target['id'], 'reason': REASON}}
    assert_planner_refusal(h, w, value, 'SWAP_INVALID', 'SUPERVISION_GAP', first['shift_id'])


@pytest.mark.parametrize('finding', ['LEAD_GAP', 'SUPERVISION_GAP'])
def test_staffing_gap_cannot_be_overridden(h, finding):
    typ, cover, other = staffing(h, finding)
    created = h.shift(h.workspace(), *DAY, typ=typ, people=[cover, other])
    w, sid = created['workspace'], created['shift_id']
    [shift] = h.detail(w)['shifts']
    old = next(row for row in shift['participants'] if row['person_id'] == cover['person_id'])
    # A replacement must be distinct from the people already on the session.
    replacement = next(pid for pid in h.seed['people'] if pid not in {cover['person_id'], other['person_id']})
    result = options(h, w, sid, replace_assignment_id=old['id'])
    candidate = next(row for row in result['candidates'] if row['person_id'] == replacement)
    assert candidate['status'] == 'blocked'
    assert any(row['code'] == finding for row in candidate['findings'])
    assert candidate['override_allowed'] is False
