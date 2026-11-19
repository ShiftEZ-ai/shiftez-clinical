from packages.domain.cp_sat import generate


def test_fixed_people_survive_copy_and_allow_trainee_additions(h):
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'person_id': trainee})
    typ = h.configure('assignment_types', {'name': 'Fixed consultant FM clinic', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[{'person_id': supervisor,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'participation': 'supervisor', 'fixed': True}])['workspace']
    data = h.snapshot(w)
    sid = data['shifts'][0]['id']
    shift = data['shifts'][0]
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=sid, participants=[{
        'person_id': supervisor, 'start': shift['start'], 'end': shift['end'], 'participation': 'supervisor'}])
    w = h.command('coordinator', 'shift.update', payload, revision=w['revision'])['workspace']
    assert h.snapshot(w)['shifts'][0]['participants'][0]['fixed']
    w = h.command('coordinator', 'shift.copy', {'workspace_id': w['id'], 'shift_ids': [sid],
        'offset_days': 1, 'reason': 'Copy fixed FM commitment'}, revision=w['revision'])['workspace']
    data = h.snapshot(w)
    assert len(data['shifts']) == 2
    assert all(s['participants'][0]['fixed'] for s in data['shifts'])
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert result['removals'] == []
    assert len(result['additions']) == 2
    assert all(p['participant']['person_id'] == trainee for p in result['additions'])
    assert all(p['participant']['supervisor_person_id'] == supervisor for p in result['additions'])
# Include this file in hosted supervision and team-lead qualification.
