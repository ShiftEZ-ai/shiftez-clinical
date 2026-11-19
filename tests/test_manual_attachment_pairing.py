"""Manual attachment naming reserves scarce places without changing explicit decisions."""
import pytest

from packages.contracts.schemas import ParticipantInput

from .test_alpha_calendar import programme
from .test_attachments import END, START, people, setup


def pairing_setup(h, constraint):
    flexible = h.seed['people'][0]
    first, second = sorted(h.seed['people'][1:])
    constrained = h.configure('people', {'display_name': 'Synthetic constrained trainee'})['record']['id']
    h.command('coordinator', 'membership.create', {'person_id': constrained,
        'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
    h.command('credentialer', 'eligibility.create', {'person_id': constrained,
        'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Synthetic scarce attachment places', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']['id']
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 1})
    p, _, _ = programme(h)
    h.configure('programme_memberships', {'person_id': constrained, 'programme_id': p['id'],
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    for trainee, mentor, strength, scope in [
        (flexible, first, 'preferred', {}),
        (flexible, second, 'preferred', {'programme_id': p['id']}),
        (constrained, first, 'required' if constraint == 'required' else 'preferred', {}),
    ]:
        h.configure('mentorships', {'person_id': trainee, 'mentor_id': mentor, 'strength': strength,
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01', **scope}, role='coordinator')
    if constraint == 'rank_limit':
        h.configure('mentorships', {'person_id': constrained, 'mentor_id': second, 'strength': 'preferred',
            'programme_id': p['id'], 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
        h.command('coordinator', 'membership.create', {'person_id': flexible,
            'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
        h.policy({'kind': 'supervision_limit', 'mode': 'allowed_ranks',
            'trainee_category_ids': [h.seed['trainee_category']]}, {'person_id': second})
    return flexible, constrained, first, second, typ


def participants(trainees, supervisors, explicit=None):
    return [{'person_id': person, 'start': START, 'end': END, 'participation': 'trainee',
             'supervisor_person_id': (explicit or {}).get(person)} for person in trainees] + [
        {'person_id': person, 'start': START, 'end': END, 'participation': 'supervisor'} for person in supervisors]


def edit_participants(h, workspace, shift, participants, *, status=200):
    return h.command('coordinator', 'shift.update', {
        'workspace_id': workspace['id'], 'shift_id': shift['id'],
        **{key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
        'participants': participants,
    }, revision=workspace['revision'], status=status)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('case,ends_on', [
    pytest.param('required', None, id='required'),
    pytest.param('required', '2026-10-05', id='required-bounded'),
    pytest.param('preferred', None, id='preferred'),
    pytest.param('scarce-required', None, id='scarce-required'),
])
def test_edit_clearing_an_active_attachment_supervisor(h, case, ends_on):
    if case == 'scarce-required':
        flexible, trainee, supervisor, second, typ = pairing_setup(h, 'required')
        draft_people = participants([flexible, trainee], [supervisor, second])
        cleared = {flexible, trainee}
    else:
        trainee, other, supervisor, typ = setup(h, case, ends_on=ends_on)
        draft_people = people(trainee, other, supervisor)
        cleared = {trainee}
    created = h.shift(h.workspace(), typ=typ, people=draft_people)
    assert created['validation']['valid']
    workspace = created['workspace']
    before = h.detail(workspace)
    shift = before['shifts'][0]
    assert next(p['supervisor_person_id'] for p in shift['participants'] if p['person_id'] == trainee) == supervisor
    edited_people = [{key: value for key, value in person.items() if key in ParticipantInput.model_fields}
                     for person in shift['participants']]
    for person in edited_people:
        if person['person_id'] in cleared:
            person['supervisor_person_id'] = None

    if case == 'preferred':
        edit_participants(h, workspace, shift, edited_people)
        saved = h.detail(workspace)
        assert saved['workspace']['validation']['valid']
        assert next(p['supervisor_person_id'] for p in saved['shifts'][0]['participants']
                    if p['person_id'] == trainee) is None
    else:
        reply = edit_participants(h, workspace, shift, edited_people, status=422)
        error = reply['error']
        assert error['code'] == 'EDIT_INVALID'
        findings = {(item['code'], item['person_id']) for item in error['details']['findings']
                    if item['shift_id'] == shift['id'] and item['severity'] == 'error'}
        assert ('ATTACHMENT_PAIRING', trainee) in findings
        if case == 'scarce-required':
            assert any(code == 'SUPERVISION_PAIRING' and person in cleared for code, person in findings)
        assert h.detail(workspace) == before


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_edit_names_a_new_trainee_without_changing_existing_pair(h):
    flexible, trainee, first, second, typ = pairing_setup(h, 'required')
    people_before = participants([flexible], [first, second], explicit={flexible: second})
    created = h.shift(h.workspace(), typ=typ, people=people_before)
    workspace = created['workspace']
    shift = h.detail(workspace)['shifts'][0]
    existing_pair = next(p for p in shift['participants'] if p['person_id'] == flexible)
    assert existing_pair['supervisor_person_id'] == second

    added = {'person_id': trainee, 'start': START, 'end': END, 'participation': 'trainee'}
    edit_participants(h, workspace, shift, [*people_before, added])

    saved = h.detail(workspace)['shifts'][0]
    indexed = {p['person_id']: p for p in saved['participants']}
    assert indexed[trainee]['supervisor_person_id'] == first
    assert indexed[flexible] == existing_pair


@pytest.mark.parametrize('constraint', ['required', 'only_attachment', 'rank_limit'])
@pytest.mark.parametrize('reverse', [False, True])
def test_manual_pairing_reserves_required_and_constrained_supervisors_in_either_order(h, constraint, reverse):
    flexible, constrained, first, second, typ = pairing_setup(h, constraint)
    trainees = [constrained, flexible] if reverse else [flexible, constrained]
    reply = h.shift(h.workspace(), typ=typ, people=participants(trainees, [first, second]))
    for shift in (reply['shifts'][0], h.detail(reply['workspace'])['shifts'][0]):
        pairs = {p['person_id']: p['supervisor_person_id'] for p in shift['participants']}
        assert pairs[constrained] == first and pairs[flexible] == second
    assert reply['validation']['valid'], reply['validation']['findings']


@pytest.mark.parametrize('reverse', [False, True])
def test_manual_pairing_preserves_an_explicit_pair_even_when_it_uses_the_required_place(h, reverse):
    flexible, constrained, first, second, typ = pairing_setup(h, 'required')
    trainees = [constrained, flexible] if reverse else [flexible, constrained]
    reply = h.shift(h.workspace(), typ=typ,
        people=participants(trainees, [first, second], {flexible: first}))
    pairs = {p['person_id']: p['supervisor_person_id'] for p in h.detail(reply['workspace'])['shifts'][0]['participants']}
    assert pairs[flexible] == first and pairs[constrained] is None
    codes = {f['code'] for f in reply['validation']['findings']}
    assert {'ATTACHMENT_PAIRING', 'SUPERVISION_PAIRING'} <= codes
    assert not codes & {'SUPERVISOR_CAPACITY', 'SUPERVISOR_SCOPED_CAPACITY', 'MEMBERSHIP_MISSING', 'ELIGIBILITY_MISSING'}


@pytest.mark.postgres
def test_manual_required_attachment_reservation_postgres(pg):
    test_manual_pairing_reserves_required_and_constrained_supervisors_in_either_order(pg, 'required', False)


def test_failed_automatic_reassignment_keeps_the_required_pair(h):
    flexible, constrained, first, _, typ = pairing_setup(h, 'required')
    reply = h.shift(h.workspace(), typ=typ, people=participants([flexible, constrained], [first]))
    pairs = {p['person_id']: p['supervisor_person_id'] for p in h.detail(reply['workspace'])['shifts'][0]['participants']}
    assert pairs[constrained] == first and pairs[flexible] is None
    assert not {'SUPERVISOR_CAPACITY', 'SUPERVISOR_SCOPED_CAPACITY'} & {f['code'] for f in reply['validation']['findings']}


@pytest.mark.parametrize('case', ['shared_gap', 'overnight_rank'])
def test_manual_pairing_checks_the_supervisor_only_during_actual_trainee_presence(h, case):
    trainee, mentor, staff = h.seed['people']
    capability = h.seed['supervisor_capability']
    timing = {}
    start, end = START, END
    if case == 'overnight_rank':
        start, end = '2026-10-04T22:00:00+03:00', '2026-10-05T02:00:00+03:00'
        timing = {'default_timing': {'start_time': '22:00', 'end_time': '02:00'}}
        h.command('coordinator', 'membership.create', {'person_id': trainee,
            'category_id': h.seed['trainee_category'], 'starts_on': '2026-10-05'})
        capability = h.configure('capabilities', {'name': 'Synthetic overnight authority',
            'starts_on': '2020-01-01'})['record']['id']
        h.command('credentialer', 'grant.create', {'person_id': mentor, 'capability_id': capability,
            'starts_on': '2026-10-05', 'evidence': 'Synthetic authority begins with the trainee rank'})
    typ = h.configure('assignment_types', {'name': 'Synthetic interval attachment', 'starts_on': '2020-01-01',
        'definition': {**timing, 'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']['id']
    h.configure('mentorships', {'person_id': trainee, 'mentor_id': mentor, 'strength': 'required',
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    people = [{'person_id': person, 'start': start, 'end': end, 'participation': 'staff'}
              for person in (trainee, mentor, staff)]
    if case == 'shared_gap':
        people[0]['participation'] = 'trainee'
        for person in people[:2]:
            person['segments'] = [{'start': START, 'end': '2026-10-04T09:00:00+03:00'},
                                  {'start': '2026-10-04T10:00:00+03:00', 'end': END}]
    reply = h.shift(h.workspace(), start=start, end=end, typ=typ, people=people)
    for shift in (reply['shifts'][0], h.detail(reply['workspace'])['shifts'][0]):
        assert next(p['supervisor_person_id'] for p in shift['participants'] if p['person_id'] == trainee) == mentor
    assert reply['validation']['valid'], reply['validation']['findings']


@pytest.mark.parametrize('reverse', [False, True])
def test_manual_pairing_relocates_automatic_pairs_to_resolve_shared_supervisor_contention(h, reverse):
    trainees = []
    p = h.configure('programmes', {'name': 'Synthetic shared attachment options', 'starts_on': '2020-01-01'})['record']
    for index in range(3):
        person = h.configure('people', {'display_name': f'Synthetic attachment trainee {index}'})['record']['id']
        trainees.append(person)
        h.command('coordinator', 'membership.create', {'person_id': person,
            'category_id': h.seed['category'], 'starts_on': '2020-01-01'})
        h.command('credentialer', 'eligibility.create', {'person_id': person,
            'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
        h.configure('programme_memberships', {'person_id': person, 'programme_id': p['id'],
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    # Sort the generated IDs so the first automatic choice deterministically needs relocation.
    first, second, third = sorted(trainees)
    s1, s2, s3 = sorted(h.seed['people'])
    for trainee, alternative in [(first, s3), (second, s2), (third, s2)]:
        for mentor, scope in [(s1, {}), (alternative, {'programme_id': p['id']})]:
            h.configure('mentorships', {'person_id': trainee, 'mentor_id': mentor, 'strength': 'preferred',
                'starts_on': '2026-10-01', 'ends_on': '2026-11-01', **scope}, role='coordinator')
    typ = h.configure('assignment_types', {'name': 'Synthetic augmenting attachment path', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']['id']
    h.policy({'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 1})
    order = [third, second, first] if reverse else [first, second, third]
    reply = h.shift(h.workspace(), typ=typ, people=participants(order, [s1, s2, s3]))
    for shift in (reply['shifts'][0], h.detail(reply['workspace'])['shifts'][0]):
        pairs = {p['person_id']: p['supervisor_person_id'] for p in shift['participants']}
        assert pairs[first] == s3 and {pairs[second], pairs[third]} == {s1, s2}
    assert reply['validation']['valid'], reply['validation']['findings']


@pytest.mark.parametrize('constraint', ['required', 'only_attachment', 'rank_limit'])
def test_manual_pairing_partial_checks_reject_the_same_new_errors_as_whole_checks(h, monkeypatch, constraint):
    from apps.api.services import planning
    from packages.domain.feasibility import compare_findings

    flexible, constrained, first, second, typ = pairing_setup(h, constraint)
    evaluate = planning.evaluate
    baseline = []
    trials = []

    def checked(data, **kwargs):
        result = evaluate(data, **kwargs)
        if '_partial' in data:
            whole = evaluate({key: value for key, value in data.items() if key not in ('_partial', 'quota_subjects')})
            partial = {'findings': [*result['session_findings'], *result['person_findings']]}
            if not baseline:
                baseline.extend((partial, whole))
            assert compare_findings(baseline[0], partial)['new_or_worse_findings'] == compare_findings(
                baseline[1], whole)['new_or_worse_findings']
            trials.append(partial)
        return result

    monkeypatch.setattr(planning, 'evaluate', checked)
    reply = h.shift(h.workspace(), typ=typ, people=participants([flexible, constrained], [first, second]))
    pairs = {p['person_id']: p['supervisor_person_id'] for p in reply['shifts'][0]['participants']}
    assert pairs[constrained] == first and pairs[flexible] == second
    assert len(trials) > 2


def test_manual_pairing_keeps_supervisor_grouped_limits_across_other_sessions(h):
    from .test_attachments import people, setup

    trainee, first, attached, typ = setup(h, 'preferred')
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week', 'group_by': 'supervisor'},
             {'person_id': trainee})
    workspace = h.shift(h.workspace(), typ=typ, people=people(trainee, first, attached, paired=attached))['workspace']
    start, end = '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'
    reply = h.shift(workspace, start=start, end=end, typ=typ, people=[{**p, 'start': start, 'end': end}
        for p in people(trainee, first, attached)])
    added = next(shift for shift in reply['shifts'] if shift['start'].startswith('2026-10-05'))
    assert next(p['supervisor_person_id'] for p in added['participants'] if p['person_id'] == trainee) is None
    assert 'MAX_DUTIES' not in {f['code'] for f in reply['validation']['findings']}
