"""Who is with whom: staff see their own pairs (their supervisor, the trainees who name them) and no other colleague."""
START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def paired_rota(h):
    """A published session: the staff account's person (a trainee) paired with a supervisor, and a third colleague."""
    trainee, supervisor, other = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Paired clinic', 'starts_on': '2020-01-01', 'definition': {
        'supervision': {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
                        'trainees_per_supervisor': 1}}})['record']['id']
    people = [{'person_id': trainee, 'start': START, 'end': END, 'participation': 'trainee', 'supervisor_person_id': supervisor},
              {'person_id': supervisor, 'start': START, 'end': END, 'participation': 'supervisor'},
              {'person_id': other, 'start': START, 'end': END}]
    h.publish(h.shift(h.workspace(), typ=typ, people=people)['workspace'])
    return trainee, supervisor, other


def test_staff_see_their_supervisor_and_planners_see_everyone(h):
    trainee, supervisor, other = paired_rota(h)
    window = '/api/schedule/sessions?start=2026-10-04&end=2026-10-05'
    staff = h.get('staff', window)
    assert {p['person_id'] for p in staff['shifts'][0]['participants']} == {trainee, supervisor}
    assert set(staff['people']) == {trainee, supervisor}
    planner = h.get('coordinator', window)
    assert {p['person_id'] for p in planner['shifts'][0]['participants']} == {trainee, supervisor, other}
    calendar = h.get('staff', '/api/calendar?start=2026-10-04&end=2026-10-05&person_id=' + trainee)
    duty = next(e for e in calendar['events'] if e['kind'] == 'duty')
    assert duty['supervisor_person_id'] == supervisor and duty['trainee_ids'] == []
    assert set(calendar['pair_names']) == {supervisor}
    team = h.get('coordinator', '/api/calendar?start=2026-10-04&end=2026-10-05')
    lead = next(e for e in team['events'] if e['kind'] == 'duty' and e['person_id'] == supervisor)
    assert lead['trainee_ids'] == [trainee]
    publication = h.get('staff', '/api/publications')['publications'][0]['id']
    projected = h.get('staff', '/api/publications/' + publication)['snapshot']
    assert set(projected['people']) == {trainee, supervisor}
# Include this file in hosted supervision and team-lead qualification.
