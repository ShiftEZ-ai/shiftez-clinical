"""Trainee-consultant attachments (mentorships with duty types, clinics and a strength): API, rota check, solver, greedy."""
import copy

import pytest

from packages.domain import generation
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate

START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def setup(h, strength='required', **attachment):
    trainee, b, c = h.seed['people']
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Attached clinic', 'starts_on': '2020-01-01', 'definition': {
        'requirements': [{'minimum': 3, 'maximum': 3}],
        'supervision': {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
                        'trainees_per_supervisor': 1}}})['record']['id']
    h.configure('mentorships', {'person_id': trainee, 'mentor_id': c, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                                'strength': strength, **attachment}, role='coordinator')
    return trainee, b, c, typ


def people(trainee, b, c, paired=None):
    return [{'person_id': trainee, 'start': START, 'end': END, 'participation': 'trainee', 'supervisor_person_id': paired},
            {'person_id': b, 'start': START, 'end': END, 'participation': 'supervisor'},
            {'person_id': c, 'start': START, 'end': END, 'participation': 'supervisor'}]


def codes(data):
    return {f['code'] for f in evaluate(data)['findings']}


def test_attachments_are_saved_with_scope_and_checked_for_overlap(h):
    trainee, b, c, typ = setup(h, assignment_type_ids=[])
    saved = next(r for r in h.get('coordinator', '/api/catalogues')['mentorships'] if r['person_id'] == trainee)
    assert (saved['mentor_id'], saved['strength'], saved['assignment_type_ids'], saved['service_ids']) == (c, 'required', [], [])
    value = {'person_id': trainee, 'mentor_id': b, 'starts_on': '2026-10-15', 'ends_on': '2026-12-01'}
    h.configure('mentorships', {**value, 'assignment_type_ids': [typ]}, role='coordinator', status=409)  # all types already
    h.configure('mentorships', {**value, 'strength': 'always'}, role='coordinator', status=422)
    h.configure('mentorships', {**value, 'starts_on': '2026-11-01'}, role='coordinator')  # after it: fine
    h.configure('mentorships', value, role='staff', status=403)


def test_rota_check_flags_a_required_attachment_paired_elsewhere(h):
    trainee, b, c, typ = setup(h)
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=people(trainee, b, c, paired=c))['workspace'])
    assert 'ATTACHMENT_PAIRING' not in codes(data)
    elsewhere = copy.deepcopy(data)
    next(p for p in elsewhere['shifts'][0]['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = b
    found = [f for f in evaluate(elsewhere)['findings'] if f['code'] == 'ATTACHMENT_PAIRING']
    assert found and found[0]['severity'] == 'error' and found[0]['detail']['supervisor_person_id'] == c
    preferred = copy.deepcopy(elsewhere)
    preferred['mentorships'][0]['strength'] = 'preferred'
    assert 'ATTACHMENT_PAIRING' not in codes(preferred)
    other_type = copy.deepcopy(elsewhere)
    other_type['mentorships'][0]['assignment_type_ids'] = [h.seed['type']]
    assert 'ATTACHMENT_PAIRING' not in codes(other_type)
    other_clinic = copy.deepcopy(elsewhere)
    other_clinic['mentorships'][0]['service_ids'] = [h.seed['services'][1]]
    assert 'ATTACHMENT_PAIRING' not in codes(other_clinic)


def supervisor_of(result, trainee):
    return next(r['participant']['supervisor_person_id'] for r in result['additions'] if r['participant']['person_id'] == trainee)


@pytest.mark.parametrize('strength', ['required', 'preferred'])
@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'])
def test_solver_and_greedy_pair_the_trainee_with_their_consultant(h, strength, mode):
    trainee, b, c, typ = setup(h, strength=strength)
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=people(trainee, b, c))['workspace'])
    # Exercise solver repair rather than the manual save's attachment pairing.
    next(p for p in data['shifts'][0]['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = None
    data['generation_options'] = {'mode': mode}
    solved = generate(copy.deepcopy(data), 20)
    assert solved['status'] == 'candidate_feasible', solved.get('status')
    assert supervisor_of(solved, trainee) == c
    original = next(p for p in data['shifts'][0]['participants'] if p['person_id'] == trainee)
    assert original['supervisor_person_id'] is None
    assert [r['assignment_id'] for r in solved['removals']] == [original['id']]
    assert solved['summary']['changes_kept'] == 2
    if strength == 'preferred':
        assert solved['summary']['score_before']['components']['preferences']['value'] == 0
        assert solved['objective_components']['preferences']['value'] == solved['compiled_component_values']['preferences'] == 1
    greedy = generation.generate(copy.deepcopy(data), 20)
    assert greedy['validation']['valid'], greedy['validation']['findings']
    assert supervisor_of(greedy, trainee) == c


@pytest.mark.parametrize('protection', ['fixed', 'locked'])
def test_solver_does_not_repair_a_protected_attachment(h, protection):
    trainee, b, c, typ = setup(h)
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=people(trainee, b, c))['workspace'])
    shift = data['shifts'][0]
    next(p for p in shift['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = None
    if protection == 'locked':
        shift['locked'] = True
    else:
        next(p for p in shift['participants'] if p['person_id'] == trainee)['fixed'] = True
    solved = generate(data, 20)
    assert solved['status'] == 'invalid_inputs'
    assert {f['code'] for f in solved['issues']} == {'ATTACHMENT_PAIRING'}
    assert solved['additions'] == solved['removals'] == []


def test_solver_keeps_an_already_correct_attachment(h):
    trainee, b, c, typ = setup(h)
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=people(trainee, b, c, paired=c))['workspace'])
    solved = generate(data, 20)
    assert solved['status'] == 'candidate_feasible'
    assert solved['additions'] == solved['removals'] == []
    assert solved['summary']['changes_kept'] == 3


def test_solver_keeps_an_attached_trainee_with_their_consultant(h):
    """Two sessions, the consultant on one: auto-schedule puts the trainee on the consultant's session."""
    trainee, b, c, typ = setup(h)
    # An attachment selects the consultant when assigned; the quota requests the trainee's duty.
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'minimum', 'amount': 1}, {'person_id': trainee})
    later = ('2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00')
    w = h.shift(h.workspace(), typ=typ, people=[{'person_id': c, 'start': START, 'end': END, 'participation': 'supervisor', 'fixed': True}])['workspace']
    w = h.shift(w, start=later[0], end=later[1], typ=typ, people=[{'person_id': b, 'start': later[0], 'end': later[1], 'participation': 'supervisor', 'fixed': True}])['workspace']
    data = h.snapshot(w)
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['requirements'] = [{**r, 'minimum': 2, 'maximum': 2} for r in shift['type_snapshot']['definition']['requirements']]
    data['people'] = {p: data['people'][p] for p in (trainee, b, c)}
    solved = generate(data, 20)
    assert solved['status'] == 'candidate_feasible', solved.get('status')
    placed = [r for r in solved['additions'] if r['participant']['person_id'] == trainee]
    assert [r['participant']['supervisor_person_id'] for r in placed] == [c]  # c is only on the first session
# Include this file in hosted supervision and team-lead qualification.
