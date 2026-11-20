"""Why scores changed reservations with the same supervision facts as a fresh full check."""
import copy

import pytest

from apps.api.services import staffing
from packages.domain.score import placement_effects, schedule_score, score_effect
from packages.domain.validation import evaluate
from .test_planner_why import why


START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def competing_supervisor(h, assigned=True):
    subject, supervisor, alternative = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Why supervisor reservation', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']['id']
    h.policy({'kind': 'allocation_preference', 'priority': 10, 'supervisor_ids': [supervisor]},
             {'person_id': subject}, severity='advisory')
    seat = {'person_id': supervisor, 'start': START, 'end': END, 'participation': 'supervisor'}
    workspace = h.shift(h.workspace(), typ=typ, people=[seat] if assigned else [])['workspace']
    target = h.detail(workspace)['shifts'][0]
    workspace = h.shift(workspace, typ=typ, location=1, people=[seat, subject])['workspace']
    return workspace, target, subject, supervisor, alternative


@pytest.mark.parametrize('assigned', [False, True])
def test_why_placement_effect_matches_fresh_score_after_supervisor_reservation_changes(h, assigned):
    workspace, target, _, supervisor, _ = competing_supervisor(h, assigned)
    source = h.snapshot(workspace)
    changed = {**source, 'shifts': [dict(row, participants=[] if assigned else [{
        'id': 'why-' + supervisor, 'shift_id': row['id'], 'person_id': supervisor,
        'start': row['start'], 'end': row['end'], 'participation': 'staff', 'overtime': False}])
        if row['id'] == target['id'] else row for row in source['shifts']]}
    original_score = schedule_score(source, evaluate(source), {}, limit=0)
    changed_score = schedule_score(changed, evaluate(changed), {}, limit=0)
    with_score, without_score = ((original_score, changed_score) if assigned else (changed_score, original_score))
    assert without_score['score'] > with_score['score']
    expected = {**score_effect(with_score, without_score), 'wishes': []}

    prepared = staffing.people_index(source)['fast']
    commitments = prepared['_supervision_commitments']
    before = copy.deepcopy(commitments)
    direct = placement_effects(prepared, evaluate(prepared), target['id'], [supervisor], {})[supervisor]
    assert direct == expected
    assert prepared['_supervision_commitments'] is commitments and commitments == before
    session = why(h, workspace, [target['id']], person_id=supervisor)['sessions'][0]
    assert session['score_status'] == 'complete'
    assert session['rows'][0]['effect'] == expected


def test_why_runner_up_ranks_fresh_canonical_scores_after_releasing_supervisor(h):
    workspace, target, subject, supervisor, alternative = competing_supervisor(h)
    source = h.snapshot(workspace)
    second = h.configure('people', {'display_name': 'Second eligible Why alternative'})['record']['id']
    category = next(row['category_id'] for row in source['memberships'] if row['person_id'] == alternative)
    h.command('coordinator', 'membership.create', {
        'person_id': second, 'category_id': category, 'starts_on': '2020-01-01'})
    h.command('credentialer', 'eligibility.create', {
        'person_id': second, 'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'allocation_preference', 'priority': 10},
             {'person_id': alternative, 'location_id': target['location_id']}, severity='advisory')
    workspace = h.refresh_draft_baseline(workspace)
    source = h.snapshot(workspace)
    selected_score = schedule_score(source, evaluate(source), {}, limit=0)
    scores = {}
    for person_id in (alternative, second):
        seat = {**target['participants'][0], 'person_id': person_id, 'fixed': False}
        trial = {**source, 'shifts': [dict(row, participants=[seat])
                 if row['id'] == target['id'] else row for row in source['shifts']]}
        check = evaluate(trial)
        assert check['valid'], check['findings']
        scores[person_id] = schedule_score(trial, check, {}, limit=0)
    ranked = sorted(scores, key=lambda pid: (-scores[pid]['score'], -scores[pid]['uncapped_score'], pid))
    assert scores[ranked[0]]['score'] > scores[ranked[1]]['score']

    session = why(h, workspace, [target['id']], person_id=supervisor, runner_up=True)['sessions'][0]
    assert session['score_status'] == 'complete'
    runner_up = session['runner_up']
    assert runner_up['person_id'] == ranked[0]
    assert runner_up['person_id'] != subject
    assert runner_up['point_difference'] == round(selected_score['score'] - scores[ranked[0]]['score'], 1)
    assert runner_up['uncapped_point_difference'] == round(
        selected_score['uncapped_score'] - scores[ranked[0]]['uncapped_score'], 1)
