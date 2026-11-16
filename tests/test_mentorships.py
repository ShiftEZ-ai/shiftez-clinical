def test_dated_mentor_does_not_grant_authority_and_rejects_overlap(h):
    people = h.seed['people']
    before = h.get('coordinator', '/api/catalogues')['grants']
    value = {'person_id': people[0], 'mentor_id': people[1], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'}
    saved = h.configure('mentorships', value, role='coordinator')['record']
    h.configure('mentorships', value, role='coordinator', status=409)
    h.configure('mentorships', {**value, 'mentor_id': people[0]}, status=422)
    h.configure('mentorships', {**value, 'starts_on': '2027-01-01', 'ends_on': '2028-01-01'}, role='coordinator')
    after = h.get('coordinator', '/api/catalogues')
    assert after['grants'] == before
    assert saved['id'] in {r['id'] for r in after['mentorships']}
    h.configure('mentorships', value, role='staff', status=403)
